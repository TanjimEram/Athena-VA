"""Athena's always-on loop, as a clean state machine.

    WAITING   asleep ear: only the local wake-word detector is running
    LISTENING recording your words (stops when you stop talking)
    THINKING  transcribing + the brain deciding what to do
    SPEAKING  Emily's voice answering
    IDLE      shut down (sleep phrase or Ctrl+C)

After she answers there's a ~6 second follow-up window where you can just
keep talking - no wake word needed. Say "go to sleep", "goodbye Athena",
"that's all" or "stand down" to shut her down.

The mic is owned by exactly one state at a time: wake.py and audio_io.py
both open their stream in a with-block, so it's fully released on every
exit path - detection, timeout, error, or Ctrl+C.

Run from the repo root:  python -m athena.main"""

import os
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import pygame

from athena import audio_io, brain, config, stt, tts, wake

FOLLOWUP_SECONDS = 6

SLEEP_PHRASES = ("go to sleep", "goodbye athena", "that's all", "stand down")

_state = None


def set_state(new_state: str) -> None:
    """Track and print state transitions so you can watch what she's doing."""
    global _state
    if new_state != _state:
        _state = new_state
        print(f"[{new_state}]")


def beep() -> None:
    """A short rising chirp meaning 'I heard you, go ahead' - generated in
    code and played instantly, much faster than synthesizing speech."""
    mixer_settings = pygame.mixer.get_init()
    if not mixer_settings:
        return
    samplerate, _size, channels = mixer_settings
    duration = 0.18
    t = np.linspace(0, duration, int(samplerate * duration), False)
    f0, f1 = 600, 1100  # rising = friendly "yes?"
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t**2 / (2 * duration))
    tone = np.sin(phase)
    # Quick fade in/out so it doesn't click.
    fade = int(0.015 * samplerate)
    envelope = np.ones_like(tone)
    envelope[:fade] = np.linspace(0, 1, fade)
    envelope[-fade:] = np.linspace(1, 0, fade)
    samples = (tone * envelope * 0.4 * 32767).astype(np.int16)
    if channels > 1:
        samples = np.column_stack([samples] * channels)
    channel = pygame.sndarray.make_sound(np.ascontiguousarray(samples)).play()
    while channel is not None and channel.get_busy():
        time.sleep(0.01)


def hear(max_seconds: float = 12) -> str:
    """LISTENING: record until silence, then transcribe. "" if nothing said."""
    set_state("LISTENING")
    wav_path = audio_io.record_until_silence(max_seconds=max_seconds)
    if wav_path is None:
        return ""
    set_state("THINKING")
    try:
        return stt.transcribe(wav_path)
    finally:
        os.remove(wav_path)


def say(text: str) -> None:
    set_state("SPEAKING")
    print(f"Athena: {text}")
    tts.speak(text)


def is_yes(text: str) -> bool:
    text = text.lower()
    return any(w in text for w in ("yes", "yeah", "yep", "sure", "go ahead", "do it"))


def wants_sleep(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in SLEEP_PHRASES)


def converse(first_text: str, history: list) -> bool:
    """One conversation: think/speak turns with follow-up windows until the
    user goes quiet. Returns False if a sleep phrase means 'shut down'."""
    text = first_text
    while text:
        print(f"You said: {text}")
        if wants_sleep(text):
            say("Alright, going quiet. Call me when you need me.")
            return False

        set_state("THINKING")
        result = brain.think(text, history)
        reply = result["reply_text"]
        say(reply)

        # Confirm-level action: she just asked, now listen for yes or no.
        if result["needs_confirmation"]:
            answer = hear(max_seconds=FOLLOWUP_SECONDS)
            if answer:
                print(f"You said: {answer}")
            if is_yes(answer):
                reply = brain.run_confirmed(result["tool_called"], result["args"])
            else:
                reply = "Okay, cancelled."
            say(reply)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})

        # Follow-up window: keep talking, no wake word needed.
        text = hear(max_seconds=FOLLOWUP_SECONDS)
    return True


def main() -> None:
    # Initialize everything once, up front: wake model, Groq client, mixer
    # (the tts import warms pygame.mixer at module load).
    config.check_config()
    wake.preload()
    config.get_groq_client()
    print(f"Athena is online. Wake model '{config.WAKE_MODEL}' "
          f"(threshold {config.WAKE_THRESHOLD}). Ctrl+C to quit.")

    history: list[dict] = []
    while True:
        set_state("WAITING")
        if not wake.listen_for_wake():
            print("Wake listener failed - fix the mic and restart.")
            break
        beep()  # "go ahead" - faster and snappier than spoken words

        first_text = hear()
        if not first_text:
            continue  # nothing said: back to WAITING, silently

        if not converse(first_text, history):
            break  # sleep phrase

    set_state("IDLE")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Mic streams live in with-blocks, so they're already released.
        set_state("IDLE")
        print("Athena going offline. Goodbye.")
