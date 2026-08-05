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


# --- documents (Google Docs, with a local Word fallback) ---
# All lazy imports: the Google libraries are heavy and most turns never
# touch them.

def create_document(title: str) -> str:
    """Create a new, empty Google Doc."""
    from athena import documents
    print(f"[skills] creating document: {title!r}")
    return documents.create_document(title)


def write_to_document(title: str, text: str, mode: str = "append") -> str:
    """Write text the brain composed into a document, then open it."""
    from athena import documents
    print(f"[skills] writing {len(text.split())} words to {title!r} ({mode})")
    return documents.write_to_document(title, text, mode)


def find_document(title: str) -> str:
    """Search the user's documents by name."""
    from athena import documents
    print(f"[skills] finding document: {title!r}")
    return documents.find_document(title)


def read_document(title: str) -> str:
    """Read a document back aloud."""
    from athena import documents
    print(f"[skills] reading document: {title!r}")
    return documents.read_document(title)


def write_local_docx(title: str, text: str) -> str:
    """Write a Word file to the Documents folder - the offline fallback."""
    from athena import documents
    print(f"[skills] writing local docx: {title!r}")
    return documents.write_local_docx(title, text)


# --- email (Gmail) ---

def list_recent_emails(n: int = 10) -> str:
    """Who emailed recently and about what - senders and subjects only."""
    from athena import mail
    print(f"[skills] listing {n} recent emails")
    return mail.list_recent_emails(n)


def summarize_emails(n: int = 10) -> str:
    """Read the recent emails and summarize what they're about."""
    from athena import mail
    print(f"[skills] summarizing {n} recent emails")
    return mail.summarize_emails(n)


def draft_email(to: str, subject: str, body: str) -> str:
    """Save a Gmail draft. This never sends."""
    from athena import mail
    print(f"[skills] drafting email to {to!r}")
    return mail.draft_email(to, subject, body)


def send_email(to: str, subject: str, body: str) -> str:
    """Send an email. mail.send_email reads the recipient and subject back
    and refuses to send without an explicit yes - see mail.py."""
    from athena import mail
    print(f"[skills] send_email requested for {to!r}")
    return mail.send_email(to, subject, body)


# --- spreadsheets (Google Sheets) ---
# Lazy imports again: sheets.py pulls in the Google client, and most turns
# never touch a spreadsheet.

def open_spreadsheet(name_or_url_or_id: str) -> str:
    """Point Athena at a spreadsheet and remember it for later commands."""
    from athena import sheets
    print(f"[skills] opening spreadsheet: {name_or_url_or_id!r}")
    return sheets.open_spreadsheet(name_or_url_or_id)


def list_tabs() -> str:
    from athena import sheets
    return sheets.list_tabs()


def use_tab(name: str) -> str:
    from athena import sheets
    print(f"[skills] switching to tab: {name!r}")
    return sheets.use_tab(name)


def read_range(a1_range: str) -> str:
    from athena import sheets
    print(f"[skills] reading range: {a1_range!r}")
    return sheets.read_range(a1_range)


def describe_sheet() -> str:
    from athena import sheets
    return sheets.describe_sheet()


def count_matching(column: str, condition: str, value: str = "") -> str:
    from athena import sheets
    return sheets.count_matching(column, condition, value)


def sort_range(a1_range: str, column: str, order: str = "asc") -> str:
    from athena import sheets
    print(f"[skills] sorting {a1_range!r} by {column!r} {order}")
    return sheets.sort_range(a1_range, column, order)


def filter_rows(column: str, condition: str, value: str = "") -> str:
    from athena import sheets
    return sheets.filter_rows(column, condition, value)


def clear_filter() -> str:
    from athena import sheets
    return sheets.clear_filter()


def color_range(a1_range: str, color_name: str) -> str:
    from athena import sheets
    return sheets.color_range(a1_range, color_name)


def highlight_rows_where(column: str, condition: str, value: str,
                         color_name: str) -> str:
    from athena import sheets
    return sheets.highlight_rows_where(column, condition, value, color_name)


def add_formula(cell: str, formula: str) -> str:
    from athena import sheets
    print(f"[skills] formula into {cell!r}: {formula!r}")
    return sheets.add_formula(cell, formula)


def insert_rows(at_index: int, count: int = 1) -> str:
    from athena import sheets
    return sheets.insert_rows(at_index, count)


def insert_columns(at_index: int, count: int = 1) -> str:
    from athena import sheets
    return sheets.insert_columns(at_index, count)


def delete_rows(start: int, end: int = 0) -> str:
    from athena import sheets
    print(f"[skills] deleting rows {start} to {end or start}")
    return sheets.delete_rows(start, end)


def delete_columns(start: int, end: int = 0) -> str:
    from athena import sheets
    print(f"[skills] deleting columns {start} to {end or start}")
    return sheets.delete_columns(start, end)


def move_rows(from_start: int, from_end: int, to_index: int) -> str:
    from athena import sheets
    return sheets.move_rows(from_start, from_end, to_index)


def freeze_header(rows: int = 1) -> str:
    from athena import sheets
    return sheets.freeze_header(rows)


def autosize_columns() -> str:
    from athena import sheets
    return sheets.autosize_columns()


def undo_last_change() -> str:
    """Put back the last change Athena made to a spreadsheet."""
    from athena import sheets
    print("[skills] undoing the last sheet change")
    return sheets.undo_last_change()


def apply_sheet_operation(natural_language_request: str) -> str:
    """The escape hatch: sheets.py reads its plan back and refuses to run
    without an explicit yes - see sheets.apply_sheet_operation."""
    from athena import sheets
    print(f"[skills] generic sheet operation: {natural_language_request!r}")
    return sheets.apply_sheet_operation(natural_language_request)


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
    "create_document": create_document,
    "write_to_document": write_to_document,
    "find_document": find_document,
    "read_document": read_document,
    "write_local_docx": write_local_docx,
    "list_recent_emails": list_recent_emails,
    "summarize_emails": summarize_emails,
    "draft_email": draft_email,
    "send_email": send_email,
    "open_spreadsheet": open_spreadsheet,
    "list_tabs": list_tabs,
    "use_tab": use_tab,
    "read_range": read_range,
    "describe_sheet": describe_sheet,
    "count_matching": count_matching,
    "sort_range": sort_range,
    "filter_rows": filter_rows,
    "clear_filter": clear_filter,
    "color_range": color_range,
    "highlight_rows_where": highlight_rows_where,
    "add_formula": add_formula,
    "insert_rows": insert_rows,
    "insert_columns": insert_columns,
    "delete_rows": delete_rows,
    "delete_columns": delete_columns,
    "move_rows": move_rows,
    "freeze_header": freeze_header,
    "autosize_columns": autosize_columns,
    "undo_last_change": undo_last_change,
    "apply_sheet_operation": apply_sheet_operation,
    "open_dashboard": open_dashboard,
    "close_dashboard": close_dashboard,
}
