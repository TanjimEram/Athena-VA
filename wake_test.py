"""Test the wake word on its own - no brain, no voice, no API calls.

    python wake_test.py             # listen and report every detection
    python wake_test.py --scores    # also show the live confidence score
    python wake_test.py --model hey_jarvis

Say the wake phrase. Each detection prints the score that triggered it, so
you can tell a confident detection from a marginal one. Ctrl+C to stop.

Nothing here costs anything: openWakeWord runs entirely on this machine.

If it misses you, lower the threshold in the dashboard (Wake word ->
Sensitivity) or with --threshold here. If it fires when you haven't spoken,
raise it. Tune with this script rather than mid-demo."""

import sys
import time

import numpy as np
import sounddevice as sd

from athena import config, settings, wake


def main() -> int:
    args = sys.argv[1:]
    show_scores = "--scores" in args
    threshold = None
    model_name = None
    for i, arg in enumerate(args):
        if arg == "--threshold" and i + 1 < len(args):
            threshold = float(args[i + 1])
        if arg == "--model" and i + 1 < len(args):
            model_name = args[i + 1]

    if model_name:
        settings.set("wake_model", model_name)
    chosen = settings.get("wake_model", config.WAKE_MODEL)
    if threshold is None:
        threshold = float(settings.get("wake_threshold", config.WAKE_THRESHOLD))

    resolved = wake.resolve_model(chosen)
    print(f"model     : {chosen}")
    print(f"resolved  : {resolved}")
    print(f"threshold : {threshold}")
    print("loading...")

    started = time.monotonic()
    try:
        model = wake._get_model()
    except Exception as exc:
        print(f"\ncouldn't load that model: {exc}")
        print("If it's a custom one, check the .onnx is in models/.")
        return 1
    print(f"loaded in {time.monotonic() - started:.1f}s\n")
    print("Say the wake phrase. Ctrl+C to stop.\n")

    detections = 0
    peak = 0.0
    last_print = 0.0
    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=wake.CHUNK) as stream:
            while True:
                audio, _overflowed = stream.read(wake.CHUNK)
                scores = model.predict(np.squeeze(audio))
                score = max(scores.values())
                peak = max(peak, score)

                if score >= threshold:
                    detections += 1
                    print(f"  DETECTED  score {score:.3f}   "
                          f"(detection #{detections})")
                    model.reset()          # don't retrigger on the same audio
                    peak = 0.0
                    time.sleep(0.6)
                elif show_scores and time.monotonic() - last_print > 0.25:
                    bar = "#" * int(score * 40)
                    print(f"    {score:.3f} {bar}", end="\r")
                    last_print = time.monotonic()
    except KeyboardInterrupt:
        print("\n")
    except sd.PortAudioError as exc:
        print(f"\ncouldn't open the microphone: {exc}")
        print("Check Settings > Privacy & security > Microphone.")
        return 1

    print(f"{detections} detection(s). Highest score since the last one: "
          f"{peak:.3f}")
    if detections == 0:
        print(f"Nothing fired. If you were speaking, the peak above tells you "
              f"how close you got to the {threshold} threshold - try "
              f"--threshold {max(0.1, round(peak - 0.05, 2))} to see if it's "
              "just set too high.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
