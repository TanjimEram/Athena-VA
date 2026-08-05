"""Prove the undo layer actually works, before anything that mutates exists.

    python sheets_undo_test.py <spreadsheet-url-or-id> [scratch-range]

THIS TEST WRITES TO THE SPREADSHEET. Point it at a THROWAWAY sheet only.

It works in a far-off scratch block (BZ100:CB102 by default) so it doesn't
touch anything you'd have put in the top-left of a sheet, and it saves that
block's original state at the start and puts it back at the end - so even the
scratch area is left exactly as it was found.

What it proves:
  1. a snapshot captures values AND background colours
  2. a mutation really changes both
  3. undo_last_change puts both back, cell for cell
  4. the stack is depth-limited to 5 and pops newest-first
  5. undoing with an empty stack says so instead of doing something odd

Run google_auth_test.py and sheets_test.py first."""

import sys

from athena import google_auth, sheets

DEFAULT_RANGE = "BZ100:CB102"

ORIGINAL = [["alpha", "1", "x"], ["beta", "2", "y"], ["gamma", "3", "z"]]
MUTATED = [["CHANGED", "99", "!"], ["CHANGED", "98", "!"], ["CHANGED", "97", "!"]]
GREEN = {"red": 0.72, "green": 0.88, "blue": 0.80}
RED = {"red": 0.96, "green": 0.78, "blue": 0.78}

results = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def cells(rows: list, color: dict) -> list:
    """Build updateCells rowData: values plus a background colour."""
    return [{"values": [{"userEnteredValue": {"stringValue": v},
                         "userEnteredFormat": {"backgroundColor": color}}
                        for v in row]} for row in rows]


def write(a1: str, rows: list, color: dict) -> str | None:
    """Mutate the range directly, bypassing any named operation."""
    return sheets._apply([{"updateCells": {
        "rows": cells(rows, color),
        "fields": "userEnteredValue,userEnteredFormat",
        "range": sheets._bounded(sheets._a1_to_grid(a1)),
    }}])


def read_back(a1: str) -> tuple[list, list]:
    """(values, background colours) for a range, straight from the API."""
    response = sheets._service().spreadsheets().get(
        spreadsheetId=sheets.current()["id"], ranges=[sheets._qualify(a1)],
        includeGridData=True,
        fields=("sheets(data(rowData(values("
                "formattedValue,userEnteredFormat.backgroundColor))))")
    ).execute()
    data = response.get("sheets", [{}])[0].get("data", [{}])[0]
    values, colors = [], []
    for row in data.get("rowData", []):
        values.append([c.get("formattedValue", "") for c in row.get("values", [])])
        colors.append([c.get("userEnteredFormat", {}).get("backgroundColor", {})
                       for c in row.get("values", [])])
    return values, colors


def same_color(a: dict, b: dict) -> bool:
    """Sheets rounds colour floats, so compare with a tolerance."""
    keys = ("red", "green", "blue")
    return all(abs(float(a.get(k, 0)) - float(b.get(k, 0))) < 0.02 for k in keys)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python sheets_undo_test.py <spreadsheet-url-or-id> [range]")
        print("\nUse a THROWAWAY spreadsheet - this test writes to it.")
        sys.exit(1)
    target, scratch = args[0], (args[1] if len(args) > 1 else DEFAULT_RANGE)

    check_conn = google_auth.check_connection()
    if not check_conn["ok"]:
        print(f"Google isn't connected: {check_conn['detail']}")
        sys.exit(1)

    opened = sheets.open_spreadsheet(target)
    print(f"\n{opened}")
    if not sheets.current()["id"]:
        sys.exit(1)
    print(f"Scratch range: {scratch}\n")

    # Save whatever is really there, so the sheet is left untouched at the end.
    pristine = sheets._snapshot(scratch)
    if pristine is None:
        print("Couldn't snapshot the scratch range - stopping rather than "
              "writing with no way back.")
        sys.exit(1)

    try:
        print("=== 1. set up a known original state ===")
        error = write(scratch, ORIGINAL, GREEN)
        check("wrote the original values and green background", error is None,
              error or "")
        before_values, before_colors = read_back(scratch)
        print(f"  values now: {before_values}")

        print("\n=== 2. snapshot it, the way a mutating op will ===")
        sheets._clear_undo()
        snap = sheets._snapshot(scratch)
        check("snapshot captured something", snap is not None)
        check("snapshot holds rows", bool(snap and snap.get("rows")),
              f"{len(snap.get('rows', [])) if snap else 0} rows")
        check("snapshot range is bounded",
              bool(snap and "endRowIndex" in snap["grid"]
                   and "endColumnIndex" in snap["grid"]), str(snap["grid"]))
        pushed = sheets._push_undo("the test change", snap)
        check("_push_undo accepted it", pushed)
        check("undo depth is 1", sheets.undo_depth() == 1)

        print("\n=== 3. mutate: different values AND a different colour ===")
        error = write(scratch, MUTATED, RED)
        check("mutation applied", error is None, error or "")
        mid_values, mid_colors = read_back(scratch)
        check("values really changed", mid_values != before_values,
              f"{mid_values[0] if mid_values else []}")
        check("colours really changed",
              not same_color(mid_colors[0][0], before_colors[0][0]),
              f"{mid_colors[0][0] if mid_colors else {}}")

        print("\n=== 4. undo ===")
        spoken = sheets.undo_last_change()
        print(f"  she says: {spoken}")
        after_values, after_colors = read_back(scratch)

        check("values came back", after_values == before_values,
              f"got {after_values}")
        colors_ok = (len(after_colors) == len(before_colors) and
                     all(len(a) == len(b) and
                         all(same_color(x, y) for x, y in zip(a, b))
                         for a, b in zip(after_colors, before_colors)))
        check("background colours came back", colors_ok,
              f"got {after_colors[0][0] if after_colors else {}}")
        check("stack is empty again", sheets.undo_depth() == 0)

        print("\n=== 5. the stack is depth-limited and newest-first ===")
        sheets._clear_undo()
        for i in range(7):
            sheets._push_undo(f"change {i}", sheets._snapshot(scratch))
        check(f"depth capped at {sheets.UNDO_DEPTH}",
              sheets.undo_depth() == sheets.UNDO_DEPTH, str(sheets.undo_depth()))
        newest = sheets._undo_stack[-1]["label"]
        oldest = sheets._undo_stack[0]["label"]
        check("newest kept, oldest dropped",
              newest == "change 6" and oldest == "change 2",
              f"newest={newest!r} oldest={oldest!r}")
        spoken = sheets.undo_last_change()
        print(f"  she says: {spoken}")
        check("popped the newest first", "change 6" in spoken, spoken)

        print("\n=== 6. nothing to undo ===")
        sheets._clear_undo()
        spoken = sheets.undo_last_change()
        print(f"  she says: {spoken}")
        check("says so plainly", "nothing" in spoken.lower(), spoken)

        print("\n=== 7. a snapshot of an unreadable range refuses ===")
        check("bad range snapshots to None", sheets._snapshot("banana") is None)
        check("_push_undo refuses an empty snapshot",
              sheets._push_undo("nope", None) is False)

    finally:
        print("\n=== restoring the scratch range to how it was found ===")
        sheets._clear_undo()
        sheets._push_undo("the test setup", pristine)
        print(f"  {sheets.undo_last_change()}")
        sheets._clear_undo()

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
