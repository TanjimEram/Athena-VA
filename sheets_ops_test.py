"""Exercise every named sheet operation against a real spreadsheet, and undo
each one.

    python sheets_ops_test.py <spreadsheet-url-or-id>

THIS TEST WRITES TO THE SPREADSHEET. Point it at a THROWAWAY sheet only.

It builds its own small table in a far-off scratch block (BM200 by default),
runs each operation on that block, undoes it, and checks the block came back.
The scratch block's original contents are saved at the start and put back at
the end, so the sheet is left as it was found.

The structural operations (insert, delete, move) shift whole rows and columns
of the TAB, not just the scratch block, so those are tested on rows far below
anything you'd have data in. If your test sheet is small, they're harmless.

Run google_auth_test.py, sheets_test.py and sheets_undo_test.py first."""

import sys

from athena import google_auth, sheets

ANCHOR_COL, ANCHOR_ROW = "BM", 200
TABLE = [["Name", "Score", "City"],
         ["Ada", "90", "London"],
         ["Linus", "70", "Helsinki"],
         ["Grace", "85", "New York"]]

results = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def say(label: str, spoken: str) -> str:
    print(f"\n--- {label} ---")
    print(f"  she says: {spoken}")
    return spoken


def block(rows: int, cols: int) -> str:
    """The scratch range in A1, sized to the table."""
    end_col = sheets._index_to_col(sheets._col_to_index(ANCHOR_COL) + cols - 1)
    return f"{ANCHOR_COL}{ANCHOR_ROW}:{end_col}{ANCHOR_ROW + rows - 1}"


def write_table() -> str | None:
    rows = [{"values": [{"userEnteredValue": {"stringValue": v}} for v in row]}
            for row in TABLE]
    return sheets._apply([{"updateCells": {
        "rows": rows, "fields": "userEnteredValue,userEnteredFormat",
        "range": sheets._bounded(sheets._a1_to_grid(block(len(TABLE), 3)))}}])


def values_now(a1: str) -> list:
    return sheets._values(a1) or []


def undo_and_check(label: str, before: list, a1: str) -> None:
    spoken = say(f"undo {label}", sheets.undo_last_change())
    after = values_now(a1)
    check(f"{label}: values restored", after == before,
          f"got {after[:2]} want {before[:2]}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python sheets_ops_test.py <spreadsheet-url-or-id>")
        print("\nUse a THROWAWAY spreadsheet - this test writes to it.")
        sys.exit(1)

    conn = google_auth.check_connection()
    if not conn["ok"]:
        print(f"Google isn't connected: {conn['detail']}")
        sys.exit(1)

    print(sheets.open_spreadsheet(args[0]))
    if not sheets.current()["id"]:
        sys.exit(1)

    table_a1 = block(len(TABLE), 3)
    print(f"Scratch block: {table_a1}\n")

    pristine = sheets._snapshot(table_a1)
    if pristine is None:
        print("Couldn't snapshot the scratch block - stopping.")
        sys.exit(1)

    try:
        error = write_table()
        if error:
            print(f"Couldn't set up the test table: {error}")
            sys.exit(1)
        baseline = values_now(table_a1)
        print(f"Test table in place: {baseline}")
        sheets._clear_undo()

        # ---- read-only first ----
        say("count_matching (Score greater than 75)",
            sheets.count_matching("Score", "greater than", "75"))
        say("count_matching (City contains 'on')",
            sheets.count_matching("City", "contains", "on"))
        say("count_matching (nothing matches)",
            sheets.count_matching("Name", "equals", "Nobody"))
        check("count_matching pushed no undo", sheets.undo_depth() == 0)

        # ---- cell operations, each undone ----
        say("sort_range by Score descending",
            sheets.sort_range(table_a1, "Score", "desc"))
        sorted_values = values_now(table_a1)
        check("sort changed the order", sorted_values != baseline,
              str([r[0] for r in sorted_values]))
        undo_and_check("sort", baseline, table_a1)

        say("color_range yellow", sheets.color_range(table_a1, "yellow"))
        undo_and_check("colour", baseline, table_a1)

        say("highlight_rows_where Score greater than 75 -> green",
            sheets.highlight_rows_where("Score", "greater than", "75", "green"))
        undo_and_check("highlight", baseline, table_a1)

        formula_cell = f"{sheets._index_to_col(sheets._col_to_index(ANCHOR_COL) + 3)}{ANCHOR_ROW}"
        col = f"{sheets._index_to_col(sheets._col_to_index(ANCHOR_COL) + 1)}"
        say("add_formula", sheets.add_formula(
            formula_cell, f"SUM({col}{ANCHOR_ROW + 1}:{col}{ANCHOR_ROW + 3})"))
        got = values_now(formula_cell)
        check("formula computed a value", bool(got and got[0] and got[0][0]),
              str(got))
        say("undo formula", sheets.undo_last_change())
        check("formula cell cleared", not values_now(formula_cell),
              str(values_now(formula_cell)))

        # ---- tab-level operations ----
        say("freeze_header", sheets.freeze_header(1))
        frozen = sheets._tab_props().get("gridProperties", {}).get("frozenRowCount", 0)
        check("header is frozen", frozen == 1, str(frozen))
        say("undo freeze", sheets.undo_last_change())
        frozen = sheets._tab_props().get("gridProperties", {}).get("frozenRowCount", 0)
        check("freeze reverted", frozen == 0, str(frozen))

        say("filter_rows Score greater than 75",
            sheets.filter_rows("Score", "greater than", "75"))
        check("a filter exists now", sheets._current_filter() is not None)
        say("undo filter", sheets.undo_last_change())
        check("filter gone again", sheets._current_filter() is None)

        say("clear_filter with no filter", sheets.clear_filter())

        say("autosize_columns", sheets.autosize_columns())
        say("undo autosize", sheets.undo_last_change())

        # ---- structural operations, well below any real data ----
        far = ANCHOR_ROW + 20
        say(f"insert_rows at {far}", sheets.insert_rows(far, 2))
        undo_spoken = say("undo insert", sheets.undo_last_change())
        check("insert undo ran an inverse", "undone" in undo_spoken.lower(),
              undo_spoken)
        check("scratch block unmoved", values_now(table_a1) == baseline)

        say(f"delete_rows {far} to {far + 1}", sheets.delete_rows(far, far + 1))
        say("undo delete", sheets.undo_last_change())
        check("scratch block still intact", values_now(table_a1) == baseline,
              str(values_now(table_a1)[:2]))

        say(f"move_rows {far} to {far} -> {far + 5}",
            sheets.move_rows(far, far, far + 5))
        say("undo move", sheets.undo_last_change())
        check("scratch block survived the move",
              values_now(table_a1) == baseline, str(values_now(table_a1)[:2]))

        # ---- refusals ----
        print("\n=== refusals ===")
        print(f"  unknown colour:    {sheets.color_range(table_a1, 'puce')}")
        print(f"  unknown condition: {sheets.count_matching('Score', 'smells like', 'x')}")
        print(f"  unknown column:    {sheets.count_matching('Wibble', 'is', 'x')}")
        print(f"  bad range:         {sheets.sort_range('banana', 'Score')}")
        print(f"  row zero:          {sheets.insert_rows(0, 1)}")
        print(f"  backwards:         {sheets.delete_rows(9, 4)}")

    finally:
        print("\n=== putting the scratch block back ===")
        sheets._clear_undo()
        sheets._push_undo("the test setup", pristine)
        print(f"  {sheets.undo_last_change()}")
        sheets._clear_undo()

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
