"""Athena's calendar, read aloud in plain English.

Google Calendar through the API, never a driven browser. This phase is
READ-ONLY: nothing here creates, moves or cancels anything. Every function
returns one short honest sentence, because whatever it returns is what Athena
says out loud.

Named `calendar_skill.py`, not `calendar.py`, so it can never be mistaken for
the standard library's `calendar` module by a reader or a stray import.

**Everything is said the way a person says it.** The API speaks in
`2026-08-26T14:30:00+06:00`; a listener hears "half past two this afternoon".
That translation is the bulk of this module and it lives in the `_spoken_*`
helpers, which are pure functions of a datetime and are tested offline.

**The clock is the machine's local clock.** Windows here reports Bangladesh
Standard Time, UTC+06:00. There is no IANA time-zone database on this machine
(`zoneinfo.TZPATH` is empty), so the local zone is taken from
`datetime.now().astimezone()` - a real offset, with the name Windows gives it.
That is enough for the Calendar API, which accepts RFC3339 stamps carrying
their own offset and needs no IANA name from us.

**A week ends on Friday.** The weekend here is Friday and Saturday, so
"this week" means from today through the coming Friday inclusive - see
`_window`.

Nothing here raises into the assistant loop. No sign-in, no calendar
permission, no network: it says so in one sentence and Athena carries on.
"""

import datetime

from athena import google_auth

# The scope this module needs. It is one of google_auth.SCOPES; named here too
# so the "you signed in before I could see your calendar" check reads clearly.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# Which calendar. "primary" is whatever the signed-in account calls its own.
CALENDAR_ID = "primary"

# How many events one spoken answer may list before it stops and says how
# many are left. Five is about as much as anyone holds from a sentence.
MAX_SPOKEN_EVENTS = 5

# How far next_event and find_event look ahead before giving up.
NEXT_EVENT_HORIZON_DAYS = 30
FIND_HORIZON_DAYS = 90
# find_event looks a little way BACK too, because "when was my dentist thing"
# is usually asked just after it happened.
FIND_LOOKBACK_DAYS = 7


# --- the local clock -------------------------------------------------------

def local_zone() -> datetime.tzinfo:
    """The machine's own time zone, as a real tzinfo with the right offset."""
    zone = datetime.datetime.now().astimezone().tzinfo
    return zone or datetime.timezone.utc


def now() -> datetime.datetime:
    """Right now, aware, in local time. Every other function starts here, so
    a test can move time by patching this one name."""
    return datetime.datetime.now(local_zone())


def timezone_name() -> str:
    """What time zone we detected, for the setup notes and the test script.
    e.g. 'Bangladesh Standard Time (UTC+06:00)'."""
    moment = now()
    offset = moment.utcoffset() or datetime.timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    name = moment.tzname() or "local time"
    return f"{name} (UTC{sign}{hours:02d}:{minutes:02d})"


# --- saying numbers, times and dates out loud ------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _number_word(n: int) -> str:
    """0-59 in words. Only ever used for clock faces, so it stops there."""
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    word = _TENS.get(tens, str(n))
    return word if ones == 0 else f"{word}-{_ONES[ones]}"


def _ordinal(n: int) -> str:
    """3 -> '3rd'. Speech engines read these correctly; a bare '3' gets read
    as 'three of September', which is not how anyone says a date."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _spoken_clock(moment: datetime.datetime) -> str:
    """The clock face in words: 'half past two', 'quarter to six',
    'ten past nine', 'seven o'clock', 'two thirty-seven'."""
    hour = moment.hour % 12 or 12
    following = (moment.hour + 1) % 12 or 12
    minute = moment.minute

    if minute == 0:
        return f"{_number_word(hour)} o'clock"
    if minute == 15:
        return f"quarter past {_number_word(hour)}"
    if minute == 30:
        return f"half past {_number_word(hour)}"
    if minute == 45:
        return f"quarter to {_number_word(following)}"
    if minute % 5 == 0:
        if minute < 30:
            return f"{_number_word(minute)} past {_number_word(hour)}"
        return f"{_number_word(60 - minute)} to {_number_word(following)}"
    # An odd minute. "two oh five" and "two thirty-seven" are both how people
    # actually read a clock; the split is at ten past.
    if minute < 10:
        return f"{_number_word(hour)} oh {_number_word(minute)}"
    return f"{_number_word(hour)} {_number_word(minute)}"


def _part_of_day(moment: datetime.datetime) -> str:
    hour = moment.hour
    if hour < 5:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "night"


def _part_of_day_phrase(moment: datetime.datetime) -> str:
    """'in the morning' / 'at night' - the form used after a day name."""
    part = _part_of_day(moment)
    return "at night" if part == "night" else f"in the {part}"


def _spoken_time(moment: datetime.datetime, today: bool = False) -> str:
    """'half past two this afternoon' when it is today, 'half past two in the
    afternoon' otherwise. 'this night' is not English, so night becomes
    'tonight' or plain 'at night'."""
    clock = _spoken_clock(moment)
    part = _part_of_day(moment)
    if part == "night":
        return f"{clock} tonight" if today else f"{clock} at night"
    return f"{clock} this {part}" if today else f"{clock} in the {part}"


def _spoken_date(day: datetime.date) -> str:
    """'the 26th of August' - a date said the way it is said aloud."""
    return f"the {_ordinal(day.day)} of {_MONTHS[day.month - 1]}"


def _day_label(day: datetime.date, today: datetime.date) -> str:
    """How to name a day in speech: 'today', 'tomorrow', 'on Thursday' inside
    the coming week, and a full date beyond that - because "on Thursday" two
    weeks out is genuinely ambiguous."""
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 2 <= delta <= 6:
        return f"on {day.strftime('%A')}"
    return f"on {day.strftime('%A')}, {_spoken_date(day)}"


# --- working out which days the question is about --------------------------

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}
# Friday is weekday 4. The weekend here is Friday and Saturday, so a week runs
# up to and including Friday - that is what "this week" is measured against.
WEEK_ENDS_ON = 4


def _start_of_day(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=local_zone())


def _end_of_day(day: datetime.date) -> datetime.datetime:
    return _start_of_day(day) + datetime.timedelta(days=1)


def _parse_day(text: str, today: datetime.date) -> datetime.date | None:
    """A single named day, or None if the words don't resolve to one.

    Deliberately small: this phase only has to understand the day words a
    schedule question uses. Phase 2 brings a real natural-language time
    resolver for "next Monday morning" and "in two hours"; when it lands this
    defers to it rather than growing."""
    words = text.strip().lower().strip("?.!")
    if not words:
        return None
    if words in ("today", "now", "tonight", "this evening", "this afternoon",
                 "this morning"):
        return today
    if words in ("tomorrow", "tmr", "tomorow"):
        return today + datetime.timedelta(days=1)
    if words == "yesterday":
        return today - datetime.timedelta(days=1)

    # ISO, the one numeric form with no day/month ambiguity to guess at.
    try:
        return datetime.date.fromisoformat(words)
    except ValueError:
        pass

    parts = words.replace(",", " ").split()

    # "monday", "this monday", "next monday"
    if parts[-1] in _WEEKDAYS and len(parts) <= 2:
        target = _WEEKDAYS[parts[-1]]
        ahead = (target - today.weekday()) % 7
        if ahead == 0:
            ahead = 7                    # "Monday" said on a Monday = the next
        if parts[0] == "next" and len(parts) == 2:
            ahead += 7
        return today + datetime.timedelta(days=ahead)

    # "3 September" / "September 3rd" / "sept 3 2026"
    month = day_number = year = None
    for part in parts:
        # Three letters is enough to name a month: "sep", "sept", "September".
        if len(part) >= 3 and part[:3].isalpha():
            names = [name[:3].lower() for name in _MONTHS]
            if part[:3] in names:
                month = names.index(part[:3]) + 1
                continue
        stripped = part.rstrip("stndrh")  # 3rd -> 3, 21st -> 21
        if stripped.isdigit():
            value = int(stripped)
            if value > 31:
                year = value
            elif day_number is None:
                day_number = value
    if month and day_number:
        try:
            candidate = datetime.date(year or today.year, month, day_number)
        except ValueError:
            return None
        # No year given and the date is long past? They mean next year.
        if year is None and candidate < today - datetime.timedelta(days=180):
            candidate = candidate.replace(year=today.year + 1)
        return candidate
    return None


def _window(when: str):
    """Turn 'today' / 'tomorrow' / 'this week' / a date into the span to ask
    Google for. Returns (start, end, label, group_by_day) - or
    (None, None, the words we couldn't read, False)."""
    words = (when or "today").strip().lower().strip("?.!")
    today = now().date()

    if words in ("this week", "the week", "week", "rest of the week",
                 "this weeks", "remainder of the week"):
        # From today up to and including the coming Friday. Asked ON a Friday
        # that is just today; asked on a Saturday the week runs the full seven
        # days round to next Friday.
        ahead = (WEEK_ENDS_ON - today.weekday()) % 7
        last = today + datetime.timedelta(days=ahead)
        label = "this week" if ahead else "for the rest of today"
        return _start_of_day(today), _end_of_day(last), label, ahead > 0

    if words == "next week":
        ahead = (WEEK_ENDS_ON - today.weekday()) % 7
        first = today + datetime.timedelta(days=ahead + 1)
        return (_start_of_day(first),
                _end_of_day(first + datetime.timedelta(days=6)),
                "next week", True)

    day = _parse_day(words, today)
    if day is None:
        return None, None, when, False
    return _start_of_day(day), _end_of_day(day), _day_label(day, today), False


# --- talking to Google -----------------------------------------------------

def _service():
    """(service, "") or (None, one sentence saying why not).

    The scope check is separate from the sign-in check on purpose: "you are
    signed in but not for your calendar" is a different problem with a
    different fix, and saying "I'm not signed in" would send the user looking
    in the wrong place."""
    if not google_auth.is_configured():
        return None, google_auth.not_connected_message()
    granted = google_auth.granted_scopes()
    if granted and CALENDAR_SCOPE not in granted:
        return None, ("I'm signed in to your Google account, but not yet for "
                      "your calendar. Run the Google sign-in once more and "
                      "I'll be able to see it.")
    service = google_auth.get_service("calendar", "v3")
    if service is None:
        return None, google_auth.not_connected_message()
    return service, ""


def _failure_sentence(exc: Exception) -> str:
    """One spoken sentence for a call that didn't come back. Never a stack
    trace, and never a claim that it worked."""
    detail = google_auth._short(exc)
    lowered = detail.lower()
    if "insufficient" in lowered or "scope" in lowered or "403" in lowered:
        return ("Google wouldn't let me read your calendar - that permission "
                "hasn't been granted yet. Sign in once more and try again.")
    if any(word in lowered for word in
           ("network", "resolve", "unreachable", "timed out", "connection")):
        return ("I couldn't reach your calendar just now - it looks like the "
                "network is down.")
    if "has not been used" in lowered or "disabled" in lowered:
        return ("The Google Calendar API isn't switched on for this project "
                "yet, so I can't read your calendar.")
    return f"I couldn't read your calendar: {detail}"


def _load(start, end, query: str | None = None, limit: int = 50):
    """The events in a span, oldest first, already expanded from any repeats.
    Returns (events, "") or ([], one honest sentence)."""
    service, problem = _service()
    if service is None:
        return [], problem
    try:
        response = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            # Expand recurring events into real occurrences - without this a
            # weekly standup is one row with a rule attached rather than a
            # meeting on Tuesday, and orderBy would be refused outright.
            singleEvents=True,
            orderBy="startTime",
            maxResults=limit,
            q=query,
        ).execute()
    except Exception as exc:
        return [], _failure_sentence(exc)
    return response.get("items", []), ""


# --- turning events into sentences -----------------------------------------

def _title(event: dict) -> str:
    return (event.get("summary") or "").strip() or "an untitled event"


def _starts_at(event: dict):
    """(when it starts, is it an all-day event). All-day events come back as
    a bare date with no time at all, and must not be read out as midnight."""
    start = event.get("start", {})
    if start.get("dateTime"):
        moment = datetime.datetime.fromisoformat(start["dateTime"])
        return moment.astimezone(local_zone()), False
    if start.get("date"):
        return _start_of_day(datetime.date.fromisoformat(start["date"])), True
    return None, False


def _event_phrase(event: dict, today: datetime.date) -> str:
    """'Dentist at half past two this afternoon', or 'Dentist, all day'."""
    moment, all_day = _starts_at(event)
    if moment is None:
        return _title(event)
    if all_day:
        return f"{_title(event)}, all day"
    return (f"{_title(event)} at "
            f"{_spoken_time(moment, today=moment.date() == today)}")


def _join(phrases: list[str]) -> str:
    """'a', 'a and b', 'a, b and c' - the way a list is spoken."""
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + f" and {phrases[-1]}"


def _describe(events: list, label: str, group_by_day: bool, empty: str) -> str:
    """The spoken answer for a list of events. Caps the list, groups by day
    when it spans more than one, and says how many it left out."""
    if not events:
        return empty

    today = now().date()
    spoken = events[:MAX_SPOKEN_EVENTS]
    remaining = len(events) - len(spoken)

    if not group_by_day:
        phrases = [_event_phrase(event, today) for event in spoken]
        if label in ("today", "tomorrow", "yesterday"):
            sentence = f"{label.capitalize()} you have {_join(phrases)}."
        else:
            sentence = f"You have {_join(phrases)} {label}."
        if remaining:
            sentence += f" And {remaining} more after that."
        return sentence

    # More than one day: group them, so the days are heard as headings rather
    # than a run-on list of times with nothing to anchor them to.
    days: dict[datetime.date, list] = {}
    for event in spoken:
        moment, _ = _starts_at(event)
        if moment is None:
            continue
        days.setdefault(moment.date(), []).append(event)

    chunks = []
    for day in sorted(days):
        phrases = [_event_phrase(event, today) for event in days[day]]
        chunks.append(f"{_day_label(day, today).capitalize()}, {_join(phrases)}")
    sentence = ". ".join(chunks) + "."
    if remaining:
        sentence += f" And {remaining} more {label}."
    return sentence


# --- the skills ------------------------------------------------------------

def get_current_time() -> str:
    """The local time and date, said the way a person says it."""
    moment = now()
    return (f"It's {_spoken_clock(moment)} {_part_of_day_phrase(moment)} on "
            f"{moment.strftime('%A')}, {_spoken_date(moment.date())}.")


def read_schedule(when: str = "today") -> str:
    """What's on today, tomorrow, this week, or on a named date."""
    start, end, label, group = _window(when)
    if start is None:
        return (f"I'm not sure which day you meant by {label}. "
                "Try today, tomorrow, this week, or a date.")

    events, problem = _load(start, end)
    if problem:
        return problem

    if label == "for the rest of today":
        empty = "You have nothing else scheduled today."
    elif label in ("today", "tomorrow", "yesterday"):
        empty = f"You have nothing scheduled {label}."
    else:
        empty = f"You have nothing scheduled {label}."
    return _describe(events, label, group, empty)


def next_event() -> str:
    """The next thing coming up, whenever it is."""
    start = now()
    end = start + datetime.timedelta(days=NEXT_EVENT_HORIZON_DAYS)
    events, problem = _load(start, end, limit=1)
    if problem:
        return problem
    if not events:
        return ("You have nothing scheduled in the next "
                f"{NEXT_EVENT_HORIZON_DAYS} days.")

    event = events[0]
    moment, all_day = _starts_at(event)
    today = start.date()
    if moment is None:
        return f"Next up is {_title(event)}."
    day = _day_label(moment.date(), today)
    if all_day:
        return f"Next up is {_title(event)}, all day {day}."
    if moment.date() == today:
        return f"Next up is {_title(event)} at {_spoken_time(moment, today=True)}."
    return (f"Next up is {_title(event)} {day}, at "
            f"{_spoken_clock(moment)} {_part_of_day_phrase(moment)}.")


def find_event(query: str) -> str:
    """Find events whose title matches what was asked for."""
    words = (query or "").strip()
    if not words:
        return "What should I look for?"

    start = now() - datetime.timedelta(days=FIND_LOOKBACK_DAYS)
    end = now() + datetime.timedelta(days=FIND_HORIZON_DAYS)
    # Google's q searches the whole event - description, location, attendees.
    # We let it narrow the fetch, then match on the TITLE ourselves, so
    # "find my dentist appointment" doesn't return a meeting that merely
    # mentions a dentist in its notes.
    events, problem = _load(start, end, query=words)
    if problem:
        return problem

    needle = words.lower()
    matches = [event for event in events if needle in _title(event).lower()]
    if not matches:
        if not events:
            return f"I couldn't find anything called {words} on your calendar."
        # Google matched something, just not in the title. Say so rather than
        # claiming there is nothing when there plainly is something related.
        matches = events

    today = now().date()
    spoken = matches[:MAX_SPOKEN_EVENTS]
    phrases = []
    for event in spoken:
        moment, all_day = _starts_at(event)
        day = _day_label(moment.date(), today) if moment else ""
        if moment is None:
            phrases.append(_title(event))
        elif all_day:
            phrases.append(f"{_title(event)}, all day {day}")
        else:
            phrases.append(f"{_title(event)} {day} at {_spoken_clock(moment)} "
                           f"{_part_of_day_phrase(moment)}")

    if len(matches) == 1:
        return f"I found {phrases[0]}."
    sentence = f"I found {len(matches)}: {_join(phrases)}."
    if len(matches) > len(spoken):
        sentence += f" And {len(matches) - len(spoken)} more."
    return sentence
