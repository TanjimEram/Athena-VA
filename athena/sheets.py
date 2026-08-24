"""Working with Google Sheets by voice.

Athena keeps a "current spreadsheet" and a "current tab" in module state, so
you name the sheet once and then just give commands. Every function falls
back to that state when you don't say which sheet or tab you mean.

A NOTE ON RANGES, because the Sheets API mixes two incompatible systems and
this is where such code goes wrong:

  * A1 notation  - "B3:D10", 1-based, and the end cell is INCLUDED. This is
    what a person says out loud, and every public function here takes it.
  * GridRange    - {startRowIndex: 2, endRowIndex: 10, ...}, 0-based, and
    the end index is EXCLUDED. batchUpdate speaks only this.

The conversion between them lives in exactly one place, `_a1_to_grid`.
Nothing else in this module should be doing index arithmetic.

The row/column functions (insert, delete, move, and freeze) take NUMBERS, not
A1. Those numbers are 1-based and INCLUSIVE - "delete rows 3 to 5" means the
three rows labelled 3, 4 and 5 in the sheet's own margin, which is what a
person means when they say it. `_dim_range` does that conversion, and it is
the only other place index arithmetic happens.

Every mutating function snapshots first and refuses to act if it can't - a
change with no way back is worse than no change.

Opening by URL or id reaches any of your spreadsheets. Opening by NAME goes
through Drive, which only shows files Athena created - so a spreadsheet you
made yourself has to be opened by URL the first time.

Everything returns one short honest sentence, spoken verbatim."""

import json
import re
import time

from athena import config, google_auth

# A spreadsheet id out of a full Google Sheets URL.
_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
# A bare id: long and with no spaces. Sheet names never look like this.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,}$")
# "Tab name!B3:D10" -> ("Tab name", "B3:D10"). The tab part is optional.
_A1_RE = re.compile(r"^(?:'(?P<q>[^']+)'|(?P<p>[^!]+))!(?P<rest>.+)$")
# A single A1 cell reference like "B" or "B3" or "$B$3".
_CELL_RE = re.compile(r"^\$?(?P<col>[A-Za-z]*)\$?(?P<row>\d*)$")

# Reading is spoken aloud, so a range is summarised, never dumped.
MAX_SPOKEN_ROWS = 3
MAX_SPOKEN_CELLS = 8

# Colours are spoken by name, never as hex. Soft shades on purpose: these sit
# behind black text and still have to be readable.
COLORS = {
    "red": {"red": 0.96, "green": 0.78, "blue": 0.78},
    "green": {"red": 0.72, "green": 0.88, "blue": 0.80},
    "yellow": {"red": 1.0, "green": 0.95, "blue": 0.66},
    "blue": {"red": 0.74, "green": 0.85, "blue": 0.97},
    "orange": {"red": 0.99, "green": 0.85, "blue": 0.71},
    "grey": {"red": 0.87, "green": 0.87, "blue": 0.87},
}
COLOR_ALIASES = {"gray": "grey", "silver": "grey", "amber": "orange"}

# What a person says -> (Sheets condition type, how to test it locally).
# The local test is what count_matching and highlight_rows_where use; the
# condition type is what the basic filter needs.
CONDITIONS = {
    "equals": ("TEXT_EQ", lambda cell, want: cell.strip().lower() == want),
    "not equals": ("TEXT_NOT_EQ",
                   lambda cell, want: cell.strip().lower() != want),
    "contains": ("TEXT_CONTAINS", lambda cell, want: want in cell.lower()),
    "not contains": ("TEXT_NOT_CONTAINS",
                     lambda cell, want: want not in cell.lower()),
    "starts with": ("TEXT_STARTS_WITH",
                    lambda cell, want: cell.lower().startswith(want)),
    "ends with": ("TEXT_ENDS_WITH",
                  lambda cell, want: cell.lower().endswith(want)),
    "greater than": ("NUMBER_GREATER", lambda cell, want: _num_cmp(cell, want, "gt")),
    "less than": ("NUMBER_LESS", lambda cell, want: _num_cmp(cell, want, "lt")),
    "at least": ("NUMBER_GREATER_THAN_EQ",
                 lambda cell, want: _num_cmp(cell, want, "ge")),
    "at most": ("NUMBER_LESS_THAN_EQ",
                lambda cell, want: _num_cmp(cell, want, "le")),
    "empty": ("BLANK", lambda cell, want: cell.strip() == ""),
    "not empty": ("NOT_BLANK", lambda cell, want: cell.strip() != ""),
}
# The many ways someone says the same condition out loud.
CONDITION_ALIASES = {
    "is": "equals", "=": "equals", "==": "equals", "equal": "equals",
    "equal to": "equals", "is equal to": "equals", "matches": "equals",
    "is not": "not equals", "!=": "not equals", "isn't": "not equals",
    "does not equal": "not equals", "doesn't equal": "not equals",
    "includes": "contains", "has": "contains", "containing": "contains",
    "does not contain": "not contains", "doesn't contain": "not contains",
    "excludes": "not contains",
    "begins with": "starts with",
    ">": "greater than", "more than": "greater than", "above": "greater than",
    "over": "greater than", "bigger than": "greater than",
    "<": "less than", "below": "less than", "under": "less than",
    "smaller than": "less than", "fewer than": "less than",
    ">=": "at least", "at least": "at least", "no less than": "at least",
    "<=": "at most", "no more than": "at most",
    "blank": "empty", "is empty": "empty", "is blank": "empty",
    "not blank": "not empty", "is not empty": "not empty",
    "has a value": "not empty", "filled": "not empty",
}
# Conditions where the value is a number, not text.
NUMERIC_CONDITIONS = {"greater than", "less than", "at least", "at most"}
# Conditions that take no value at all.
VALUELESS_CONDITIONS = {"empty", "not empty"}

_current = {"id": "", "title": "", "tab": "", "sheet_id": None}

# How many changes back you can go. Small on purpose: this lives in memory
# and dies with the process, so it's a safety net for "no, put that back",
# not a history.
UNDO_DEPTH = 5
_undo_stack: list = []

# main.py injects its yes/no asker so apply_sheet_operation can read its plan
# back before doing anything. Until it does, that function cannot run.
_confirm = None


def set_confirm(confirm_fn=None) -> None:
    """main.py wires in its confirm(question) -> bool here - the same one the
    safety gate uses, so a generic operation can be approved by voice or by
    clicking the card."""
    global _confirm
    _confirm = confirm_fn


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def current() -> dict:
    """What Athena is pointed at right now. Handy in tests."""
    return dict(_current)


def _forget() -> None:
    _current.update({"id": "", "title": "", "tab": "", "sheet_id": None})


def _service():
    return google_auth.get_service("sheets", "v4")


def _need_sheet() -> str | None:
    """The spoken complaint when no spreadsheet is open yet, else None."""
    if not _current["id"]:
        return ("I don't have a spreadsheet open. Tell me which one - the "
                "easiest way is to say or paste its link.")
    return None


# --------------------------------------------------------------------------
# A1 <-> GridRange. The ONLY index arithmetic in this module.
# --------------------------------------------------------------------------

def _col_to_index(letters: str) -> int:
    """'A' -> 0, 'B' -> 1, 'AA' -> 26. Zero-based, for GridRange.

    Sheets stops at column ZZZ, so more than three letters is not a column
    reference at all - it's a word. Rejecting it here is what stops
    read_range("banana") being taken as a column and sent to the API."""
    if not letters or len(letters) > 3:
        raise ValueError(f"not a column: {letters!r}")
    index = 0
    for char in letters.upper():
        if not char.isalpha():
            raise ValueError(f"not a column: {letters!r}")
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _index_to_col(index: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA'. The inverse of _col_to_index."""
    if index < 0:
        raise ValueError(f"negative column index: {index}")
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _parse_a1(a1: str) -> tuple[str, str]:
    """Split "Tab!B3:D10" into ("Tab", "B3:D10"). With no tab, the current
    tab is used. Quoted tab names ('My Sheet'!A1) are handled."""
    a1 = (a1 or "").strip()
    match = _A1_RE.match(a1)
    if match:
        tab = match.group("q") or match.group("p")
        return tab.strip(), match.group("rest").strip()
    return _current["tab"], a1


def _a1_to_grid(a1: str, sheet_id: int | None = None) -> dict:
    """A1 notation -> a GridRange. This is where 1-based-inclusive becomes
    0-based-exclusive; nowhere else should do it.

    An open-ended half ("B:B", "3:5") leaves that dimension unbounded, which
    is exactly what the API wants for a whole column or row."""
    tab, rest = _parse_a1(a1)
    grid = {"sheetId": _current["sheet_id"] if sheet_id is None else sheet_id}
    if tab and tab != _current["tab"]:
        resolved = _tab_id(tab)
        if resolved is not None:
            grid["sheetId"] = resolved

    if not rest:
        return grid

    start_ref, _, end_ref = rest.partition(":")
    end_ref = end_ref or start_ref

    start = _CELL_RE.match(start_ref.strip())
    end = _CELL_RE.match(end_ref.strip())
    if not start or not end:
        raise ValueError(f"not a range I understand: {a1!r}")

    if start.group("col"):
        grid["startColumnIndex"] = _col_to_index(start.group("col"))
    if end.group("col"):
        # +1 because A1's end column is included and GridRange's is not.
        grid["endColumnIndex"] = _col_to_index(end.group("col")) + 1
    if start.group("row"):
        grid["startRowIndex"] = int(start.group("row")) - 1
    if end.group("row"):
        grid["endRowIndex"] = int(end.group("row"))
    return grid


def _grid_to_a1(grid: dict, tab: str = "") -> str:
    """A GridRange back to A1, for saying what a range was."""
    start_col = grid.get("startColumnIndex")
    end_col = grid.get("endColumnIndex")
    start_row = grid.get("startRowIndex")
    end_row = grid.get("endRowIndex")
    left = (_index_to_col(start_col) if start_col is not None else "") + \
           (str(start_row + 1) if start_row is not None else "")
    right = (_index_to_col(end_col - 1) if end_col is not None else "") + \
            (str(end_row) if end_row is not None else "")
    body = f"{left}:{right}" if right and right != left else (left or "")
    return f"{tab}!{body}" if tab and body else (body or tab)


def _qualify(a1: str) -> str:
    """Put the current tab in front of a bare range, quoting a name with
    spaces, so values.get always hits the tab we think it does."""
    tab, rest = _parse_a1(a1)
    if not tab:
        return rest
    safe = f"'{tab}'" if re.search(r"[^A-Za-z0-9_]", tab) else tab
    return f"{safe}!{rest}" if rest else safe


# --------------------------------------------------------------------------
# spreadsheet metadata
# --------------------------------------------------------------------------

def _meta(include_grid: bool = False) -> dict | None:
    """The open spreadsheet's metadata, or None on failure."""
    sheets = _service()
    if sheets is None or not _current["id"]:
        return None
    try:
        return sheets.spreadsheets().get(
            spreadsheetId=_current["id"], includeGridData=include_grid,
            fields=("spreadsheetId,properties.title,"
                    "sheets.properties(sheetId,title,index,"
                    "gridProperties(rowCount,columnCount,frozenRowCount))")
        ).execute()
    except Exception as exc:
        print(f"[sheets] couldn't read the spreadsheet: {google_auth._short(exc)}")
        return None


def _tabs() -> list:
    meta = _meta()
    if not meta:
        return []
    return [s["properties"] for s in meta.get("sheets", [])]


def _tab_id(name: str) -> int | None:
    """The numeric sheetId for a tab name, or None if there's no such tab."""
    for props in _tabs():
        if props.get("title", "").lower() == (name or "").strip().lower():
            return props.get("sheetId")
    return None


def _find_by_name(name: str) -> list:
    """Spreadsheets matching a name, via Drive. Only finds files Athena
    created - the drive.file scope can't see the rest."""
    drive = google_auth.get_service("drive", "v3")
    if drive is None:
        return []
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    try:
        found = drive.files().list(
            q=(f"name contains '{escaped}' and "
               "mimeType='application/vnd.google-apps.spreadsheet' and "
               "trashed=false"),
            fields="files(id,name)", orderBy="modifiedTime desc",
            pageSize=10).execute()
        return found.get("files", [])
    except Exception as exc:
        print(f"[sheets] search failed: {google_auth._short(exc)}")
        return []


# --------------------------------------------------------------------------
# the read-only skills
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# undo
#
# The Sheets API has no undo. Undo is a feature of the browser editor, and
# nothing done through the API goes anywhere near that stack - so we keep our
# own. Before any mutating call, capture the affected range's values AND
# formatting; undo_last_change writes the captured state back.
# --------------------------------------------------------------------------

def _apply(requests: list, spreadsheet_id: str = "") -> str | None:
    """Run a batchUpdate. Returns None on success, or a spoken error."""
    sheets = _service()
    if sheets is None:
        return google_auth.not_connected_message()
    target = spreadsheet_id or _current["id"]
    if not target:
        return _need_sheet()
    if not requests:
        return None
    try:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=target, body={"requests": requests}).execute()
    except Exception as exc:
        return f"the spreadsheet refused that: {google_auth._short(exc)}"
    return None


def _tab_props(sheet_id: int | None = None) -> dict:
    """gridProperties for a tab, so an unbounded range can be given limits."""
    want = _current["sheet_id"] if sheet_id is None else sheet_id
    for props in _tabs():
        if props.get("sheetId") == want:
            return props
    return {}


def _bounded(grid: dict) -> dict:
    """Fill in any missing edge of a GridRange with the tab's real size.

    "B:B" has no row bounds, but a restore MUST be bounded: updateCells
    clears whatever part of the target range the restored rows don't cover,
    and an unbounded range would decide that extent for us."""
    props = _tab_props(grid.get("sheetId")).get("gridProperties", {})
    bounded = dict(grid)
    bounded.setdefault("startRowIndex", 0)
    bounded.setdefault("startColumnIndex", 0)
    if "endRowIndex" not in bounded:
        bounded["endRowIndex"] = props.get("rowCount", 1000)
    if "endColumnIndex" not in bounded:
        bounded["endColumnIndex"] = props.get("columnCount", 26)
    return bounded


def _snapshot(a1_range: str) -> dict | None:
    """Capture a range's values AND formatting, ready to be written back.
    None if it couldn't be read - in which case the caller must NOT mutate,
    since there would be no way back."""
    sheets = _service()
    if sheets is None or not _current["id"]:
        return None
    try:
        grid = _bounded(_a1_to_grid(a1_range))
    except ValueError:
        print(f"[sheets] can't snapshot {a1_range!r}: not a range")
        return None

    try:
        response = sheets.spreadsheets().get(
            spreadsheetId=_current["id"], ranges=[_qualify(a1_range)],
            includeGridData=True,
            fields=("sheets(properties(sheetId,title),data("
                    "rowData(values(userEnteredValue,userEnteredFormat))))")
        ).execute()
    except Exception as exc:
        print(f"[sheets] couldn't snapshot {a1_range!r}: "
              f"{google_auth._short(exc)}")
        return None

    tabs = response.get("sheets", [])
    data = (tabs[0].get("data", [{}])[0] if tabs else {})
    return {
        "spreadsheet_id": _current["id"],
        "spreadsheet_title": _current["title"],
        "tab": (tabs[0].get("properties", {}).get("title", "") if tabs else ""),
        "a1": a1_range,
        "grid": grid,
        # Exactly what updateCells wants back. Empty means the range was
        # blank, and restoring it should clear whatever is there now.
        "rows": data.get("rowData", []) or [],
        "taken_at": time.time(),
    }


def _push_undo(label: str, snapshot: dict | None) -> bool:
    """Remember how to put something back. `label` is spoken on undo, so it
    should read naturally: "the sort of A1 to D20".

    A snapshot may carry an "inverse" key - a list of batchUpdate requests to
    run instead of restoring cells. Structural edits (inserting or deleting
    rows) shift the grid, so writing old values into a range does NOT reverse
    them; those get an inverse request instead. Nothing uses it yet, it's the
    hook Phase 3 needs.

    Returns False if there was nothing to record - the caller must then
    refuse to mutate rather than act with no way back."""
    if not snapshot:
        return False
    snapshot["label"] = label or "that change"
    _undo_stack.append(snapshot)
    while len(_undo_stack) > UNDO_DEPTH:
        _undo_stack.pop(0)          # oldest falls off the bottom
    print(f"[sheets] undo point: {snapshot['label']} "
          f"({len(_undo_stack)} on the stack)")
    return True


def undo_depth() -> int:
    """How many changes can still be undone."""
    return len(_undo_stack)


def _clear_undo() -> None:
    _undo_stack.clear()


def undo_last_change() -> str:
    """Put back the most recent change Athena made."""
    if _service() is None:
        return google_auth.not_connected_message()
    if not _undo_stack:
        return "There's nothing for me to undo."

    snapshot = _undo_stack.pop()
    label = snapshot.get("label", "that change")

    requests = snapshot.get("inverse") or [{
        "updateCells": {
            "rows": snapshot["rows"],
            # Both, so colours come back with the values. Cells the saved
            # rows don't cover are cleared, which is what makes this undo
            # anything the change added beyond the original extent.
            "fields": "userEnteredValue,userEnteredFormat",
            "range": snapshot["grid"],
        }
    }]

    error = _apply(requests, snapshot.get("spreadsheet_id", ""))
    if error:
        # Put it back on the stack - it wasn't undone, so it's still pending.
        _undo_stack.append(snapshot)
        return f"I couldn't undo {label}: {error}"

    where = snapshot.get("a1", "")
    # Say which spreadsheet if it isn't the one we're looking at now - undoing
    # somewhere the user has moved on from should never be silent.
    other = snapshot.get("spreadsheet_title", "")
    elsewhere = (f" in {other}" if other and snapshot.get("spreadsheet_id")
                 != _current["id"] else "")
    remaining = len(_undo_stack)
    tail = (f" I can go back {remaining} more change{'s' if remaining != 1 else ''}."
            if remaining else "")
    # Only claim a range is "back as it was" when we really did restore its
    # cells - an inverse request reverses a structural edit, which is a
    # different thing and shouldn't be described as a range being restored.
    restored_cells = not snapshot.get("inverse")
    return (f"I've undone {label}{elsewhere}"
            + (f", so {where} is back as it was." if where and restored_cells
               else ".") + tail)


def open_spreadsheet(name_or_url_or_id: str) -> str:
    """Point Athena at a spreadsheet and remember it. Accepts a full URL, a
    raw id, or a name (names only find spreadsheets Athena created)."""
    sheets = _service()
    if sheets is None:
        return google_auth.not_connected_message()

    raw = (name_or_url_or_id or "").strip()
    if not raw:
        return "Tell me which spreadsheet - its name, or its link."

    url_match = _URL_RE.search(raw)
    if url_match:
        target = url_match.group(1)
    elif _ID_RE.match(raw):
        target = raw
    else:
        matches = _find_by_name(raw)
        if not matches:
            return (f"I couldn't find a spreadsheet called {raw}. I can only "
                    "search ones I created myself - for any other, say or "
                    "paste its link instead.")
        if len(matches) > 1:
            exact = [m for m in matches if m["name"].lower() == raw.lower()]
            if len(exact) != 1:
                names = ", ".join(m["name"] for m in matches[:3])
                return (f"I found more than one spreadsheet matching {raw}: "
                        f"{names}. Which one?")
            matches = exact
        target = matches[0]["id"]

    try:
        meta = sheets.spreadsheets().get(
            spreadsheetId=target,
            fields=("spreadsheetId,properties.title,"
                    "sheets.properties(sheetId,title,index,"
                    "gridProperties(rowCount,columnCount))")).execute()
    except Exception as exc:
        _forget()
        return (f"I couldn't open that spreadsheet: "
                f"{google_auth._short(exc)}")

    tabs = [s["properties"] for s in meta.get("sheets", [])]
    if not tabs:
        _forget()
        return "That spreadsheet has no tabs in it, so there's nothing to work on."

    first = tabs[0]
    _current.update({
        "id": meta.get("spreadsheetId", target),
        "title": meta.get("properties", {}).get("title", "the spreadsheet"),
        "tab": first.get("title", ""),
        "sheet_id": first.get("sheetId"),
    })
    print(f"[sheets] opened {_current['title']!r} ({_current['id']})")

    count = len(tabs)
    if count == 1:
        return f"I've got {_current['title']} open, on the tab {_current['tab']}."
    return (f"I've got {_current['title']} open. It has {count} tabs, and "
            f"I'm on {_current['tab']}.")


def list_tabs() -> str:
    """Name the tabs in the open spreadsheet, and say which one is current."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    tabs = _tabs()
    if not tabs:
        return "I couldn't read the tabs in that spreadsheet."
    names = [t.get("title", "") for t in tabs]
    if len(names) == 1:
        return f"There's one tab, {names[0]}, and that's the one I'm on."
    return (f"There are {len(names)} tabs: {', '.join(names)}. "
            f"I'm on {_current['tab']}.")


def use_tab(name: str) -> str:
    """Switch the current tab. Read-only - it only changes what we point at."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    sheet_id = _tab_id(name)
    if sheet_id is None:
        names = ", ".join(t.get("title", "") for t in _tabs()) or "none"
        return f"There's no tab called {name}. The tabs are: {names}."
    _current.update({"tab": name.strip(), "sheet_id": sheet_id})
    return f"I'm on the {_current['tab']} tab now."


def _values(a1: str) -> list | None:
    """Raw cell values for a range, or None on failure."""
    sheets = _service()
    if sheets is None or not _current["id"]:
        return None
    try:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=_current["id"], range=_qualify(a1)).execute()
        return result.get("values", [])
    except Exception as exc:
        print(f"[sheets] couldn't read {a1!r}: {google_auth._short(exc)}")
        return None


def _say_row(row: list) -> str:
    """One row as a short spoken phrase."""
    cells = [str(c) for c in row[:MAX_SPOKEN_CELLS] if str(c).strip() != ""]
    text = ", ".join(cells)
    if len(row) > MAX_SPOKEN_CELLS:
        text += f", and {len(row) - MAX_SPOKEN_CELLS} more"
    return text or "an empty row"


def read_range(a1_range: str) -> str:
    """Summarise what's in a range - shape, headers, a couple of rows. This
    is spoken, so it never reads a whole grid aloud."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    if not (a1_range or "").strip():
        return "Tell me which cells to read, like A1 to D10."

    try:
        _a1_to_grid(a1_range)          # fail early on nonsense like "banana"
    except ValueError:
        return f"I couldn't make sense of the range {a1_range}."

    rows = _values(a1_range)
    if rows is None:
        return f"I couldn't read {a1_range} - check the range and the tab name."
    if not rows:
        return f"{a1_range} is empty."

    width = max(len(r) for r in rows)
    shape = (f"{len(rows)} row{'s' if len(rows) != 1 else ''} and "
             f"{width} column{'s' if width != 1 else ''}")
    lines = [f"{a1_range} has {shape}."]
    for row in rows[:MAX_SPOKEN_ROWS]:
        lines.append(_say_row(row) + ".")
    remaining = len(rows) - MAX_SPOKEN_ROWS
    if remaining > 0:
        lines.append(f"And {remaining} more row{'s' if remaining != 1 else ''}.")
    return " ".join(lines)


def describe_sheet() -> str:
    """The shape of the current tab: headers, how many rows of data, and
    what other tabs are there."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    tabs = _tabs()
    if not tabs:
        return "I couldn't read that spreadsheet's structure."
    props = next((t for t in tabs
                  if t.get("title", "").lower() == _current["tab"].lower()), tabs[0])
    grid = props.get("gridProperties", {})

    rows = _values("A1:ZZ1") or []
    headers = [str(c) for c in (rows[0] if rows else []) if str(c).strip()]

    used = _values("A:A")
    data_rows = max(0, len(used) - 1) if used else 0

    parts = [f"{_current['title']}, on the tab {props.get('title', '')}."]
    if headers:
        shown = headers[:MAX_SPOKEN_CELLS]
        more = f", and {len(headers) - len(shown)} more" if len(headers) > len(shown) else ""
        parts.append(f"The columns are {', '.join(shown)}{more}.")
    else:
        parts.append("It has no header row that I can see.")
    parts.append(f"There {'is' if data_rows == 1 else 'are'} about {data_rows} "
                 f"row{'s' if data_rows != 1 else ''} of data, in a grid of "
                 f"{grid.get('rowCount', '?')} by {grid.get('columnCount', '?')}.")
    if len(tabs) > 1:
        others = [t.get("title", "") for t in tabs if t is not props]
        parts.append(f"The other tabs are {', '.join(others)}.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# resolving what the user said into columns, conditions and colours
# --------------------------------------------------------------------------

def _num_cmp(cell: str, want, how: str) -> bool:
    """Compare a cell numerically. A cell that isn't a number never matches -
    saying "greater than 10" about the word "apple" is False, not an error."""
    try:
        left = float(str(cell).replace(",", "").strip())
        right = float(want)
    except (TypeError, ValueError):
        return False
    return {"gt": left > right, "lt": left < right,
            "ge": left >= right, "le": left <= right}[how]


def _headers() -> list:
    """The current tab's header row, as text."""
    rows = _values("A1:ZZ1") or []
    return [str(c) for c in (rows[0] if rows else [])]


def _resolve_column(column) -> tuple[int | None, str]:
    """Turn what the user said into (zero-based column index, spoken name).

    Accepts a header name ("Score"), a column letter ("C"), or a number.
    Header names win over letters, because someone saying "sort by C" almost
    certainly means a column headed C if one exists."""
    if column is None or str(column).strip() == "":
        return None, ""
    text = str(column).strip()

    headers = _headers()
    for index, name in enumerate(headers):
        if name.strip().lower() == text.lower():
            return index, name.strip()

    if text.isdigit():                    # "column 3", spoken 1-based
        number = int(text)
        if number >= 1:
            index = number - 1
            return index, (headers[index] if index < len(headers)
                           else f"column {_index_to_col(index)}")

    try:
        index = _col_to_index(text)
    except ValueError:
        return None, text
    return index, (headers[index] if index < len(headers)
                   else f"column {text.upper()}")


def _resolve_condition(condition: str) -> str | None:
    """Normalise a spoken condition to a key in CONDITIONS, or None."""
    text = " ".join(str(condition or "").lower().split())
    text = CONDITION_ALIASES.get(text, text)
    return text if text in CONDITIONS else None


def _resolve_color(name: str) -> dict | None:
    text = str(name or "").strip().lower()
    text = COLOR_ALIASES.get(text, text)
    return COLORS.get(text)


def _color_names() -> str:
    return ", ".join(sorted(COLORS))


def _dim_range(start: int, end: int, dimension: str = "ROWS",
               sheet_id: int | None = None) -> dict:
    """Rows/columns the way a person says them - 1-based and INCLUSIVE -
    into the API's 0-based, end-exclusive DimensionRange. Along with
    _a1_to_grid, the only index arithmetic in this module."""
    start, end = int(start), int(end)
    if start < 1 or end < start:
        raise ValueError(f"not a range of {dimension.lower()}: {start} to {end}")
    return {"sheetId": _current["sheet_id"] if sheet_id is None else sheet_id,
            "dimension": dimension,
            "startIndex": start - 1,
            "endIndex": end}


def _tab_values(tab: str = "") -> list:
    """Every used cell on a tab.

    Not _values(): there the argument is a RANGE, and _qualify prefixes it
    with the current tab - so asking for "Sheet1" became "Sheet1!Sheet1",
    which the API rejects. That silently returned nothing, which made
    _used_extent report 0 x 0 and quietly shrank the filter, the highlight,
    the autosize and - worst - the escape hatch's undo snapshot down to a
    single cell. Here the tab name IS the range, so it goes through
    untouched."""
    service = _service()
    if service is None or not _current["id"]:
        return []
    name = tab or _current["tab"]
    if not name:
        return []
    safe = f"'{name}'" if re.search(r"[^A-Za-z0-9_]", name) else name
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=_current["id"], range=safe).execute()
        return result.get("values", []) or []
    except Exception as exc:
        print(f"[sheets] couldn't read the tab {name!r}: {google_auth._short(exc)}")
        return []


def _used_extent() -> tuple[int, int]:
    """(rows, columns) actually in use on the current tab."""
    rows = _tab_values() or []
    return len(rows), (max((len(r) for r in rows), default=0))


def _inverse_entry(a1: str, inverse: list) -> dict:
    """An undo entry that reverses something by running requests, rather than
    by writing cells back. Structural edits need this - see _push_undo."""
    return {"spreadsheet_id": _current["id"],
            "spreadsheet_title": _current["title"],
            "tab": _current["tab"], "a1": a1, "grid": {}, "rows": [],
            "inverse": inverse, "taken_at": time.time()}


def _mutate(label: str, snapshot: dict | None, requests: list,
            success: str) -> str:
    """The shape every mutating function takes: record how to undo, then act,
    and never leave those two out of step.

    If nothing could be recorded we don't act at all. If the change fails, the
    undo entry is removed again - a phantom entry would make the next undo
    put back something that was never changed."""
    if not _push_undo(label, snapshot):
        return ("I couldn't save a way back, so I've not changed anything. "
                "Check the range and try again.")
    error = _apply(requests)
    if error:
        _undo_stack.pop()
        # Labels are noun phrases, worded to read after "I've undone ..." -
        # so the failure sentence has to be built to fit them too.
        return f"Something went wrong with {label}: {error}"
    return success


# --------------------------------------------------------------------------
# named operations
# --------------------------------------------------------------------------

def _via_builder(configure, what: str = 'that change') -> str:
    """Run one named operation through the builder.

    Every mutating named operation goes through here, so there is one code
    path rather than two: the same validation, the same snapshot, the same
    all-or-nothing execution and the same undo entry that the escape hatch
    gets."""
    from athena.sheet_builder import PlanError, SheetRequestBuilder, execute
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    try:
        builder = SheetRequestBuilder()
        configure(builder)
        builder.resolve()
        plan = builder.build()
    except PlanError as exc:
        return str(exc)
    except Exception as exc:
        print(f"[sheets] couldn't plan {what}: {exc!r}")
        return f"I couldn't work out how to {what}, so I've changed nothing."
    return execute(plan)


def sort_range(a1_range: str, column, order: str = "asc") -> str:
    """Sort a range by one column. The range is A1 notation and is sorted
    exactly as given - include the header row only if you want it sorted."""
    return _via_builder(lambda b: b.sort(column, order, a1_range=a1_range),
                        "sort that")


def color_range(a1_range: str, color_name: str) -> str:
    """Fill a range with a named background colour."""
    return _via_builder(lambda b: b.colour(a1_range, color_name),
                        "colour that")


def highlight_rows_where(column, condition: str, value: str,
                         color_name: str) -> str:
    """Colour every row whose column matches a condition."""
    return _via_builder(
        lambda b: b.highlight_where(column, condition, value, color_name),
        "highlight those rows")


def _matching_rows(index: int, key: str, value) -> list | None:
    """Zero-based sheet row indices whose column matches. Skips the header
    row. None if the sheet couldn't be read."""
    letter = _index_to_col(index)
    column_values = _values(f"{letter}:{letter}")
    if column_values is None:
        return None
    test = CONDITIONS[key][1]
    want = ("" if key in VALUELESS_CONDITIONS
            else (str(value).strip().lower() if key not in NUMERIC_CONDITIONS
                  else value))
    matches = []
    for row_number, row in enumerate(column_values):
        if row_number == 0:
            continue                     # the header is never a match
        cell = str(row[0]) if row else ""
        try:
            if test(cell, want):
                matches.append(row_number)
        except Exception:
            continue
    return matches


def count_matching(column, condition: str, value: str = "") -> str:
    """Count rows matching a condition. Reads only - changes nothing."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    key = _resolve_condition(condition)
    if key is None:
        return f"I don't know how to test for {condition}."
    index, name = _resolve_column(column)
    if index is None:
        return f"I couldn't work out which column {column} is."

    matches = _matching_rows(index, key, value)
    if matches is None:
        return "I couldn't read that column."
    count = len(matches)
    tail = "" if key in VALUELESS_CONDITIONS else f" {value}"
    if count == 0:
        return f"No rows where {name} {key}{tail}."
    return (f"{count} row{'s' if count != 1 else ''} where {name} {key}{tail}.")


def _current_filter() -> dict | None:
    """The basic filter currently on the tab, or None if there isn't one."""
    sheets = _service()
    if sheets is None or not _current["id"]:
        return None
    try:
        response = sheets.spreadsheets().get(
            spreadsheetId=_current["id"],
            fields="sheets(properties(sheetId),basicFilter)").execute()
    except Exception as exc:
        print(f"[sheets] couldn't read the filter: {google_auth._short(exc)}")
        return None
    for tab in response.get("sheets", []):
        if tab.get("properties", {}).get("sheetId") == _current["sheet_id"]:
            return tab.get("basicFilter")
    return None


def _filter_undo() -> dict:
    """An undo entry that puts the previous filter back - or clears it, if
    there wasn't one. A cell snapshot can't capture a filter."""
    existing = _current_filter()
    if existing:
        inverse = [{"setBasicFilter": {"filter": existing}}]
    else:
        inverse = [{"clearBasicFilter": {"sheetId": _current["sheet_id"]}}]
    return _inverse_entry(_current["tab"], inverse)


def filter_rows(column, condition: str, value: str = "") -> str:
    """Set the tab's basic filter to show only matching rows."""
    return _via_builder(lambda b: b.filter(column, condition, value),
                        "filter that")


def clear_filter() -> str:
    """Remove the tab's basic filter, showing every row again."""
    if _current_filter() is None:
        return "There's no filter on this tab to clear."
    return _via_builder(lambda b: b.clear_filter(), "clear the filter")


def add_formula(cell: str, formula: str) -> str:
    """Put a formula in one cell. `cell` is A1 notation like "E2"; the
    formula is entered as if typed, so "SUM(A1:A5)" and "=SUM(A1:A5)" both
    work."""
    return _via_builder(lambda b: b.set_formula(cell, formula),
                        "add that formula")


def insert_rows(at_index: int, count: int = 1) -> str:
    """Insert blank rows BEFORE row `at_index`. 1-based, as spoken."""
    return _via_builder(lambda b: b.insert_rows(at_index, count),
                        "insert those rows")


def insert_columns(at_index: int, count: int = 1) -> str:
    """Insert blank columns BEFORE column `at_index`. 1-based, as spoken."""
    return _via_builder(lambda b: b.insert_columns(at_index, count),
                        "insert those columns")


def _insert(dimension: str, at_index: int, count: int) -> str:
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    try:
        count = max(1, int(count))
        at_index = int(at_index)
        target = _dim_range(at_index, at_index + count - 1, dimension)
    except (TypeError, ValueError):
        return f"I couldn't work out where to insert - {at_index} isn't a row number."

    word = "row" if dimension == "ROWS" else "column"
    # Inserting is reversed by deleting exactly what was inserted, not by
    # writing cells back: the insert shifted everything below it.
    undo = _inverse_entry(f"{count} new {word}s at {at_index}",
                          [{"deleteDimension": {"range": target}}])
    request = {"insertDimension": {"range": target, "inheritFromBefore": False}}
    return _mutate(f"inserting {count} {word}{'s' if count != 1 else ''}",
                   undo, [request],
                   f"Inserted {count} {word}{'s' if count != 1 else ''} "
                   f"at {word} {at_index}.")


def delete_rows(start: int, end: int = 0) -> str:
    """Delete rows `start` to `end` inclusive, 1-based, as spoken."""
    return _via_builder(lambda b: b.delete_rows(start, end or None),
                        "delete those rows")


def delete_columns(start: int, end: int = 0) -> str:
    """Delete columns `start` to `end` inclusive, 1-based, as spoken."""
    return _via_builder(lambda b: b.delete_columns(start, end or None),
                        "delete those columns")


def _delete(dimension: str, start: int, end: int) -> str:
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    try:
        start = int(start)
        end = int(end) if end else start
        target = _dim_range(start, end, dimension)
    except (TypeError, ValueError):
        return f"I couldn't work out which to delete - {start} to {end}."

    word = "row" if dimension == "ROWS" else "column"
    count = end - start + 1

    # Deleting needs BOTH halves to come back: put the rows back, then write
    # their contents into them. A cell snapshot alone would restore nothing,
    # because after the delete those cells don't exist.
    if dimension == "ROWS":
        a1 = f"{start}:{end}"
    else:
        a1 = f"{_index_to_col(start - 1)}:{_index_to_col(end - 1)}"
    doomed = _snapshot(a1)
    if doomed is None:
        return (f"I couldn't read those {word}s first, so I've not deleted "
                "anything - I'd have had no way to put them back.")
    undo = _inverse_entry(a1, [
        {"insertDimension": {"range": target, "inheritFromBefore": False}},
        {"updateCells": {"rows": doomed["rows"],
                         "fields": "userEnteredValue,userEnteredFormat",
                         "range": doomed["grid"]}},
    ])
    request = {"deleteDimension": {"range": target}}
    span = f"{word} {start}" if count == 1 else f"{word}s {start} to {end}"
    return _mutate(f"deleting {span}", undo, [request], f"Deleted {span}.")


def _move_inverse(start0: int, end0: int, dest0: int) -> tuple[int, int, int]:
    """Where a moved block ends up, and how to move it back.

    All indices 0-based, end-exclusive, and `dest0` is in the coordinates
    BEFORE the move - which is what moveDimension means by destinationIndex.
    Returns (new_start, new_end, destination_to_put_it_back)."""
    size = end0 - start0
    landed = dest0 - size if dest0 >= end0 else dest0
    # To send it home: if it moved up, aim past its old home; if it moved
    # down, aim straight at it. Same before/after coordinate rule as above.
    back = start0 + size if landed < start0 else start0
    return landed, landed + size, back


def move_rows(from_start: int, from_end: int, to_index: int) -> str:
    """Move rows `from_start` to `from_end` (inclusive, 1-based) so they sit
    before row `to_index` as the sheet is numbered now."""
    return _via_builder(
        lambda b: b.move_rows(from_start, from_end, to_index),
        "move those rows")


def freeze_header(rows: int = 1) -> str:
    """Freeze the top `rows` rows so they stay put when scrolling."""
    return _via_builder(lambda b: b.freeze(rows), "freeze the header")


def _column_widths(count: int) -> list | None:
    """Current pixel widths of the first `count` columns, so autosize can be
    undone. None if they couldn't be read."""
    sheets = _service()
    if sheets is None:
        return None
    try:
        response = sheets.spreadsheets().get(
            spreadsheetId=_current["id"], ranges=[_current["tab"]],
            includeGridData=True,
            fields="sheets(properties(sheetId),data(columnMetadata(pixelSize)))"
        ).execute()
    except Exception as exc:
        print(f"[sheets] couldn't read column widths: {google_auth._short(exc)}")
        return None
    tabs = response.get("sheets", [])
    data = (tabs[0].get("data", [{}])[0] if tabs else {})
    metadata = data.get("columnMetadata", []) or []
    return [m.get("pixelSize") for m in metadata[:count]]


def autosize_columns() -> str:
    """Resize every used column to fit its contents."""
    return _via_builder(lambda b: b.autosize(), "resize the columns")


# --------------------------------------------------------------------------
# the escape hatch
#
# For the unusual request no named operation covers.
#
# The model no longer writes batchUpdate JSON. It writes a STRUCTURED PLAN -
# a list of named steps with arguments - which our own code translates into
# builder calls. An invented step name or a malformed argument is rejected
# here, by us, and never reaches Google. Raw JSON could only ever be checked
# for plausibility; a step list can be checked for correctness.
#
# It also fixes what made "delete all the rows coloured orange" impossible:
# one batchUpdate cannot read the formats, decide which rows match, and
# delete them in descending order. A plan can.
# --------------------------------------------------------------------------

MAX_PLAN_STEPS = 10

# step name -> (builder method, required args, optional args)
PLAN_STEPS = {
    "sort": ("sort", ("column",), ("order",)),
    "filter": ("filter", ("column", "condition"), ("value",)),
    "clear_filter": ("clear_filter", (), ()),
    "colour": ("colour", ("range", "colour"), ()),
    "highlight_where": ("highlight_where",
                        ("column", "condition", "value", "colour"), ()),
    "delete_rows_where": ("delete_rows_where", ("match",),
                          ("column", "value", "colour")),
    "insert_rows": ("insert_rows", ("at",), ("count",)),
    "delete_rows": ("delete_rows", ("start",), ("end",)),
    "insert_columns": ("insert_columns", ("at",), ("count",)),
    "delete_columns": ("delete_columns", ("start",), ("end",)),
    "move_rows": ("move_rows", ("from_start", "from_end", "to_index"), ()),
    "set_formula": ("set_formula", ("cell", "formula"), ()),
    "freeze": ("freeze", (), ("rows",)),
    "autosize": ("autosize", (), ()),
    "export": ("export", (), ("path",)),
}

PLAN_SYSTEM = (
    "You turn a spoken spreadsheet request into a PLAN: a JSON array of "
    "steps. Reply with ONLY that array - no prose, no markdown fences.\n\n"
    "Each step is an object with a 'step' key naming one of:\n"
    "  sort {column, order}            filter {column, condition, value}\n"
    "  clear_filter {}                 colour {range, colour}\n"
    "  highlight_where {column, condition, value, colour}\n"
    "  delete_rows_where {match, column, value, colour}\n"
    "  insert_rows {at, count}         delete_rows {start, end}\n"
    "  insert_columns {at, count}      delete_columns {start, end}\n"
    "  move_rows {from_start, from_end, to_index}\n"
    "  set_formula {cell, formula}     freeze {rows}\n"
    "  autosize {}                     export {path}\n\n"
    "Rows and columns are 1-based, as the sheet labels them. Ranges are A1 "
    "like B2:D10. Colours are one of: red, green, yellow, blue, orange, "
    "grey.\n\n"
    "For delete_rows_where, 'match' is either a condition (equals, contains, "
    "greater than, less than, empty) with a column and value, or the words "
    "'background colour' with a colour. Use it whenever rows are chosen by "
    "what they contain or how they look - never guess row numbers you have "
    "not been told.\n\n"
    "Use the fewest steps that do the job. If the request cannot be done "
    "with these steps, reply with exactly []."
)


# --- which model does the sheet thinking -------------------------------
# Routed HERE, at the one call site that needs it. brain.py is deliberately
# untouched: the voice loop stays on Groq, and only this token-heavy call can
# be moved off it. Any failure falls back to Groq, so pointing this at Gemini
# can never leave the feature broken.

GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")
MODEL_TIMEOUT = 30


def _sheets_provider() -> str:
    """Read at call time, so switching providers needs no restart."""
    from athena import settings
    return str(settings.get("sheets_provider", config.SHEETS_PROVIDER)).lower()


def _ask_gemini(system: str, user: str, max_tokens: int) -> tuple[str, str]:
    """(text, error). Plain REST over urllib - one call site doesn't justify
    another dependency. The key goes in a header, never in the URL."""
    if not config.GEMINI_API_KEY:
        return "", "no GEMINI_API_KEY in .env"
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
    }).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_URL.format(model=config.GEMINI_MODEL), data=payload,
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": config.GEMINI_API_KEY})
    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return "", f"Gemini returned {exc.code}"
    except Exception as exc:
        return "", f"{type(exc).__name__}"

    try:
        parts = body["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts), ""
    except (KeyError, IndexError, TypeError):
        return "", "Gemini sent back nothing usable"


def _ask_groq(system: str, user: str, max_tokens: int) -> tuple[str, str]:
    try:
        response = config.get_groq_client().chat.completions.create(
            model=config.BRAIN_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.1, max_tokens=max_tokens)
        return (response.choices[0].message.content or ""), ""
    except Exception as exc:
        return "", f"{type(exc).__name__}"


def _ask_model(system: str, user: str, max_tokens: int = 900) -> tuple[str, str]:
    """Ask whichever model is configured for sheet work. Gemini first if it's
    selected, Groq otherwise - and Groq as the fallback either way."""
    if _sheets_provider() == "gemini":
        text, error = _ask_gemini(system, user, max_tokens)
        if not error:
            return text, ""
        print(f"[sheets] Gemini unavailable ({error}) - falling back to Groq")
    return _ask_groq(system, user, max_tokens)


def _sheet_context() -> str:
    """What the model needs to plan against THIS tab."""
    headers = _headers()
    rows, cols = _used_extent()
    columns = ", ".join(
        f"{_index_to_col(i)}={h}" for i, h in enumerate(headers) if h) or "none"
    return (f"Spreadsheet: {_current['title']}. Tab: {_current['tab']}. "
            f"Used range: {rows} rows by {cols} columns. "
            f"Header row: {columns}.")


def _strip_fences(text: str) -> str:
    """Models wrap JSON in fences however firmly you ask them not to."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _generate_plan(request_text: str) -> tuple:
    """Ask the model for a step list. (steps, error_sentence)."""
    raw, error = _ask_model(PLAN_SYSTEM, (
        f"{_sheet_context()}\n\nThe user asked: {request_text}\n\n"
        "Give the JSON array of steps now."), 700)
    if error:
        print(f"[sheets] couldn't plan that: {error}")
        return None, "I couldn't work out how to do that just now."
    try:
        parsed = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        print(f"[sheets] plan wasn't JSON: {raw[:200]!r}")
        return None, ("I couldn't turn that into a plan I trust, so I've "
                      "done nothing.")
    if isinstance(parsed, dict):
        parsed = parsed.get("steps", parsed)
        if isinstance(parsed, dict):
            parsed = [parsed]
    if not isinstance(parsed, list):
        return None, ("I couldn't turn that into a plan I trust, so I've "
                      "done nothing.")
    return parsed, ""


def _foreign_reference(steps: list) -> str | None:
    """Anything naming another spreadsheet. A structured step list has no
    place to put one, so its presence means the model went off-piste."""
    blob = json.dumps(steps).lower()
    for marker in ("spreadsheetid", "docs.google.com/spreadsheets", "/d/"):
        if marker in blob:
            return ("That plan pointed at a different spreadsheet, so I've "
                    "refused it.")
    return None


def _build_from_steps(steps: list):
    """Structured steps -> a builder. Raises PlanError with a speakable
    sentence for anything unrecognised, so a bad step is caught by our code
    rather than by Google."""
    from athena.sheet_builder import PlanError, SheetRequestBuilder

    if not isinstance(steps, list) or not steps:
        raise PlanError("I couldn't see a way to do that, so I've changed "
                        "nothing.")
    if len(steps) > MAX_PLAN_STEPS:
        raise PlanError(f"That would take {len(steps)} separate steps, which "
                        "is more than I'll do in one go.")

    builder = SheetRequestBuilder()
    for step in steps:
        if not isinstance(step, dict):
            raise PlanError("That plan was malformed, so I've not run it.")
        name = str(step.get("step", "")).strip()
        if name not in PLAN_STEPS:
            raise PlanError(f"I came up with a step called "
                            f"{name or 'nothing'}, which isn't something I "
                            "know how to do.")
        method_name, required, optional = PLAN_STEPS[name]
        missing = [k for k in required if step.get(k) in (None, "")]
        if missing:
            raise PlanError(f"The {name} step was missing "
                            f"{' and '.join(missing)}, so I've not run it.")

        if name == "delete_rows_where":
            match = str(step.get("match", "")).strip()
            lowered = match.lower()
            if "colour" in lowered or "color" in lowered:
                predicate = {"kind": "background colour",
                             "colour": step.get("colour")}
            else:
                predicate = {"kind": match, "column": step.get("column"),
                             "value": step.get("value", "")}
            builder.delete_rows_where(predicate)
            continue

        kwargs = {k: step[k] for k in tuple(required) + tuple(optional)
                  if step.get(k) not in (None, "")}
        # The builder's parameter name differs from the spoken step name in
        # one place; map it rather than renaming the public API.
        if name == "colour":
            kwargs["a1_range"] = kwargs.pop("range")
        getattr(builder, method_name)(**kwargs)
    return builder


def apply_sheet_operation(natural_language_request: str) -> str:
    """The escape hatch: a multi-stage change, planned, described, approved,
    then run all at once or not at all."""
    from athena.sheet_builder import PlanError, execute

    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    if not (natural_language_request or "").strip():
        return "Tell me what you'd like me to do to the sheet."

    steps, error = _generate_plan(natural_language_request)
    if error:
        return error
    foreign = _foreign_reference(steps)
    if foreign:
        return foreign

    try:
        builder = _build_from_steps(steps)
        builder.resolve()          # look at the sheet, turn predicates to rows
        plan = builder.build()     # validate everything, then freeze it
    except PlanError as exc:
        return str(exc)            # already written to be spoken
    except Exception as exc:
        print(f"[sheets] planning failed: {exc!r}")
        return ("I couldn't turn that into a plan I trust, so I've done "
                "nothing.")

    if plan.is_empty():
        return "That worked out to no changes at all, so I've left it alone."

    print(f"[sheets] plan: {json.dumps(steps)[:300]}")

    if _confirm is None:
        return (f"{plan.description} But I can't ask you to confirm that "
                "right now, so I haven't done it.")
    try:
        approved = bool(_confirm(f"{plan.description} Shall I go ahead?"))
    except Exception as exc:
        print(f"[sheets] confirm failed: {exc!r}")
        approved = False
    if not approved:
        return "Right, I've left the sheet alone."

    return execute(plan)
