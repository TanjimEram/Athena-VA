"""Prove the calendar writes only what you agreed to.

    python calendar_write_test.py           # offline: resolution + the gate
    python calendar_write_test.py --live    # + creates, moves and deletes ONE
                                            #   real event on your calendar

The offline half is the important one and needs nothing - no sign-in, no
network, no calendar. It checks two things:

  1. "tomorrow at 3" resolves to the right moment, in OUR code. Neither
     dateutil nor dateparser gets these right (see calendar_skill's
     docstring for the measured comparison), which is why we own it.
  2. Nothing is ever written without a yes. Every write is run against a
     fake calendar that records what it was asked to do, so a write that
     escaped its gate would show up here as a recorded call.

The --live half creates one throwaway event called "Athena test event",
moves it, then deletes it. It cleans up after itself even if a check fails.
It needs the calendar permission - if you haven't re-consented since the
scope was added, run  python google_auth_test.py --reset  first.
"""

import datetime
import sys

from athena import calendar_skill as cal

PASSED = 0
FAILED = 0

# A Wednesday, quarter past one in the afternoon. Every offline check reads
# from here, so none of them depend on when you happen to run this.
BASE = datetime.datetime(2026, 8, 26, 13, 15, tzinfo=cal.local_zone())


def check(label: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


def resolves_to(phrase: str, expected: str, base=BASE) -> None:
    """expected is 'Thu 2026-08-27 15:00', or 'ASK' if it must ask instead."""
    moment, problem = cal.resolve_when(phrase, base=base)
    got = moment.strftime("%a %Y-%m-%d %H:%M") if moment else "ASK"
    check(f"{phrase!r}", got, expected)


# --- a fake calendar, so the gate can be tested with nothing switched on ----

class FakeCalendar:
    """Stands in for the Google client. Records every write it is asked to
    make, which is what lets a check assert that NOTHING was written."""

    def __init__(self, existing=None):
        self.existing = existing or []
        self.written = []          # every insert/patch/delete that got through

    # the events().list(...).execute() shape
    def events(self):
        return self

    def list(self, **kwargs):
        self._call = ("list", kwargs)
        return self

    def insert(self, **kwargs):
        self._call = ("insert", kwargs)
        return self

    def patch(self, **kwargs):
        self._call = ("patch", kwargs)
        return self

    def delete(self, **kwargs):
        self._call = ("delete", kwargs)
        return self

    def execute(self):
        kind, kwargs = self._call
        if kind == "list":
            return {"items": self.existing}
        self.written.append((kind, kwargs))
        return {"id": "fake-id"}


def event(title, start, minutes=60, event_id=None):
    finish = start + datetime.timedelta(minutes=minutes)
    return {"id": event_id or f"id-{title}", "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": finish.isoformat()}}


class Wired:
    """Point calendar_skill at a fake calendar and a scripted yes/no, then
    put everything back afterwards."""

    def __init__(self, answer, existing=None):
        self.answer = answer          # True, False, or None for "no hook"
        self.fake = FakeCalendar(existing)
        self.asked = []

    def __enter__(self):
        self._real_service = cal._service
        self._real_now = cal.now
        cal._service = lambda: (self.fake, "")
        cal.now = lambda: BASE
        if self.answer is None:
            cal.set_confirm(None)
        else:
            cal.set_confirm(self._record)
        return self

    def _record(self, question):
        self.asked.append(question)
        return self.answer

    def __exit__(self, *exc):
        cal._service = self._real_service
        cal.now = self._real_now
        cal.set_confirm(None)

    @property
    def question(self):
        return self.asked[0] if self.asked else ""


def resolution_checks() -> None:
    print("\n--- our code resolves the time, not the model ---")
    resolves_to("tomorrow at 3", "Thu 2026-08-27 15:00")
    resolves_to("next Monday morning", "Mon 2026-09-07 09:00")
    resolves_to("in two hours", "Wed 2026-08-26 15:15")
    resolves_to("Friday at 7pm", "Fri 2026-08-28 19:00")
    resolves_to("tomorrow at 9am", "Thu 2026-08-27 09:00")
    resolves_to("tomorrow at 3:30pm", "Thu 2026-08-27 15:30")
    resolves_to("half past two tomorrow", "Thu 2026-08-27 14:30")
    resolves_to("quarter to six on friday", "Fri 2026-08-28 17:45")
    resolves_to("friday at 7 in the evening", "Fri 2026-08-28 19:00")
    resolves_to("3 September at 10", "Thu 2026-09-03 10:00")
    resolves_to("in 30 minutes", "Wed 2026-08-26 13:45")
    resolves_to("in half an hour", "Wed 2026-08-26 13:45")
    resolves_to("in 3 days", "Sat 2026-08-29 13:15")
    resolves_to("noon tomorrow", "Thu 2026-08-27 12:00")
    resolves_to("midnight", "Thu 2026-08-27 00:00")
    resolves_to("tomorrow evening", "Thu 2026-08-27 19:00")

    print("\n--- the one guess it makes, and makes on purpose ---")
    resolves_to("at 3", "Wed 2026-08-26 15:00")          # 1-6 is the afternoon
    resolves_to("at 9", "Thu 2026-08-27 09:00")          # 7-11 is the morning
    check("a bare hour is read back so a wrong guess is heard first",
          cal._spoken_moment(cal.resolve_when("at 3", base=BASE)[0]),
          "at three o'clock this afternoon")

    print("\n--- a time that already passed today means tomorrow ---")
    resolves_to("at 9am", "Thu 2026-08-27 09:00")        # 9am was four hours ago
    check("but an explicitly named past day is NOT quietly corrected",
          cal.resolve_when("yesterday at 9am", base=BASE)[0].date(),
          datetime.date(2026, 8, 25))

    print("\n--- it asks rather than guessing ---")
    resolves_to("next monday", "ASK")                    # which time?
    resolves_to("tomorrow", "ASK")
    resolves_to("bananas", "ASK")
    resolves_to("", "ASK")
    check("a day with no time asks about that day by name",
          cal.resolve_when("tomorrow", base=BASE)[1], "What time tomorrow?")

    print("\n--- how long it runs ---")
    start = datetime.datetime(2026, 8, 27, 15, 0, tzinfo=cal.local_zone())
    check("an end time on the same day",
          cal._resolve_end("5pm", start)[0].strftime("%H:%M"), "17:00")
    check("a duration - 'for two hours'",
          cal._resolve_end("for two hours", start)[0].strftime("%H:%M"), "17:00")
    check("a duration - '90 minutes'",
          cal._resolve_end("90 minutes", start)[0].strftime("%H:%M"), "16:30")
    check("an end is never rolled to the next day",
          cal._resolve_end("2pm", start)[0].date(), datetime.date(2026, 8, 27))


def gate_checks() -> None:
    print("\n--- nothing is written without a yes ---")
    with Wired(answer=True) as w:
        said = cal.create_event("Dentist", "tomorrow at 3")
        check("the question names the title AND the resolved time",
              w.question, "Create Dentist tomorrow at three o'clock in the "
                          "afternoon?")
        check("a yes really creates it", len(w.fake.written), 1)
        check("and says so honestly", said,
              "Done - Dentist is in your calendar tomorrow at three o'clock "
              "in the afternoon.")
        kind, kwargs = w.fake.written[0]
        body = kwargs["body"]
        check("it books the resolved time, not the words it was given",
              body["start"]["dateTime"][:16], "2026-08-27T15:00")
        check("one hour by default",
              body["end"]["dateTime"][:16], "2026-08-27T16:00")

    with Wired(answer=False) as w:
        said = cal.create_event("Dentist", "tomorrow at 3")
        check("a no writes NOTHING", w.fake.written, [])
        check("and admits it", said, "I haven't added Dentist.")

    with Wired(answer=None) as w:
        said = cal.create_event("Dentist", "tomorrow at 3")
        check("no confirm hook wired means no write", w.fake.written, [])
        check("and it says the calendar was left alone",
              said.endswith("I've left your calendar alone."), True)

    with Wired(answer=True) as w:
        cal.create_event("Lunch", "tomorrow at 1", end="for two hours")
        check("a non-default length is read back too",
              w.question, "Create Lunch tomorrow at one o'clock in the "
                          "afternoon, for two hours?")

    print("\n--- a clash is asked about, not discovered later ---")
    busy = [event("Standup", datetime.datetime(2026, 8, 27, 15, 0,
                                               tzinfo=cal.local_zone()))]
    with Wired(answer=False, existing=busy) as w:
        cal.create_event("Dentist", "tomorrow at 3")
        check("the clash is named in the question",
              w.question, "You already have Standup at three o'clock then. "
                          "Shall I add Dentist tomorrow at three o'clock in "
                          "the afternoon anyway?")
        check("and saying no leaves the calendar alone", w.fake.written, [])

    with Wired(answer=True, existing=busy) as w:
        cal.create_event("Dentist", "tomorrow at 3")
        check("a deliberate double-booking is allowed once it's agreed",
              len(w.fake.written), 1)

    free = [dict(event("Eid holiday",
                       datetime.datetime(2026, 8, 27, 0, 0,
                                         tzinfo=cal.local_zone())),
                 start={"date": "2026-08-27"}, end={"date": "2026-08-28"})]
    with Wired(answer=True, existing=free) as w:
        cal.create_event("Dentist", "tomorrow at 3")
        check("an all-day event doesn't count as a clash",
              w.question.startswith("Create Dentist"), True)

    print("\n--- moving an event ---")
    existing = [event("Dentist", datetime.datetime(2026, 8, 27, 15, 0,
                                                   tzinfo=cal.local_zone()),
                      minutes=120)]
    with Wired(answer=True, existing=existing) as w:
        said = cal.reschedule_event("Dentist", "Friday at 10am")
        check("the move is read back both ways round",
              w.question, "Move Dentist from tomorrow at three o'clock in the "
                          "afternoon to on Friday at ten o'clock in the "
                          "morning?")
        kind, kwargs = w.fake.written[0]
        check("it patches rather than recreating", kind, "patch")
        check("it keeps the length the event already had",
              (datetime.datetime.fromisoformat(kwargs["body"]["end"]["dateTime"])
               - datetime.datetime.fromisoformat(
                   kwargs["body"]["start"]["dateTime"])),
              datetime.timedelta(hours=2))
        check("and reports the new time", said,
              "Moved - Dentist is now on Friday at ten o'clock in the morning.")

    with Wired(answer=False, existing=existing) as w:
        said = cal.reschedule_event("Dentist", "Friday at 10am")
        check("a no moves nothing", w.fake.written, [])
        check("and says where it still is", said,
              "I've left Dentist where it was.")

    print("\n--- cancelling ---")
    with Wired(answer=True, existing=existing) as w:
        said = cal.cancel_event("Dentist")
        check("cancelling reads the event back first",
              w.question, "Cancel Dentist tomorrow at three o'clock in the "
                          "afternoon?")
        check("a yes deletes it", w.fake.written[0][0], "delete")
        check("and confirms what went", said,
              "Cancelled - Dentist tomorrow at three o'clock in the afternoon "
              "is off your calendar.")

    with Wired(answer=False, existing=existing) as w:
        cal.cancel_event("Dentist")
        check("a no deletes nothing", w.fake.written, [])

    print("\n--- which one did you mean ---")
    two = [event("Review", datetime.datetime(2026, 8, 27, 15, 0,
                                             tzinfo=cal.local_zone()),
                 event_id="a"),
           event("Review", datetime.datetime(2026, 8, 28, 10, 0,
                                             tzinfo=cal.local_zone()),
                 event_id="b")]
    with Wired(answer=True, existing=two) as w:
        said = cal.cancel_event("Review")
        check("two matches means a question, never a pick", w.fake.written, [])
        check("and the candidates are read back", said,
              "There are 2 that match: Review tomorrow at three o'clock in "
              "the afternoon and Review on Friday at ten o'clock in the "
              "morning. Which one did you mean?")

    with Wired(answer=True, existing=two) as w:
        said = cal.cancel_event("Nothing Like This")
        check("no match is said plainly", said,
              "I couldn't find anything called Nothing Like This on your "
              "calendar.")
        check("and nothing is deleted on a miss", w.fake.written, [])

    print("\n--- a failed write is never reported as success ---")
    class Broken(FakeCalendar):
        def execute(self):
            kind, _ = self._call
            if kind == "list":
                return {"items": []}
            raise RuntimeError("network is unreachable")

    real_service, real_now = cal._service, cal.now
    broken = Broken()
    cal._service = lambda: (broken, "")
    cal.now = lambda: BASE
    cal.set_confirm(lambda q: True)
    try:
        said = cal.create_event("Dentist", "tomorrow at 3")
        check("a write that blew up says so",
              "didn't add that to your calendar" in said
              or said.startswith("I couldn't"), True)
        check("and never claims it worked", "Done -" in said, False)
    finally:
        cal._service, cal.now = real_service, real_now
        cal.set_confirm(None)


def live_checks() -> None:
    print("\n=== the real calendar (creates and deletes ONE event) ===\n")
    service, problem = cal._service()
    if service is None:
        print(f"  Skipped: {problem}")
        return

    title = "Athena test event"
    cal.set_confirm(lambda question: (print(f"    asked: {question}"), True)[1])
    created = False
    try:
        said = cal.create_event(title, "tomorrow at 4pm")
        print(f"  create:     {said}")
        created = said.startswith("Done -")
        check("the event was really created", created, True)

        if created:
            print(f"  read back:  {cal.find_event(title)}")
            said = cal.reschedule_event(title, "tomorrow at 5pm")
            print(f"  reschedule: {said}")
            check("the move really happened", said.startswith("Moved -"), True)
    finally:
        if created:
            said = cal.cancel_event(title)
            print(f"  cancel:     {said}")
            check("and it cleaned up after itself",
                  said.startswith("Cancelled -"), True)
        cal.set_confirm(None)


if __name__ == "__main__":
    print("=== Athena calendar, phase 2: creating and rescheduling ===")
    resolution_checks()
    gate_checks()
    print(f"\n{PASSED} passed, {FAILED} failed.")

    if "--live" in sys.argv:
        live_checks()
        print(f"\n{PASSED} passed, {FAILED} failed (including live).")
    else:
        print("(No real calendar was touched. Use --live for that.)")

    sys.exit(1 if FAILED else 0)
