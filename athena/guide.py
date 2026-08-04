"""Guided mode - Athena reads your screen and talks you through a task one
step at a time, using the screen vision she already has.

General-purpose: works for any goal, not hard-coded to one task. Each loop she
captures the screen, asks the vision model for the SINGLE next action (or
DONE), speaks that one sentence, then waits for you before advancing. She only
INSTRUCTS - she never clicks or controls anything. This is guidance, not
automation."""

import os
import time

from athena import audio_io, settings, stt, tts, vision

GUIDE_SYSTEM_PROMPT = (
    "You are Athena, guiding a user through an on-screen task one step at a "
    "time by looking at their screen. You never control the computer - you "
    "only tell them the next thing to do. Reply with ONE short spoken "
    "sentence giving the single next action to take right now, based on what "
    "is currently on screen. No lists, no numbering, no markdown - just the "
    "one next action, natural to say aloud. If the goal already appears "
    "complete, reply with exactly the word DONE and nothing else."
)

MAX_STEPS = 12
STOP_WORDS = ("done", "finished", "complete", "stop", "quit", "that's all",
              "exit", "cancel")

# Preset goals for known multi-step tasks: (trigger keywords, detailed goal).
PRESETS = [
    (("train", "wake"),
     "Train a custom 'athena' wake word using the openWakeWord training "
     "notebook in Google Colab. The steps are: open the openWakeWord "
     "training notebook in Colab; set the runtime type to GPU; in the "
     "configuration cell enter the wake phrase 'athena'; run all cells "
     "(Runtime, Run all) and wait for training to finish; then download the "
     "resulting .onnx model file to the computer. Guide the user through "
     "whichever of these steps is next based on what's on their screen."),
]

# Optional UI-aware speak/listen, injected by main.py via set_io(); falls back
# to plain tts + recorder so guide works standalone (and in tests).
_speak = None
_listen = None


def set_io(speak_fn=None, listen_fn=None) -> None:
    """main.py wires in its UI-aware speak()/hear() so the orb reacts during
    a guide. Without this, guide uses tts + the recorder directly."""
    global _speak, _listen
    _speak = speak_fn
    _listen = listen_fn


def _say(text: str) -> None:
    if _speak is not None:
        _speak(text)
    else:
        tts.speak(text)


def _listen_for_cue(seconds: float) -> str:
    """Listen for the user's advance/stop cue. Returns the transcript ("")."""
    if _listen is not None:
        try:
            return _listen() or ""
        except Exception:
            return ""
    wav = audio_io.record_until_silence(max_seconds=seconds)
    if not wav:
        return ""
    try:
        return stt.transcribe(wav) or ""
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def _resolve_preset(goal: str) -> str:
    low = goal.lower()
    for keywords, detailed in PRESETS:
        if all(k in low for k in keywords):
            return detailed
    return goal


def _next_step(goal: str) -> str | None:
    """Ask the vision model for the single next action, or 'DONE'. None on a
    hard vision failure."""
    prompt = (
        f"The user's goal is: {goal}. Here is their current screen. In ONE "
        "short spoken sentence, tell them the single next action to take. If "
        "the goal appears complete, reply exactly DONE."
    )
    result = vision.analyze_screen(prompt, system_prompt=GUIDE_SYSTEM_PROMPT,
                                   max_tokens=80)
    if not result or result.startswith("I couldn't") or result.startswith("I grabbed"):
        return None
    return result.strip()


def _is_stop(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in STOP_WORDS)


def start_guide(goal: str) -> str:
    """Walk the user through `goal` step by step. Speaks each step; waits for a
    spoken cue (manual, the reliable default) or auto-advances on a timer.
    Returns a short spoken wrap-up for the brain to say at the end."""
    goal = _resolve_preset(goal)
    wait_mode = settings.get("guide_wait_mode", "manual")   # "manual" | "auto"
    interval = float(settings.get("guide_auto_seconds", 4))
    max_steps = int(settings.get("guide_max_steps", MAX_STEPS))

    _say("Okay, I'll guide you through it. Watch your screen and follow along.")
    last = ""
    for _ in range(max_steps):
        instr = _next_step(goal)
        if instr is None:
            _say("I'm having trouble reading your screen, so I'll stop here.")
            return "I couldn't read the screen to keep guiding you."
        if instr.upper().startswith("DONE"):
            _say("That's it - you're all done.")
            return "Guided you through it; the task looks complete."

        # Don't repeat the exact same instruction; wait for real progress.
        if instr != last:
            _say(instr)
            last = instr

        if wait_mode == "auto":
            time.sleep(interval)          # re-check on the next loop
        else:
            cue = _listen_for_cue(8)
            if _is_stop(cue):
                _say("Alright, stopping the guide.")
                return "Stopped the guide."
            # any other cue (or silence) -> re-capture for the next step

    _say("We've been through several steps, so I'll pause the guide here.")
    return "Paused the guide after several steps."
