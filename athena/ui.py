"""The orb window. Wraps pywebview around assets/ui/orb.html: a small
frameless, always-on-top window you can drag around. pywebview needs to own
the main thread, so start() BLOCKS until the window closes — pass your app
loop as main_fn and it runs in a background thread while the orb is up."""

import threading
from pathlib import Path

import webview

STATES = ("idle", "listening", "thinking", "speaking")

ORB_HTML = Path(__file__).resolve().parent.parent / "assets" / "ui" / "orb.html"

WINDOW_SIZE = 320

_window = None
_loaded = threading.Event()


class _Api:
    """Methods the orb's JavaScript may call back into (Esc = close)."""

    def close(self):
        stop()


def start(main_fn=None) -> None:
    """Open the orb and run the GUI loop. Blocks until the window closes.
    If main_fn is given, it runs in a background thread once the orb is
    on screen — put your assistant loop there and call set_state from it."""
    global _window
    if _window is not None:
        return
    _window = webview.create_window(
        "Athena",
        ORB_HTML.as_uri(),
        js_api=_Api(),
        width=WINDOW_SIZE,
        height=WINDOW_SIZE,
        frameless=True,
        easy_drag=True,      # drag anywhere on the orb to move the window
        on_top=True,
        resizable=False,
        background_color="#05080d",
    )
    _window.events.loaded += _on_loaded
    try:
        webview.start(func=main_fn) if main_fn else webview.start()
    finally:
        _window = None
        _loaded.clear()


def set_state(state: str) -> None:
    """Switch the orb's look: 'idle' | 'listening' | 'thinking' | 'speaking'.
    Safe to call from any thread. Unknown states fall back to idle."""
    if state not in STATES:
        state = "idle"
    if not _loaded.wait(timeout=10):
        print(f"[ui] window not ready, dropped state '{state}'")
        return
    window = _window
    if window is not None:
        window.evaluate_js(f"window.setState('{state}')")


def stop() -> None:
    """Close the orb window (which also unblocks start())."""
    window = _window
    if window is not None:
        window.destroy()


def _on_loaded(*_args) -> None:
    _loaded.set()
