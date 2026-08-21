"""Where the time actually goes in one turn.

Measurement only - nothing here makes anything faster, and nothing here is
allowed to make anything slower or more fragile. Every entry point swallows
its own errors, so a broken stopwatch can never break a conversation.

The number that matters is TOTAL: the silence between the user finishing
their sentence and Athena starting hers. That is anchored on `speech_ended`,
which is when the last loud frame was heard - NOT when the recorder stopped.
The recorder deliberately keeps listening through a pause, and that wait is
itself one of the stages we're measuring, so it has to sit inside the total
rather than before it.

Two stages are honest about what they can't separate:

  stt        the SDK does connect, upload and inference inside one opaque
             call, so this is all three together. `wav_bytes` is recorded
             alongside it so upload size can at least be reasoned about.
  brain      request sent to reply complete. There is no time-to-first-token
             to report: run_agent's completion call is blocking, so the
             first token and the last arrive at the same moment.
"""

import json
import os
import statistics
import threading
import time

from athena import config

LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "latency_log.jsonl")

# Printed in this order; anything not listed here is appended after.
STAGES = ["silence_wait", "stt", "brain_total", "tool_exec",
          "tts_first_audio", "total"]

LABELS = {
    "silence_wait": "silence wait (end of speech -> recorder stops)",
    "stt": "STT (upload + inference, not separable)",
    "brain_total": "brain, ALL model calls this turn summed (= TTFT; blocking)",
    "tool_exec": "tool execution, all steps summed",
    "tts_first_audio": "TTS first audio",
    "total": "TOTAL end-of-speech -> first word",
}

_lock = threading.Lock()
_current: dict | None = None
_turns: list = []


def _on() -> bool:
    try:
        return bool(config.LATENCY_TRACE)
    except Exception:
        return False


def start_turn() -> None:
    """Begin a new turn. Anything left over from an abandoned one is dropped."""
    global _current
    if not _on():
        return
    try:
        with _lock:
            _current = {"stages": {}, "anchors": {}, "meta": {},
                        "started": time.monotonic()}
    except Exception:
        pass


def mark(stage: str, seconds: float) -> None:
    """Record how long a stage took."""
    if not _on():
        return
    try:
        with _lock:
            if _current is not None:
                _current["stages"][stage] = float(seconds)
    except Exception:
        pass


def anchor(name: str, timestamp: float | None = None) -> None:
    """Record WHEN something happened, on the monotonic clock. Used for the
    two ends of the total, which span stages in different threads."""
    if not _on():
        return
    try:
        with _lock:
            if _current is not None:
                _current["anchors"][name] = (time.monotonic() if timestamp is None
                                             else float(timestamp))
    except Exception:
        pass


def mark_request(provider: str, seconds: float) -> None:
    """One model call. run_agent can make several in a turn - a batched call,
    chain steps, then the summary - and a median over calls can't tell one
    slow call from four fast ones, which need different fixes. So each is
    kept individually, with the provider that served it: a turn that quietly
    fell through to a backup would otherwise read as an unexplained outlier."""
    if not _on():
        return
    try:
        with _lock:
            if _current is not None:
                _current.setdefault("requests", []).append(
                    {"provider": provider or "unknown", "s": float(seconds)})
    except Exception:
        pass


def mark_tool(name: str, seconds: float) -> None:
    """One skill execution. This is real dead air between the model choosing
    an action and the result coming back, and nothing else measures it."""
    if not _on():
        return
    try:
        with _lock:
            if _current is not None:
                _current.setdefault("tools", []).append(
                    {"tool": name, "s": float(seconds)})
    except Exception:
        pass


def shape(label: str) -> None:
    """What KIND of turn this was - conversational, single, multi, vision.
    Medians differ by shape, and one blended median hides that."""
    if not _on():
        return
    try:
        with _lock:
            if _current is not None:
                _current["shape"] = label
    except Exception:
        pass


def meta(key: str, value) -> None:
    """Something worth knowing that isn't a duration - wav_bytes, the model."""
    if not _on():
        return
    try:
        with _lock:
            if _current is not None:
                _current["meta"][key] = value
    except Exception:
        pass


def end_turn(user_text: str = "") -> dict | None:
    """Close the turn, work out the total, keep it and append it to the log."""
    global _current
    if not _on():
        return None
    try:
        with _lock:
            turn = _current
            _current = None
        if turn is None:
            return None

        ended = turn["anchors"].get("speech_ended")
        audible = turn["anchors"].get("first_audio")
        if ended is not None and audible is not None:
            turn["stages"]["total"] = max(0.0, audible - ended)

        requests = turn.get("requests", [])
        tools_run = turn.get("tools", [])
        if requests:
            # Per TURN, not per request: how many calls, and how long they
            # took altogether. The individual times are kept below.
            turn["stages"]["brain_total"] = sum(r["s"] for r in requests)
        if tools_run:
            turn["stages"]["tool_exec"] = sum(t["s"] for t in tools_run)

        record = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "shape": turn.get("shape", ""),
            "said": (user_text or "")[:80],
            "stages": {k: round(v, 3) for k, v in turn["stages"].items()},
            "requests": [{"provider": r["provider"], "s": round(r["s"], 3)}
                         for r in requests],
            "tools": [{"tool": t["tool"], "s": round(t["s"], 3)}
                      for t in tools_run],
            "meta": turn["meta"],
        }
        _turns.append(record)
        _append(record)
        return record
    except Exception as exc:
        print(f"[latency] couldn't close the turn: {exc!r}")
        return None


def _append(record: dict) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"[latency] couldn't write the log: {exc}")


def turns() -> list:
    with _lock:
        return list(_turns)


def clear() -> None:
    with _lock:
        _turns.clear()


def _stage_names(records: list) -> list:
    seen = []
    for record in records:
        for name in record["stages"]:
            if name not in seen:
                seen.append(name)
    ordered = [s for s in STAGES if s in seen]
    return ordered + [s for s in seen if s not in ordered]


def medians(records: list | None = None) -> dict:
    """Median seconds per stage. Median rather than mean on purpose: one slow
    turn from a cold connection shouldn't drag the figure everyone reads."""
    records = turns() if records is None else records
    out = {}
    for name in _stage_names(records):
        values = [r["stages"][name] for r in records if name in r["stages"]]
        if values:
            out[name] = statistics.median(values)
    return out


def table(records: list | None = None) -> str:
    """The per-turn breakdown plus a median row, in milliseconds."""
    records = turns() if records is None else records
    if not records:
        return "No turns measured yet."

    names = _stage_names(records)
    width = max(len(n) for n in names) + 2
    lines = []
    header = "stage".ljust(width) + "".join(f"{i + 1:>8}" for i in range(len(records)))
    lines.append(header + f"{'median':>10}")
    lines.append("-" * len(header + f"{'median':>10}"))

    mids = medians(records)
    for name in names:
        row = name.ljust(width)
        for record in records:
            value = record["stages"].get(name)
            row += f"{value * 1000:>8.0f}" if value is not None else f"{'-':>8}"
        row += f"{mids.get(name, 0) * 1000:>9.0f}ms"
        lines.append(row)

    sizes = [r["meta"].get("wav_bytes") for r in records if r["meta"].get("wav_bytes")]
    if sizes:
        lines.append("")
        lines.append(f"audio uploaded: median {statistics.median(sizes) / 1024:.0f} KB "
                     f"({min(sizes) / 1024:.0f}-{max(sizes) / 1024:.0f} KB over "
                     f"{len(sizes)} turns)")

    lines.append("")
    lines.append("what each row means:")
    for name in names:
        lines.append(f"  {name:<18} {LABELS.get(name, '')}")
    return "\n".join(lines)


SHAPES = ["conversational", "single", "multi-step", "vision"]


def per_turn_table(records: list | None = None) -> str:
    """(A) One row per turn: what shape it was, who served it, every stage."""
    records = turns() if records is None else records
    if not records:
        return "No turns measured yet."
    names = _stage_names(records)
    head = (f"{'#':>2}  {'shape':<14} {'reqs':>4} {'providers':<22} "
            + "".join(f"{n[:9]:>10}" for n in names))
    lines = [head, "-" * len(head)]
    for i, record in enumerate(records, 1):
        used = []
        for request in record.get("requests", []):
            if request["provider"] not in used:
                used.append(request["provider"])
        row = (f"{i:>2}  {record.get('shape', '')[:14]:<14} "
               f"{len(record.get('requests', [])):>4} {','.join(used)[:22]:<22} ")
        for name in names:
            value = record["stages"].get(name)
            row += f"{value * 1000:>10.0f}" if value is not None else f"{'-':>10}"
        lines.append(row)
    return "\n".join(lines)


def by_shape_table(records: list | None = None) -> str:
    """(B) Medians per stage, grouped by shape. Blending them would hide the
    thing we're looking for: the stages differ by what kind of turn it is."""
    records = turns() if records is None else records
    if not records:
        return "No turns measured yet."
    names = _stage_names(records)
    present = [s for s in SHAPES if any(r.get("shape") == s for r in records)]
    present += sorted({r.get("shape", "") for r in records
                       if r.get("shape") and r.get("shape") not in SHAPES})
    head = f"{'stage':<18}" + "".join(f"{s[:13]:>15}" for s in present)
    lines = [head, "-" * len(head)]
    for name in names:
        row = f"{name:<18}"
        for label in present:
            subset = [r for r in records if r.get("shape") == label]
            mids = medians(subset)
            row += (f"{mids[name] * 1000:>13.0f}ms" if name in mids
                    else f"{'-':>15}")
        lines.append(row)
    counts = "  ".join(f"{s}={sum(1 for r in records if r.get('shape') == s)}"
                       for s in present)
    lines.append("")
    lines.append(f"turns per shape: {counts}")
    return "\n".join(lines)


def tools_table(records: list | None = None) -> str:
    """(C) Tool execution time, per tool name. This is the number that decides
    whether a spoken acknowledgement while an action runs is worth having."""
    records = turns() if records is None else records
    seen: dict = {}
    for record in records:
        for entry in record.get("tools", []):
            seen.setdefault(entry["tool"], []).append(entry["s"])
    if not seen:
        return ("No tools ran in these turns - so nothing to say yet about "
                "whether an acknowledgement would earn its place.")
    head = f"{'tool':<24}{'runs':>6}{'median':>10}{'min':>9}{'max':>9}"
    lines = [head, "-" * len(head)]
    for tool in sorted(seen, key=lambda t: -statistics.median(seen[t])):
        values = seen[tool]
        lines.append(f"{tool:<24}{len(values):>6}"
                     f"{statistics.median(values) * 1000:>8.0f}ms"
                     f"{min(values) * 1000:>7.0f}ms{max(values) * 1000:>7.0f}ms")
    everything = [v for values in seen.values() for v in values]
    lines.append("")
    lines.append(f"across all {len(everything)} tool runs: "
                 f"median {statistics.median(everything) * 1000:.0f}ms")
    return "\n".join(lines)


def full_report(records: list | None = None) -> str:
    records = turns() if records is None else records
    parts = [
        "A) PER-TURN", "=" * 72, per_turn_table(records), "",
        "B) MEDIANS BY SHAPE", "=" * 72, by_shape_table(records), "",
        "C) TOOL EXECUTION", "=" * 72, tools_table(records), "",
        "ALL TURNS BLENDED (for reference only)", "=" * 72, table(records),
    ]
    return "\n".join(parts)
