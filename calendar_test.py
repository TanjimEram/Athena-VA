"""Prove the calendar reads correctly, and reads well ALOUD.

    python calendar_test.py             # phrasing checks, then the real calendar
    python calendar_test.py --offline   # phrasing and date maths only, no network

Two halves, and the first one is the point. Everything this module returns is
spoken, so the offline half checks the wording against fixed datetimes - no
sign-in, no network, no calendar needed. The live half then reads your actual
schedule out so you can hear whether it sounds like a person.

Phase 1 is READ-ONLY. Nothing in this script can create, move or cancel
anything.

If the live half says the calendar permission is missing, that is expected
the first time: adding a scope invalidates the old sign-in on purpose. Run
    python google_auth_test.py --reset
then sign in again, and run this once more.
"""

import datetime
import sys

from athena import calendar_skill as cal

PASSED = 0
FAILED = 0


def check(label: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


def at(year, month, day, hour, minute=0):
    """A local-time datetime, so the phrasing checks don't depend on now()."""
    return datetime.datetime(year, month, day, hour, minute,
                             tzinfo=cal.local_zone())


def phrasing_checks() -> None:
    print("\n--- the clock, in words ---")
    check("14:30 reads as a person says it",
          cal._spoken_clock(at(2026, 8, 26, 14, 30)), "half past two")
    check("09:00", cal._spoken_clock(at(2026, 8, 26, 9, 0)), "nine o'clock")
    check("09:15", cal._spoken_clock(at(2026, 8, 26, 9, 15)), "quarter past nine")
    check("17:45", cal._spoken_clock(at(2026, 8, 26, 17, 45)), "quarter to six")
    check("10:10", cal._spoken_clock(at(2026, 8, 26, 10, 10)), "ten past ten")
    check("10:50", cal._spoken_clock(at(2026, 8, 26, 10, 50)), "ten to eleven")
    check("14:05", cal._spoken_clock(at(2026, 8, 26, 14, 5)), "five past two")
    check("14:03 - an odd minute under ten gets the 'oh'",
          cal._spoken_clock(at(2026, 8, 26, 14, 3)), "two oh three")
    check("14:37", cal._spoken_clock(at(2026, 8, 26, 14, 37)), "two thirty-seven")
    check("12:00 is noon, not zero",
          cal._spoken_clock(at(2026, 8, 26, 12, 0)), "twelve o'clock")
    check("00:30 is half past twelve, not half past zero",
          cal._spoken_clock(at(2026, 8, 26, 0, 30)), "half past twelve")

    print("\n--- never a raw timestamp ---")
    check("today, afternoon", cal._spoken_time(at(2026, 8, 26, 14, 30), today=True),
          "half past two this afternoon")
    check("another day, afternoon", cal._spoken_time(at(2026, 8, 28, 14, 30)),
          "half past two in the afternoon")
    check("morning", cal._spoken_time(at(2026, 8, 26, 9, 0), today=True),
          "nine o'clock this morning")
    check("evening", cal._spoken_time(at(2026, 8, 26, 19, 0), today=True),
          "seven o'clock this evening")
    check("night is 'tonight', never 'this night'",
          cal._spoken_time(at(2026, 8, 26, 22, 0), today=True),
          "ten o'clock tonight")

    print("\n--- naming days ---")
    today = datetime.date(2026, 8, 26)          # a Wednesday
    check("today", cal._day_label(today, today), "today")
    check("tomorrow", cal._day_label(today + datetime.timedelta(days=1), today),
          "tomorrow")
    check("inside the week, a weekday name",
          cal._day_label(today + datetime.timedelta(days=3), today), "on Saturday")
    check("beyond the week, a full date",
          cal._day_label(today + datetime.timedelta(days=10), today),
          "on Saturday, the 5th of September")
    check("a date is said aloud, not printed",
          cal._spoken_date(datetime.date(2026, 9, 3)), "the 3rd of September")

    print("\n--- which day did they mean ---")
    check("tomorrow", cal._parse_day("tomorrow", today), datetime.date(2026, 8, 27))
    check("an ISO date", cal._parse_day("2026-09-03", today),
          datetime.date(2026, 9, 3))
    check("'3 September'", cal._parse_day("3 september", today),
          datetime.date(2026, 9, 3))
    check("'September 3rd'", cal._parse_day("September 3rd", today),
          datetime.date(2026, 9, 3))
    check("'Sept 3'", cal._parse_day("sept 3", today), datetime.date(2026, 9, 3))
    check("a weekday means the NEXT one",
          cal._parse_day("friday", today), datetime.date(2026, 8, 28))
    check("'next monday' skips a week",
          cal._parse_day("next monday", today), datetime.date(2026, 9, 7))
    check("nonsense resolves to nothing rather than a guess",
          cal._parse_day("bananas", today), None)

    print("\n--- 'this week' ends on Friday (the weekend is Fri + Sat) ---")
    # _window reads the real today, so check the arithmetic directly instead.
    for name, day, expected_last in (
            ("Sunday", datetime.date(2026, 8, 23), datetime.date(2026, 8, 28)),
            ("Wednesday", datetime.date(2026, 8, 26), datetime.date(2026, 8, 28)),
            ("Friday", datetime.date(2026, 8, 28), datetime.date(2026, 8, 28)),
            ("Saturday", datetime.date(2026, 8, 29), datetime.date(2026, 9, 4)),
    ):
        ahead = (cal.WEEK_ENDS_ON - day.weekday()) % 7
        check(f"asked on {name}, the week runs to {expected_last}",
              day + datetime.timedelta(days=ahead), expected_last)

    print("\n--- speaking a list of events ---")
    def event(title, start_iso, all_day=False):
        key = "date" if all_day else "dateTime"
        return {"summary": title, "start": {key: start_iso}}

    today_str = cal.now().date().isoformat()
    check("nothing scheduled is a sentence, not an empty list",
          cal._describe([], "today", False, "You have nothing scheduled today."),
          "You have nothing scheduled today.")
    one = cal._describe([event("Dentist", f"{today_str}T14:30:00")],
                        "today", False, "empty")
    check("one event today",
          one, "Today you have Dentist at half past two this afternoon.")
    two = cal._describe([event("Standup", f"{today_str}T09:00:00"),
                         event("Dentist", f"{today_str}T14:30:00")],
                        "today", False, "empty")
    check("two events read as a list",
          two, "Today you have Standup at nine o'clock this morning and "
               "Dentist at half past two this afternoon.")
    check("an all-day event is not read as midnight",
          cal._describe([event("Eid holiday", today_str, all_day=True)],
                        "today", False, "empty"),
          "Today you have Eid holiday, all day.")

    many = [event(f"Thing {n}", f"{today_str}T0{n}:00:00") for n in range(1, 8)]
    spoken = cal._describe(many, "today", False, "empty")
    check("a long list stops at five and says how many are left",
          spoken.endswith("And 2 more after that."), True)
    check("and it really only names five",
          sum(1 for n in range(1, 8) if f"Thing {n}" in spoken), 5)


def live_checks() -> None:
    print("\n=== the real calendar (read-only) ===\n")
    print(f"  Time zone detected: {cal.timezone_name()}")
    print(f"  Right now:          {cal.get_current_time()}\n")

    for label, value in (
            ("today", cal.read_schedule("today")),
            ("tomorrow", cal.read_schedule("tomorrow")),
            ("this week", cal.read_schedule("this week")),
            ("a named date", cal.read_schedule("3 September")),
            ("next event", cal.next_event()),
            ("find 'meeting'", cal.find_event("meeting")),
            ("a day she can't read", cal.read_schedule("bananas")),
    ):
        print(f"  {label:>16}: {value}")

    print("\n  Read those aloud in your head. If any of them sound like a "
          "computer,\n  that is the bug.")


if __name__ == "__main__":
    print("=== Athena calendar, phase 1: reading ===")
    phrasing_checks()

    print(f"\n{PASSED} passed, {FAILED} failed.")

    if "--offline" not in sys.argv:
        live_checks()
    else:
        print("\n(Skipping the live calendar - run without --offline for that.)")

    sys.exit(1 if FAILED else 0)
