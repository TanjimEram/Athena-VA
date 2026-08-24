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

from dataclasses import dataclass, field
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
    export_path: str | None = None
    warnings: tuple = ()

    def __len__(self) -> int:
        return len(self.requests) + (1 if self.export_path else 0)

    def is_empty(self) -> bool:
        return not self.requests and not self.export_path


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
            raise PlanError(f"I can't match rows by {kind}.")
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
