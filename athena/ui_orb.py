"""The floating chathead orb - transparent via Win32 colour keying.

WebView2 ignores pywebview's transparent=True on Windows (the window came
out solid white), so transparency is done the reliable way: the window is
created opaque with background #010203, then marked WS_EX_LAYERED and given
SetLayeredWindowAttributes(LWA_COLORKEY) with that same colour. Windows
then renders every #010203 pixel fully transparent AND lets clicks fall
through it - so only the orb and the bubble are visible or clickable, and
the desktop shows everywhere else. The key colour is near-black so the
anti-aliased orb edge fades to an almost invisible dark fringe, never a
white halo.

Sizing stays tight to the orb: the window is 90x90. When show_bubble(text)
is called we measure the text in the page, widen the window toward the
open side (capped), keeping the orb pinned to its screen edge, and shrink
back after the bubble fades.

Drag the orb anywhere - on release it snaps to the nearest left/right
edge; the vertical position stays wherever you dropped it (clamped to the
screen), so any corner works. A single click fires on_orb_click().

pywebview owns the main thread: start() BLOCKS, pass your loop as main_fn."""

import ctypes
import json
import threading
import time
from pathlib import Path

import webview

WINDOW_TITLE = "Athena"
ORB_W, ORB_H = 90, 90          # tight window: just the orb
BUBBLE_MAX_PX = 300            # cap for the bubble itself
BUBBLE_GAP_PX = 16             # bubble-to-orb spacing inside the window
BUBBLE_VISIBLE_SECONDS = 6.6   # bubble fade (6 s) + slack before shrinking

# The colour that becomes transparent. HTML #010203 == COLORREF 0x00BBGGRR.
KEY_COLOR_HTML = "#010203"
KEY_COLORREF = 0x00030201

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_COLORKEY = 0x00000001

ORB_HTML = Path(__file__).resolve().parent.parent / "assets" / "ui" / "orb.html"

STATES = ("idle", "listening", "thinking", "speaking", "confirm")

_window = None
_loaded = threading.Event()
_screen = None            # (width, height) of the primary screen
_dock = "right"           # which edge we're snapped to
_expanded = False
_shrink_timer = None


def on_orb_click() -> None:
    """Called when the orb is clicked. Stub for now - wire it up later
    (e.g. wake Athena without the wake word, or open a settings menu)."""
    print("[orb] clicked")


class _Api:
    """Called from the orb's JavaScript."""

    def orb_clicked(self):
        on_orb_click()

    def drag_ended(self):
        # Snap in a worker thread; JS callbacks shouldn't block.
        threading.Thread(target=_snap_to_edge, daemon=True).start()


def start(main_fn=None) -> None:
    """Open the orb at the right screen edge, vertically centred.
    Blocks until the window closes."""
    global _window, _screen
    if _window is not None:
        return
    screen = webview.screens[0]
    _screen = (screen.width, screen.height)
    _window = webview.create_window(
        WINDOW_TITLE,
        ORB_HTML.as_uri(),
        js_api=_Api(),
        width=ORB_W,
        height=ORB_H,
        x=_screen[0] - ORB_W,
        y=(_screen[1] - ORB_H) // 2,
        # pywebview's default min_size is (200, 100); without this override
        # the 90x90 request gets silently inflated and hangs off-screen.
        min_size=(ORB_W, ORB_H),
        frameless=True,
        on_top=True,
        easy_drag=True,
        resizable=False,
        background_color=KEY_COLOR_HTML,  # never flash white before load
    )
    _window.events.loaded += _on_loaded
    try:
        webview.start(func=main_fn) if main_fn else webview.start()
    finally:
        _window = None
        _loaded.clear()


def set_state(state: str) -> None:
    """'idle' | 'listening' | 'thinking' | 'speaking' | 'confirm'."""
    if state not in STATES:
        state = "idle"
    _js(f"window.setState('{state}')")


def set_amplitude(value: float) -> None:
    """Feed a 0..1 level so the bars and speaking pulse follow the audio."""
    try:
        value = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return
    _js(f"window.setAmplitude({value:.3f})")


def show_bubble(text: str) -> None:
    """Show a message bubble beside the orb (fades after ~6 s). The window
    temporarily widens toward the open side to fit it, orb staying put."""
    global _shrink_timer
    text = str(text)
    _expand_for_bubble(text)
    _js(f"window.showBubble({json.dumps(text)})")
    if _shrink_timer is not None:
        _shrink_timer.cancel()
    _shrink_timer = threading.Timer(BUBBLE_VISIBLE_SECONDS, _shrink_after_bubble)
    _shrink_timer.daemon = True
    _shrink_timer.start()


def stop() -> None:
    window = _window
    if window is not None:
        window.destroy()


# ---------------------------------------------------------------- internals

def _js(code: str):
    if not _loaded.wait(timeout=10):
        return None
    window = _window
    if window is None:
        return None
    try:
        return window.evaluate_js(code)
    except Exception:
        return None  # window mid-close


def _on_loaded(*_args) -> None:
    _loaded.set()
    threading.Thread(target=_post_load_setup, daemon=True).start()


def _post_load_setup() -> None:
    time.sleep(0.3)  # let the form settle
    _apply_colorkey()
    window = _window
    if window is None:
        return
    try:
        # Creation can inflate the size (WebView2 minimums); an explicit
        # resize sticks, then pin flush to the docked edge.
        window.resize(ORB_W, ORB_H)
    except Exception:
        pass
    _dock_flush()
    _js(f"window.setDock('{_dock}')")


def _find_hwnd() -> int:
    """The window's Win32 handle, via pywebview's native form if possible."""
    window = _window
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        pass
    try:
        return ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
    except Exception:
        return 0


def _apply_colorkey() -> None:
    """Make every #010203 pixel transparent and click-through."""
    hwnd = _find_hwnd()
    if not hwnd:
        print("[orb] couldn't get the window handle - orb will have a dark box")
        return
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
    if not user32.SetLayeredWindowAttributes(hwnd, KEY_COLORREF, 0, LWA_COLORKEY):
        print("[orb] colour keying failed - orb will have a dark box")


def _actual_size() -> tuple[int, int]:
    window = _window
    try:
        return int(window.width), int(window.height)
    except Exception:
        return ORB_W, ORB_H


def _dock_flush() -> None:
    """Pin the window flush against the current dock edge at its real size,
    keeping the vertical position (clamped on-screen)."""
    window = _window
    if window is None or _screen is None:
        return
    w, h = _actual_size()
    try:
        y = max(0, min(window.y, _screen[1] - h))
        x = 0 if _dock == "left" else _screen[0] - w
        window.move(x, y)
    except Exception:
        pass


def _expand_for_bubble(text: str) -> None:
    """Widen the window toward the open side so the measured bubble fits.
    Docked right: grow leftward (right edge stays put -> orb doesn't move).
    Docked left: grow rightward (x stays 0 -> orb doesn't move)."""
    global _expanded
    window = _window
    if window is None:
        return
    measured = _js(f"window.measureBubble({json.dumps(str(text))})")
    try:
        bubble_px = min(int(measured), BUBBLE_MAX_PX)
    except (TypeError, ValueError):
        bubble_px = 220
    width = ORB_W + BUBBLE_GAP_PX + bubble_px
    try:
        window.resize(width, ORB_H)
        _expanded = True
        _dock_flush()
    except Exception:
        _expanded = False


def _shrink_after_bubble() -> None:
    global _expanded
    window = _window
    if window is None or not _expanded:
        return
    try:
        window.resize(ORB_W, ORB_H)
    except Exception:
        pass
    _expanded = False
    _dock_flush()


def _snap_to_edge() -> None:
    """Animate to the nearest left/right screen edge; vertical position is
    kept where the user dropped it (clamped), so corners work too."""
    global _dock
    window = _window
    if window is None or _screen is None:
        return
    try:
        x0, y0 = window.x, window.y
    except Exception:
        return
    screen_w, screen_h = _screen
    w, h = _actual_size()

    on_left = (x0 + w / 2) < screen_w / 2
    _dock = "left" if on_left else "right"
    x1 = 0 if on_left else screen_w - w
    y1 = max(0, min(y0, screen_h - h))

    steps, duration = 14, 0.22
    for i in range(1, steps + 1):
        t = i / steps
        ease = 1 - (1 - t) ** 3  # ease-out cubic
        window.move(round(x0 + (x1 - x0) * ease), round(y0 + (y1 - y0) * ease))
        time.sleep(duration / steps)
    _js(f"window.setDock('{_dock}')")
