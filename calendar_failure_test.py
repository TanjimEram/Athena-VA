"""Prove Athena survives the calendar being broken, in every way it breaks.

    python calendar_failure_test.py

Nothing here touches the network. Every failure is induced deliberately, and
every public calendar function is put through every one of them. Three things
are asserted each time:

  1. It returns a sentence. It does not raise. A skill that raises reaches
     the assistant loop, and the loop is the one thing that must never stop.
  2. The sentence is speakable - one or two short lines, no stack trace, no
     exception class name, nothing a person would not say out loud.
  3. It never claims to have done something it did not do. This is the one
     that matters: a write reported as a success the user then relies on is
     worse than any crash, because nothing tells them.

The failure modes are the real ones, in the shapes Google actually sends
them: not configured, signed in without the calendar permission, the API
switched off in the Cloud Console, 403, 429, a dropped connection, a
timeout, and a response with the fields in the wrong shape.
"""

import datetime
import json
import sys

from athena import calendar_skill as cal
from athena import event_status, followup, skills

PASSED = 0
FAILED = 0

BASE = datetime.datetime(2026, 8, 26, 13, 15, tzinfo=cal.local_zone())

# Words that mean a skill claimed to have changed something.
SUCCESS_WORDS = ("Done -", "Moved -", "Cancelled -", "is in your calendar")


def check(label: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


def speakable(text) -> bool:
    """Is this something Athena could say out loud without embarrassment?"""
    if not isinstance(text, str) or not text.strip():
        return False
    if "\n" in text or len(text) > 220:
        return False
    for giveaway in ("Traceback", "<class", "Exception", "Error:", "None",
                     "{", "}", "self.", "0x"):
        if giveaway in text:
            return False
    return text.rstrip()[-1] in ".?!"


class GoogleError(Exception):
    """Shaped like googleapiclient's HttpError: a JSON body on .content,
    which is what google_auth._short reads to find the human message."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.content = json.dumps(
            {"error": {"code": status, "message": message}}).encode()


# Every way this can go wrong, in the wording Google really uses.
FAILURES = {
    "the API is switched off in the Cloud Console": GoogleError(
        "Google Calendar API has not been used in project 874924012270 before "
        "or it is disabled.", 403),
    "the permission was never granted": GoogleError(
        "Request had insufficient authentication scopes.", 403),
    "the token was revoked": GoogleError("Invalid Credentials", 401),
    "too many requests": GoogleError(
        "Rate Limit Exceeded", 429),
    "the connection dropped": OSError(
        "[Errno 11001] getaddrinfo failed"),
    "it timed out": TimeoutError("The read operation timed out"),
    "something nobody predicted": RuntimeError("kaboom"),
}


class Broken:
    """A calendar client that fails whichever way you ask it to."""

    def __init__(self, exc, fail_on=("list", "insert", "patch", "delete")):
        self.exc = exc
        self.fail_on = fail_on
        self.existing = []

    def events(self):
        return self

    def list(self, **kw):
        self._call = "list"
        return self

    def insert(self, **kw):
        self._call = "insert"
        return self

    def patch(self, **kw):
        self._call = "patch"
        return self

    def delete(self, **kw):
        self._call = "delete"
        return self

    def execute(self):
        if self._call in self.fail_on:
            raise self.exc
        return {"items": self.existing}


class Wired:
    def __init__(self, service=None, message="", confirm=True):
        self.service, self.message, self.confirm = service, message, confirm

    def __enter__(self):
        self._real = (cal._service, cal.now, cal._confirm)
        cal._service = lambda: (self.service, self.message)
        cal.now = lambda: BASE
        cal.set_confirm((lambda q: self.confirm) if self.confirm is not None
                        else None)
        return self

    def __exit__(self, *exc):
        cal._service, cal.now = self._real[0], self._real[1]
        cal.set_confirm(self._real[2])


CALLS = (
    ("read_schedule", lambda: cal.read_schedule("today")),
    ("read_schedule this week", lambda: cal.read_schedule("this week")),
    ("next_event", cal.next_event),
    ("find_event", lambda: cal.find_event("dentist")),
    ("create_event", lambda: cal.create_event("Dentist", "tomorrow at 3")),
    ("reschedule_event", lambda: cal.reschedule_event("Dentist", "Friday at 3")),
    ("cancel_event", lambda: cal.cancel_event("Dentist")),
)


def every_failure_checks() -> None:
    for description, exc in FAILURES.items():
        print(f"\n--- {description} ---")
        broken = Broken(exc)
        with Wired(service=broken):
            worst = []
            for name, call in CALLS:
                try:
                    said = call()
                except Exception as raised:
                    worst.append(f"{name} RAISED {type(raised).__name__}")
                    continue
                if not speakable(said):
                    worst.append(f"{name} said {said!r}")
                elif any(word in said for word in SUCCESS_WORDS):
                    worst.append(f"{name} CLAIMED SUCCESS: {said!r}")
            check("every skill answers in a speakable sentence", worst, [])
        # Show one, so the wording is reviewable rather than merely asserted.
        with Wired(service=broken):
            print(f"         she says: {cal.read_schedule('today')}")


def not_connected_checks() -> None:
    print("\n--- not signed in at all ---")
    from athena import google_auth
    with Wired(service=None, message=google_auth.not_connected_message()):
        for name, call in CALLS:
            said = call()
            check(f"{name} explains itself", speakable(said), True)
            check(f"{name} claims nothing",
                  any(w in said for w in SUCCESS_WORDS), False)

    print("\n--- signed in, but not for the calendar ---")
    message = ("I'm signed in to your Google account, but not yet for your "
               "calendar. Run the Google sign-in once more and I'll be able "
               "to see it.")
    with Wired(service=None, message=message):
        check("it says which permission is missing, not 'not signed in'",
              cal.read_schedule("today"), message)
        check("and a write refuses the same way",
              cal.create_event("Dentist", "tomorrow at 3"), message)


def no_false_success_checks() -> None:
    print("\n--- a write that fails is never a success ---")
    existing = [{"id": "a", "summary": "Dentist",
                 "start": {"dateTime": (BASE + datetime.timedelta(hours=2)).isoformat()},
                 "end": {"dateTime": (BASE + datetime.timedelta(hours=3)).isoformat()}}]

    for description, exc in FAILURES.items():
        # Reads work, the WRITE fails - the nastiest case, because everything
        # up to the last moment succeeded and the user already said yes.
        broken = Broken(exc, fail_on=("insert", "patch", "delete"))
        broken.existing = existing
        with Wired(service=broken, confirm=True):
            created = cal.create_event("Lunch", "tomorrow at 1")
            moved = cal.reschedule_event("Dentist", "Friday at 3")
            gone = cal.cancel_event("Dentist")
        claimed = [s for s in (created, moved, gone)
                   if any(w in s for w in SUCCESS_WORDS)]
        check(f"{description}: nothing is reported as done", claimed, [])
        unspeakable = [s for s in (created, moved, gone) if not speakable(s)]
        check(f"{description}: and each says so plainly", unspeakable, [])

    print("\n  For the record, what she says when a booking fails mid-write:")
    broken = Broken(FAILURES["the connection dropped"],
                    fail_on=("insert", "patch", "delete"))
    with Wired(service=broken, confirm=True):
        print(f"    {cal.create_event('Lunch', 'tomorrow at 1')}")


def clock_still_works_checks() -> None:
    print("\n--- the parts that need nothing keep working ---")
    broken = Broken(FAILURES["the connection dropped"])
    with Wired(service=broken):
        said = cal.get_current_time()
        check("she can still tell the time with the calendar down",
              speakable(said) and said.startswith("It's"), True)
        check("and still says which day she can't read",
              cal.read_schedule("bananas").startswith("I'm not sure"), True)
    check("resolving a time needs no network at all",
          cal.resolve_when("tomorrow at 3", base=BASE)[0].hour, 15)


def followup_checks() -> None:
    print("\n--- the follow-up, with everything broken ---")
    broken = Broken(FAILURES["the connection dropped"])
    with Wired(service=broken):
        check("no calendar means nothing to follow up on",
              followup.pending_followups(), [])
        spoken = []
        followup.set_io(speak_fn=spoken.append, listen_fn=lambda: "")
        try:
            said = followup.run()
            check("she doesn't invent a follow-up out of a failure",
                  said, "Nothing's outstanding - you're all caught up.")
            check("and asked nothing", spoken, [])
            check("startup stays silent", followup.on_startup(), "")
        finally:
            followup.set_io(None, None)
            followup.reset_session()

    print("\n--- and with the store broken but the calendar fine ---")
    past = [{"id": "p", "summary": "Dentist",
             "start": {"dateTime": (BASE - datetime.timedelta(hours=4)).isoformat()},
             "end": {"dateTime": (BASE - datetime.timedelta(hours=3)).isoformat()}}]
    real_load, real_now, real_store = cal._load, cal.now, followup.event_status

    class DeadStore:
        def recorded_ids(self, ids):
            return set()

        def record_async(self, event_id, status):
            raise RuntimeError("supabase is gone")

    cal._load = lambda *a, **k: (list(past), "")
    cal.now = lambda: BASE
    followup.event_status = DeadStore()
    spoken = []
    followup.set_io(speak_fn=spoken.append, listen_fn=lambda: "yes")
    try:
        said = followup.run()
        check("she still asks, and the answer being unsaveable doesn't stop her",
              any("Did you get to it?" in line for line in spoken), True)
        check("and she still acknowledges it",
              spoken[-1] in followup.ACKNOWLEDGEMENTS, True)
        check("the summary is still a sentence", speakable(said), True)
    except Exception as exc:
        check(f"a dead store must not raise ({type(exc).__name__}: {exc})",
              False, True)
    finally:
        cal._load, cal.now = real_load, real_now
        followup.event_status = real_store
        followup.set_io(None, None)
        followup.reset_session()

    print("\n--- and the store on its own ---")
    real_client = event_status.memory.shared_client
    event_status.memory.shared_client = lambda: None
    event_status._warned = True
    try:
        check("record returns False rather than pretending",
              event_status.record("x", "done"), False)
        check("recorded_ids is empty", event_status.recorded_ids(["x"]), set())
    finally:
        event_status.memory.shared_client = real_client


def loop_survives_checks() -> None:
    print("\n--- the assistant loop survives all of it ---")
    from athena import brain

    class Exploding:
        def events(self):
            raise RuntimeError("the whole client is broken")

    real_service = cal._service
    cal._service = lambda: (Exploding(), "")
    try:
        for name in ("read_schedule", "next_event", "find_event",
                     "create_event", "cancel_event", "pending_followups",
                     "get_current_time"):
            args = {}
            if name == "read_schedule":
                args = {"when": "today"}
            elif name == "find_event":
                args = {"query": "x"}
            elif name == "create_event":
                args = {"title": "X", "start": "tomorrow at 3"}
            elif name == "cancel_event":
                args = {"event_identifier": "X"}
            try:
                said = skills.SKILLS[name](**args)
                check(f"skills.{name} returns rather than raising",
                      isinstance(said, str), True)
            except Exception as exc:
                check(f"skills.{name} raised {type(exc).__name__}: {exc}",
                      False, True)
    finally:
        cal._service = real_service

    print("\n--- and main.py's startup call is wrapped ---")
    source = open("athena/main.py", encoding="utf-8").read()
    start = source.index("followup.on_startup()")
    window = source[max(0, start - 400):start + 200]
    check("on_startup sits inside a try", "try:" in window, True)
    check("...with an except that carries on",
          "except Exception" in window and "carrying on" in window, True)


def malformed_checks() -> None:
    print("\n--- Google sends something in the wrong shape ---")
    junk = [
        {},
        {"summary": "No start at all"},
        {"id": "x", "summary": "Junk time", "start": {"dateTime": "not-a-time"}},
        {"id": "y", "summary": "Junk date", "start": {"date": "2026-13-45"}},
        {"id": "z", "summary": "Fine", "start": {"dateTime": BASE.isoformat()},
         "end": {"dateTime": "also-not-a-time"}},
    ]
    real_load, real_now = cal._load, cal.now
    cal._load = lambda *a, **k: (list(junk), "")
    cal.now = lambda: BASE
    try:
        for name, call in CALLS[:4]:
            try:
                said = call()
                check(f"{name} survives junk", speakable(said), True)
            except Exception as exc:
                check(f"{name} raised {type(exc).__name__}: {exc}", False, True)
        check("an event with no readable time is not read out as scheduled",
              "Junk time" in cal.read_schedule("today"), False)
        check("...but the good one still is",
              "Fine" in cal.read_schedule("today"), True)
        check("next_event skips past the unreadable ones",
              "Fine" in cal.next_event(), True)
        check("a broken end time falls back to the default hour, not a crash",
              cal._length_of(junk[4]), None)
    finally:
        cal._load, cal.now = real_load, real_now


if __name__ == "__main__":
    print("=== Athena calendar, phase 6: behaviour under failure ===")
    every_failure_checks()
    not_connected_checks()
    no_false_success_checks()
    malformed_checks()
    clock_still_works_checks()
    followup_checks()
    loop_survives_checks()
    print(f"\n{PASSED} passed, {FAILED} failed.")
    sys.exit(1 if FAILED else 0)
