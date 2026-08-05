"""Test reading a spreadsheet without the voice loop (Phase 1: read-only).

    python sheets_test.py <spreadsheet-url-or-id>

Point this at a THROWAWAY spreadsheet, never at real data. Nothing in Phase 1
can write - these are all read calls - but later phases reuse this script and
will mutate, so build the habit now.

The target is required on purpose: there is no default, so this can never
wander into a spreadsheet you didn't name.

Run google_auth_test.py first. The spreadsheets scope is new, so your old
sign-in won't cover it - use  python google_auth_test.py --reset  and approve
all five permissions.

A good test sheet has a header row, a few rows of data, and ideally a second
tab."""

import sys

from athena import google_auth, sheets

USAGE = "usage: python sheets_test.py <spreadsheet-url-or-id>"


def show(label: str, spoken: str) -> None:
    print(f"\n--- {label} ---")
    print(f"  she says: {spoken}")


if __name__ == "__main__":
    targets = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not targets:
        print(USAGE)
        print("\nOpen your throwaway sheet in the browser and copy the URL.")
        sys.exit(1)
    target = targets[0]

    check = google_auth.check_connection()
    if not check["ok"]:
        print(f"Google isn't connected: {check['detail']}")
        print("Run  python google_auth_test.py  first.")
        sys.exit(1)
    if "https://www.googleapis.com/auth/spreadsheets" not in check["scopes"]:
        print("Your sign-in doesn't include the spreadsheets permission yet.")
        print("Run  python google_auth_test.py --reset  and approve all five.")
        sys.exit(1)
    print(f"Signed in as {check['account']}")

    # ---------- opening ----------
    show("open_spreadsheet", sheets.open_spreadsheet(target))
    if not sheets.current()["id"]:
        print("\nCouldn't open it, so there's nothing more to test.")
        sys.exit(1)
    print(f"  state: {sheets.current()}")

    # ---------- structure ----------
    show("list_tabs", sheets.list_tabs())
    show("describe_sheet", sheets.describe_sheet())

    # ---------- reading ----------
    show("read_range A1:D5", sheets.read_range("A1:D5"))
    show("read_range A1:A1", sheets.read_range("A1:A1"))
    show("read_range a whole column A:A", sheets.read_range("A:A"))
    show("read_range far-off empty block", sheets.read_range("BX900:BZ905"))

    # ---------- switching tabs ----------
    tabs = sheets._tabs()
    if len(tabs) > 1:
        other = tabs[1].get("title", "")
        show(f"use_tab {other!r}", sheets.use_tab(other))
        show("read_range A1:C3 on that tab", sheets.read_range("A1:C3"))
        show("back to the first tab", sheets.use_tab(tabs[0].get("title", "")))
    else:
        print("\n(only one tab, so the tab-switching checks are skipped)")

    # ---------- error paths ----------
    print("\n=== error handling ===")
    print(f"  no such tab:   {sheets.use_tab('Definitely Not A Tab')}")
    print(f"  nonsense range:{sheets.read_range('banana')}")
    print(f"  empty range:   {sheets.read_range('')}")
    print(f"  tab in the A1: {sheets.read_range(tabs[0].get('title', '') + '!A1:B2')}")

    # ---------- state is required ----------
    print("\n=== with nothing open ===")
    sheets._forget()
    print(f"  list_tabs:      {sheets.list_tabs()}")
    print(f"  read_range:     {sheets.read_range('A1:B2')}")
    print(f"  describe_sheet: {sheets.describe_sheet()}")

    print("\nDone. Nothing was written - Phase 1 is read-only.")
