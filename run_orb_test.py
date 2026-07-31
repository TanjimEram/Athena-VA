"""Standalone orb test - opaque window, no transparency, no colour-keying.

Opens the orb and, once the page is ready, auto-cycles the state so you can
confirm the JS<->Python bridge works:

  * The orb should visibly change (idle / listening / thinking / speaking).
  * RIGHT-CLICK the orb -> Inspect to open the webview console. Clicking the
    orb should log 'mousedown', 'mouseup', then 'CLICK', and the TERMINAL
    should print 'expand() ran'.
  * Drag the orb to move it; it snaps to the nearest screen edge on release.
  * In the dashboard, press Esc (or the - button) to collapse back.

Close the window to exit.
"""

import time

from athena import ui


def cycle_states() -> None:
    time.sleep(2)  # let the page load and wire handlers (pywebviewready)
    print("Bridge check: auto-cycling states. Click the orb to expand, "
          "drag to move, Esc to collapse.")
    states = ["idle", "listening", "thinking", "speaking"]
    i = 0
    while ui._window is not None:
        state = states[i % len(states)]
        ui.set_state(state)
        print(f"[test] setState -> {state}")
        i += 1
        time.sleep(2)


if __name__ == "__main__":
    print("Opening the orb (devtools enabled: right-click -> Inspect for the console)...")
    ui.start(main_fn=cycle_states, debug=True)
    print("Orb window closed. Goodbye.")
