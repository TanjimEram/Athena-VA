"""Visual test for the floating orb. Run this over a bright window: you
should see ONLY the circular orb (and its bubble) - your desktop visible
everywhere else, no white or dark box. It cycles all five states every
2.5 s, feeds fake audio levels during listening/speaking, and shows a
short and a long bubble so you can watch the window grow and shrink.
Drag the orb anywhere - it snaps to the nearest screen edge, any corner
you like. Click it to see the callback print. Closes itself at the end."""

import math
import time

from athena import ui_orb


def demo() -> None:
    ui_orb.show_bubble("Hello!")
    for state in ui_orb.STATES:
        print(f"state: {state}")
        ui_orb.set_state(state)
        if state == "thinking":
            ui_orb.show_bubble("This longer bubble should stretch the window "
                               "further left, then tuck away again.")
        if state in ("listening", "speaking"):
            # fake audio level so the bars/pulse move
            for i in range(25):
                ui_orb.set_amplitude(abs(math.sin(i / 3.5)))
                time.sleep(0.1)
            ui_orb.set_amplitude(0)
        else:
            time.sleep(2.5)
    print("Done - closing the orb.")
    ui_orb.stop()


if __name__ == "__main__":
    print("Opening the orb at the right screen edge...")
    ui_orb.start(main_fn=demo)
    print("Orb closed.")
