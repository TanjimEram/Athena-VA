"""Demo of Athena's two-mode UI. Starts as the little orb docked at the
right edge (click it yourself to expand - or wait: the script does it),
then the window grows into the full dashboard, fills every panel with
sample data, pops the confirm card, and collapses back to the orb.
Drag the orb to any corner; drag the dashboard by its top bar.
Closes itself at the end."""

import math
import time

from athena import ui


def demo() -> None:
    # --- orb mode: run through the states ---
    ui.show_bubble("Hello - orb mode. Click me anytime to expand.")
    for state in ui.STATES:
        print(f"orb state: {state}")
        ui.set_state(state)
        if state in ("listening", "speaking"):
            for i in range(18):
                ui.set_amplitude(abs(math.sin(i / 3.0)))
                time.sleep(0.1)
            ui.set_amplitude(0)
        else:
            time.sleep(1.8)

    # --- grow into the dashboard ---
    print("expanding to dashboard...")
    ui.expand()
    time.sleep(1.0)
    ui.set_state("listening")
    ui.set_status({"groq": True, "supabase": False, "mic": True,
                   "cpu": 28, "battery": 62, "stt": 0.41, "brain": 0.83, "tts": 0.52})
    ui.add_log("open_app(name=Chrome)", "free")
    ui.add_log("web_search(query=weather dhaka)", "free")
    ui.add_transcript("you", "open chrome and check the weather")
    time.sleep(1.5)
    ui.set_state("speaking")
    for i in range(20):
        ui.set_amplitude(abs(math.sin(i / 3.0)))
        time.sleep(0.1)
    ui.set_amplitude(0)
    ui.add_transcript("athena", "Chrome's open, and it's 31 degrees and humid in Dhaka.")
    time.sleep(2)
    ui.add_log("lock_screen()", "confirm")
    ui.set_state("confirm")
    ui.show_confirm("Lock the screen now?")
    time.sleep(3)
    ui.set_state("idle")
    print("(type in the transcript box or press the confirm buttons - "
          "responses print here)")
    ui.open_sessions()
    time.sleep(1.5)
    ui.close_sessions()
    time.sleep(2)

    # --- collapse back to the orb ---
    print("collapsing to orb...")
    ui.collapse()
    time.sleep(2.5)
    print("Done - closing.")
    ui.stop()


if __name__ == "__main__":
    ui.on_typed_input = lambda text: print(f"typed: {text}")
    ui.on_confirm = lambda ans: print(f"confirm answered: {ans}")
    print("Opening the orb at the right screen edge...")
    ui.start(main_fn=demo)
    print("Window closed.")
