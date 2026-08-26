"""Prove the follow-up asks well, and stops asking.

    python followup_test.py           # the whole exchange, scripted, offline
    python followup_test.py --live    # + one real past event on your calendar

The offline half runs the entire conversation with a scripted user: speech
and listening are injected through set_io, the calendar is a fake, and the
completion store is a fake that records what it was told. Nothing needs a
microphone, a sign-in, or Supabase.

What it is really checking is the restraint. Asking is easy; the hard part
is one at a time, capped at three, stopping the moment someone moves on, and
not saying anything at all when they say no. Each of those is a check below.

The --live half creates a real event in the past hour, lets pending_followups
find it, then deletes it. It leaves nothing behind.
"""

import datetime
import sys

from athena import calendar_skill as cal
from athena import followup

PASSED = 0
FAILED = 0

BASE = datetime.datetime(2026, 8, 26, 13, 15, tzinfo=cal.local_zone())


def check(label: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


def past_event(title, hours_ago, event_id=None, minutes=60, all_day=False,
               declined=False):
    start = BASE - datetime.timedelta(hours=hours_ago)
    event = {"id": event_id or f"id-{title}", "summary": title}
    if all_day:
        event["start"] = {"date": start.date().isoformat()}
        event["end"] = {"date": start.date().isoformat()}
    else:
        event["start"] = {"dateTime": start.isoformat()}
        event["end"] = {"dateTime":
                        (start + datetime.timedelta(minutes=minutes)).isoformat()}
    if declined:
        event["attendees"] = [{"self": True, "responseStatus": "declined"}]
    return event


class FakeStore:
    """Stands in for event_status. Records what it was told, and can pretend
    to be an unreachable Supabase by holding nothing."""

    def __init__(self, already=None, available=True):
        self.written = {}
        self.already = set(already or [])
        self.available = available

    def record_async(self, event_id, status):
        self.written[event_id] = status

    def record(self, event_id, status):
        self.written[event_id] = status
        return True

    def recorded_ids(self, ids):
        if not self.available:
            return set()
        return {i for i in ids if i in self.already}


class Conversation:
    """Points followup at a fake calendar, a fake store, and a scripted user.

    `answers` is what the user says, in order. Everything Athena says is
    collected in `said`, so a check can assert on the exact words - including
    asserting that she said NOTHING."""

    def __init__(self, events, answers, already=None, store_available=True,
                 moves=True):
        self.events = events
        self.answers = list(answers)
        self.said = []
        self.store = FakeStore(already, store_available)
        self.moves = moves          # does the reschedule succeed?
        self.reschedules = []

    def __enter__(self):
        self._real = {
            "load": cal._load, "now": cal.now, "store": followup.event_status,
            "reschedule": cal.reschedule_event,
        }
        cal._load = lambda *a, **k: (list(self.events), "")
        cal.now = lambda: BASE
        followup.event_status = self.store
        cal.reschedule_event = self._reschedule
        followup.set_io(speak_fn=self.said.append, listen_fn=self._answer)
        followup.reset_session()
        return self

    def _answer(self):
        return self.answers.pop(0) if self.answers else ""

    def _reschedule(self, identifier, when, new_end=None, event=None):
        self.reschedules.append((identifier, when, (event or {}).get("id")))
        if not self.moves:
            return "I couldn't move that event: the network is down."
        moment, _ = cal.resolve_when(when, base=BASE)
        return f"Moved - {identifier} is now {cal._spoken_moment(moment)}."

    def __exit__(self, *exc):
        cal._load = self._real["load"]
        cal.now = self._real["now"]
        cal.reschedule_event = self._real["reschedule"]
        followup.event_status = self._real["store"]
        followup.set_io(None, None)
        followup.reset_session()

    @property
    def transcript(self):
        return " | ".join(self.said)


def pending_checks() -> None:
    print("\n--- what counts as outstanding ---")
    events = [past_event("Dentist", hours_ago=4, event_id="a"),
              past_event("Standup", hours_ago=28, event_id="b")]
    with Conversation(events, []) as c:
        pending = followup.pending_followups()
        check("past events with no answer are outstanding", len(pending), 2)
        check("oldest first, so the stalest question is asked first",
              [e["id"] for e in pending], ["b", "a"])

    with Conversation(events, [], already=["a"]) as c:
        check("an event already answered for is never asked about again",
              [e["id"] for e in followup.pending_followups()], ["b"])

    with Conversation([past_event("Eid holiday", 30, all_day=True)], []) as c:
        check("all-day events are left alone", followup.pending_followups(), [])

    with Conversation([past_event("Party", 5, declined=True)], []) as c:
        check("an invitation you declined is not a question",
              followup.pending_followups(), [])

    running = past_event("Workshop", hours_ago=1, minutes=180)
    with Conversation([running], []) as c:
        check("a meeting still in progress is not asked about",
              followup.pending_followups(), [])

    with Conversation([], []) as c:
        check("nothing past means nothing outstanding",
              followup.pending_followups(), [])

    broken_now = cal.now
    real_load = cal._load
    cal.now, cal._load = (lambda: BASE), (lambda *a, **k: ([], "no network"))
    try:
        check("no calendar means no follow-ups, not an exception",
              followup.pending_followups(), [])
    finally:
        cal.now, cal._load = broken_now, real_load

    with Conversation(events, [], store_available=False) as c:
        check("an unreachable store leaves everything looking unanswered",
              len(followup.pending_followups()), 2)


def exchange_checks() -> None:
    print("\n--- yes: one short warm sentence, and it's recorded ---")
    with Conversation([past_event("Dentist", 4, event_id="a")], ["yes"]) as c:
        followup.run()
        check("she names the event and its time", c.said[0],
              "You had Dentist at quarter past nine this morning. "
              "Did you get to it?")
        check("done is recorded", c.store.written, {"a": "done"})
        check("the acknowledgement is one sentence", len(c.said), 2)
        check("and it is one of the warm ones",
              c.said[1] in followup.ACKNOWLEDGEMENTS, True)

    print("\n--- and the wording varies ---")
    many = [past_event(f"Thing {n}", 4 + n, event_id=str(n)) for n in range(3)]
    with Conversation(many, ["yes", "yes", "yes"]) as c:
        followup.run()
        acks = [line for line in c.said if line in followup.ACKNOWLEDGEMENTS]
        check("three yeses get three acknowledgements", len(acks), 3)
        check("...and no two are the same", len(set(acks)), 3)

    print("\n--- no, and no thanks: recorded, and NOTHING is said ---")
    with Conversation([past_event("Gym", 4, event_id="a")], ["no", "no"]) as c:
        said = followup.run()
        check("skipped is recorded", c.store.written, {"a": "skipped"})
        check("she asked about rescheduling once", c.said[1],
              "Do you want to reschedule it?")
        check("and then said nothing about it at all", len(c.said), 2)
        check("no consolation, no encouragement",
              any(word in c.transcript.lower()
                  for word in ("okay", "fine", "no problem", "don't worry")),
              False)
        check("the summary doesn't editorialise either", said,
              "That's everything caught up.")

    print("\n--- no, but move it: resolved, confirmed, recorded ---")
    with Conversation([past_event("Dentist", 4, event_id="a")],
                      ["no", "yes", "tomorrow at 3"]) as c:
        followup.run()
        check("she asks when", c.said[2], "When should I move it to?")
        check("the move goes through the gated reschedule",
              c.reschedules, [("Dentist", "tomorrow at 3", "a")])
        check("it moves THAT event, not one matched by name",
              c.reschedules[0][2], "a")
        check("rescheduled is recorded", c.store.written, {"a": "rescheduled"})

    print("\n--- a time she can't read gets one more chance, not a loop ---")
    with Conversation([past_event("Dentist", 4, event_id="a")],
                      ["no", "yes", "bananas", "tomorrow at 3"]) as c:
        followup.run()
        check("she says what she couldn't read",
              c.said[3].startswith("I couldn't work out a time"), True)
        check("and the second answer is used", c.reschedules[0][1],
              "tomorrow at 3")

    with Conversation([past_event("Dentist", 4, event_id="a")],
                      ["no", "yes", "bananas", "also bananas"]) as c:
        followup.run()
        check("two failures deferred rather than asked a third time",
              c.store.written, {"a": "deferred"})
        check("and nothing was moved", c.reschedules, [])

    print("\n--- a move that fails is never recorded as a move ---")
    with Conversation([past_event("Dentist", 4, event_id="a")],
                      ["no", "yes", "tomorrow at 3"], moves=False) as c:
        followup.run()
        check("nothing is recorded when the write didn't land",
              c.store.written, {})
        check("and the failure is spoken, once",
              c.said[-1], "I couldn't move that event: the network is down.")


def restraint_checks() -> None:
    print("\n--- silence: deferred, dropped, not chased ---")
    two = [past_event("Dentist", 4, event_id="a"),
           past_event("Gym", 5, event_id="b")]
    with Conversation(two, [""]) as c:
        said = followup.run()
        check("silence records deferred", c.store.written, {"b": "deferred"})
        check("she asked about exactly one thing",
              sum(1 for line in c.said if "Did you get to it?" in line), 1)
        check("and said nothing else", said, "")

    print("\n--- a changed subject is not an answer ---")
    with Conversation(two, ["actually what's the weather like"]) as c:
        followup.run()
        check("it's recorded as deferred, not guessed at",
              c.store.written, {"b": "deferred"})
        check("and the second event is never raised", len(c.said), 1)

    check("'I didn't' is a no, not a yes hiding a 'did'",
          followup._yes_or_no("I didn't get round to it"), False)
    check("'yes I did' is a yes", followup._yes_or_no("yes I did"), True)
    check("anything else is neither",
          followup._yes_or_no("what's the weather like"), None)
    check("and so is silence", followup._yes_or_no(""), None)

    print("\n--- three questions a session, no more ---")
    five = [past_event(f"Thing {n}", 4 + n, event_id=str(n)) for n in range(5)]
    with Conversation(five, ["yes"] * 5) as c:
        said = followup.run()
        asked = sum(1 for line in c.said if "Did you get to it?" in line)
        check("five outstanding, three asked", asked, 3)
        check("and it says how many are left, without nagging", said,
              "That's those. 2 more whenever you want them.")

    with Conversation(five, ["yes"] * 5) as c:
        followup.run()
        again = followup.run()
        check("asking again in the same session doesn't reopen the cap",
              again, "I've asked enough for one session - I'll leave the rest.")

    print("\n--- nothing to ask about ---")
    with Conversation([], []) as c:
        check("she says so plainly", followup.run(),
              "Nothing's outstanding - you're all caught up.")
        check("and asked nothing", c.said, [])

    print("\n--- the startup gate ---")
    from athena import settings
    real_get = settings.get
    try:
        settings.get = lambda key, default=None: (
            False if key == "followup_on_startup" else real_get(key, default))
        with Conversation(two, ["yes", "yes"]) as c:
            check("FOLLOWUP_ON_STARTUP off means startup asks nothing",
                  followup.on_startup(), "")
            check("and really nothing was said", c.said, [])
        settings.get = lambda key, default=None: (
            True if key == "followup_on_startup" else real_get(key, default))
        with Conversation(two, ["yes", "yes"]) as c:
            followup.on_startup()
            check("on, it asks", len(c.reschedules) == 0 and len(c.said) > 0,
                  True)
        with Conversation([], []) as c:
            check("but with nothing outstanding it stays silent at startup",
                  followup.on_startup(), "")
    finally:
        settings.get = real_get

    print("\n--- there is no timer in this module ---")
    source = open("athena/followup.py", encoding="utf-8").read()
    for banned in ("threading.Timer", "sched.", "time.sleep", "schedule."):
        check(f"no {banned}", banned in source, False)


def live_checks() -> None:
    print("\n=== a real past event ===\n")
    service, problem = cal._service()
    if service is None:
        print(f"  Skipped: {problem}")
        return

    title = "Athena followup test"
    finish = cal.now() - datetime.timedelta(minutes=5)
    start = finish - datetime.timedelta(minutes=30)
    created = None
    try:
        body = {"summary": title,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": finish.isoformat()}}
        created = service.events().insert(
            calendarId=cal.CALENDAR_ID, body=body).execute()
        print(f"  created a past event: {title}")

        pending = followup.pending_followups()
        ids = [e.get("id") for e in pending]
        check("the real past event shows up as outstanding",
              created["id"] in ids, True)
        for event in pending:
            if event["id"] == created["id"]:
                print(f"  she would ask: You had {cal._title(event)} "
                      f"{cal._spoken_moment(cal._starts_at(event)[0])}. "
                      "Did you get to it?")
    finally:
        if created:
            try:
                service.events().delete(calendarId=cal.CALENDAR_ID,
                                        eventId=created["id"]).execute()
                print("  cleaned up.")
            except Exception as exc:
                print(f"  COULD NOT CLEAN UP - delete {title} by hand: {exc}")


if __name__ == "__main__":
    print("=== Athena calendar, phase 4: the follow-up ===")
    pending_checks()
    exchange_checks()
    restraint_checks()

    if "--live" in sys.argv:
        live_checks()

    print(f"\n{PASSED} passed, {FAILED} failed.")
    sys.exit(1 if FAILED else 0)
