"""Asking whether you actually did the thing.

An event whose time has passed tells you nothing about whether it happened.
This module closes that loop: it finds past events nobody has answered for,
asks about them one at a time, and writes the answer to `event_status.py` so
the same question is never asked twice.

**The restraint here is the feature.** Everything below exists to stop this
becoming nagging:

  - ONE event per question, oldest first. Never a batch, never a list.
  - At most `MAX_PER_SESSION` (3) in a session. Someone back from a week
    away gets three questions, not an interrogation.
  - On startup and on request only. **Never on a timer** - there is no
    scheduler in this module and there must not be one, because a question
    that arrives in the middle of something else is an interruption
    regardless of how well it is worded.
  - Silence or a changed subject records `deferred` and stops the whole run.
    Not just that event - the whole run. Continuing to the next question
    after someone has moved on IS chasing, however politely.
  - "No" records `skipped` and moves on **without comment**. No "that's
    okay!", no encouragement. They said no; that is the end of it.
  - "Yes" gets one short, varied acknowledgement. Varied because a fixed
    phrase heard three times in a row is worse than saying nothing at all -
    it stops being an answer and starts being a tic.

Speech goes through `set_io`, the same injection `guide.py` uses, so the orb
reacts and the tests can run the entire exchange with no microphone.

Degrades quietly at every level. No calendar, no store, no network: there is
nothing to ask about, so nothing is asked and Athena carries on.
"""

import datetime
import os
import random

from athena import calendar_skill as cal
from athena import event_status

# Never more than this many questions in one session.
MAX_PER_SESSION = 3

# How far back to look for unanswered events. Beyond a fortnight "did you go
# to that?" is archaeology, not a follow-up.
LOOKBACK_DAYS = 14

# One short, warm sentence for a yes. Never sycophantic, never more than a
# sentence, and never the same one twice in a session - see _acknowledge.
ACKNOWLEDGEMENTS = (
    "Good.",
    "Nice one.",
    "Good, that's off the list.",
    "Right, that's done then.",
    "Glad that one happened.",
    "Good - one less thing.",
    "Noted, thanks.",
    "That's ticked off.",
)

_speak = None
_listen = None
_used_acknowledgements: set = set()
_asked_this_session = 0


def set_io(speak_fn=None, listen_fn=None) -> None:
    """main.py wires in its UI-aware speak()/hear() so the orb reacts during
    a follow-up. Without this, speech falls back to tts and the recorder."""
    global _speak, _listen
    _speak = speak_fn
    _listen = listen_fn


def reset_session() -> None:
    """Forget how many questions have been asked and which acknowledgements
    have been used. main.py calls this when a session starts."""
    global _asked_this_session, _used_acknowledgements
    _asked_this_session = 0
    _used_acknowledgements = set()


def _say(text: str) -> None:
    if _speak is not None:
        _speak(text)
    else:
        from athena import tts
        tts.speak(text)


def _hear() -> str:
    """What they said back, or "" if they said nothing."""
    if _listen is not None:
        try:
            return (_listen() or "").strip()
        except Exception:
            return ""
    from athena import audio_io, stt
    wav = audio_io.record_until_silence()
    if not wav:
        return ""
    try:
        return (stt.transcribe(wav) or "").strip()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


# --- reading a yes or a no out of a spoken sentence -------------------------

_YES = {"yes", "yeah", "yep", "yup", "yah", "sure", "did", "done", "affirmative",
        "correct", "right", "mhm", "uhhuh", "definitely", "absolutely"}
_NO = {"no", "nope", "nah", "didn't", "didnt", "not", "never", "couldn't",
       "couldnt", "missed", "skipped", "forgot", "failed", "cancelled"}


def _tokens(text: str) -> list:
    return [word.strip(".,!?;:\"'") for word in (text or "").lower().split()]


def _yes_or_no(text: str):
    """True, False, or **None for neither** - and None is the important one.

    "Anything that isn't a clear yes or no" is how a changed subject is
    detected, so this must not stretch to fit. A guess here turns "actually,
    what's the weather" into a recorded answer about the dentist."""
    words = _tokens(text)
    if not words:
        return None
    # No first: "I didn't" contains "did", and reading that as a yes would
    # record the exact opposite of what was said.
    if any(word in _NO for word in words):
        return False
    if any(word in _YES for word in words):
        return True
    return None


def _acknowledge() -> str:
    """One short warm sentence, different from the last ones used."""
    global _used_acknowledgements
    unused = [line for line in ACKNOWLEDGEMENTS
              if line not in _used_acknowledgements]
    if not unused:
        _used_acknowledgements = set()
        unused = list(ACKNOWLEDGEMENTS)
    choice = random.choice(unused)
    _used_acknowledgements.add(choice)
    return choice


# --- what is still unanswered ----------------------------------------------

def _record(event_id: str, status: str) -> None:
    """Write an answer to the store, and never let the store stop the
    conversation. This is called from the middle of a spoken exchange; if
    Supabase is gone, or the module has been swapped for something that
    misbehaves, the worst acceptable outcome is asking the same question
    again another day - not raising into the assistant loop."""
    try:
        event_status.record_async(event_id, status)
    except Exception as exc:
        print(f"[followup] couldn't record {status} for {event_id}: {exc!r}")


def _declined(event: dict) -> bool:
    """True if the user turned this invitation down. Nobody wants to be asked
    whether they went to a meeting they said no to."""
    for attendee in event.get("attendees", []) or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def pending_followups() -> list:
    """Past events with no answer recorded, oldest first.

    All-day events are left out: they are overwhelmingly holidays, birthdays
    and reminders, and "did you do Independence Day?" is not a question worth
    asking. Flip the `all_day` test below if that turns out wrong for how you
    use your calendar.

    [] whenever anything is unavailable - no sign-in, no network, no store -
    which is what makes "she simply does not follow up" the failure mode."""
    right_now = cal.now()
    start = right_now - datetime.timedelta(days=LOOKBACK_DAYS)
    events, problem = cal._load(start, right_now)
    if problem:
        return []

    past = []
    for event in events:
        moment, all_day = cal._starts_at(event)
        if moment is None or all_day:
            continue
        if not event.get("id"):
            continue
        if _declined(event):
            continue
        # It has to have FINISHED, not merely started - asking about a
        # meeting someone is sitting in would be its own kind of rude.
        # _parsed rather than fromisoformat: an unreadable end time must not
        # raise out of here and into the startup path.
        finish = cal._parsed(event.get("end", {}).get("dateTime"))
        if finish and finish > right_now:
            continue
        past.append(event)

    past.sort(key=lambda e: cal._starts_at(e)[0])
    answered = event_status.recorded_ids([e["id"] for e in past])
    return [event for event in past if event["id"] not in answered]


# --- the exchange -----------------------------------------------------------

def _ask_about(event: dict) -> str:
    """Ask about one event and record what comes back.

    Returns "answered" (carry on), "stop" (they've moved on - drop it), or
    "failed" (the write didn't land)."""
    title = cal._title(event)
    moment, _ = cal._starts_at(event)
    when = cal._spoken_moment(moment) if moment else "recently"

    _say(f"You had {title} {when}. Did you get to it?")
    verdict = _yes_or_no(_hear())

    if verdict is None:
        # Silence, or they said something else entirely. Record it so we
        # never ask again, and stop - pressing on would be chasing.
        _record(event["id"], "deferred")
        return "stop"

    if verdict:
        _record(event["id"], "done")
        _say(_acknowledge())
        return "answered"

    _say("Do you want to reschedule it?")
    wants_move = _yes_or_no(_hear())

    if wants_move is None:
        _record(event["id"], "deferred")
        return "stop"

    if not wants_move:
        # They said no. Record it and move on WITHOUT COMMENT - anything
        # said here is either a platitude or a judgement.
        _record(event["id"], "skipped")
        return "answered"

    _say("When should I move it to?")
    phrase = _hear()
    if not phrase:
        _record(event["id"], "deferred")
        return "stop"

    # Resolved here only to find out whether it CAN be resolved - if not, we
    # get to ask once more before handing it on. reschedule_event resolves it
    # again and remains the authority on what time was actually booked.
    _, problem = cal.resolve_when(phrase)
    if problem:
        # One more question, not an interrogation. If it still doesn't
        # resolve, leave the event alone rather than looping.
        _say(problem)
        phrase = _hear()
        if not phrase or cal.resolve_when(phrase)[1]:
            _record(event["id"], "deferred")
            return "stop"

    # By event, not by title: "Standup" also matches tomorrow's. And
    # reschedule_event does its own read-back and needs its own yes - a real
    # calendar write does not get to ride along on a follow-up.
    said = cal.reschedule_event(title, phrase, event=event)
    _say(said)
    if said.startswith("Moved -"):
        _record(event["id"], "rescheduled")
        return "answered"
    # The move didn't happen, so nothing is recorded as though it did. The
    # event stays pending and can be asked about again another day.
    return "failed"


def run(limit: int = MAX_PER_SESSION, pending: list | None = None) -> str:
    """Work through the pending follow-ups. Returns one spoken sentence.

    This is what both triggers call - startup and "what did I miss". It never
    starts itself; something has to ask. `pending` lets on_startup hand over
    the list it already fetched rather than costing a second API call on the
    slowest part of the session."""
    global _asked_this_session

    remaining = min(limit, MAX_PER_SESSION - _asked_this_session)
    if remaining <= 0:
        return "I've asked enough for one session - I'll leave the rest."

    if pending is None:
        pending = pending_followups()
    if not pending:
        return "Nothing's outstanding - you're all caught up."

    answered = 0
    for event in pending[:remaining]:
        _asked_this_session += 1
        outcome = _ask_about(event)
        if outcome == "answered":
            answered += 1
        if outcome in ("stop", "failed"):
            # "stop": they've moved on, so say nothing more about it.
            # "failed": _ask_about already spoke the reason the move didn't
            # land, and repeating it as a summary would say it twice.
            return ""

    left = len(pending) - answered
    if left > 0:
        return f"That's those. {left} more whenever you want them."
    return "That's everything caught up."


def on_startup() -> str:
    """The startup trigger, and the whole of it. Gated by the
    `followup_on_startup` setting (default True) so a demo can start clean.

    Returns "" when it does nothing, which is most of the time."""
    try:
        from athena import settings
        if not settings.get("followup_on_startup", True):
            return ""
    except Exception:
        pass                                   # settings unavailable: proceed
    pending = pending_followups()
    if not pending:
        # Silence, not "you're all caught up" - nobody asked. That sentence
        # is an answer to a question, and at startup there wasn't one.
        return ""
    return run(pending=pending)
