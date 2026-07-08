"""Visual test for the orb. Opens the window and walks it through all four
states every 2 seconds, three full rounds, then closes itself. Press Esc
(or Ctrl+C in the terminal) to quit early. Drag the orb to move it."""

import time

from athena import ui


def cycle_states() -> None:
    for round_number in range(1, 4):
        for state in ui.STATES:
            print(f"round {round_number}: {state}")
            ui.set_state(state)
            time.sleep(2)
    print("Done — closing the orb.")
    ui.stop()


if __name__ == "__main__":
    print("Opening the orb. It cycles idle -> listening -> thinking -> speaking.")
    ui.start(main_fn=cycle_states)
    print("Orb closed. Goodbye.")
