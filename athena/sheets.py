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


def _used_extent() -> tuple[int, int]:
    """(rows, columns) actually in use on the current tab."""
    rows = _values(_current["tab"]) or []
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

def sort_range(a1_range: str, column, order: str = "asc") -> str:
    """Sort a range by one column. The range is A1 notation and is sorted
    exactly as given - include the header row only if you want it sorted."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    try:
        grid = _bounded(_a1_to_grid(a1_range))
    except ValueError:
        return f"I couldn't make sense of the range {a1_range}."

    index, name = _resolve_column(column)
    if index is None:
        return f"I couldn't work out which column {column} is."

    descending = str(order or "").lower().startswith(("desc", "z", "high", "big"))
    request = {"sortRange": {
        "range": grid,
        "sortSpecs": [{"dimensionIndex": index,
                       "sortOrder": "DESCENDING" if descending else "ASCENDING"}],
    }}
    direction = "descending" if descending else "ascending"
    return _mutate(f"the sort of {a1_range}", _snapshot(a1_range), [request],
                   f"Sorted {a1_range} by {name}, {direction}.")


def color_range(a1_range: str, color_name: str) -> str:
    """Fill a range with a named background colour."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    color = _resolve_color(color_name)
    if color is None:
        return (f"I don't know the colour {color_name}. I can do "
                f"{_color_names()}.")
    try:
        grid = _bounded(_a1_to_grid(a1_range))
    except ValueError:
        return f"I couldn't make sense of the range {a1_range}."

    request = {"repeatCell": {
        "range": grid,
        "cell": {"userEnteredFormat": {"backgroundColor": color}},
        "fields": "userEnteredFormat.backgroundColor",
    }}
    return _mutate(f"the colouring of {a1_range}", _snapshot(a1_range),
                   [request], f"Coloured {a1_range} {color_name}.")


def highlight_rows_where(column, condition: str, value: str,
                         color_name: str) -> str:
    """Colour every row whose column matches a condition."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    color = _resolve_color(color_name)
    if color is None:
        return (f"I don't know the colour {color_name}. I can do "
                f"{_color_names()}.")
    key = _resolve_condition(condition)
    if key is None:
        return f"I don't know how to test for {condition}."
    index, name = _resolve_column(column)
    if index is None:
        return f"I couldn't work out which column {column} is."

    matches = _matching_rows(index, key, value)
    if matches is None:
        return "I couldn't read the sheet to find matching rows."
    if not matches:
        return f"No rows where {name} {key} {value}, so I've left it alone."

    rows, cols = _used_extent()
    cols = max(cols, 1)
    a1 = f"A1:{_index_to_col(cols - 1)}{max(rows, 1)}"
    requests = [{"repeatCell": {
        "range": {"sheetId": _current["sheet_id"],
                  "startRowIndex": r, "endRowIndex": r + 1,
                  "startColumnIndex": 0, "endColumnIndex": cols},
        "cell": {"userEnteredFormat": {"backgroundColor": color}},
        "fields": "userEnteredFormat.backgroundColor",
    }} for r in matches]

    count = len(matches)
    return _mutate("that highlighting", _snapshot(a1), requests,
                   f"Highlighted {count} row{'s' if count != 1 else ''} "
                   f"where {name} {key} {value}, in {color_name}.")


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
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    key = _resolve_condition(condition)
    if key is None:
        return f"I don't know how to filter by {condition}."
    index, name = _resolve_column(column)
    if index is None:
        return f"I couldn't work out which column {column} is."

    condition_body = {"type": CONDITIONS[key][0]}
    if key not in VALUELESS_CONDITIONS:
        condition_body["values"] = [{"userEnteredValue": str(value)}]

    rows, cols = _used_extent()
    request = {"setBasicFilter": {"filter": {
        "range": {"sheetId": _current["sheet_id"],
                  "startRowIndex": 0, "endRowIndex": max(rows, 1),
                  "startColumnIndex": 0, "endColumnIndex": max(cols, 1)},
        "filterSpecs": [{"columnIndex": index,
                         "filterCriteria": {"condition": condition_body}}],
    }}}
    tail = "" if key in VALUELESS_CONDITIONS else f" {value}"
    return _mutate("that filter", _filter_undo(), [request],
                   f"Filtered to rows where {name} {key}{tail}.")


def clear_filter() -> str:
    """Remove the tab's basic filter, showing every row again."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    if _current_filter() is None:
        return "There's no filter on this tab to clear."
    request = {"clearBasicFilter": {"sheetId": _current["sheet_id"]}}
    return _mutate("clearing the filter", _filter_undo(), [request],
                   "Cleared the filter, so every row is showing again.")


def add_formula(cell: str, formula: str) -> str:
    """Put a formula in one cell. `cell` is A1 notation like "E2"; the
    formula is entered as if typed, so "SUM(A1:A5)" and "=SUM(A1:A5)" both
    work."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    if not (formula or "").strip():
        return "Tell me what the formula should be."

    text = formula.strip()
    if not text.startswith("="):
        text = "=" + text
    try:
        grid = _bounded(_a1_to_grid(cell))
    except ValueError:
        return f"I couldn't make sense of the cell {cell}."

    # formulaValue is the batchUpdate equivalent of typing it in, which is
    # what USER_ENTERED means for the values API.
    request = {"updateCells": {
        "rows": [{"values": [{"userEnteredValue": {"formulaValue": text}}]}],
        "fields": "userEnteredValue",
        "range": grid,
    }}
    return _mutate(f"the formula in {cell}", _snapshot(cell), [request],
                   f"Put {text} in {cell}.")


def insert_rows(at_index: int, count: int = 1) -> str:
    """Insert blank rows BEFORE row `at_index`. 1-based, as spoken."""
    return _insert("ROWS", at_index, count)


def insert_columns(at_index: int, count: int = 1) -> str:
    """Insert blank columns BEFORE column `at_index`. 1-based, as spoken."""
    return _insert("COLUMNS", at_index, count)


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
    return _delete("ROWS", start, end)


def delete_columns(start: int, end: int = 0) -> str:
    """Delete columns `start` to `end` inclusive, 1-based, as spoken."""
    return _delete("COLUMNS", start, end)


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
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    try:
        from_start, from_end, to_index = (int(from_start), int(from_end),
                                          int(to_index))
        source = _dim_range(from_start, from_end, "ROWS")
    except (TypeError, ValueError):
        return f"I couldn't work out which rows to move."
    if to_index < 1:
        return "I need a row number to move them to."

    start0, end0, dest0 = source["startIndex"], source["endIndex"], to_index - 1
    if start0 <= dest0 < end0:
        return "Those rows are already there, so I've left them alone."

    landed_start, landed_end, back = _move_inverse(start0, end0, dest0)
    undo = _inverse_entry(f"rows {from_start} to {from_end}", [{"moveDimension": {
        "source": {"sheetId": _current["sheet_id"], "dimension": "ROWS",
                   "startIndex": landed_start, "endIndex": landed_end},
        "destinationIndex": back,
    }}])
    request = {"moveDimension": {"source": source, "destinationIndex": dest0}}
    count = from_end - from_start + 1
    return _mutate(f"moving {count} row{'s' if count != 1 else ''}", undo,
                   [request],
                   f"Moved {count} row{'s' if count != 1 else ''} to row {to_index}.")


def freeze_header(rows: int = 1) -> str:
    """Freeze the top `rows` rows so they stay put when scrolling."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    try:
        rows = max(0, int(rows))
    except (TypeError, ValueError):
        return f"I couldn't work out how many rows to freeze."

    was = _tab_props().get("gridProperties", {}).get("frozenRowCount", 0)
    undo = _inverse_entry(_current["tab"], [{"updateSheetProperties": {
        "properties": {"sheetId": _current["sheet_id"],
                       "gridProperties": {"frozenRowCount": was}},
        "fields": "gridProperties.frozenRowCount",
    }}])
    request = {"updateSheetProperties": {
        "properties": {"sheetId": _current["sheet_id"],
                       "gridProperties": {"frozenRowCount": rows}},
        "fields": "gridProperties.frozenRowCount",
    }}
    said = ("Unfroze the top rows." if rows == 0 else
            f"Froze the top {rows} row{'s' if rows != 1 else ''}.")
    return _mutate("that freeze", undo, [request], said)


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
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint

    _, cols = _used_extent()
    cols = max(cols, 1)
    widths = _column_widths(cols)
    if widths is None:
        return ("I couldn't read the current column widths, so I've not "
                "resized anything - I'd have had no way to put them back.")

    undo_requests = [{"updateDimensionProperties": {
        "range": {"sheetId": _current["sheet_id"], "dimension": "COLUMNS",
                  "startIndex": i, "endIndex": i + 1},
        "properties": {"pixelSize": width},
        "fields": "pixelSize",
    }} for i, width in enumerate(widths) if width]
    undo = _inverse_entry(_current["tab"], undo_requests)

    request = {"autoResizeDimensions": {"dimensions": {
        "sheetId": _current["sheet_id"], "dimension": "COLUMNS",
        "startIndex": 0, "endIndex": cols,
    }}}
    return _mutate("that resize", undo, [request],
                   f"Resized {cols} column{'s' if cols != 1 else ''} to fit.")


# --------------------------------------------------------------------------
# the escape hatch
#
# For the unusual request no named operation covers. The model writes the
# batchUpdate body; nothing about that is trusted. It is checked against the
# list of real request types, checked for any reference to another
# spreadsheet, and then DESCRIBED FROM THE JSON ITSELF - not from what the
# model said it was doing - so the sentence the user approves is derived from
# what will actually run.
# --------------------------------------------------------------------------

# Every batchUpdate request type. Anything not here is refused outright: an
# unknown key means the model invented something, and inventions don't run.
KNOWN_REQUEST_TYPES = {
    "addBanding", "addChart", "addConditionalFormatRule", "addDimensionGroup",
    "addFilterView", "addNamedRange", "addProtectedRange", "addSheet",
    "addSlicer", "appendCells", "appendDimension", "autoFill",
    "autoResizeDimensions", "clearBasicFilter", "copyPaste", "createDeveloperMetadata",
    "cutPaste", "deleteBanding", "deleteConditionalFormatRule",
    "deleteDeveloperMetadata", "deleteDimension", "deleteDimensionGroup",
    "deleteDuplicates", "deleteEmbeddedObject", "deleteFilterView",
    "deleteNamedRange", "deleteProtectedRange", "deleteRange", "deleteSheet",
    "duplicateFilterView", "duplicateSheet", "findReplace", "insertDimension",
    "insertRange", "mergeCells", "moveDimension", "pasteData",
    "randomizeRange", "repeatCell", "setBasicFilter", "setDataValidation",
    "sortRange", "textToColumns", "trimWhitespace", "unmergeCells",
    "updateBanding", "updateBorders", "updateCells", "updateChartSpec",
    "updateConditionalFormatRule", "updateDeveloperMetadata",
    "updateDimensionGroup", "updateDimensionProperties", "updateEmbeddedObjectPosition",
    "updateFilterView", "updateNamedRange", "updateProtectedRange",
    "updateSheetProperties", "updateSlicerSpec", "updateSpreadsheetProperties",
}

# Types that change the SHAPE of the sheet. A cell snapshot can't put these
# back, so the confirmation says so out loud rather than implying a clean undo.
STRUCTURAL_TYPES = {
    "addSheet", "deleteSheet", "duplicateSheet", "deleteDimension",
    "insertDimension", "appendDimension", "moveDimension", "insertRange",
    "deleteRange", "cutPaste", "mergeCells", "unmergeCells", "textToColumns",
    "deleteDuplicates", "randomizeRange", "autoFill", "appendCells",
}

# Any key that could point the operation at a different file.
_FOREIGN_KEYS = {"spreadsheetid", "destinationspreadsheetid"}

MAX_GENERATED_REQUESTS = 10

GENERATE_SYSTEM = (
    "You write Google Sheets API batchUpdate requests. Reply with ONLY a "
    "JSON array of Request objects - no prose, no markdown fences, no "
    "explanation. Each element must be an object with exactly one key naming "
    "a real batchUpdate request type, for example repeatCell, sortRange, "
    "updateCells, setBasicFilter, deleteDimension. Use only the sheetId you "
    "are given. Never include a spreadsheetId. GridRange indexes are "
    "ZERO-BASED with the end index EXCLUSIVE. Keep it to the fewest requests "
    "that do the job. If the request cannot be done with batchUpdate, reply "
    "with exactly []."
)


def _sheet_context() -> str:
    """What the model needs to write a correct request for THIS tab."""
    headers = _headers()
    rows, cols = _used_extent()
    columns = ", ".join(
        f"{_index_to_col(i)}={h}" for i, h in enumerate(headers) if h) or "none"
    return (f"Spreadsheet: {_current['title']}. Tab: {_current['tab']}, "
            f"sheetId {_current['sheet_id']}. Used range: {rows} rows by "
            f"{cols} columns. Header row: {columns}.")


def _strip_fences(text: str) -> str:
    """Models wrap JSON in ```json fences however firmly you ask them not to."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


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


def _generate_requests(request_text: str) -> tuple[list | None, str]:
    """Ask the model for a batchUpdate body. (requests, error_sentence)."""
    raw, error = _ask_model(GENERATE_SYSTEM, (
        f"{_sheet_context()}\n\nThe user asked: {request_text}\n\n"
        "Give the JSON array of requests now."))
    if error:
        print(f"[sheets] couldn't generate the operation: {error}")
        return None, "I couldn't work out how to do that just now."
    raw = _strip_fences(raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[sheets] generated body wasn't JSON: {raw[:200]!r}")
        return None, "I couldn't turn that into a change I trust, so I've done nothing."

    if isinstance(parsed, dict):
        parsed = parsed.get("requests", parsed)
        if isinstance(parsed, dict):
            parsed = [parsed]
    if not isinstance(parsed, list):
        return None, "I couldn't turn that into a change I trust, so I've done nothing."
    return parsed, ""


def _walk(node):
    """Every (key, value) pair anywhere in a nested structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _validate_requests(requests: list) -> tuple[list, str]:
    """Check a generated body before it can run. (requests, error_sentence)
    - an error means nothing should execute."""
    if not isinstance(requests, list) or not requests:
        return [], "I couldn't see a way to do that, so I've changed nothing."
    if len(requests) > MAX_GENERATED_REQUESTS:
        return [], (f"That would take {len(requests)} separate changes, which "
                    "is more than I'll do in one go.")

    known_tab_ids = {props.get("sheetId") for props in _tabs()}

    for request in requests:
        if not isinstance(request, dict) or len(request) != 1:
            return [], "The change I came up with was malformed, so I've not run it."
        name = next(iter(request))
        if name not in KNOWN_REQUEST_TYPES:
            return [], (f"I came up with a {name} step, which isn't a real "
                        "spreadsheet operation, so I've not run it.")
        if not isinstance(request[name], dict):
            return [], "The change I came up with was malformed, so I've not run it."

        for key, value in _walk(request):
            lowered = str(key).lower()
            # Nothing may point at another file.
            if lowered in _FOREIGN_KEYS:
                if str(value) != str(_current["id"]):
                    return [], ("That change pointed at a different "
                                "spreadsheet, so I've refused it.")
            # Nor at a tab that isn't in this spreadsheet.
            if lowered == "sheetid" and isinstance(value, int):
                if known_tab_ids and value not in known_tab_ids:
                    return [], ("That change pointed at a tab that isn't in "
                                "this spreadsheet, so I've refused it.")
    return requests, ""


def _grid_phrase(grid) -> str:
    """A GridRange as something sayable."""
    if not isinstance(grid, dict):
        return "part of the sheet"
    a1 = _grid_to_a1(grid)
    if not a1:
        return "the whole tab"
    return a1.replace(":", " to ")


def _dimension_phrase(dim_range) -> str:
    if not isinstance(dim_range, dict):
        return "some rows"
    word = "column" if str(dim_range.get("dimension", "")).upper() == "COLUMNS" else "row"
    start = dim_range.get("startIndex")
    end = dim_range.get("endIndex")
    if start is None or end is None:
        return f"{word}s"
    count = end - start
    if count == 1:
        return f"{word} {start + 1}"
    return f"{word}s {start + 1} to {end}"


def _describe_request(request: dict) -> str:
    """Say what ONE request will do, read out of the request itself."""
    name = next(iter(request))
    body = request[name] if isinstance(request[name], dict) else {}

    if name == "repeatCell":
        fields = str(body.get("fields", ""))
        what = ("colour" if "backgroundColor" in fields
                else "formatting" if "Format" in fields else "contents")
        return f"change the {what} of {_grid_phrase(body.get('range'))}"
    if name == "updateCells":
        return f"overwrite {_grid_phrase(body.get('range'))}"
    if name == "sortRange":
        return f"sort {_grid_phrase(body.get('range'))}"
    if name == "deleteDimension":
        return f"delete {_dimension_phrase(body.get('range'))}"
    if name == "insertDimension":
        return f"insert {_dimension_phrase(body.get('range'))}"
    if name == "appendDimension":
        count = body.get("length", "some")
        word = "columns" if str(body.get("dimension", "")).upper() == "COLUMNS" else "rows"
        return f"add {count} more {word} at the end"
    if name == "moveDimension":
        return (f"move {_dimension_phrase(body.get('source'))} to position "
                f"{body.get('destinationIndex', 0) + 1}")
    if name == "setBasicFilter":
        return "set a filter on the tab"
    if name == "clearBasicFilter":
        return "remove the filter"
    if name == "mergeCells":
        return f"merge {_grid_phrase(body.get('range'))}"
    if name == "unmergeCells":
        return f"unmerge {_grid_phrase(body.get('range'))}"
    if name == "updateBorders":
        return f"change the borders of {_grid_phrase(body.get('range'))}"
    if name == "addConditionalFormatRule":
        return "add a conditional formatting rule"
    if name == "deleteConditionalFormatRule":
        return "remove a conditional formatting rule"
    if name == "setDataValidation":
        return f"set data validation on {_grid_phrase(body.get('range'))}"
    if name == "autoResizeDimensions":
        return "resize columns to fit their contents"
    if name == "updateDimensionProperties":
        return f"resize {_dimension_phrase(body.get('range'))}"
    if name == "updateSheetProperties":
        return "change the tab's settings"
    if name == "addSheet":
        title = body.get("properties", {}).get("title", "a new tab")
        return f"add a tab called {title}"
    if name == "deleteSheet":
        return "DELETE A WHOLE TAB"
    if name == "duplicateSheet":
        return "duplicate the tab"
    if name == "findReplace":
        return (f"replace {body.get('find', 'something')} with "
                f"{body.get('replacement', 'something else')}")
    if name == "deleteDuplicates":
        return f"delete duplicate rows in {_grid_phrase(body.get('range'))}"
    if name == "trimWhitespace":
        return f"trim spaces in {_grid_phrase(body.get('range'))}"
    if name == "textToColumns":
        return "split text into columns"
    if name == "cutPaste":
        return f"cut and paste into {_grid_phrase(body.get('destination'))}"
    if name == "copyPaste":
        return f"copy into {_grid_phrase(body.get('destination'))}"

    # Anything else: say its name in plain words rather than pretend to know.
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower()
    return f"run a {spaced} step"


def _plan_parts(requests: list) -> tuple[str, str]:
    """(what it will do, any warning). Kept separate so the confirmation and
    the after-the-fact sentence can be built from the same phrase without
    slicing strings apart."""
    parts = [_describe_request(r) for r in requests]
    action = parts[0] if len(parts) == 1 else ", then ".join(parts)
    warning = ""
    if any(next(iter(r)) in STRUCTURAL_TYPES for r in requests):
        warning = (" That changes the shape of the sheet, so I'd only be "
                   "able to put part of it back.")
    return action, warning


def _describe_requests(requests: list) -> str:
    """The plain-English plan the user is asked to approve."""
    action, warning = _plan_parts(requests)
    return f"I'd {action}, on the {_current['tab']} tab.{warning}"


def _snapshot_tab() -> dict | None:
    """Snapshot everything in use on the current tab."""
    rows, cols = _used_extent()
    a1 = f"A1:{_index_to_col(max(cols, 1) - 1)}{max(rows, 1)}"
    return _snapshot(a1)


def apply_sheet_operation(natural_language_request: str) -> str:
    """The escape hatch: do something no named operation covers.

    The model writes the batchUpdate body, which is then validated, described
    from the JSON itself, and read back for approval. Nothing runs without an
    explicit yes."""
    if _service() is None:
        return google_auth.not_connected_message()
    complaint = _need_sheet()
    if complaint:
        return complaint
    if not (natural_language_request or "").strip():
        return "Tell me what you'd like me to do to the sheet."

    requests, error = _generate_requests(natural_language_request)
    if error:
        return error
    requests, error = _validate_requests(requests)
    if error:
        return error

    plan = _describe_requests(requests)
    print(f"[sheets] generated: {json.dumps(requests)[:400]}")

    if _confirm is None:
        # No way to ask, so no way to run it. Say the plan instead of doing it.
        return (f"{plan} But I can't ask you to confirm that right now, so I "
                "haven't done it.")

    try:
        approved = bool(_confirm(f"{plan} Shall I go ahead?"))
    except Exception as exc:
        print(f"[sheets] confirm failed: {exc!r}")
        approved = False
    if not approved:
        return "Right, I've left the sheet alone."

    action, _ = _plan_parts(requests)
    snapshot = _snapshot_tab()
    return _mutate("that change", snapshot, requests,
                   f"Done - {action} on the {_current['tab']} tab.")
