"""Athena's face: one pywebview window with two modes.

ORB MODE       a 90x90 chathead docked to a screen edge, transparent via
               Win32 colour keying (see ui_orb.py for the background story:
               WebView2 ignores pywebview's transparent=True, so we key out
               every #010203 pixel with LWA_COLORKEY - keyed pixels are
               invisible AND click-through).
DASHBOARD MODE the full HUD (assets/ui/dashboard.html), 1150x700, centred,
               opaque: tool log, transcript with typed input, status strip,
               big ringed orb.

Clicking the orb expands to the dashboard; the dashboard's minimise button
(or the rail's "orb mode" item) collapses back. The switch animates the
window bounds over ~200 ms so it grows/shrinks instead of snapping. Drag
the orb anywhere (it snaps to the nearest edge on release, any corner);
drag the dashboard by its top bar.

Hooks main.py can set:
    ui.on_typed_input = fn(text)   text typed into the dashboard transcript
    ui.on_confirm     = fn(bool)   approve/deny pressed on the confirm card
    ui.on_orb_click   = fn()       override the orb click (default: expand)

pywebview owns the main thread: start() BLOCKS, pass your loop as main_fn."""

import ctypes
import ctypes.wintypes
import json
import threading
import time
from pathlib import Path

import webview

WINDOW_TITLE = "Athena"

ORB_W, ORB_H = 90, 90
DASH_W, DASH_H = 1150, 700
BUBBLE_MAX_PX = 300
BUBBLE_GAP_PX = 16
BUBBLE_VISIBLE_SECONDS = 6.6

# The colour that becomes transparent in orb mode. #010203 == 0x00BBGGRR.
KEY_COLOR_HTML = "#010203"
KEY_COLORREF = 0x00030201

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_COLORKEY = 0x00000001
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2

# Where the orb's last position is remembered between sessions.
STATE_FILE = Path(__file__).resolve().parent.parent / ".athena_ui_state.json"


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "ui"
ORB_HTML = _ASSETS / "orb.html"
DASH_HTML = _ASSETS / "dashboard.html"

STATES = ("idle", "listening", "thinking", "speaking", "confirm")

# Hooks for main.py (see module docstring).
on_typed_input = None
on_confirm = None
on_orb_click = None

_window = None
_loaded = threading.Event()
_screen = None
_mode = "orb"             # "orb" | "dashboard"
_dock = "right"
_orb_phys = None          # orb's physical (x, y) while the dashboard is open
_restored = False         # saved position applied once per session
_expanded = False         # orb mode: widened for a bubble
_shrink_timer = None
_switching = threading.Lock()

# Python owns the UI's history: every mode switch reloads the page, so the
# assistant state, transcript, tool log, status and any pending confirm are
# cached here and replayed into whichever page just loaded.
_MAX_CACHED = 200
_cache = {
    "state": "idle",
    "log": [],        # [text, tier, "HH:MM:SS"]
    "chat": [],       # [who, text]
    "status": {},
    "confirm": None,  # pending confirm question, or None
}


class _Api:
    """Called from the pages' JavaScript."""

    def orb_clicked(self):
        hook = on_orb_click
        if hook is not None:
            threading.Thread(target=hook, daemon=True).start()
        else:
            threading.Thread(target=expand, daemon=True).start()

    def move_window(self, dx, dy):
        """Manual drag: the orb page ships pointer deltas; move by them.
        Runs on pywebview's js-api thread, so it must stay quick."""
        if _mode != "orb":
            return
        window = _window
        try:
            window.move(window.x + int(dx), window.y + int(dy))
        except Exception:
            pass

    def drag_ended(self):
        if _mode == "orb":
            threading.Thread(target=_snap_to_edge, daemon=True).start()

    def minimize(self):
        threading.Thread(target=collapse, daemon=True).start()

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


def start(main_fn=None) -> None:
    """Open Athena's window in orb mode. Blocks until it closes."""
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
        min_size=(ORB_W, ORB_H),  # default (200,100) would inflate the orb
        frameless=True,
        on_top=True,
        easy_drag=False,          # drag via .pywebview-drag-region elements
        resizable=False,
        background_color=KEY_COLOR_HTML,
    )
    _window.events.loaded += _on_loaded
    try:
        webview.start(func=main_fn) if main_fn else webview.start()
    finally:
        _window = None
        _loaded.clear()


def stop() -> None:
    window = _window
    if window is not None:
        window.destroy()


def mode() -> str:
    """Current mode: 'orb' or 'dashboard'."""
    return _mode


# ------------------------------------------------- shared state + audio API

def set_state(state: str) -> None:
    """'idle' | 'listening' | 'thinking' | 'speaking' | 'confirm'.
    Works in both modes - each page has its own setState."""
    if state not in STATES:
        state = "idle"
    _cache["state"] = state
    _js(f"window.setState && window.setState('{state}')")


def set_amplitude(value: float) -> None:
    """0..1 audio level: orb bars/pulse, dashboard waveform."""
    try:
        value = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return
    _js(f"window.setAmplitude && window.setAmplitude({value:.3f})")


# ------------------------------------------------------------- orb-mode API

def show_bubble(text: str) -> None:
    """Orb mode only: message bubble beside the orb, window widening to fit."""
    global _shrink_timer
    if _mode != "orb":
        return
    text = str(text)
    _expand_for_bubble(text)
    _js(f"window.showBubble && window.showBubble({json.dumps(text)})")
    if _shrink_timer is not None:
        _shrink_timer.cancel()
    _shrink_timer = threading.Timer(BUBBLE_VISIBLE_SECONDS, _shrink_after_bubble)
    _shrink_timer.daemon = True
    _shrink_timer.start()


# ------------------------------------------------------- dashboard-mode API

def add_log(text: str, tier: str = "free") -> None:
    """Tool log line with a free/confirm/blocked badge."""
    entry = [str(text), tier, time.strftime("%H:%M:%S")]
    _cache["log"].append(entry)
    del _cache["log"][:-_MAX_CACHED]
    _js(f"window.addLogLine && window.addLogLine({json.dumps(entry[0])}, "
        f"{json.dumps(entry[1])}, {json.dumps(entry[2])})")


def add_transcript(who: str, text: str) -> None:
    """Transcript entry ('you' or 'athena'), streamed character by character."""
    _cache["chat"].append([str(who), str(text)])
    del _cache["chat"][:-_MAX_CACHED]
    _js(f"window.addTranscript && window.addTranscript({json.dumps(str(who))}, {json.dumps(str(text))})")


def set_status(status: dict) -> None:
    """Bottom strip: {groq, supabase, mic, cpu, battery, stt, brain, tts}."""
    _cache["status"].update(status)
    _js(f"window.setStatus && window.setStatus({json.dumps(status)})")


def show_confirm(question: str) -> None:
    """Approve/deny card; the answer arrives via the on_confirm hook."""
    _cache["confirm"] = str(question)
    _js(f"window.showConfirm && window.showConfirm({json.dumps(str(question))})")


def hide_confirm() -> None:
    _cache["confirm"] = None
    _js("window.hideConfirm && window.hideConfirm()")


def open_sessions() -> None:
    _js("window.openSessions && window.openSessions()")


def close_sessions() -> None:
    _js("window.closeSessions && window.closeSessions()")


# --------------------------------------------------------- mode switching

def expand() -> None:
    """Orb -> dashboard: grow to 1150x700, centred on the monitor the orb
    is currently on, opaque."""
    global _mode, _orb_phys
    with _switching:
        window = _window
        if window is None or _mode == "dashboard":
            return
        hwnd = _find_hwnd()
        if not hwnd:
            return
        x, y, _w, _h = _win_rect(hwnd)
        _orb_phys = (x, y)                      # come back here on collapse
        scale = _dpi_scale(hwnd)
        wl, wt, wr, wb = _work_area(hwnd)
        w = min(int(DASH_W * scale), wr - wl)
        h = min(int(DASH_H * scale), wb - wt)
        tx = wl + ((wr - wl) - w) // 2
        ty = wt + ((wb - wt) - h) // 2

        _mode = "dashboard"
        _remove_colorkey()  # opaque page: keying off so nothing drops out
        # Let the click's JS promise resolve before we tear the page down,
        # or pywebview logs a callback-into-dead-page TypeError.
        time.sleep(0.1)
        _loaded.clear()
        window.load_url(DASH_HTML.as_uri())
        _animate_phys(hwnd, tx, ty, w, h)


def collapse() -> None:
    """Dashboard -> orb: shrink back to where the orb was docked."""
    global _mode
    with _switching:
        window = _window
        if window is None or _mode == "orb":
            return
        hwnd = _find_hwnd()
        if not hwnd:
            return
        scale = _dpi_scale(hwnd)
        ow, oh = int(ORB_W * scale), int(ORB_H * scale)
        wl, wt, wr, wb = _work_area(hwnd)
        if _orb_phys is not None:
            tx, ty = _orb_phys
        else:
            tx, ty = wr - ow, (wt + wb - oh) // 2
        ty = max(wt, min(ty, wb - oh))

        _mode = "orb"
        time.sleep(0.1)  # same courtesy for the minimise click's promise
        _loaded.clear()
        window.load_url(ORB_HTML.as_uri())  # loaded handler re-keys colours
        _animate_phys(hwnd, tx, ty, ow, oh)


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
        return None  # window mid-close or mid-navigation


def _on_loaded(*_args) -> None:
    """Fires after every navigation (orb page and dashboard page alike)."""
    _loaded.set()
    threading.Thread(target=_after_load, daemon=True).start()


def _after_load() -> None:
    time.sleep(0.3)  # let the form settle
    window = _window
    if window is None:
        return
    if _mode == "orb":
        global _restored
        _apply_colorkey()
        try:
            window.resize(ORB_W, ORB_H)  # creation/switch size can drift
        except Exception:
            pass
        if not _restored:
            _restored = True
            _restore_state()             # last session's spot, once
        _dock_flush()
        _js(f"window.setDock && window.setDock('{_dock}')")
        # the orb page only carries the assistant state
        _js(f"window.setState && window.setState('{_cache['state']}')")
    else:
        _remove_colorkey()
        # hand the full history back to the freshly loaded dashboard
        _js(f"window.__replay && window.__replay({json.dumps(_cache)})")


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


def _apply_colorkey() -> None:
    hwnd = _find_hwnd()
    if not hwnd:
        print("[ui] couldn't get the window handle - orb will have a dark box")
        return
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
    if not user32.SetLayeredWindowAttributes(hwnd, KEY_COLORREF, 0, LWA_COLORKEY):
        print("[ui] colour keying failed - orb will have a dark box")


def _remove_colorkey() -> None:
    hwnd = _find_hwnd()
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_LAYERED)


def _actual_size() -> tuple[int, int]:
    window = _window
    try:
        return int(window.width), int(window.height)
    except Exception:
        return ORB_W, ORB_H


# All snapping/clamping below works in PHYSICAL pixels straight through
# Win32, using the work area of whichever monitor the window is actually on
# (multi-monitor + taskbar aware). pywebview's logical coordinates are only
# used for the live drag deltas, which are relative anyway.

def _win_rect(hwnd) -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _work_area(hwnd) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the work area - the visible desktop
    minus taskbar - of the monitor this window currently sits on."""
    user32 = ctypes.windll.user32
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    w = info.rcWork
    return w.left, w.top, w.right, w.bottom


def _dpi_scale(hwnd) -> float:
    """Physical pixels per logical pixel for this window (e.g. 1.25)."""
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


def _animate_phys(hwnd, x1, y1, w1, h1, steps: int = 12, duration: float = 0.2) -> None:
    """Ease the window to the target physical bounds in small steps."""
    x0, y0, w0, h0 = _win_rect(hwnd)
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 3  # ease-out cubic
        _set_pos(hwnd,
                 round(x0 + (x1 - x0) * e), round(y0 + (y1 - y0) * e),
                 round(w0 + (w1 - w0) * e), round(h0 + (h1 - h0) * e))
        time.sleep(duration / steps)


def _save_state(x: int, y: int) -> None:
    """Remember the orb's docked position (physical) between sessions."""
    try:
        STATE_FILE.write_text(json.dumps({"x": int(x), "y": int(y), "dock": _dock}))
    except OSError:
        pass


def _restore_state() -> None:
    """Once per session: put the orb back where it was last dropped."""
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
    # _dock_flush right after re-clamps into that monitor's work area, so a
    # position saved on a since-unplugged monitor still lands somewhere sane.


def _dock_flush() -> None:
    """Orb mode: pin flush against the docked edge of the current monitor's
    work area, clamping the vertical position inside it."""
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


def _expand_for_bubble(text: str) -> None:
    global _expanded
    window = _window
    if window is None:
        return
    measured = _js(f"window.measureBubble && window.measureBubble({json.dumps(str(text))})")
    try:
        bubble_px = min(int(measured), BUBBLE_MAX_PX)
    except (TypeError, ValueError):
        bubble_px = 220
    try:
        window.resize(ORB_W + BUBBLE_GAP_PX + bubble_px, ORB_H)
        _expanded = True
        _dock_flush()
    except Exception:
        _expanded = False


def _shrink_after_bubble() -> None:
    global _expanded
    window = _window
    if window is None or not _expanded or _mode != "orb":
        _expanded = False
        return
    try:
        window.resize(ORB_W, ORB_H)
    except Exception:
        pass
    _expanded = False
    _dock_flush()


def _snap_to_edge() -> None:
    """Orb mode, after a drag: animate to the nearest left/right edge of the
    monitor the orb was dropped on. The vertical position stays where it
    was dropped (clamped inside the work area), so any corner works. The
    final spot is saved and restored next session."""
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

    _animate_phys(hwnd, x1, y1, w, h, steps=14, duration=0.22)
    _js(f"window.setDock && window.setDock('{_dock}')")
    _save_state(x1, y1)
