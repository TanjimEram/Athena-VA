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

# The prescribed run: shapes differ, and blending their medians would hide
# exactly what we're looking for. Each entry is (shape, what to say).
SCRIPT = [
    ("conversational", "what can you do"),
    ("conversational", "how are you today"),
    ("conversational", "tell me something about the moon"),
    ("single", "what's my battery level"),
    ("single", "open notepad"),
    ("single", "open the BBC website"),
    ("multi-step", "open notepad, check my battery, and search the web for lofi music"),
    ("multi-step", "what's my battery, open calculator, and open google"),
    ("multi-step", "check my system info, open notepad, and open the BBC website"),
    ("vision", "look at my screen and tell me what you see"),
]


def one_turn(shape: str) -> bool:
    """Record, transcribe, think, speak. True if a turn was measured."""
    latency.start_turn()
    latency.shape(shape)

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
        providers_used = [r["provider"] for r in record.get("requests", [])]
        if providers_used:
            print(f"  {len(providers_used)} model call(s) via "
                  f"{', '.join(providers_used)}")
        for entry in record.get("tools", []):
            print(f"  tool {entry['tool']}: {entry['s'] * 1000:.0f}ms")
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
    plan = SCRIPT[:wanted] if wanted <= len(SCRIPT) else SCRIPT
    print(f"The run is scripted so the shapes are labelled correctly:")
    for i, (shape, say) in enumerate(plan, 1):
        print(f"  {i:>2}. [{shape:<14}] {say!r}")
    print("\nSay each one roughly as written. If a turn fails or falls back to")
    print("another provider, it's kept and labelled - those are real conditions.\n")

    done = 0
    for index, (shape, say) in enumerate(plan, 1):
        try:
            input(f"turn {index}/{len(plan)} [{shape}] - press Enter, "
                  f"then say: {say!r}\n> ")
        except (EOFError, KeyboardInterrupt):
            print("\nstopping early.")
            break
        try:
            if one_turn(shape):
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
    print()
    print(latency.full_report())
    print()
    print(f"Full log appended to {latency.LOG_PATH}")
    print("Paste everything from 'A) PER-TURN' down.")
