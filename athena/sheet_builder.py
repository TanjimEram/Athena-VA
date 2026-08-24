"""Assembling a multi-stage spreadsheet change before any of it is sent.

"Delete all the rows that are coloured orange" is not one API request. It is:
read the cell formats, work out which rows match, then delete them in
DESCENDING order so that removing row 4 doesn't shift row 7 out from under
you. The escape hatch could only ever produce a single request, so it
correctly refused - and refusing honestly is right. This is the capability
that was missing.

The Builder pattern, in its textbook parts:

    SheetRequestBuilder   the ConcreteBuilder. Steps are chained onto it and
                          it accumulates state. It performs NO API calls
                          while chaining - that is the whole point of
                          separating construction from execution.
    SheetPlan             the Product. Immutable once built, and it carries
                          BOTH the API requests and a plain-English
                          description of them. One construction process,
                          two representations - which is what Builder is
                          actually for.
    PlanDirector          the Director. Canned sequences for the orders that
                          come up again and again.
    PlanError             a refusal, worded so it can be spoken aloud
                          unchanged.

A1 notation and GridRange are different systems and the API mixes them. The
conversion lives in sheets._a1_to_grid and this module DELEGATES to it
rather than growing a second copy - one implementation in the codebase, not
two. It is called with an explicit sheet_id and a bare range (tab prefixes
stripped here first), because that function will otherwise look a tab name
up over the network, and nothing here is allowed to touch the network."""

import colorsys
from dataclasses import dataclass, field, replace
from typing import Any

from athena import sheets


class PlanError(Exception):
    """A plan that must not run. The message is spoken to the user verbatim,
    so it is written as a sentence and not as a developer diagnostic."""


@dataclass(frozen=True)
class SheetFacts:
    """What the builder knows about the sheet without asking the network.

    Supplied by hand in tests and filled in by resolve() from the live sheet
    later. When it is absent the builder still validates everything that
    doesn't need the sheet, and says so rather than pretending otherwise."""
    tab: str = ""
    sheet_id: int = 0
    headers: tuple = ()
    row_count: int = 0
    column_count: int = 0
    tabs: tuple = ()          # ((name, sheet_id), ...)

    def column_index(self, column) -> int | None:
        """A header name, a column letter, or a spoken number -> 0-based
        index. Header names win over letters: someone saying "sort by C"
        almost certainly means a column headed C when one exists."""
        text = str(column or "").strip()
        if not text:
            return None
        for index, header in enumerate(self.headers):
            if header.strip().lower() == text.lower():
                return index
        if text.isdigit():
            number = int(text)
            return number - 1 if number >= 1 else None
        try:
            return sheets._col_to_index(text)
        except ValueError:
            return None

    def column_name(self, index: int) -> str:
        if 0 <= index < len(self.headers) and self.headers[index]:
            return self.headers[index]
        return f"column {sheets._index_to_col(index)}"

    def sheet_id_for(self, tab: str) -> int | None:
        for name, ident in self.tabs:
            if name.strip().lower() == (tab or "").strip().lower():
                return ident
        if tab and self.tab and tab.strip().lower() == self.tab.strip().lower():
            return self.sheet_id
        return None


@dataclass(frozen=True)
class SheetPlan:
    """The Product: what will be done, said in English, and where it lands.
    Frozen, and every field a tuple, so a built plan cannot be edited into
    something other than the thing that was validated."""
    tab: str
    sheet_id: int
    requests: tuple = ()
    description: str = ""
    ranges: tuple = ()
    steps: tuple = ()
    # export_requested is separate from export_path on purpose: .export()
    # with no path is a real request with a path we work out later, and a
    # None path alone cannot tell that apart from no export at all.
    export_requested: bool = False
    export_path: str | None = None
    warnings: tuple = ()

    def __len__(self) -> int:
        return len(self.requests) + (1 if self.export_requested else 0)

    def is_empty(self) -> bool:
        return not self.requests and not self.export_requested


@dataclass
class _Step:
    """One accumulated instruction. Kept as intent, not as a request, until
    build() - so a step whose row indices aren't known yet can sit here
    waiting for resolve() instead of forcing a guess."""
    kind: str
    args: dict = field(default_factory=dict)
    says: str = ""
    needs_resolution: bool = False
    resolved_rows: tuple | None = None      # 0-based sheet row indices


# Predicates .delete_rows_where and .highlight_where understand. Value
# predicates reuse the condition table sheets.py already speaks; the colour
# one is separate because it inspects formatting rather than values.
VALUE_PREDICATES = ("equals", "contains", "greater than", "less than", "empty")
COLOUR_PREDICATE = "background colour"

# Properties people ask to match on that we cannot. Each gets its own
# sentence, because "I couldn't see a way to do that" tells nobody anything.
UNSUPPORTED_PREDICATES = {
    "font colour": "I can't match rows by font colour, only by background.",
    "text colour": "I can't match rows by text colour, only by background.",
    "bold": "I can't match rows by whether the text is bold.",
    "italic": "I can't match rows by whether the text is italic.",
    "font": "I can't match rows by which font they use.",
    "border": "I can't match rows by their borders.",
    "note": "I can't match rows by the notes attached to cells.",
    "comment": "I can't match rows by the comments on them.",
    "formula": "I can only match on what a cell shows, not the formula behind it.",
    "conditional formatting": ("I can't tell a conditional-formatting colour "
                               "from one someone applied by hand."),
    "merged": "I can't match rows by whether their cells are merged.",
    "hidden": "I can't match rows by whether they're hidden.",
}

# --------------------------------------------------------------------------
# Colour matching.
#
# A user's "orange" is whatever they clicked in Google's palette, which is
# not our orange. Matching has to tolerate that without confusing orange
# with red.
#
# Plain RGB distance cannot do it. Measured against our own palette, orange
# sits 0.103 from red and 0.112 from yellow - so any threshold loose enough
# to accept a user's shade is also loose enough to confuse the three. Tested
# on five real Google palette swatches, nearest-RGB got two wrong: #FF9900
# came out "yellow", and light yellow came out "orange".
#
# Hue got all five right, so that is what we match on:
#   * a cell with saturation below ACHROMATIC_SAT has no meaningful hue. It
#     counts as grey - unless it is nearly white, which is just an uncoloured
#     cell and must never match anything.
#   * otherwise, take the NEAREST palette hue rather than testing a fixed
#     tolerance: our hues are unevenly spaced (orange and yellow are 21
#     degrees apart, red and orange 30) so one threshold cannot suit all.
#   * reject anything further than MAX_HUE_DISTANCE from every palette hue,
#     so a purple cell doesn't get called blue.
# --------------------------------------------------------------------------
# Both numbers were set by testing against real Google palette swatches, not
# picked by eye. Google's pale tints are far less saturated than ours: their
# light green 3 measures 0.099, so a cutoff of 0.12 read it as grey. Their
# pale greens are also yellower than ours - 104 degrees against our 150 - so
# a 40 degree bound rejected it outright.
ACHROMATIC_SAT = 0.06       # below this there is no hue worth reading
NEAR_WHITE_VAL = 0.95       # at or above this an uncoloured cell, not grey
MIN_GREY_VAL = 0.25         # below this it is near-black, not a grey anyone
                            # means; 0.45 was rejecting a plain #666666
MAX_HUE_DISTANCE = 55.0     # degrees; beyond this it is not one of ours
                            # (purple lands 65 from blue, so still rejected)


def _hsv(rgb: dict) -> tuple:
    return colorsys.rgb_to_hsv(float(rgb.get("red", 0.0)),
                               float(rgb.get("green", 0.0)),
                               float(rgb.get("blue", 0.0)))


def _hue_gap(a: float, b: float) -> float:
    """Degrees between two hues, the short way round the circle."""
    gap = abs(a - b) % 360.0
    return min(gap, 360.0 - gap)


def match_colour_name(rgb: dict | None) -> str | None:
    """Which of our named colours this cell is, or None for uncoloured.

    Exposed rather than private because it is the piece most likely to need
    tuning against a real sheet, and it is worth being able to test on its
    own."""
    if not rgb:
        return None                      # unset background: never a match
    hue, sat, val = _hsv(rgb)
    if sat < ACHROMATIC_SAT:
        if val >= NEAR_WHITE_VAL or val < MIN_GREY_VAL:
            return None                  # white/unset, or near-black
        return "grey" if "grey" in sheets.COLORS else None

    degrees = hue * 360.0
    best, best_gap = None, None
    for name, palette in sheets.COLORS.items():
        p_hue, p_sat, _ = _hsv(palette)
        if p_sat < ACHROMATIC_SAT:
            continue                     # grey has no hue to compare against
        gap = _hue_gap(degrees, p_hue * 360.0)
        if best_gap is None or gap < best_gap:
            best, best_gap = name, gap
    if best is None or best_gap > MAX_HUE_DISTANCE:
        return None
    return best


class LiveSheetReader:
    """Reads the open sheet through sheets.py. The one thing in this module
    that talks to Google, and it is only ever called from resolve() - never
    while steps are being chained.

    Kept behind this small surface so the builder can be resolved against a
    stub in tests without any network."""

    def facts(self) -> SheetFacts:
        current = sheets.current()
        rows, cols = sheets._used_extent()
        headers = tuple(str(h) for h in sheets._headers())
        tabs = tuple((t.get("title", ""), t.get("sheetId"))
                     for t in sheets._tabs())
        return SheetFacts(tab=current.get("tab", ""),
                          sheet_id=current.get("sheet_id") or 0,
                          headers=headers, row_count=rows,
                          column_count=cols, tabs=tabs)

    def values(self) -> list:
        """Every used cell on the tab, as text."""
        return sheets._values(sheets.current().get("tab", "")) or []

    def backgrounds(self) -> list:
        """The background colour of every used cell, as a grid matching
        values(). effectiveFormat, not userEnteredFormat: we want the colour
        actually on screen, which is what the user was looking at when they
        said "the orange ones"."""
        service = sheets._service()
        if service is None:
            raise PlanError(google_auth_message())
        tab = sheets.current().get("tab", "")
        try:
            response = service.spreadsheets().get(
                spreadsheetId=sheets.current().get("id", ""),
                ranges=[tab], includeGridData=True,
                fields=("sheets(data(rowData(values("
                        "effectiveFormat.backgroundColor))))")).execute()
        except Exception as exc:
            raise PlanError("I couldn't read the cell colours from that "
                            f"sheet: {sheets.google_auth._short(exc)}")
        tabs = response.get("sheets", [])
        data = (tabs[0].get("data", [{}])[0] if tabs else {})
        grid = []
        for row in data.get("rowData", []) or []:
            grid.append([
                (cell.get("effectiveFormat", {}) or {}).get("backgroundColor")
                for cell in (row.get("values", []) or [])])
        return grid


def google_auth_message() -> str:
    from athena import google_auth
    return google_auth.not_connected_message()


class SheetRequestBuilder:
    """Chain steps on, then call build(). Nothing reaches Google until
    execute() is handed the finished plan."""

    def __init__(self, facts: SheetFacts | None = None):
        self._facts = facts or SheetFacts()
        self._tab = self._facts.tab
        self._sheet_id = self._facts.sheet_id
        self._steps: list[_Step] = []
        self._export: tuple | None = None
        self._warnings: list[str] = []

    # ---------------------------------------------------------------- steps

    def on_sheet(self, tab: str) -> "SheetRequestBuilder":
        """Which tab everything after this applies to."""
        tab = (tab or "").strip()
        if not tab:
            return self
        resolved = self._facts.sheet_id_for(tab)
        if resolved is None and self._facts.tabs:
            known = ", ".join(name for name, _ in self._facts.tabs) or "none"
            raise PlanError(f"There's no tab called {tab}. The tabs are: {known}.")
        self._tab = tab
        if resolved is not None:
            self._sheet_id = resolved
        return self

    def sort(self, column, order: str = "asc") -> "SheetRequestBuilder":
        descending = str(order or "").lower().startswith(("desc", "z", "high"))
        self._steps.append(_Step(
            "sort", {"column": column, "descending": descending},
            says=f"sort by {self._say_column(column)}, "
                 f"{'descending' if descending else 'ascending'}"))
        return self

    def filter(self, column, condition: str, value: str = "") -> "SheetRequestBuilder":
        key = sheets._resolve_condition(condition)
        if key is None:
            raise PlanError(f"I don't know how to filter by {condition}.")
        self._steps.append(_Step(
            "filter", {"column": column, "condition": key, "value": value},
            says=f"filter to rows where {self._say_column(column)} {key} {value}".strip()))
        return self

    def colour(self, a1_range: str, colour: str) -> "SheetRequestBuilder":
        rgb = sheets._resolve_color(colour)
        if rgb is None:
            raise PlanError(f"I don't know the colour {colour}. "
                            f"I can do {', '.join(sorted(sheets.COLORS))}.")
        self._steps.append(_Step(
            "colour", {"range": a1_range, "rgb": rgb, "name": colour},
            says=f"colour {a1_range} {colour}"))
        return self

    def highlight_where(self, column, condition: str, value, colour: str) -> "SheetRequestBuilder":
        rgb = sheets._resolve_color(colour)
        if rgb is None:
            raise PlanError(f"I don't know the colour {colour}. "
                            f"I can do {', '.join(sorted(sheets.COLORS))}.")
        key = sheets._resolve_condition(condition)
        if key is None:
            raise PlanError(f"I don't know how to test for {condition}.")
        self._steps.append(_Step(
            "highlight_where",
            {"column": column, "condition": key, "value": value,
             "rgb": rgb, "name": colour},
            says=f"highlight rows where {self._say_column(column)} {key} "
                 f"{value} in {colour}",
            needs_resolution=True))
        return self

    def insert_rows(self, at: int, count: int = 1) -> "SheetRequestBuilder":
        at = self._whole(at, "row")
        # allow_zero here so the clearer sentence below is the one raised,
        # rather than the generic "0 isn't a count I can use".
        count = self._whole(count, "count", allow_zero=True)
        if count < 1:
            raise PlanError("I need at least one row to insert.")
        self._steps.append(_Step(
            "insert_rows", {"at": at, "count": count},
            says=f"insert {count} row{'s' if count != 1 else ''} at row {at}"))
        return self

    def delete_rows(self, start: int, end: int | None = None) -> "SheetRequestBuilder":
        start = self._whole(start, "row")
        end = start if end in (None, 0) else self._whole(end, "row")
        if end < start:
            raise PlanError(f"Rows {start} to {end} runs backwards, "
                            "so I've not built that.")
        self._steps.append(_Step(
            "delete_rows", {"start": start, "end": end},
            says=(f"delete row {start}" if start == end
                  else f"delete rows {start} to {end}")))
        return self

    def delete_rows_where(self, predicate: dict) -> "SheetRequestBuilder":
        """Delete every row matching a predicate. The matching rows aren't
        known until resolve() has looked, so this stays unresolved until
        then - and build() refuses a plan still carrying one."""
        self._check_predicate(predicate)
        self._steps.append(_Step(
            "delete_rows_where", {"predicate": dict(predicate)},
            says=f"delete every row where {self._say_predicate(predicate)}",
            needs_resolution=True))
        return self

    def set_formula(self, cell: str, formula: str) -> "SheetRequestBuilder":
        text = (formula or "").strip()
        if not text:
            raise PlanError("You didn't tell me what the formula should be.")
        if not text.startswith("="):
            text = "=" + text
        self._steps.append(_Step(
            "set_formula", {"cell": cell, "formula": text},
            says=f"put {text} in {cell}"))
        return self

    def freeze(self, rows: int = 1) -> "SheetRequestBuilder":
        rows = self._whole(rows, "count", allow_zero=True)
        self._steps.append(_Step(
            "freeze", {"rows": rows},
            says=("unfreeze the top rows" if rows == 0
                  else f"freeze the top {rows} row{'s' if rows != 1 else ''}")))
        return self

    def autosize(self) -> "SheetRequestBuilder":
        self._steps.append(_Step("autosize", {}, says="resize the columns to fit"))
        return self

    def export(self, path: str | None = None, fmt: str = "csv") -> "SheetRequestBuilder":
        """Write the sheet out to a local file after the changes land.

        CSV via the standard library, read through the Sheets API we already
        have permission for. Drive's export would need drive.readonly and
        another consent, and wouldn't work on a sheet Athena didn't create.

        Note this is a local file write, not a batchUpdate request, so it
        sits outside the all-or-nothing guarantee. execute() runs it LAST,
        after the mutations have succeeded, so a rolled-back plan leaves no
        file behind."""
        fmt = str(fmt or "csv").lower().lstrip(".")
        if fmt != "csv":
            raise PlanError(f"I can only export as a CSV file, not {fmt}.")
        self._export = (path, fmt)
        return self

    # -------------------------------------------------------------- resolve

    def resolve(self, reader=None) -> "SheetRequestBuilder":
        """Look at the sheet and turn predicates into concrete row numbers.

        This is the ONLY step that reads from Google, and it sits between
        chaining and build() on purpose: a predicate like "the orange ones"
        cannot become a row list until someone has looked, and build() must
        not be the thing that looks - it validates, and validation should
        not have side effects.

        Also refreshes SheetFacts, so build()'s column and row checks are
        made against the real sheet rather than whatever was passed in."""
        reader = reader or LiveSheetReader()

        try:
            self._facts = reader.facts()
        except PlanError:
            raise
        except Exception as exc:
            raise PlanError("I couldn't read that sheet, so I've not changed "
                            f"anything: {exc}")
        if not self._tab:
            self._tab = self._facts.tab
        if self._facts.sheet_id is not None:
            resolved = self._facts.sheet_id_for(self._tab)
            self._sheet_id = (resolved if resolved is not None
                              else self._facts.sheet_id)

        pending = [s for s in self._steps if s.needs_resolution]
        if not pending:
            return self

        values = reader.values() or []
        # Only pay for the colour grid if something actually asks about it.
        backgrounds = None
        if any(self._predicate_of(s).get("kind") == COLOUR_PREDICATE
               for s in pending):
            backgrounds = reader.backgrounds() or []

        for step in pending:
            rows = self._rows_matching(self._predicate_of(step), values,
                                       backgrounds)
            step.resolved_rows = tuple(sorted(rows))
            step.says = f"{step.says} ({len(rows)} row"\
                        f"{'s' if len(rows) != 1 else ''})"
        return self

    def _predicate_of(self, step: _Step) -> dict:
        """Both predicate-bearing steps, read the same way - one is written
        as a predicate dict, the other as loose column/condition/value."""
        if "predicate" in step.args:
            return step.args["predicate"]
        return {"kind": step.args.get("condition"),
                "column": step.args.get("column"),
                "value": step.args.get("value")}

    def _rows_matching(self, predicate: dict, values: list,
                       backgrounds: list | None) -> list:
        """0-based sheet row indices matching a predicate. Row 0 is the
        header and is never matched - deleting the header because it happened
        to be coloured would be a nasty surprise."""
        kind = predicate.get("kind")

        if kind == COLOUR_PREDICATE:
            wanted = str(predicate.get("colour", "")).strip().lower()
            wanted = sheets.COLOR_ALIASES.get(wanted, wanted)
            if backgrounds is None:
                raise PlanError("I couldn't read the cell colours, so I've "
                                "not changed anything.")
            matched = []
            for index, row in enumerate(backgrounds):
                if index == 0:
                    continue
                names = [match_colour_name(cell) for cell in row if cell]
                names = [n for n in names if n]
                # A row counts as coloured when the colour is what the row
                # actually looks like - the most common colour across its
                # filled cells, not merely present in one of them.
                if names and max(set(names), key=names.count) == wanted:
                    matched.append(index)
            return matched

        key = sheets._resolve_condition(kind)
        if key is None:
            message = UNSUPPORTED_PREDICATES.get(str(kind).strip().lower())
            raise PlanError(message or f"I can't match rows by {kind}.")

        index = self._facts.column_index(predicate.get("column"))
        if index is None:
            known = ", ".join(h for h in self._facts.headers if h) or "none"
            raise PlanError(f"There's no column called "
                            f"{predicate.get('column')} in this sheet. "
                            f"The columns are {known}.")

        test = sheets.CONDITIONS[key][1]
        raw = predicate.get("value", "")
        want = ("" if key in sheets.VALUELESS_CONDITIONS
                else (raw if key in sheets.NUMERIC_CONDITIONS
                      else str(raw).strip().lower()))
        matched = []
        for row_index, row in enumerate(values):
            if row_index == 0:
                continue
            cell = str(row[index]) if index < len(row) else ""
            try:
                if test(cell, want):
                    matched.append(row_index)
            except Exception:
                continue
        return matched

    # ---------------------------------------------------------------- build

    def build(self) -> SheetPlan:
        """Validate everything, then freeze it into a Product. Raises
        PlanError - whose message is the sentence Athena says - rather than
        returning a plan that would half-work."""
        if not self._steps and self._export is None:
            raise PlanError("That plan is empty, so there's nothing for me to do.")

        unresolved = [s for s in self._steps if s.needs_resolution
                      and s.resolved_rows is None]
        if unresolved:
            raise PlanError(
                f"I need to look at the sheet before I can {unresolved[0].says}. "
                "Nothing has been changed.")

        self._check_columns()
        self._check_rows()
        self._check_delete_overlap()

        requests, ranges = [], []
        # Deletions go last and in DESCENDING order: removing row 4 shifts
        # every row below it up, so a plan that deleted 4 then 7 would take
        # the wrong second row. Descending order makes each delete
        # independent of the ones after it.
        deletes, others = [], []
        for step in self._steps:
            built, touched = self._to_requests(step)
            (deletes if step.kind in ("delete_rows", "delete_rows_where")
             else others).extend(built)
            ranges.extend(touched)
        deletes.sort(key=lambda r: r["deleteDimension"]["range"]["startIndex"],
                     reverse=True)
        requests = others + deletes

        return SheetPlan(
            tab=self._tab, sheet_id=self._sheet_id,
            requests=tuple(requests),
            description=self._describe(),
            ranges=tuple(dict.fromkeys(ranges)),
            steps=tuple(s.says for s in self._steps),
            export_requested=self._export is not None,
            export_path=(self._export[0] if self._export else None),
            warnings=tuple(self._warnings),
        )

    # ------------------------------------------------------------ internals

    def _whole(self, value, what: str, allow_zero: bool = False) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise PlanError(f"{value} isn't a {what} number I can use.")
        floor = 0 if allow_zero else 1
        if number < floor:
            raise PlanError(f"{what.capitalize()} {number} isn't a "
                            f"{what} I can use.")
        return number

    def _say_column(self, column) -> str:
        index = self._facts.column_index(column)
        if index is None:
            return str(column)
        return self._facts.column_name(index)

    def _say_predicate(self, predicate: dict) -> str:
        kind = predicate.get("kind")
        if kind == COLOUR_PREDICATE:
            return f"the background is {predicate.get('colour')}"
        return (f"{self._say_column(predicate.get('column'))} "
                f"{kind} {predicate.get('value', '')}").strip()

    def _check_predicate(self, predicate) -> None:
        if not isinstance(predicate, dict) or not predicate.get("kind"):
            raise PlanError("I couldn't work out what to match those rows on.")
        kind = predicate["kind"]
        if kind == COLOUR_PREDICATE:
            if sheets._resolve_color(predicate.get("colour")) is None:
                raise PlanError(
                    f"I don't know the colour {predicate.get('colour')}. "
                    f"I can do {', '.join(sorted(sheets.COLORS))}.")
            return
        if sheets._resolve_condition(kind) is None:
            # Say WHY, specifically, when we know why. Refused at chaining
            # time rather than at resolve, so nothing is read from the sheet
            # for a plan that was never going to run.
            raise PlanError(UNSUPPORTED_PREDICATES.get(str(kind).strip().lower())
                            or f"I can't match rows by {kind}.")
        if not predicate.get("column"):
            raise PlanError("Tell me which column to match on.")

    def _check_columns(self) -> None:
        if not self._facts.headers:
            self._warnings.append("columns not checked - sheet not read yet")
            return
        for step in self._steps:
            for key in ("column",):
                if key not in step.args:
                    continue
                if self._facts.column_index(step.args[key]) is None:
                    known = ", ".join(h for h in self._facts.headers if h)
                    raise PlanError(
                        f"There's no column called {step.args[key]} in this "
                        f"sheet. The columns are {known}.")
            predicate = step.args.get("predicate")
            if isinstance(predicate, dict) and predicate.get("column"):
                if self._facts.column_index(predicate["column"]) is None:
                    known = ", ".join(h for h in self._facts.headers if h)
                    raise PlanError(
                        f"There's no column called {predicate['column']} in "
                        f"this sheet. The columns are {known}.")

    def _check_rows(self) -> None:
        if not self._facts.row_count:
            self._warnings.append("row bounds not checked - sheet not read yet")
            return
        limit = self._facts.row_count
        for step in self._steps:
            for key in ("at", "start", "end"):
                if key in step.args and step.args[key] > limit:
                    raise PlanError(
                        f"Row {step.args[key]} is past the end of the sheet, "
                        f"which stops at row {limit}.")

    def _check_delete_overlap(self) -> None:
        spans = []
        for step in self._steps:
            if step.kind == "delete_rows":
                spans.append((step.args["start"], step.args["end"]))
            elif step.kind == "delete_rows_where" and step.resolved_rows:
                spans.extend((r + 1, r + 1) for r in step.resolved_rows)
        spans.sort()
        for (a_start, a_end), (b_start, b_end) in zip(spans, spans[1:]):
            if b_start <= a_end:
                raise PlanError(
                    f"I can't do that - it would delete rows {b_start} to "
                    f"{min(a_end, b_end)} twice over.")

    def _grid(self, a1: str) -> dict:
        """A1 -> GridRange, delegated to sheets._a1_to_grid so there is one
        conversion in the codebase. The tab prefix is stripped HERE first:
        that function looks an unfamiliar tab name up over the network, and
        chaining is not allowed to touch the network."""
        text = (a1 or "").strip()
        if "!" in text:
            text = text.split("!", 1)[1]
        try:
            return sheets._a1_to_grid(text, sheet_id=self._sheet_id)
        except ValueError:
            raise PlanError(f"I couldn't make sense of the range {a1}.")

    def _rows_grid(self, start: int, end: int) -> dict:
        try:
            return sheets._dim_range(start, end, "ROWS", sheet_id=self._sheet_id)
        except ValueError:
            raise PlanError(f"Rows {start} to {end} aren't a range I can use.")

    def _to_requests(self, step: _Step) -> tuple[list, list]:
        """One step -> its API requests and the A1 ranges it touches."""
        kind, args = step.kind, step.args
        sid = self._sheet_id

        if kind == "sort":
            index = self._facts.column_index(args["column"]) or 0
            width = max(self._facts.column_count, index + 1, 1)
            rows = max(self._facts.row_count, 1)
            a1 = f"A1:{sheets._index_to_col(width - 1)}{rows}"
            return ([{"sortRange": {
                "range": self._grid(a1),
                "sortSpecs": [{"dimensionIndex": index,
                               "sortOrder": "DESCENDING" if args["descending"]
                                            else "ASCENDING"}]}}], [a1])

        if kind == "filter":
            index = self._facts.column_index(args["column"]) or 0
            body = {"type": sheets.CONDITIONS[args["condition"]][0]}
            if args["condition"] not in sheets.VALUELESS_CONDITIONS:
                body["values"] = [{"userEnteredValue": str(args["value"])}]
            return ([{"setBasicFilter": {"filter": {
                "range": {"sheetId": sid, "startRowIndex": 0,
                          "endRowIndex": max(self._facts.row_count, 1),
                          "startColumnIndex": 0,
                          "endColumnIndex": max(self._facts.column_count, 1)},
                "filterSpecs": [{"columnIndex": index,
                                 "filterCriteria": {"condition": body}}]}}}],
                    [self._tab])

        if kind == "colour":
            return ([{"repeatCell": {
                "range": self._grid(args["range"]),
                "cell": {"userEnteredFormat": {"backgroundColor": args["rgb"]}},
                "fields": "userEnteredFormat.backgroundColor"}}], [args["range"]])

        if kind == "highlight_where":
            width = max(self._facts.column_count, 1)
            requests, touched = [], []
            for row in step.resolved_rows or ():
                a1 = f"A{row + 1}:{sheets._index_to_col(width - 1)}{row + 1}"
                requests.append({"repeatCell": {
                    "range": self._grid(a1),
                    "cell": {"userEnteredFormat": {"backgroundColor": args["rgb"]}},
                    "fields": "userEnteredFormat.backgroundColor"}})
                touched.append(a1)
            return requests, touched

        if kind == "insert_rows":
            at, count = args["at"], args["count"]
            return ([{"insertDimension": {
                "range": self._rows_grid(at, at + count - 1),
                "inheritFromBefore": False}}], [f"{at}:{at + count - 1}"])

        if kind == "delete_rows":
            start, end = args["start"], args["end"]
            return ([{"deleteDimension": {"range": self._rows_grid(start, end)}}],
                    [f"{start}:{end}"])

        if kind == "delete_rows_where":
            requests, touched = [], []
            for row in sorted(step.resolved_rows or (), reverse=True):
                requests.append({"deleteDimension": {
                    "range": self._rows_grid(row + 1, row + 1)}})
                touched.append(f"{row + 1}:{row + 1}")
            return requests, touched

        if kind == "set_formula":
            return ([{"updateCells": {
                "rows": [{"values": [{"userEnteredValue":
                                      {"formulaValue": args["formula"]}}]}],
                "fields": "userEnteredValue",
                "range": self._grid(args["cell"])}}], [args["cell"]])

        if kind == "freeze":
            return ([{"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": args["rows"]}},
                "fields": "gridProperties.frozenRowCount"}}], [self._tab])

        if kind == "autosize":
            return ([{"autoResizeDimensions": {"dimensions": {
                "sheetId": sid, "dimension": "COLUMNS", "startIndex": 0,
                "endIndex": max(self._facts.column_count, 1)}}}], [self._tab])

        raise PlanError(f"I don't know how to {kind.replace('_', ' ')}.")

    def _describe(self) -> str:
        """The sentence read back for approval. Built from the steps that
        were validated, so it describes what will actually run."""
        parts = [s.says for s in self._steps]
        where = f" on the {self._tab} tab" if self._tab else ""
        # The export is a separate sentence: hung on the end of the action
        # list it reads as though the CSV lands on a tab.
        tail = " I'd also save a copy as a CSV file." if self._export else ""
        if not parts:
            return ("I'd save a copy as a CSV file." if self._export
                    else "do nothing")
        action = parts[0] if len(parts) == 1 else ", then ".join(parts)
        return f"I'd {action}{where}.{tail}"


# --------------------------------------------------------------------------
# Execution.
#
# All or nothing, and most of that guarantee is the API's rather than ours:
# spreadsheets.batchUpdate validates every request up front and applies none
# of them if any is invalid. Every plan this module builds is exactly one
# batchUpdate, so a half-applied plan is not a state the API will produce.
#
# We take the snapshot anyway, for two reasons. It is what makes the change
# undoable afterwards, which the user needs whether or not anything went
# wrong. And if the API ever does leave partial state, restoring is better
# than discovering it later.
#
# The snapshot covers the WHOLE used range, not just the ranges the plan
# names. A delete shifts every row beneath it, so "the rows we touched" is
# not the same as "the rows that changed".
# --------------------------------------------------------------------------

def _used_a1(rows: int, columns: int) -> str:
    return f"A1:{sheets._index_to_col(max(columns, 1) - 1)}{max(rows, 1)}"


def snapshot_for(plan: SheetPlan, reader=None) -> dict | None:
    """Everything the plan could disturb: values and formats across the used
    range, plus the row and column counts so a restore can put the SHAPE
    back too. None if it couldn't be read - in which case nothing should
    run."""
    reader = reader or LiveSheetReader()
    try:
        facts = reader.facts()
    except Exception as exc:
        print(f"[sheet_builder] couldn't read the sheet to snapshot it: {exc!r}")
        return None
    a1 = _used_a1(facts.row_count, facts.column_count)
    snapshot = sheets._snapshot(a1)
    if snapshot is None:
        return None
    snapshot["row_count"] = facts.row_count
    snapshot["column_count"] = facts.column_count
    return snapshot


def _restore(snapshot: dict, reader=None, applier=None) -> bool:
    """Put the sheet back. Handles the row count as well as the contents:
    after a delete there are fewer rows than the snapshot describes, and
    writing values into a shorter grid would silently lose the tail."""
    applier = applier or sheets._apply
    reader = reader or LiveSheetReader()
    requests = []
    try:
        facts = reader.facts()
        wanted = snapshot.get("row_count", 0)
        have = facts.row_count
        sheet_id = snapshot.get("grid", {}).get("sheetId", 0)
        if wanted and have < wanted:
            requests.append({"insertDimension": {"range": {
                "sheetId": sheet_id, "dimension": "ROWS",
                "startIndex": max(have, 0), "endIndex": wanted},
                "inheritFromBefore": False}})
        elif wanted and have > wanted:
            requests.append({"deleteDimension": {"range": {
                "sheetId": sheet_id, "dimension": "ROWS",
                "startIndex": wanted, "endIndex": have}}})
    except Exception as exc:
        print(f"[sheet_builder] couldn't measure the sheet before restoring: {exc!r}")

    requests.append({"updateCells": {
        "rows": snapshot.get("rows", []),
        "fields": "userEnteredValue,userEnteredFormat",
        "range": snapshot.get("grid", {})}})
    error = applier(requests, snapshot.get("spreadsheet_id", ""))
    if error:
        print(f"[sheet_builder] restore failed: {error}")
        return False
    return True


def export_csv(plan: SheetPlan, reader=None) -> tuple[str | None, str]:
    """Write the sheet out. (path, spoken fragment) - the path is None if it
    couldn't be written, and the fragment says so rather than staying quiet."""
    import csv
    import os
    reader = reader or LiveSheetReader()
    path = plan.export_path
    if not path:
        title = (sheets.current().get("title") or plan.tab or "sheet").strip()
        safe = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        folder = os.path.join(os.path.expanduser("~"), "Documents")
        if not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        path = os.path.join(folder, f"{safe or 'sheet'}.csv")
    try:
        rows = reader.values() or []
        with open(path, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
    except Exception as exc:
        print(f"[sheet_builder] export failed: {exc!r}")
        return None, " I couldn't save the CSV copy, though."
    return path, f" I've saved a copy to {os.path.basename(path)}."


def execute(plan: SheetPlan, reader=None, applier=None) -> str:
    """Run a built plan, all of it or none of it. Returns one short honest
    sentence, the way every skill does."""
    applier = applier or sheets._apply

    if plan.is_empty():
        return "There was nothing in that plan to do, so I've left it alone."

    # Nothing runs until we know we could put it back.
    snapshot = snapshot_for(plan, reader)
    if snapshot is None:
        return ("I couldn't take a copy of the sheet first, so I've not "
                "changed anything - I'd have had no way to undo it.")
    if not sheets._push_undo(plan.description, snapshot):
        return ("I couldn't save a way back, so I've not changed anything.")

    error = applier(list(plan.requests), snapshot.get("spreadsheet_id", ""))
    if error:
        # batchUpdate is all-or-nothing, so this should mean nothing was
        # applied. Restore anyway - writing identical data back costs one
        # call and is the difference between believing that and knowing it.
        sheets._undo_stack.pop()          # nothing happened; no phantom undo
        restored = _restore(snapshot, reader, applier)
        if restored:
            return (f"That didn't work, so I've put the sheet back as it was. "
                    f"Nothing was changed. The problem was: {error}")
        return (f"That didn't work and I couldn't fully put the sheet back. "
                f"Check it before doing anything else. The problem was: {error}")

    said = _spoken_result(plan)
    # Export LAST, and only after the changes landed. It writes a local file,
    # which no rollback can take back - so a plan that failed must never
    # leave one behind.
    if plan.export_requested:
        _path, fragment = export_csv(plan, reader)
        said += fragment
    return said


def _spoken_result(plan: SheetPlan) -> str:
    """What she says after it worked. Built from the plan's own steps, so it
    describes what actually ran."""
    count = len(plan.requests)
    if plan.steps:
        action = plan.steps[0] if len(plan.steps) == 1 else \
            f"{len(plan.steps)} changes"
    else:
        action = f"{count} change{'s' if count != 1 else ''}"
    where = f" on {plan.tab}" if plan.tab else ""
    return f"Done - {action}{where}. Say undo if that wasn't right."


class PlanDirector:
    """The Director: sequences that come up often enough to be worth naming.
    It knows the ORDER of construction; the builder knows how to construct.
    Separating those is the reason the pattern exists."""

    @staticmethod
    def tidy(builder: SheetRequestBuilder) -> SheetPlan:
        """Make a sheet presentable: header frozen, columns readable."""
        return builder.freeze(1).autosize().build()

    @staticmethod
    def clean_rows_by_colour(builder: SheetRequestBuilder,
                             colour: str) -> SheetRequestBuilder:
        """The order that started all this. Left unbuilt because the rows
        aren't known until resolve() has looked at the formatting."""
        return builder.delete_rows_where(
            {"kind": COLOUR_PREDICATE, "colour": colour})

    @staticmethod
    def report(builder: SheetRequestBuilder, column, order: str = "desc",
               path: str | None = None) -> SheetRequestBuilder:
        """Sort, tidy, and take a copy away."""
        return builder.sort(column, order).freeze(1).autosize().export(path)
