"""Unit tests for the sheet plan builder. No network, no spreadsheet.

    python sheet_builder_test.py

The important ones are the two that prove the pattern is doing its job:
chaining never touches the network, and a plan that shouldn't run raises
before anything is assembled."""

import sys

from athena import sheets
from athena.sheet_builder import (COLOUR_PREDICATE, PlanDirector, PlanError,
                                  SheetFacts, SheetPlan, SheetRequestBuilder,
                                  match_colour_name)

results = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def raises(label: str, fn, expect: str = "") -> None:
    try:
        fn()
        check(label, False, "no PlanError raised")
    except PlanError as exc:
        ok = expect.lower() in str(exc).lower() if expect else True
        check(label, ok, f"said: {exc}")
    except Exception as exc:
        check(label, False, f"wrong error: {type(exc).__name__}: {exc}")


FACTS = SheetFacts(tab="Data", sheet_id=0,
                   headers=("Name", "Score", "City"),
                   row_count=20, column_count=3,
                   tabs=(("Data", 0), ("Notes", 77)))


def b() -> SheetRequestBuilder:
    return SheetRequestBuilder(FACTS)


# Real Google palette swatches, not our own colours - the whole point is
# that a user's orange isn't ours.
G_ORANGE = {"red": 0.988, "green": 0.898, "blue": 0.804}   # light orange 3
G_RED = {"red": 0.957, "green": 0.800, "blue": 0.800}      # light red 3
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


class FakeReader:
    """Stands in for the live sheet. Rows 2 and 4 (0-based 1 and 3) are
    orange, row 3 is red. Raises if backgrounds() is fetched when no
    predicate needed it."""

    def __init__(self, expect_backgrounds: bool = True):
        self._expect = expect_backgrounds

    def facts(self):
        return SheetFacts(tab="Data", sheet_id=0,
                          headers=("Name", "Score", "City"),
                          row_count=6, column_count=3, tabs=(("Data", 0),))

    def values(self):
        return [["Name", "Score", "City"],
                ["Ada", "90", "London"],
                ["Linus", "70", "Helsinki"],
                ["Grace", "85", "New York"],
                ["Alan", "55", "Cambridge"],
                ["Edsger", "", "Rotterdam"]]

    def backgrounds(self):
        if not self._expect:
            raise AssertionError("backgrounds() fetched when nothing asked "
                                 "about colour")
        return [[WHITE] * 3, [G_ORANGE] * 3, [G_RED] * 3,
                [G_ORANGE] * 3, [WHITE] * 3, [WHITE] * 3]


if __name__ == "__main__":
    print("=== chaining returns self (the fluent contract) ===")
    builder = b()
    for name, call in [
        ("on_sheet", lambda x: x.on_sheet("Data")),
        ("sort", lambda x: x.sort("Score", "desc")),
        ("filter", lambda x: x.filter("City", "equals", "London")),
        ("colour", lambda x: x.colour("A1:C1", "yellow")),
        ("insert_rows", lambda x: x.insert_rows(3, 2)),
        ("set_formula", lambda x: x.set_formula("E1", "SUM(B2:B10)")),
        ("freeze", lambda x: x.freeze(1)),
        ("autosize", lambda x: x.autosize()),
        ("export", lambda x: x.export()),
    ]:
        check(f"{name} returns the builder", call(builder) is builder)

    print("\n=== NO network during chaining ===")
    # If any step reaches for the sheet, this blows up loudly.
    saved = sheets._service
    sheets._service = lambda: (_ for _ in ()).throw(
        AssertionError("chaining touched the network"))
    try:
        plan = (b().on_sheet("Data").sort("Score", "desc")
                .colour("A1:C1", "yellow").freeze(1).autosize().build())
        check("built a plan with the service stubbed to explode", True)
        check("a tab-prefixed range also stays offline",
              bool(b().colour("Data!A1:B2", "red").build().requests))
    except AssertionError as exc:
        check("chaining stayed offline", False, str(exc))
    finally:
        sheets._service = saved

    print("\n=== the Product is immutable ===")
    plan = b().freeze(1).build()
    check("is a SheetPlan", isinstance(plan, SheetPlan))
    try:
        plan.requests = ()
        check("cannot be reassigned", False, "assignment succeeded")
    except Exception:
        check("cannot be reassigned", True)
    check("requests is a tuple", isinstance(plan.requests, tuple))
    check("ranges is a tuple", isinstance(plan.ranges, tuple))

    print("\n=== A1 -> GridRange, delegated, 1-based -> 0-based exclusive ===")
    grid = b().colour("B3:D10", "red").build().requests[0]["repeatCell"]["range"]
    check("B3:D10 start row 2", grid["startRowIndex"] == 2, str(grid))
    check("B3:D10 end row 10", grid["endRowIndex"] == 10)
    check("spans exactly 8 rows", grid["endRowIndex"] - grid["startRowIndex"] == 8)
    check("spans exactly 3 columns",
          grid["endColumnIndex"] - grid["startColumnIndex"] == 3)
    check("carries the builder's sheet id", grid["sheetId"] == 0)

    print("\n=== deletes are ordered DESCENDING (the whole bug) ===")
    plan = b().delete_rows(3).delete_rows(7).delete_rows(12).build()
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in plan.requests]
    check("emitted high to low", starts == sorted(starts, reverse=True), str(starts))
    check("three deletes", len(starts) == 3)

    print("\n=== deletes run after everything else ===")
    plan = b().delete_rows(5).colour("A1:C1", "red").freeze(1).build()
    kinds = [next(iter(r)) for r in plan.requests]
    check("deleteDimension is last", kinds[-1] == "deleteDimension", str(kinds))

    print("\n=== resolved predicates also order descending ===")
    builder = b().delete_rows_where({"kind": COLOUR_PREDICATE, "colour": "orange"})
    builder._steps[0].resolved_rows = (2, 8, 5)          # unordered, 0-based
    plan = builder.build()
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in plan.requests]
    check("descending regardless of input order", starts == [8, 5, 2], str(starts))

    print("\n=== the four required refusals ===")
    raises("empty plan", lambda: b().build(), "nothing for me to do")
    raises("overlapping deletes",
           lambda: b().delete_rows(3, 6).delete_rows(5, 8).build(), "twice over")
    raises("column that doesn't exist",
           lambda: b().sort("Revenue", "asc").build(), "no column called Revenue")
    raises("row past the end",
           lambda: b().delete_rows(400).build(), "past the end")

    print("\n=== other refusals, each worded to be spoken ===")
    raises("unknown colour", lambda: b().colour("A1:B2", "puce"), "puce")
    raises("unknown condition", lambda: b().filter("Score", "smells like", "x"),
           "filter by")
    raises("backwards row range", lambda: b().delete_rows(9, 4), "backwards")
    raises("empty formula", lambda: b().set_formula("E1", "  "), "formula should be")
    raises("bad range", lambda: b().colour("banana", "red").build(), "banana")
    raises("unknown tab", lambda: b().on_sheet("Nope"), "no tab called Nope")
    raises("non-csv export", lambda: b().export(fmt="xlsx"), "CSV")
    raises("zero rows to insert", lambda: b().insert_rows(3, 0), "at least one")
    raises("unresolved predicate reaches build",
           lambda: b().delete_rows_where(
               {"kind": COLOUR_PREDICATE, "colour": "orange"}).build(),
           "look at the sheet")
    raises("predicate on an unsupported property",
           lambda: b().delete_rows_where({"kind": "font colour", "column": "A"}),
           "can't match rows by font colour")

    print("\n=== description reads as a sentence ===")
    plan = (b().sort("Score", "desc").colour("A1:C1", "yellow")
            .freeze(1).export().build())
    print(f"    {plan.description}")
    for want in ["sort by Score", "descending", "colour A1:C1 yellow",
                 "freeze the top 1 row", "CSV", "Data tab"]:
        check(f"mentions {want!r}", want in plan.description)

    print("\n=== the plan reports what it touches ===")
    plan = b().colour("A1:C1", "red").delete_rows(5).build()
    check("ranges recorded", set(plan.ranges) == {"A1:C1", "5:5"}, str(plan.ranges))
    check("steps recorded", len(plan.steps) == 2, str(plan.steps))

    print("\n=== validation degrades honestly without sheet facts ===")
    bare = SheetRequestBuilder()          # no SheetFacts at all
    plan = bare.sort("Anything", "asc").build()
    check("still builds", isinstance(plan, SheetPlan))
    check("but says what it could not check",
          any("not checked" in w for w in plan.warnings), str(plan.warnings))

    print("\n=== the Director ===")
    plan = PlanDirector.tidy(b())
    kinds = [next(iter(r)) for r in plan.requests]
    check("tidy freezes and autosizes",
          kinds == ["updateSheetProperties", "autoResizeDimensions"], str(kinds))
    builder = PlanDirector.clean_rows_by_colour(b(), "orange")
    check("clean_rows_by_colour leaves it unresolved for resolve()",
          builder._steps[-1].needs_resolution)
    builder = PlanDirector.report(b(), "Score", "desc", None)
    check("report chains sort+freeze+autosize+export",
          len(builder._steps) == 3 and builder._export is not None)

    print("\n=== export is recorded, not performed ===")
    plan = b().freeze(1).export("out.csv").build()
    check("path carried on the plan", plan.export_path == "out.csv")
    check("no export request in the API body",
          all("export" not in next(iter(r)).lower() for r in plan.requests))

    # ================= phase 2: resolution =================
    print("\n=== colour classifier vs REAL Google palette swatches ===")
    swatches = [
        ("light orange 3", (0.988, 0.898, 0.804), "orange"),
        ("orange #FF9900", (1.0, 0.6, 0.0), "orange"),
        ("light orange 2", (0.976, 0.796, 0.612), "orange"),
        ("light red 3", (0.957, 0.8, 0.8), "red"),
        ("red #FF0000", (1.0, 0.0, 0.0), "red"),
        ("light yellow 3", (1.0, 0.949, 0.8), "yellow"),
        ("light green 3", (0.851, 0.918, 0.827), "green"),
        ("light blue 3", (0.812, 0.886, 0.953), "blue"),
        ("light grey 2", (0.8, 0.8, 0.8), "grey"),
        ("dark grey #666", (0.4, 0.4, 0.4), "grey"),
        ("WHITE (unset)", (1.0, 1.0, 1.0), None),
        ("black", (0.0, 0.0, 0.0), None),
        ("purple", (0.6, 0.0, 1.0), None),
    ]
    for name, (r, g, bl), want in swatches:
        got = match_colour_name({"red": r, "green": g, "blue": bl})
        check(f"{name:<16} -> {got}", got == want, f"wanted {want}")
    check("no background at all -> None", match_colour_name(None) is None)
    for name, rgb in sorted(sheets.COLORS.items()):
        check(f"our own {name} classifies as itself",
              match_colour_name(rgb) == name)

    print("\n=== the demo failure: delete rows coloured orange ===")
    plan = (SheetRequestBuilder()
            .delete_rows_where({"kind": COLOUR_PREDICATE, "colour": "orange"})
            .resolve(FakeReader()).build())
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in plan.requests]
    check("found both orange rows", sorted(starts) == [1, 3], str(starts))
    check("emitted descending", starts == sorted(starts, reverse=True), str(starts))
    check("header row never matched", 0 not in starts)
    check("count in the description", "2 rows" in plan.description, plan.description)
    print(f"    {plan.description}")

    print("\n=== value predicates resolve to the right rows ===")
    for kind, col, val, want in [("greater than", "Score", 75, [1, 3]),
                                 ("less than", "Score", 75, [2, 4]),
                                 ("equals", "City", "london", [1]),
                                 ("contains", "City", "new", [3]),
                                 ("empty", "Score", "", [5])]:
        plan = (SheetRequestBuilder()
                .delete_rows_where({"kind": kind, "column": col, "value": val})
                .resolve(FakeReader(expect_backgrounds=False)).build())
        rows = sorted(r["deleteDimension"]["range"]["startIndex"]
                      for r in plan.requests)
        check(f"{kind} {col} {val!r} -> {want}", rows == want, str(rows))

    print("\n=== the colour grid is only fetched when something asks ===")
    SheetRequestBuilder().delete_rows_where(
        {"kind": "greater than", "column": "Score", "value": 75}
    ).resolve(FakeReader(expect_backgrounds=False)).build()
    check("value-only plan never fetched backgrounds", True)

    print("\n=== highlight_where resolves the same way ===")
    plan = (SheetRequestBuilder().highlight_where("Score", "greater than", 75, "green")
            .resolve(FakeReader(expect_backgrounds=False)).build())
    check("one repeatCell per matching row", len(plan.requests) == 2)
    check("all are repeatCell", all("repeatCell" in r for r in plan.requests))

    print("\n=== resolve refreshes facts, so build validates for real ===")
    raises("unknown column caught after resolve",
           lambda: SheetRequestBuilder().sort("Revenue", "asc")
           .resolve(FakeReader(expect_backgrounds=False)).build(),
           "no column called Revenue")
    raises("row past the real end caught after resolve",
           lambda: SheetRequestBuilder().delete_rows(99)
           .resolve(FakeReader(expect_backgrounds=False)).build(),
           "past the end")

    print("\n=== unsupported predicates each say WHY ===")
    for kind, expect in [("font colour", "only by background"),
                         ("bold", "whether the text is bold"),
                         ("conditional formatting", "conditional-formatting"),
                         ("merged", "whether their cells are merged"),
                         ("formula", "not the formula behind it")]:
        raises(f"{kind}", lambda k=kind: SheetRequestBuilder().delete_rows_where(
            {"kind": k, "column": "Score"}), expect)

    print("\n=== matching nothing is not an error ===")
    plan = (SheetRequestBuilder()
            .delete_rows_where({"kind": COLOUR_PREDICATE, "colour": "blue"})
            .resolve(FakeReader()).build())
    check("no requests emitted", plan.requests == ())
    check("still describes itself honestly", "0 rows" in plan.description,
          plan.description)

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
