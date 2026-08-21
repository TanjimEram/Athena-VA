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
STAGES = ["silence_wait", "stt", "brain_total", "tts_first_audio", "total"]

LABELS = {
    "silence_wait": "silence wait (end of speech -> recorder stops)",
    "stt": "STT (upload + inference, not separable)",
    "brain_total": "brain total (= TTFT; the call is blocking)",
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

        record = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "said": (user_text or "")[:80],
            "stages": {k: round(v, 3) for k, v in turn["stages"].items()},
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
