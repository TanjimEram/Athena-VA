"""Measure where the time actually goes in a turn. Changes nothing.

    python latency_test.py           # 10 turns
    python latency_test.py 5         # fewer, to spend fewer tokens

Press Enter, speak, listen to the reply, repeat. After every turn it prints
that turn's breakdown; at the end it prints the table with a median per
stage. Everything is also appended to latency_log.jsonl.

This is the full real pipeline - mic, silence detection, Whisper, the brain,
the voice - minus the wake word and the orb, so the numbers are the ones that
matter without you having to say "hey Jarvis" thirty times.

COST: each turn is one Whisper call and one brain call. At ~4,000 tokens of
tool schema per brain call, ten turns is roughly 40,000 of the 100,000 daily
tokens. Run it with AGENTS_ENABLED=1 to cut that by half or more, or ask for
five turns - the median barely moves.

Say something you'd actually say in the demo. Vary it: a plain question, a
single action, a multi-step request. A turn that calls a tool is slower than
one that doesn't, and both are worth having in the table."""

import os
import sys
import time

from athena import audio_io, brain, config, latency, stt, tts

SUGGESTIONS = [
    "what can you do",
    "what's my battery level",
    "what's the time",
    "open notepad",
    "tell me a fact about the moon",
]


def one_turn(index: int) -> bool:
    """Record, transcribe, think, speak. True if a turn was measured."""
    latency.start_turn()

    print("  listening... (speak now, stop when you're done)")
    wav = audio_io.record_until_silence(max_seconds=12)
    if not wav:
        print("  nothing heard - skipping this one\n")
        return False

    try:
        said = stt.transcribe(wav)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    if not said:
        print("  couldn't transcribe that - skipping\n")
        return False
    print(f"  you said: {said!r}")

    result = brain.run_agent(said)
    reply = result["reply_text"]
    print(f"  Athena:   {reply!r}")

    first = {"pending": True}

    def on_sentence(_sentence):
        if first["pending"]:
            first["pending"] = False
            latency.anchor("first_audio")

    ttfa = tts.speak_stream(iter([reply]), on_sentence=on_sentence)
    if ttfa is not None:
        latency.mark("tts_first_audio", ttfa / 1000.0)

    record = latency.end_turn(said)
    if record:
        print("  " + "  ".join(f"{k}={v * 1000:.0f}ms"
                               for k, v in record["stages"].items()))
        if result["steps"]:
            print(f"  ({len(result['steps'])} tool step(s) in that turn)")
    print()
    return True


if __name__ == "__main__":
    wanted = 10
    for arg in sys.argv[1:]:
        if arg.isdigit():
            wanted = int(arg)

    if not config.LATENCY_TRACE:
        print("LATENCY_TRACE is off, so nothing would be measured.")
        print("Unset it, or set LATENCY_TRACE=1, and run again.")
        sys.exit(1)

    print(f"=== measuring {wanted} turns ===")
    print(f"agents: {'on' if config.AGENTS_ENABLED else 'OFF '
                     '(consider AGENTS_ENABLED=1 to spend fewer tokens)'}")
    print(f"brain model: {config.BRAIN_MODEL}")
    print(f"log: {latency.LOG_PATH}\n")

    print("warming up the voice so the first turn isn't unfairly slow...")
    tts.warmup()
    print("ready.\n")

    latency.clear()
    done = 0
    attempt = 0
    while done < wanted and attempt < wanted * 2:
        attempt += 1
        suggestion = SUGGESTIONS[done % len(SUGGESTIONS)]
        try:
            input(f"turn {done + 1}/{wanted} - press Enter, then say something "
                  f"(e.g. {suggestion!r}): ")
        except (EOFError, KeyboardInterrupt):
            print("\nstopping early.")
            break
        try:
            if one_turn(done + 1):
                done += 1
        except KeyboardInterrupt:
            print("\nstopping early.")
            break
        except Exception as exc:
            print(f"  that turn failed ({type(exc).__name__}: {exc}) - skipping\n")
        time.sleep(0.4)

    print("=" * 72)
    print(f"RESULTS over {done} turn(s)")
    print("=" * 72)
    print(latency.table())
    print()
    print(f"Full log appended to {latency.LOG_PATH}")
