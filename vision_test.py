"""Test Athena's screen vision without voice: capture the screen, ask what's
on it, and print the answer. Run it with something interesting on screen.

    python vision_test.py                     # whole screen
    python vision_test.py window              # just the active window
    python vision_test.py "what's this error" # custom question
"""

import sys
import time

from athena import vision

if __name__ == "__main__":
    args = sys.argv[1:]
    active_window = False
    question = "What is on the screen right now?"
    for a in args:
        if a.lower() == "window":
            active_window = True
        elif a.lower() == "screen":
            active_window = False
        else:
            question = a

    where = "active window" if active_window else "screen"
    print(f"Capturing the {where} and asking: {question!r}\n")
    started = time.monotonic()
    answer = vision.ask_about_screen(question, active_window_only=active_window)
    elapsed = time.monotonic() - started
    print(f"Athena: {answer}")
    print(f"\n({elapsed:.1f}s using {__import__('athena').config.VISION_MODEL})")
