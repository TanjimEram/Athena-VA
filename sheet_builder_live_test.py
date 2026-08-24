"""Prove the builder against a real spreadsheet.

    python sheet_builder_live_test.py <spreadsheet-url-or-id>

Everything happens on a scratch tab this script CREATES and DELETES. Your
existing tabs are never touched, so it is safe to run against a sheet with
real data in it, and it can be run repeatedly.

Scenarios, in the order the brief asks for them:
  1. delete all rows with an orange background   (the failure that started it)
  2. highlight rows where a column exceeds a value
  3. sort, then colour, then undo - values AND colours must come back
  4. an intentionally invalid plan - nothing must be written
  5. a plan that fails midway - full rollback

Run google_auth_test.py first."""

import sys
import time

from athena import google_auth, sheets
from athena.sheet_builder import (COLOUR_PREDICATE, PlanError,
                                  SheetRequestBuilder, execute,
                                  match_colour_name)

SCRATCH = "AthenaBuilderTest"
ORANGE = {"red": 0.988, "green": 0.898, "blue": 0.804}   # Google light orange 3
PLAIN = {"red": 1.0, "green": 1.0, "blue": 1.0}

TABLE = [["Name", "Score", "City"],
         ["Ada", "90", "London"],          # orange
         ["Linus", "70", "Helsinki"],
         ["Grace", "85", "New York"],      # orange
         ["Alan", "55", "Cambridge"],
         ["Edsger", "95", "Rotterdam"]]
ORANGE_ROWS = (1, 3)          # 0-based, so sheet rows 2 and 4

results = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def apply(requests):
    return sheets._apply(requests, sheets.current()["id"])


def make_scratch() -> int | None:
    """Create the scratch tab, removing a leftover one first."""
    remove_scratch(quiet=True)
    service = sheets._service()
    body = {"requests": [{"addSheet": {"properties": {"title": SCRATCH}}}]}
    try:
        out = service.spreadsheets().batchUpdate(
            spreadsheetId=sheets.current()["id"], body=body).execute()
        return out["replies"][0]["addSheet"]["properties"]["sheetId"]
    except Exception as exc:
        print(f"couldn't create the scratch tab: {exc}")
        return None


def remove_scratch(quiet: bool = False) -> None:
    for props in sheets._tabs():
        if props.get("title") == SCRATCH:
            error = apply([{"deleteSheet": {"sheetId": props["sheetId"]}}])
            if error and not quiet:
                print(f"couldn't remove the scratch tab: {error}")
            return


def seed(sheet_id: int) -> None:
    """Write the table and colour two rows orange."""
    rows = [{"values": [{"userEnteredValue": {"stringValue": v}} for v in row]}
            for row in TABLE]
    apply([{"updateCells": {
        "rows": rows, "fields": "userEnteredValue,userEnteredFormat",
        "range": {"sheetId": sheet_id, "startRowIndex": 0,
                  "endRowIndex": len(TABLE), "startColumnIndex": 0,
                  "endColumnIndex": 3}}}])
    apply([{"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                  "startColumnIndex": 0, "endColumnIndex": 3},
        "cell": {"userEnteredFormat": {"backgroundColor": ORANGE}},
        "fields": "userEnteredFormat.backgroundColor"}} for r in ORANGE_ROWS])


# Sheets allows 60 READ requests a minute per user, and this test does a lot
# of reading: every resolve, snapshot and verification is a read. Without
# pacing it exhausts the quota around scenario 5 - which the code handles
# correctly (it refuses to act without a snapshot) but which tells us
# nothing about rollback.
READ_PAUSE = 12.0


def patient(fn, tries: int = 4):
    """Retry a read through a quota refusal rather than failing the test."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:
            if "quota" not in str(exc).lower() or attempt == tries - 1:
                raise
            print(f"    (read quota reached, waiting 25s)")
            time.sleep(25)
    return None


def read_back():
    """(values, background colour names) straight from the sheet."""
    from athena.sheet_builder import LiveSheetReader
    reader = LiveSheetReader()
    values = patient(reader.values)
    colours = [[match_colour_name(c) for c in row]
               for row in patient(reader.backgrounds)]
    return values, colours


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python sheet_builder_live_test.py <spreadsheet-url-or-id>")
        sys.exit(1)

    conn = google_auth.check_connection()
    if not conn["ok"]:
        print(f"Google isn't connected: {conn['detail']}")
        sys.exit(1)

    print(sheets.open_spreadsheet(args[0]))
    sheet_id = make_scratch()
    if sheet_id is None:
        sys.exit(1)
    sheets.use_tab(SCRATCH)
    print(f"working on a scratch tab '{SCRATCH}' (created; removed at the end)\n")

    try:
        # ---------- 1. the demo failure ----------
        print("=== 1. delete all rows with an orange background ===")
        seed(sheet_id)
        before, colours = read_back()
        check("two rows really are orange",
              sum(1 for row in colours if row and row[0] == "orange") == 2,
              str([row[0] if row else None for row in colours]))
        plan = (SheetRequestBuilder()
                .delete_rows_where({"kind": COLOUR_PREDICATE, "colour": "orange"})
                .resolve().build())
        print(f"    plan: {plan.description}")
        said = execute(plan)
        print(f"    said: {said}")
        after, _ = read_back()
        names = [r[0] for r in after if r]
        check("the two orange rows are gone", len(after) == len(before) - 2,
              f"{len(before)} -> {len(after)}")
        check("Ada and Grace removed",
              "Ada" not in names and "Grace" not in names, str(names))
        check("everyone else kept",
              all(n in names for n in ("Linus", "Alan", "Edsger")), str(names))

        # ---------- 2. highlight where a column exceeds a value ----------
        print(f"\n(pausing {READ_PAUSE:.0f}s to stay inside the read quota)")
        time.sleep(READ_PAUSE)
        print("=== 2. highlight rows where Score is over 60 ===")
        seed(sheet_id)
        plan = (SheetRequestBuilder()
                .highlight_where("Score", "greater than", 60, "green")
                .resolve().build())
        print(f"    plan: {plan.description}")
        print(f"    said: {execute(plan)}")
        values, colours = read_back()
        greens = [i for i, row in enumerate(colours) if row and row[0] == "green"]
        check("the four over-60 rows went green", sorted(greens) == [1, 2, 3, 5],
              str(greens))
        check("Alan (55) was left alone", 4 not in greens, str(greens))

        # ---------- 3. sort, colour, undo ----------
        print(f"\n(pausing {READ_PAUSE:.0f}s to stay inside the read quota)")
        time.sleep(READ_PAUSE)
        print("=== 3. sort, then colour, then undo ===")
        seed(sheet_id)
        original_values, original_colours = read_back()
        sorted_plan = (SheetRequestBuilder()
                       .sort("Score", "desc", a1_range="A2:C6")
                       .resolve().build())
        execute(sorted_plan)
        colour_plan = (SheetRequestBuilder().colour("A1:C1", "blue")
                       .resolve().build())
        execute(colour_plan)
        changed_values, changed_colours = read_back()
        check("the sort really reordered", changed_values != original_values,
              str([r[0] for r in changed_values]))
        check("the header really turned blue",
              changed_colours[0][0] == "blue", str(changed_colours[0][0]))
        print(f"    undo 1: {sheets.undo_last_change()}")
        print(f"    undo 2: {sheets.undo_last_change()}")
        back_values, back_colours = read_back()
        check("VALUES came back", back_values == original_values,
              f"{[r[0] for r in back_values]} vs {[r[0] for r in original_values]}")
        check("COLOURS came back", back_colours == original_colours,
              f"{[r[0] if r else None for r in back_colours]}")

        # ---------- 4. an invalid plan writes nothing ----------
        print(f"\n(pausing {READ_PAUSE:.0f}s to stay inside the read quota)")
        time.sleep(READ_PAUSE)
        print("=== 4. an intentionally invalid plan ===")
        seed(sheet_id)
        before, before_colours = read_back()
        for label, make in [
            ("overlapping deletes",
             lambda: SheetRequestBuilder().delete_rows(2, 4).delete_rows(3, 5)),
            ("column that doesn't exist",
             lambda: SheetRequestBuilder().sort("Revenue", "asc")),
            ("row past the end",
             lambda: SheetRequestBuilder().delete_rows(9999)),
        ]:
            try:
                make().resolve().build()
                check(f"{label} refused", False, "it built anyway")
            except PlanError as exc:
                check(f"{label} refused", True, str(exc)[:70])
        after, after_colours = read_back()
        check("the sheet is untouched by all of that",
              after == before and after_colours == before_colours)

        # ---------- 5. a plan that fails, and full rollback ----------
        print(f"\n(pausing {READ_PAUSE:.0f}s to stay inside the read quota)")
        time.sleep(READ_PAUSE)
        print("=== 5. a plan that fails midway ===")
        seed(sheet_id)
        before, before_colours = read_back()
        good_plan = (SheetRequestBuilder().delete_rows(2, 3)
                     .colour("A1:C1", "red").resolve().build())

        calls = {"n": 0}
        def flaky(requests, spreadsheet_id=""):
            """Fail the plan itself; let the restore through."""
            calls["n"] += 1
            if calls["n"] == 1:
                return "the spreadsheet refused that: simulated failure"
            return sheets._apply(requests, spreadsheet_id)

        # Earlier scenarios legitimately left entries on the stack, so the
        # question is whether THIS failure added one - not whether the stack
        # happens to be empty.
        depth_before = sheets.undo_depth()
        said = execute(good_plan, applier=flaky)
        print(f"    said: {said}")
        check("reported the failure honestly", "didn't work" in said, said)
        check("said it put the sheet back", "put the sheet back" in said, said)
        after, after_colours = read_back()
        check("VALUES fully restored", after == before,
              f"{[r[0] for r in after]} vs {[r[0] for r in before]}")
        check("COLOURS fully restored", after_colours == before_colours,
              str([r[0] if r else None for r in after_colours]))
        check("no phantom undo entry added",
              sheets.undo_depth() == depth_before,
              f"{depth_before} -> {sheets.undo_depth()}")

    finally:
        print("\ncleaning up the scratch tab...")
        try:
            sheets.use_tab(SCRATCH)
            remove_scratch()
            print("  removed.")
        except Exception as exc:
            print(f"  couldn't remove it: {exc} - delete '{SCRATCH}' by hand.")
        sheets._clear_undo()

    print("\n" + "=" * 68)
    print(f"{'scenario':<44}{'result'}")
    print("=" * 68)
    for label, ok, _detail in results:
        print(f"{label:<44}{'PASS' if ok else 'FAIL'}")
    print("=" * 68)
    passed = sum(1 for _l, ok, _d in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    rollback = [ok for label, ok, _d in results if "restored" in label
                or "put the sheet back" in label]
    print("ROLLBACK TEST: " + ("PASSED" if rollback and all(rollback)
                               else "FAILED - do not mark this complete"))
    sys.exit(0 if passed == len(results) else 1)
