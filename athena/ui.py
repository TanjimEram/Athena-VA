"""Athena's face: ONE opaque pywebview window, ONE page (assets/ui/app.html)
holding both #orb-view and #dashboard-view.

NO window transparency and NO colour-keying anywhere - the orb is a dark
circle on an opaque dark rounded panel, so mouse events always land. This is
the reliable-over-fancy rebuild.

Switching modes never reloads the page: Python resizes/moves the window and
calls the page's showDashboard()/showOrb() to toggle a body class.

Root-cause guard: the page wires its handlers only after 'pywebviewready',
and every JS->Python call is try/caught and logged.

Hooks main.py can set: on_typed_input(text), on_confirm(bool), on_orb_click().
pywebview owns the main thread: start() BLOCKS, pass your loop as main_fn."""

import ctypes
import ctypes.wintypes
import json
import threading
import time
from pathlib import Path

import webview

WINDOW_TITLE = "Athena"
BG_COLOR = "#0a1420"          # opaque dark; matches the orb panel

ORB_W, ORB_H = 96, 96
DASH_W, DASH_H = 1150, 700

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
MONITOR_DEFAULTTONEAREST = 2

STATE_FILE = Path(__file__).resolve().parent.parent / ".athena_ui_state.json"
APP_HTML = Path(__file__).resolve().parent.parent / "assets" / "ui" / "app.html"
# A real multi-resolution .ico (16 through 256). Windows needs a genuine ICO,
# square - the first file supplied here was a 612x408 PNG with the extension
# renamed, which Windows ignores silently.
APP_ICON = APP_HTML.parent / "athena.ico"

STATES = ("idle", "listening", "thinking", "speaking", "confirm")


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


# Hooks for main.py.
on_typed_input = None
on_confirm = None
on_orb_click = None

_window = None
_loaded = threading.Event()
_screen = None
_mode = "orb"
_dock = "right"
_restored = False
_switching = threading.Lock()


class _Api:
    """Called from app.html's JavaScript (after pywebviewready)."""

    def debug(self, msg):
        print(f"[app-js] {msg}")

    def move_window(self, dx, dy):
        """Drag: move the window by the pointer delta."""
        if _mode != "orb":
            return
        window = _window
        if window is None:
            return
        try:
            window.move(int(window.x) + int(dx), int(window.y) + int(dy))
            print(f"[ui] move_window({int(dx)},{int(dy)})")
        except Exception as exc:
            print(f"[ui] move_window error: {exc!r}")

    def expand(self):
        hook = on_orb_click
        if hook is not None:
            threading.Thread(target=hook, daemon=True).start()
        else:
            threading.Thread(target=expand, daemon=True).start()

    def collapse(self):
        threading.Thread(target=collapse, daemon=True).start()

    def minimize(self):              # alias kept for any older caller
        threading.Thread(target=collapse, daemon=True).start()

    def drag_ended(self):
        if _mode == "orb":
            threading.Thread(target=_snap_to_edge, daemon=True).start()

    def typed_input(self, text):
        hook = on_typed_input
        if hook is not None:
            threading.Thread(target=hook, args=(str(text),), daemon=True).start()
        else:
            print(f"[ui] typed input (no handler): {text}")

    def confirm_answer(self, answer):
        hook = on_confirm
        if hook is not None:
            threading.Thread(target=hook, args=(bool(answer),), daemon=True).start()
        else:
            print(f"[ui] confirm answered (no handler): {answer}")

    def get_branding(self):
        """Text the page displays but must not own: the wake phrase, and the
        assistant's name. Called once from wire(), after pywebviewready.

        Deliberately separate from get_settings, which reads .env and probes
        connections - this runs on every page load and has to stay cheap."""
        from athena import config, settings
        try:
            model = settings.get("wake_model", config.WAKE_MODEL)
        except Exception:
            model = config.WAKE_MODEL
        return {"wake_label": config.wake_label(model),
                "assistant_name": config.ASSISTANT_NAME}

    # ---- settings dashboard (delegates to settings_api) ----
    def get_settings(self):
        from athena import settings_api
        return settings_api.get_settings()

    def save_setting(self, key, value):
        from athena import settings_api
        return settings_api.save_setting(key, value)

    def save_skill(self, name, enabled):
        from athena import settings_api
        return settings_api.save_skill(str(name), bool(enabled))

    def save_key(self, service, value):
        from athena import settings_api
        return settings_api.save_key(str(service), str(value))

    def test_connection(self, service):
        from athena import settings_api
        return settings_api.test_connection(str(service))

    def recent_memory(self):
        from athena import settings_api
        return settings_api.recent_memory()

    def clear_memory(self):
        from athena import settings_api
        return settings_api.clear_memory()

    def reset_defaults(self):
        from athena import settings_api
        return settings_api.reset_defaults()


def start(main_fn=None, debug: bool = False) -> None:
    """Open the window in orb mode. Blocks until it closes.
    debug=True enables the WebView2 devtools (right-click -> Inspect) so you
    can watch the page's console."""
    global _window, _screen
    if _window is not None:
        return
    screen = webview.screens[0]
    _screen = (screen.width, screen.height)
    _window = webview.create_window(
        WINDOW_TITLE,
        APP_HTML.as_uri(),
        js_api=_Api(),
        width=ORB_W,
        height=ORB_H,
        x=_screen[0] - ORB_W,
        y=(_screen[1] - ORB_H) // 2,
        min_size=(ORB_W, ORB_H),
        frameless=True,
        on_top=True,
        resizable=False,
        background_color=BG_COLOR,
    )
    _window.events.loaded += _on_loaded
    try:
        # The taskbar/app icon. The window is frameless, so there is no title
        # bar to show it - this is what Alt-Tab and the taskbar pick up.
        # Passed only if the file is really there: a missing icon must not
        # stop the window opening, and pywebview raises if the path is bad.
        if APP_ICON.exists():
            webview.start(func=main_fn, debug=debug, icon=str(APP_ICON))
        else:
            print(f"[ui] no icon at {APP_ICON} - starting without one")
            webview.start(func=main_fn, debug=debug)
    finally:
        _window = None
        _loaded.clear()


def stop() -> None:
    window = _window
    if window is not None:
        window.destroy()


def mode() -> str:
    return _mode


# ------------------------------------------------- state / audio / dashboard

def set_state(state: str) -> None:
    if state not in STATES:
        state = "idle"
    _js(f"window.setState && window.setState('{state}')")


def set_amplitude(value: float) -> None:
    try:
        value = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return
    _js(f"window.setAmplitude && window.setAmplitude({value:.3f})")


def show_bubble(text: str) -> None:
    """No-op in the opaque orb (kept so main.py's calls are harmless)."""
    _js(f"window.showBubble && window.showBubble({json.dumps(str(text))})")


def add_log(text: str, tier: str = "free") -> None:
    ts = time.strftime("%H:%M:%S")
    _js(f"window.addLogLine && window.addLogLine({json.dumps(str(text))}, "
        f"{json.dumps(tier)}, {json.dumps(ts)})")


def add_transcript(who: str, text: str) -> None:
    _js(f"window.addTranscript && window.addTranscript({json.dumps(str(who))}, "
        f"{json.dumps(str(text))})")


def set_status(status: dict) -> None:
    _js(f"window.setStatus && window.setStatus({json.dumps(status)})")


def show_confirm(question: str) -> None:
    _js(f"window.showConfirm && window.showConfirm({json.dumps(str(question))})")


def hide_confirm() -> None:
    _js("window.hideConfirm && window.hideConfirm()")


def open_sessions() -> None:
    _js("window.openSessions && window.openSessions()")


def close_sessions() -> None:
    _js("window.closeSessions && window.closeSessions()")


# --------------------------------------------------------- mode switching

def expand() -> None:
    """Orb -> dashboard: resize to 1150x700, centre on the current monitor,
    show the dashboard view. No page reload, no transparency."""
    global _mode
    with _switching:
        if _window is None or _mode == "dashboard":
            print(f"[ui] expand ignored (mode={_mode})")
            return
        hwnd = _find_hwnd()
        if not hwnd:
            print("[ui] expand aborted: no window handle")
            return
        scale = _dpi_scale(hwnd)
        wl, wt, wr, wb = _work_area(hwnd)
        w = min(int(DASH_W * scale), wr - wl)
        h = min(int(DASH_H * scale), wb - wt)
        tx = wl + ((wr - wl) - w) // 2
        ty = wt + ((wb - wt) - h) // 2
        _mode = "dashboard"
        _js("window.showDashboard && window.showDashboard()")
        _animate_phys(hwnd, tx, ty, w, h)
        print("[ui] expand() ran")


def collapse() -> None:
    """Dashboard -> orb: resize back to 96x96, dock to the last edge, show
    the orb view."""
    global _mode
    with _switching:
        if _window is None or _mode == "orb":
            print(f"[ui] collapse ignored (mode={_mode})")
            return
        hwnd = _find_hwnd()
        if not hwnd:
            return
        scale = _dpi_scale(hwnd)
        ow, oh = int(ORB_W * scale), int(ORB_H * scale)
        wl, wt, wr, wb = _work_area(hwnd)
        ty = max(wt, min(_win_rect(hwnd)[1], wb - oh))
        tx = wl if _dock == "left" else wr - ow
        _mode = "orb"
        _js("window.showOrb && window.showOrb()")
        _animate_phys(hwnd, tx, ty, ow, oh)
        _js(f"window.setDock && window.setDock('{_dock}')")
        print("[ui] collapse() ran")


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
        return None


def _on_loaded(*_args) -> None:
    _loaded.set()
    threading.Thread(target=_after_load, daemon=True).start()


def _after_load() -> None:
    global _restored, _dock
    time.sleep(0.3)
    if _window is None:
        return
    try:
        _window.resize(ORB_W, ORB_H)  # creation can inflate the size
    except Exception:
        pass
    if not _restored:
        _restored = True
        _restore_state()
    # Appearance settings apply at launch: dock edge + accent colour.
    try:
        from athena import settings
        _dock = "left" if settings.get("dock_edge", _dock) == "left" else "right"
        accent = settings.get("accent_color", "#4fd6e8")
        _js(f"document.documentElement.style.setProperty('--accent', {json.dumps(accent)})")
    except Exception:
        pass
    _dock_flush()
    _js(f"window.setDock && window.setDock('{_dock}')")
    print("[ui] app.html loaded, orb ready")


def _find_hwnd() -> int:
    window = _window
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        pass
    try:
        return ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
    except Exception:
        return 0


def _win_rect(hwnd) -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _work_area(hwnd) -> tuple[int, int, int, int]:
    """Work area (visible desktop minus taskbar) of the monitor the window
    is currently on - multi-monitor aware."""
    user32 = ctypes.windll.user32
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    w = info.rcWork
    return w.left, w.top, w.right, w.bottom


def _dpi_scale(hwnd) -> float:
    _x, _y, phys_w, _h = _win_rect(hwnd)
    try:
        logical_w = int(_window.width) or 1
    except Exception:
        return 1.0
    return max(phys_w / logical_w, 0.5)


def _set_pos(hwnd, x, y, w=None, h=None) -> None:
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    if w is None:
        flags |= SWP_NOSIZE
        w = h = 0
    ctypes.windll.user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), flags)


def _animate_phys(hwnd, x1, y1, w1, h1, steps: int = 12, duration: float = 0.18) -> None:
    x0, y0, w0, h0 = _win_rect(hwnd)
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 3
        _set_pos(hwnd,
                 round(x0 + (x1 - x0) * e), round(y0 + (y1 - y0) * e),
                 round(w0 + (w1 - w0) * e), round(h0 + (h1 - h0) * e))
        time.sleep(duration / steps)


def _dock_flush() -> None:
    if _mode != "orb":
        return
    hwnd = _find_hwnd()
    if not hwnd:
        return
    x, y, w, h = _win_rect(hwnd)
    wl, wt, wr, wb = _work_area(hwnd)
    y = max(wt, min(y, wb - h))
    x = wl if _dock == "left" else wr - w
    _set_pos(hwnd, x, y)


def _snap_to_edge() -> None:
    """After a drag: animate to the nearest left/right edge of the current
    monitor, keep the vertical position, remember the spot."""
    global _dock
    hwnd = _find_hwnd()
    if not hwnd:
        return
    x, y, w, h = _win_rect(hwnd)
    wl, wt, wr, wb = _work_area(hwnd)
    on_left = (x + w / 2) < (wl + wr) / 2
    _dock = "left" if on_left else "right"
    x1 = wl if on_left else wr - w
    y1 = max(wt, min(y, wb - h))
    _animate_phys(hwnd, x1, y1, w, h, steps=14, duration=0.2)
    _js(f"window.setDock && window.setDock('{_dock}')")
    _save_state(x1, y1)
    print(f"[ui] snapped to {_dock} edge")


def _save_state(x: int, y: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"x": int(x), "y": int(y), "dock": _dock}))
    except OSError:
        pass


def _restore_state() -> None:
    global _dock
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return
    hwnd = _find_hwnd()
    if not hwnd:
        return
    _dock = "left" if data.get("dock") == "left" else "right"
    _set_pos(hwnd, data.get("x", 0), data.get("y", 0))
