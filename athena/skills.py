"""The assistant's hands - real Windows implementations. Every function
actually performs its action and returns a short honest sentence about what
happened. On failure it returns (or raises) a clear reason, never a fake
success. Whatever these return is what Athena says out loud."""

import ctypes
import os
import shutil
import subprocess
import urllib.parse
import webbrowser

import psutil

# The UI module, injected by main.py via set_ui() so the dashboard skills can
# drive it without skills.py importing ui at module load (avoids a cycle and
# lets skills be used headlessly in tests).
_ui = None


def set_ui(ui_module) -> None:
    """main.py calls this once at startup to wire the dashboard skills."""
    global _ui
    _ui = ui_module


# Friendly names -> what Windows knows the app as. ShellExecute (os.startfile)
# resolves registered apps like chrome/msedge even when they're not on PATH.
APP_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "explorer": "explorer",
    "file explorer": "explorer",
    "files": "explorer",
    "spotify": "spotify",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
}


def open_app(name: str) -> str:
    """Launch an application by friendly name. Tries the alias map, then
    PATH, then Windows' registered-app lookup."""
    target = APP_ALIASES.get(name.strip().lower(), name.strip())

    # 1) On PATH? Launch directly (covers 'code', 'notepad', anything added).
    resolved = shutil.which(target)
    if resolved:
        subprocess.Popen([resolved])
        return f"Opened {name}."

    # 2) Ask the Windows shell - resolves App Paths registry entries
    #    (chrome, msedge, spotify...) even when they're not on PATH.
    try:
        os.startfile(target)
        return f"Opened {name}."
    except OSError:
        return (
            f"I couldn't find an app called {name} on this PC. "
            "It may not be installed, or it goes by a different name."
        )


def open_website(url: str) -> str:
    """Open a URL in the default browser."""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if webbrowser.open(url):
        return f"Opened {urllib.parse.urlparse(url).netloc or url} in your browser."
    return f"I couldn't open the browser for {url}."


def web_search(query: str) -> str:
    """Google the query in the default browser."""
    search_url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    if webbrowser.open(search_url):
        return f"Searching Google for {query}."
    return "I couldn't open the browser to run that search."


def _endpoint_volume():
    """Get the Windows master-volume COM interface (pycaw)."""
    import comtypes
    from pycaw.utils import AudioUtilities

    # Safe to call more than once; needed when we run on a worker thread.
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    return AudioUtilities.GetSpeakers().EndpointVolume


def set_volume(level: int) -> str:
    """Set the Windows master volume to a 0-100 percentage."""
    level = max(0, min(100, int(level)))
    try:
        volume = _endpoint_volume()
        volume.SetMasterVolumeLevelScalar(level / 100, None)
        if level > 0:
            volume.SetMute(0, None)
        actual = round(volume.GetMasterVolumeLevelScalar() * 100)
    except Exception as exc:
        return f"I couldn't change the volume: {exc}"
    return f"Volume set to {actual} percent."


def lock_screen() -> str:
    """Lock the Windows workstation."""
    if ctypes.windll.user32.LockWorkStation():
        return "Locking the screen now."
    return "Windows refused to lock the screen, sorry."


def look_up(query: str) -> str:
    """Search the web and speak a short factual answer (does NOT open a tab)."""
    from athena import research  # lazy so tavily loads only if used
    print(f"[skills] looking up: {query!r}")
    return research.look_up(query)


def research_topic(topic: str) -> str:
    """Deeper multi-source web summary, spoken aloud."""
    from athena import research
    print(f"[skills] researching: {topic!r}")
    return research.research(topic)


def guide_me(goal: str) -> str:
    """Walk the user through an on-screen task step by step (guided mode)."""
    from athena import guide  # lazy so vision/tts load only when used
    print(f"[skills] guiding through: {goal!r}")
    return guide.start_guide(goal)


def see_screen(question: str, focus: str = "screen") -> str:
    """Look at the user's screen (or just the active window) and answer a
    question about what's shown. focus is 'screen' or 'window'."""
    from athena import vision  # imported lazily so mss/Pillow load only if used
    active_window_only = str(focus).lower().startswith("window")
    where = "active window" if active_window_only else "screen"
    print(f"[skills] looking at the {where}: {question!r}")
    return vision.ask_about_screen(question, active_window_only=active_window_only)


def open_dashboard() -> str:
    """Expand the orb into the full Athena dashboard."""
    if _ui is None:
        return "The dashboard isn't available right now."
    if _ui.mode() == "dashboard":
        return "The dashboard's already open."
    _ui.expand()
    return "Opening the dashboard."


def close_dashboard() -> str:
    """Collapse the dashboard back to the floating orb."""
    if _ui is None:
        return "The dashboard isn't available right now."
    if _ui.mode() == "orb":
        return "The dashboard's already closed."
    _ui.collapse()
    return "Closing the dashboard."


def get_system_info() -> str:
    """Report real battery and volume status."""
    parts = []

    battery = psutil.sensors_battery()
    if battery is None:
        parts.append("No battery detected, so you're likely on mains power")
    else:
        state = "charging" if battery.power_plugged else "on battery"
        parts.append(f"Battery is at {battery.percent} percent and {state}")

    try:
        volume = _endpoint_volume()
        percent = round(volume.GetMasterVolumeLevelScalar() * 100)
        muted = volume.GetMute()
        parts.append(f"volume is {'muted' if muted else f'at {percent} percent'}")
    except Exception:
        pass  # volume is a bonus; battery info alone is still an answer

    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory().percent
    parts.append(f"CPU load is {cpu:.0f} percent with memory at {mem:.0f} percent")

    return ". ".join(parts) + "."


# The brain dispatches tool calls through this table.
SKILLS = {
    "open_app": open_app,
    "open_website": open_website,
    "web_search": web_search,
    "set_volume": set_volume,
    "lock_screen": lock_screen,
    "get_system_info": get_system_info,
    "see_screen": see_screen,
    "look_up": look_up,
    "research": research_topic,
    "guide_me": guide_me,
    "open_dashboard": open_dashboard,
    "close_dashboard": close_dashboard,
}
