"""Athena, fully wired: the always-on assistant loop driving the two-mode
UI (chathead orb <-> dashboard).

Threading (Windows rule: pywebview must own the main thread):
    main thread     ui.start() - the pywebview event loop, blocks until the
                    window closes
    worker thread   the assistant loop (wake -> listen -> think -> speak),
                    daemon: dies with the window
    status thread   pushes cpu/battery/connections/latency to the UI strip
    typed input     arrives on its own thread from the dashboard input box

The wake listener polls in short bursts so the loop notices shutdown fast.
Sleep phrases ("go to sleep", "goodbye athena", "that's all", "stand down")
or closing the window shut everything down cleanly.

Run from the repo root:  python -m athena.main"""

import os
import threading
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import psutil
import pygame

from athena import audio_io, brain, config, memory, safety, stt, tts, ui, wake

FOLLOWUP_SECONDS = 6
CONFIRM_LISTEN_SECONDS = 5

SLEEP_PHRASES = ("go to sleep", "goodbye athena", "that's all", "stand down")

_running = threading.Event()
_turn_lock = threading.Lock()     # one spoken/typed turn at a time
_history: list[dict] = []
_latency: dict[str, float] = {}   # stt / brain / tts, seconds

_confirm_event = threading.Event()
_confirm_clicked: bool | None = None

_mic_peak = 400.0                 # adaptive normaliser for the quiet mic
_mic_frame = 0


# ------------------------------------------------------------ audio into UI

def _mic_level(rms: float) -> None:
    """Turn raw mic RMS into a 0..1 orb/waveform level. The peak tracker
    self-scales to however hot or quiet this microphone runs."""
    global _mic_peak, _mic_frame
    _mic_frame += 1
    if _mic_frame % 2:            # every other 30ms frame is plenty
        return
    _mic_peak = max(rms, _mic_peak * 0.995, 300.0)
    ui.set_amplitude(min(1.0, rms / _mic_peak))


def beep() -> None:
    """Short rising chirp meaning 'I heard you' - faster than speech."""
    mixer_settings = pygame.mixer.get_init()
    if not mixer_settings:
        return
    samplerate, _size, channels = mixer_settings
    duration = 0.18
    t = np.linspace(0, duration, int(samplerate * duration), False)
    f0, f1 = 600, 1100
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t**2 / (2 * duration))
    tone = np.sin(phase)
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


# ------------------------------------------------------------ the pipeline

def hear(max_seconds: float = 12) -> str:
    """LISTENING: record until silence (orb reacting to your voice), then
    transcribe. Returns "" if nothing was said."""
    ui.set_state("listening")
    wav_path = audio_io.record_until_silence(
        max_seconds=max_seconds, on_level=_mic_level
    )
    ui.set_amplitude(0)
    if wav_path is None:
        return ""
    ui.set_state("thinking")
    started = time.monotonic()
    try:
        text = stt.transcribe(wav_path)
        _latency["stt"] = time.monotonic() - started
        return text
    finally:
        os.remove(wav_path)


def speak(text: str) -> None:
    """SPEAKING: say it, orb pulsing along."""
    ui.set_state("speaking")
    print(f"Athena: {text}")
    started = time.monotonic()
    tts.speak(text, on_level=ui.set_amplitude)
    _latency["tts"] = time.monotonic() - started
    ui.set_amplitude(0)


def deliver(reply: str) -> None:
    """Reply on every surface: transcript always, bubble when collapsed to
    the orb, and out loud."""
    ui.add_transcript("athena", reply)
    if ui.mode() == "orb":
        ui.show_bubble(reply)
    speak(reply)


def is_yes(text: str) -> bool:
    text = text.lower()
    return any(w in text for w in ("yes", "yeah", "yep", "sure", "go ahead", "do it"))


def wants_sleep(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in SLEEP_PHRASES)


def _await_confirmation() -> bool:
    """Confirm gate: accept a spoken yes/no OR a click on the dashboard's
    approve/deny card - whichever arrives. Click wins over voice."""
    answer_text = hear(max_seconds=CONFIRM_LISTEN_SECONDS)
    if _confirm_event.is_set():
        return bool(_confirm_clicked)
    if answer_text:
        print(f"You said: {answer_text}")
    return is_yes(answer_text)


def process_turn(user_text: str, spoken: bool = True) -> None:
    """One full exchange: think, act (maybe confirm), reply everywhere."""
    with _turn_lock:
        ui.add_transcript("you", user_text)
        ui.set_state("thinking")

        # Streaming pipeline: sentences start playing while the model is
        # still writing - the transcript fills as each sentence is SPOKEN.
        turn_started = time.monotonic()
        chunk_gen, result = brain.think_stream(user_text, _history)

        first_sentence = {"pending": True}

        def _on_sentence(sentence: str) -> None:
            if first_sentence["pending"]:
                first_sentence["pending"] = False
                ui.set_state("speaking")
            ui.add_transcript("athena", sentence)

        speak_started = time.monotonic()
        ttfa_ms = tts.speak_stream(
            chunk_gen, on_level=ui.set_amplitude, on_sentence=_on_sentence
        )
        ui.set_amplitude(0)

        reply = result["reply_text"]
        print(f"Athena: {reply}")

        if ttfa_ms is not None:
            total_ms = (speak_started - turn_started) * 1000 + ttfa_ms
            print(f"[latency] first audio in {total_ms:.0f} ms")
            ui.add_log(f"first audio in {total_ms:.0f} ms", "free")
            _latency["brain"] = result.get("first_token_s") or 0.0
            _latency["tts"] = max(total_ms / 1000.0 - _latency["brain"], 0.0)

        tool, args = result["tool_called"], result["args"]
        if tool:
            pretty = ", ".join(f"{k}={v}" for k, v in args.items())
            ui.add_log(f"{tool}({pretty})", safety.classify(tool))

        if ui.mode() == "orb":
            ui.show_bubble(reply)

        if result["needs_confirmation"]:
            # The question was already spoken by the stream above.
            _confirm_event.clear()
            ui.show_confirm(reply)
            ui.set_state("confirm")
            approved = _await_confirmation()
            ui.hide_confirm()
            if approved:
                reply = brain.run_confirmed(tool, args)
                ui.add_log(f"executed {tool}", "confirm")
            else:
                reply = "Okay, cancelled."
                ui.add_log(f"denied {tool}", "blocked")
            deliver(reply)

        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": reply})
        _push_status()

        # Spoken turns get a follow-up window: keep talking, no wake word.
        if spoken:
            followup = hear(max_seconds=FOLLOWUP_SECONDS)
            if followup:
                print(f"You said: {followup}")
                if wants_sleep(followup):
                    _shutdown()
                    return
                # recurse outside the lock would deadlock - release first
        else:
            followup = ""
    if spoken and followup:
        process_turn(followup, spoken=True)


def _shutdown() -> None:
    """Farewell, then close the window - which ends the whole process."""
    deliver("Alright, going quiet. Call me when you need me.")
    _running.clear()
    ui.stop()


# ------------------------------------------------------------ UI callbacks

def _on_typed(text: str) -> None:
    """Dashboard input box -> straight into the brain (no mic involved)."""
    print(f"You typed: {text}")
    if wants_sleep(text):
        _shutdown()
        return
    process_turn(text, spoken=False)


def _on_confirm_click(approved: bool) -> None:
    global _confirm_clicked
    _confirm_clicked = approved
    _confirm_event.set()
    ui.add_log(f"confirm card clicked: {'approve' if approved else 'deny'}", "confirm")


# ------------------------------------------------------------ status strip

def _push_status() -> None:
    status = {
        "groq": bool(config.GROQ_API_KEY),
        "supabase": memory._get_client() is not None,
        "cpu": psutil.cpu_percent(interval=None),
    }
    try:
        audio_io.sd.query_devices(kind="input")
        status["mic"] = True
    except Exception:
        status["mic"] = False
    battery = psutil.sensors_battery()
    if battery is not None:
        status["battery"] = battery.percent
    status.update({k: round(v, 2) for k, v in _latency.items()})
    ui.set_status(status)


def status_loop() -> None:
    while _running.is_set():
        try:
            _push_status()
        except Exception:
            pass
        time.sleep(3)


# ------------------------------------------------------------ the main loop

def assistant_loop() -> None:
    ui.set_state("idle")  # waits for the window to finish loading
    ui.add_log("athena online - say 'hey jarvis'", "free")

    while _running.is_set():
        ui.set_state("idle")
        # Short listening bursts so window-close shuts us down within ~3 s.
        if not wake.listen_for_wake(timeout=3):
            continue
        if not _running.is_set():
            break

        ui.add_log("wake word detected", "free")
        beep()

        text = hear()
        if not text:
            continue
        print(f"You said: {text}")
        if wants_sleep(text):
            _shutdown()
            break
        process_turn(text, spoken=True)


def main() -> None:
    config.check_config()
    wake.preload()
    config.get_groq_client()
    print(f"Athena is online. Wake model '{config.WAKE_MODEL}' "
          f"(threshold {config.WAKE_THRESHOLD}). Say 'hey Jarvis', click the "
          "orb for the dashboard, close the window or say 'go to sleep' to quit.")

    # Warm the voice path in the background: mixer, asyncio loop, and one
    # throwaway synthesis so the first real reply skips the TLS handshake.
    threading.Thread(target=tts.warmup, daemon=True).start()

    _running.set()
    ui.on_typed_input = _on_typed
    ui.on_confirm = _on_confirm_click
    # ui.on_orb_click stays default: click expands to the dashboard.

    threading.Thread(target=assistant_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()

    try:
        ui.start()          # blocks on the main thread until window closes
    finally:
        _running.clear()    # daemon threads die with the process

    print("Window closed - Athena offline. Goodbye.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _running.clear()
        print("\nAthena going offline. Goodbye.")
