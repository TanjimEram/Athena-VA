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
import traceback

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import psutil
import pygame

from athena import audio_io, brain, config, memory, skills, stt, tts, ui, wake

FOLLOWUP_SECONDS = 6
CONFIRM_LISTEN_SECONDS = 5

SLEEP_PHRASES = ("go to sleep", "goodbye athena", "that's all", "stand down")

_running = threading.Event()
_stopping = threading.Event()     # set on window close -> aborts any in-progress
                                  # recording so the mic is released promptly
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

def hear(max_seconds: float = 12, stop_event=None) -> str:
    """LISTENING: record until silence (orb reacting to your voice), then
    transcribe. Returns "" if nothing was said. `stop_event`, when set,
    aborts the recording early (used by the confirm race). `_stopping`
    (window close) always aborts too, so the mic is freed on shutdown."""
    ui.set_state("listening")
    stop = stop_event if stop_event is not None else _stopping
    wav_path = audio_io.record_until_silence(
        max_seconds=max_seconds, on_level=_mic_level, stop_event=stop
    )
    ui.set_amplitude(0)
    if wav_path is None:
        return ""
    ui.set_state("thinking")
    started = time.monotonic()
    try:
        text = stt.transcribe(wav_path)
        _latency["stt"] = round(time.monotonic() - started, 2)
        return text
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


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
    approve/deny card - whichever arrives first. A click wins instantly and
    aborts the voice listen (no waiting out the window), releasing the mic
    cleanly before the chain continues."""
    stop = threading.Event()
    box: dict[str, str] = {}

    def listen():
        box["txt"] = hear(max_seconds=CONFIRM_LISTEN_SECONDS, stop_event=stop)

    t = threading.Thread(target=listen, daemon=True)
    t.start()

    # Poll for a card click while the voice listen runs.
    while t.is_alive():
        if _confirm_event.is_set():
            stop.set()          # abort the recording now
            t.join(timeout=2.0)  # let it release the mic before we move on
            return bool(_confirm_clicked)
        time.sleep(0.03)

    if _confirm_event.is_set():
        return bool(_confirm_clicked)
    answer_text = box.get("txt", "")
    if answer_text:
        print(f"You said: {answer_text}")
    return is_yes(answer_text)


def _step_badge(step: dict) -> str:
    """Map a chain step's outcome to a tool-log badge tier."""
    if step["status"] == "ran":
        return step["tier"]          # 'free' or (approved) 'confirm'
    if step["status"] == "skipped":
        return "confirm"             # amber: user declined it
    return "blocked"                 # blocked or failed -> red


def _log_step(step: dict) -> None:
    """Stream one chain step to the tool log as it happens."""
    args = ", ".join(f"{k}={v}" for k, v in step["args"].items())
    label = f"{step['tool']}({args})"
    ui.add_log(f"{label} — {step['status']}: {step['result']}", _step_badge(step))


def _chain_confirm(question: str) -> bool:
    """Confirm a single chain step: speak the question, show the card, and
    accept a spoken yes/no OR a card click. The rest of the chain waits."""
    _confirm_event.clear()
    ui.show_confirm(question)
    speak(question)                  # sets state speaking, streams TTS
    ui.set_state("confirm")
    approved = _await_confirmation()
    ui.hide_confirm()
    ui.set_state("thinking")
    return approved


def _do_turn(user_text: str) -> None:
    """Run one exchange: agent plans + runs a chain (steps logged live,
    confirm-tier steps gated), then one natural summary is spoken and shown.
    Wrapped by process_turn so an error here can never crash the loop."""
    ui.add_transcript("you", user_text)
    ui.set_state("thinking")

    turn_started = time.monotonic()
    result = brain.run_agent(
        user_text, _history, on_step=_log_step, confirm=_chain_confirm
    )
    reply = result["reply_text"]
    _latency["brain"] = round(time.monotonic() - turn_started, 2)
    print(f"Athena: {reply}")

    # Speak the ONE final summary, streamed sentence-by-sentence so the
    # transcript and the voice arrive together.
    first_sentence = {"pending": True}

    def _on_sentence(sentence: str) -> None:
        if first_sentence["pending"]:
            first_sentence["pending"] = False
            ui.set_state("speaking")
        ui.add_transcript("athena", sentence)

    speak_started = time.monotonic()
    ttfa_ms = tts.speak_stream(
        iter([reply]), on_level=ui.set_amplitude, on_sentence=_on_sentence
    )
    ui.set_amplitude(0)
    if ttfa_ms is not None:
        total_ms = (speak_started - turn_started) * 1000 + ttfa_ms
        print(f"[latency] first audio in {total_ms:.0f} ms "
              f"({len(result['steps'])} step(s))")
        _latency["tts"] = round(ttfa_ms / 1000.0, 2)

    if ui.mode() == "orb":
        ui.show_bubble(reply)

    _history.append({"role": "user", "content": user_text})
    _history.append({"role": "assistant", "content": reply})
    _push_status()


def process_turn(user_text: str, spoken: bool = True) -> None:
    """One exchange, plus (for spoken turns) a follow-up window so you can
    keep talking without the wake word. Looped, not recursed, so a long
    back-and-forth never grows the stack. The whole thing is crash-proofed:
    any unexpected error recovers with a short spoken apology and the
    assistant keeps running."""
    with _turn_lock:
        while user_text and _running.is_set():
            try:
                _do_turn(user_text)
            except Exception as exc:
                print(f"[main] turn error: {exc!r}")
                traceback.print_exc()
                try:
                    ui.set_amplitude(0)
                    ui.hide_confirm()
                    speak("Sorry, something went wrong there. I'm still here.")
                except Exception:
                    pass

            if not spoken:
                return
            # Follow-up window: keep talking, no wake word needed.
            followup = ""
            try:
                followup = hear(max_seconds=FOLLOWUP_SECONDS)
            except Exception as exc:
                print(f"[main] follow-up listen error: {exc!r}")
            if not followup:
                return
            print(f"You said: {followup}")
            if wants_sleep(followup):
                _shutdown()
                return
            user_text = followup  # loop into the next turn


def _shutdown() -> None:
    """Farewell, then close the window - which ends the whole process."""
    deliver("Alright, going quiet. Call me when you need me.")
    _running.clear()
    _stopping.set()   # abort any in-progress recording, release the mic
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
        try:
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
        except Exception as exc:
            # Nothing transient (a mic glitch, a stray error) may ever stop
            # Athena listening for the wake word. Log it and carry on.
            print(f"[main] loop error, recovering: {exc!r}")
            traceback.print_exc()
            time.sleep(0.5)


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
    _stopping.clear()
    skills.set_ui(ui)                    # wire the open/close dashboard skills
    ui.on_typed_input = _on_typed
    ui.on_confirm = _on_confirm_click
    # ui.on_orb_click stays default: click expands to the dashboard.

    # The assistant loop (wake -> record -> stt -> brain -> skills -> tts) runs
    # in a daemon thread. It NEVER touches the window object directly - every
    # UI update goes through ui.set_state / add_log / ... which use the
    # thread-safe evaluate_js bridge.
    threading.Thread(target=assistant_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    print("assistant thread started")

    # pywebview MUST own the main thread. This is the exact same window that
    # works in run_orb_test.py - same ui.py, same click/drag/expand handlers.
    print("webview starting on main thread")
    try:
        ui.start()          # blocks on the main thread until the window closes
    finally:
        # Window closed: stop the assistant loop and abort any in-progress
        # recording so the microphone is released cleanly.
        _running.clear()
        _stopping.set()

    print("Window closed - Athena offline. Goodbye.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _running.clear()
        print("\nAthena going offline. Goodbye.")
