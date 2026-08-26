"""Athena's calendar, read aloud in plain English.

Google Calendar through the API, never a driven browser. Every function
returns one short honest sentence, because whatever it returns is what Athena
says out loud.

Reading is free. **Creating, moving and cancelling are gated**: each one
resolves the time, reads the title and the resolved time back in plain words,
and does nothing at all without an explicit yes. The gate lives INSIDE this
module (`set_confirm`, as `mail.py` does) rather than only in `safety.py`,
because the resolved time is not knowable outside it - the model hands us
"tomorrow at 3" and only this module can say "three o'clock tomorrow
afternoon". Reading the raw words back would defeat the point of the check.
With no confirm hook wired in, every write refuses. A misheard time is caught
before it reaches the calendar rather than after.

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

**"Tomorrow at 3" is resolved HERE, not by the model, and not by a library.**
`resolve_when` is stdlib `datetime` and `re`, and that is a decision with
evidence behind it. Measured against the phrases this assistant actually
hears, from a Wednesday at 13:15:

    phrase                  dateutil                dateparser
    "tomorrow at 3"         2026-08-03 13:15        2026-08-27 13:15
    "next Monday morning"   2026-08-31 13:15        None
    "in two hours"          ParserError             2026-08-26 15:15
    "Friday at 7pm"         2026-08-28 19:15        2026-08-28 19:00
    "at 3"                  2026-08-03 13:15        2027-03-26 00:00

dateutil read the "3" of "tomorrow at 3" as a day of the month and put the
event three weeks in the PAST; dateparser dropped the "at 3" and kept the
current time. Neither raised. A calendar that is silently wrong is worse than
one that says it doesn't understand, so we own this. `resolve_when` returns
either a time or a question - never a guess it can't justify. Its one
deliberate guess is the bare hour (see `_apply_meridiem`), and the read-back
exists precisely to catch that one.

Nothing here raises into the assistant loop. No sign-in, no calendar
permission, no network: it says so in one sentence and Athena carries on.
"""

import datetime
import re

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

# An event with no end given runs an hour.
DEFAULT_DURATION_MINUTES = 60

# main.py injects confirm(question) -> bool here. Until it does, every write
# in this module refuses - see set_confirm.
_confirm = None


def set_confirm(confirm_fn=None) -> None:
    """main.py wires in its confirm(question) -> bool here, the same one the
    safety gate uses, so a booking can be approved by voice or by clicking."""
    global _confirm
    _confirm = confirm_fn


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
    # These two name their own part of the day, so they never take one -
    # "twelve o'clock in the afternoon" is not how anyone says midday.
    if moment.minute == 0 and moment.hour == 12:
        return "midday"
    if moment.minute == 0 and moment.hour == 0:
        return "midnight"
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
    """'in the morning' / 'at night' - the form used after a day name.
    Empty for midday and midnight, which already say which part they are."""
    if moment.minute == 0 and moment.hour in (0, 12):
        return ""
    part = _part_of_day(moment)
    return "at night" if part == "night" else f"in the {part}"


def _clock_and_part(moment: datetime.datetime) -> str:
    """'seven o'clock in the evening', or just 'midday'."""
    return f"{_spoken_clock(moment)} {_part_of_day_phrase(moment)}".strip()


def _spoken_time(moment: datetime.datetime, today: bool = False) -> str:
    """'half past two this afternoon' when it is today, 'half past two in the
    afternoon' otherwise. 'this night' is not English, so night becomes
    'tonight' or plain 'at night'."""
    clock = _spoken_clock(moment)
    if clock in ("midday", "midnight"):
        return clock
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


# --- resolving "tomorrow at 3" into an actual moment ---------------------

# Words to digits, so "half past three" and "half past 3" take one code path.
_WORD_NUMBERS = {word: value for value, word in enumerate(_ONES)}
_WORD_NUMBERS.update({word: value * 10 for value, word in _TENS.items()})

# What a bare part of the day means when no clock time comes with it. These
# are conventions, not facts, which is why the read-back says them out loud.
PART_OF_DAY_HOURS = {"morning": 9, "afternoon": 14, "evening": 19,
                     "tonight": 20, "night": 20, "noon": 12, "midday": 12,
                     "midnight": 0}

_RELATIVE = re.compile(
    r"^in (?:(?P<count>\d+)|an?) ?(?P<unit>minute|min|hour|hr|day|week)s?$")
_HALF_HOUR = re.compile(r"^in half an hour$")
_UNIT_SECONDS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600,
                 "day": 86400, "week": 604800}


def _normalise(text: str) -> str:
    """Lower case, no stray punctuation, single spaces, 'p.m.' as 'pm'."""
    words = (text or "").strip().lower()
    words = words.replace("a.m.", "am").replace("p.m.", "pm")
    words = words.replace(",", " ").replace("'o clock", " o'clock")
    words = re.sub(r"\s+", " ", words).strip(" ?.!")
    return words


def _words_to_digits(words: str) -> str:
    """'half past three' -> 'half past 3'. Speech-to-text gives us both forms
    depending on how the sentence ran, so they are flattened to one."""
    names = sorted(_WORD_NUMBERS, key=len, reverse=True)
    pattern = r"\b(" + "|".join(names) + r")\b"
    return re.sub(pattern, lambda m: str(_WORD_NUMBERS[m.group(0)]), words)


def _apply_meridiem(hour: int, meridiem: str, part: str) -> int:
    """Turn a clock-face hour into a 24-hour one.

    In order: an explicit am/pm wins; then a part of the day said in the same
    breath ("7 in the evening"); then the one guess this module makes -
    **a bare 1 to 6 means the afternoon, 7 to 11 the morning, 12 is noon**.
    Nobody books a 3am dentist by voice. It is still a guess, which is why
    the read-back speaks it back as "three o'clock in the afternoon" before
    anything is written."""
    if meridiem == "am":
        return 0 if hour == 12 else hour
    if meridiem == "pm":
        return hour if hour == 12 else hour + 12
    if part in ("afternoon", "evening", "night", "tonight"):
        return hour if hour >= 12 else hour + 12
    if part == "morning":
        return 0 if hour == 12 else hour
    if hour == 12:
        return 12
    return hour + 12 if 1 <= hour <= 6 else hour


def _extract_clock(words: str):
    """Pull a time of day out of the words.

    Returns (hour, minute, meridiem, leftover words) with hour None when the
    words carry no time at all. The leftover is handed to `_parse_day`, so
    each pattern removes exactly what it consumed."""
    for name, hour in (("midnight", 0), ("noon", 12), ("midday", 12)):
        if re.search(rf"\b{name}\b", words):
            return hour, 0, "explicit", re.sub(rf"\b{name}\b", " ", words)

    patterns = (
        # "half past 3", "quarter past 3", "quarter to 4"
        (r"\bhalf past (?P<h>\d{1,2})\b", lambda h, _: (h, 30)),
        (r"\bquarter past (?P<h>\d{1,2})\b", lambda h, _: (h, 15)),
        (r"\bquarter to (?P<h>\d{1,2})\b", lambda h, _: ((h - 1) or 12, 45)),
        # "3:30pm", "15.30". No \b after the minutes: "30pm" has no word
        # boundary in it, and requiring one dropped 3:30pm through to the
        # bare-hour rule below, which read it as three in the afternoon.
        (r"\b(?P<h>\d{1,2})[:.](?P<m>\d{2})(?!\d)", lambda h, m: (h, m)),
        # "3pm", "3 pm"
        (r"\b(?P<h>\d{1,2}) ?(?=am|pm)", lambda h, _: (h, 0)),
        # "3 o'clock"
        (r"\b(?P<h>\d{1,2}) ?o'?clock\b", lambda h, _: (h, 0)),
        # "at 3" - last, so it never steals the hour from a fuller form
        (r"\bat (?P<h>\d{1,2})\b", lambda h, _: (h, 0)),
    )
    for pattern, unpack in patterns:
        match = re.search(pattern, words)
        if not match:
            continue
        hour = int(match.group("h"))
        minute = int(match.groupdict().get("m") or 0)
        hour, minute = unpack(hour, minute)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        rest = words[:match.start()] + " " + words[match.end():]
        # The am/pm sits just past what we matched, so read it from there.
        meridiem = ""
        trailing = re.match(r"\s*(am|pm)\b", words[match.end():])
        if trailing:
            meridiem = trailing.group(1)
            rest = words[:match.start()] + " " + words[match.end() + trailing.end():]
        return hour, minute, meridiem, rest
    return None, 0, "", words


def _find_part_of_day(words: str):
    """The part of the day named in the words, and what's left without it."""
    for name in ("morning", "afternoon", "evening", "tonight", "night",
                 "noon", "midday", "midnight"):
        if re.search(rf"\b{name}\b", words):
            return name, re.sub(rf"\b(this |in the |at )?{name}\b", " ", words)
    return "", words


def resolve_when(text: str, base: datetime.datetime | None = None,
                 roll_past: bool = True):
    """'tomorrow at 3' -> a real local datetime.

    Returns (moment, "") or (None, one short question to ask instead). It
    never returns a moment it had to invent a day AND a time for: a day with
    no time asks what time, because booking a silent 9am is how a calendar
    ends up wrong."""
    base = base or now()
    words = _normalise(text)
    if not words:
        return None, "When should that be?"

    words = _words_to_digits(words)

    if _HALF_HOUR.match(words):
        return base + datetime.timedelta(minutes=30), ""
    relative = _RELATIVE.match(words)
    if relative:
        count = int(relative.group("count") or 1)
        return (base + datetime.timedelta(
            seconds=count * _UNIT_SECONDS[relative.group("unit")]), "")

    # A full machine timestamp, in case one is ever handed straight through.
    try:
        exact = datetime.datetime.fromisoformat(words.upper().replace(" ", "T"))
        return (exact if exact.tzinfo else exact.replace(tzinfo=local_zone())), ""
    except ValueError:
        pass

    hour, minute, meridiem, rest = _extract_clock(words)
    part, rest = _find_part_of_day(rest)
    rest = re.sub(r"\b(on|at|the|this|coming)\b", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip()

    day = _parse_day(rest, base.date()) if rest else None
    day_was_said = day is not None

    if hour is None:
        if part:
            hour, minute, meridiem = PART_OF_DAY_HOURS[part], 0, "explicit"
        elif day_was_said:
            return None, (f"What time {_day_label(day, now().date())}?")
        else:
            return None, f"I couldn't work out a time from {text!r}."

    if meridiem != "explicit":
        hour = _apply_meridiem(hour, meridiem, part)

    moment = datetime.datetime.combine(
        day or base.date(), datetime.time(hour % 24, minute),
        tzinfo=local_zone())

    # "at 3" said at four in the afternoon means tomorrow, not two hours ago.
    # Only ever rolled when the DAY was left unsaid - an explicit past date is
    # a mistake to report, not to quietly correct.
    if roll_past and not day_was_said and moment <= base:
        moment += datetime.timedelta(days=1)
    return moment, ""


def _resolve_end(text: str, start: datetime.datetime):
    """The end of an event. Accepts a clock time ('5pm'), a duration ('for
    two hours', 'in 90 minutes'), or nothing at all."""
    words = _words_to_digits(_normalise(text))
    duration = re.match(
        r"^(?:for |lasting )?(?P<count>\d+) ?(?P<unit>minute|min|hour|hr)s?$",
        words)
    if duration:
        return start + datetime.timedelta(
            seconds=int(duration.group("count"))
            * _UNIT_SECONDS[duration.group("unit")]), ""
    # Relative to the START, not to now, and never rolled forward a day.
    return resolve_when(text, base=start, roll_past=False)


def _spoken_moment(moment: datetime.datetime) -> str:
    """'tomorrow at three o'clock in the afternoon' - a moment named in full,
    for reading back before anything is written."""
    day = _day_label(moment.date(), now().date())
    if day == "today":
        # "at" is not optional here. Without it every read-back for today
        # reads as "Dentist half past two this afternoon" - the day names
        # carry their own preposition ("on Friday at..."), today does not.
        return f"at {_spoken_time(moment, today=True)}"
    return f"{day} at {_clock_and_part(moment)}"


def _spoken_duration(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return "an hour" if hours == 1 else f"{_number_word(hours)} hours"
    if minutes < 60:
        return f"{_number_word(minutes)} minutes"
    return f"{_number_word(minutes // 60)} and a half hours" \
        if minutes % 60 == 30 else f"{minutes} minutes"


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


# Fragments that identify a failure, in the wording the libraries really use.
# `getaddrinfo failed` is here because it is what a dead DNS entry actually
# says on Windows, and it matches none of the obvious words like "network" or
# "connection" - it was falling through to the catch-all and being read aloud
# as "[Errno 11001] getaddrinfo failed".
_NETWORK_MARKERS = ("network", "resolve", "unreachable", "timed out",
                    "timeout", "connection", "getaddrinfo", "errno", "socket",
                    "ssl", "eof occurred", "name or service not known",
                    "temporarily unavailable", "broken pipe")
_DISABLED_MARKERS = ("has not been used", "is disabled", "disabled")
_PERMISSION_MARKERS = ("insufficient", "scope", "permission_denied",
                       "forbidden")
_EXPIRED_MARKERS = ("invalid credentials", "invalid_grant", "unauthorized",
                    "token has been expired", "invalid authentication")
_BUSY_MARKERS = ("rate limit", "quota", "too many requests", "backend error",
                 "user rate", "429")


def _failure_sentence(exc: Exception, doing: str = "read your calendar") -> str:
    """One spoken sentence for a call that didn't come back.

    `doing` completes "I couldn't ___", so it reads correctly for a read and
    for a write alike. Never a stack trace, never an error code, and never a
    claim that it worked - a write that failed says so.

    The catch-all deliberately does NOT read the raw error out. Google's
    messages are written for a log, not for a room: "Invalid Credentials",
    "[Errno 11001] getaddrinfo failed". The detail goes to the console, where
    whoever is debugging can see all of it."""
    detail = google_auth._short(exc)
    lowered = detail.lower()

    # Order matters: the "API not enabled" message is itself a 403, so it has
    # to be recognised before the permission check claims it.
    if any(marker in lowered for marker in _DISABLED_MARKERS):
        return ("The Google Calendar API isn't switched on for this project "
                f"yet, so I couldn't {doing}.")
    if any(marker in lowered for marker in _PERMISSION_MARKERS):
        return (f"Google wouldn't let me {doing} - that permission hasn't "
                "been granted yet. Sign in once more and try again.")
    if any(marker in lowered for marker in _EXPIRED_MARKERS):
        return (f"I couldn't {doing} - the Google sign-in has expired. Run "
                "the sign-in again and I'll be able to.")
    if any(marker in lowered for marker in _BUSY_MARKERS):
        return (f"I couldn't {doing} - Google's had too many requests from me "
                "just now. Try again in a moment.")
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return f"I couldn't {doing} - it looks like the network is down."

    print(f"[calendar] unrecognised failure while trying to {doing}: {detail}")
    return f"I couldn't {doing} - something went wrong at Google's end."


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


def _parsed(stamp: str):
    """One RFC3339 stamp as a local datetime, or None if it isn't one.

    Never lets a bad value out of the module. This is the boundary where
    someone else's data becomes ours, and `fromisoformat` raises on anything
    it doesn't like - which took a ValueError all the way out of
    read_schedule and into the assistant loop before it was caught."""
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(stamp).astimezone(local_zone())
    except (ValueError, TypeError):
        print(f"[calendar] couldn't read the time {stamp!r} - skipping it")
        return None


def _starts_at(event: dict):
    """(when it starts, is it an all-day event). All-day events come back as
    a bare date with no time at all, and must not be read out as midnight.
    (None, False) for anything we can't make sense of - callers already skip
    that, and a spoken answer missing one event beats no answer at all."""
    start = event.get("start", {})
    if start.get("dateTime"):
        return _parsed(start["dateTime"]), False
    if start.get("date"):
        try:
            return _start_of_day(datetime.date.fromisoformat(start["date"])), True
        except (ValueError, TypeError):
            print(f"[calendar] couldn't read the date {start['date']!r}")
            return None, False
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
    # An event whose time we couldn't read has no place on a spoken schedule:
    # reading its title with no time implies it is on the day being asked
    # about, which is exactly what we don't know. _parsed has already said so
    # on the console.
    events = [event for event in events if _starts_at(event)[0] is not None]
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
    return (f"It's {_clock_and_part(moment)} on "
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
    # A handful rather than one, so an event with an unreadable time doesn't
    # become the answer - we skip to the next one we can actually place.
    events, problem = _load(start, end, limit=5)
    if problem:
        return problem
    readable = [event for event in events if _starts_at(event)[0] is not None]
    if not readable:
        return ("You have nothing scheduled in the next "
                f"{NEXT_EVENT_HORIZON_DAYS} days.")

    event = readable[0]
    moment, all_day = _starts_at(event)
    today = start.date()
    day = _day_label(moment.date(), today)
    if all_day:
        return f"Next up is {_title(event)}, all day {day}."
    if moment.date() == today:
        return f"Next up is {_title(event)} at {_spoken_time(moment, today=True)}."
    return f"Next up is {_title(event)} {day}, at {_clock_and_part(moment)}."


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
            phrases.append(f"{_title(event)} {day} at {_clock_and_part(moment)}")

    if len(matches) == 1:
        return f"I found {phrases[0]}."
    sentence = f"I found {len(matches)}: {_join(phrases)}."
    if len(matches) > len(spoken):
        sentence += f" And {len(matches) - len(spoken)} more."
    return sentence


# --- finding the one event they meant --------------------------------------

def _length_of(event: dict) -> datetime.timedelta | None:
    """How long an event runs, or None if it has no readable start and end.
    None makes reschedule_event fall back to the default hour, which is a
    worse guess than the real length but a far better one than a crash."""
    start = _parsed(event.get("start", {}).get("dateTime"))
    end = _parsed(event.get("end", {}).get("dateTime"))
    if not (start and end):
        return None
    return end - start


def _find_by_title(identifier: str):
    """The single event whose title matches, or a sentence to say instead.

    Returns (event, "", []) when exactly one thing matches, and
    (None, sentence, candidates) when nothing does or several do. Several is
    not an error - it is a question, and the sentence asks it."""
    words = (identifier or "").strip()
    if not words:
        return None, "Which event did you mean?", []

    start = now() - datetime.timedelta(days=FIND_LOOKBACK_DAYS)
    end = now() + datetime.timedelta(days=FIND_HORIZON_DAYS)
    events, problem = _load(start, end, query=words)
    if problem:
        return None, problem, []

    needle = words.lower()
    exact = [e for e in events if _title(e).lower() == needle]
    partial = [e for e in events if needle in _title(e).lower()]
    matches = exact or partial

    if not matches:
        return None, f"I couldn't find anything called {words} on your calendar.", []
    if len(matches) == 1:
        return matches[0], "", []

    # More than one. Read them back and ask - never pick for them.
    spoken = matches[:MAX_SPOKEN_EVENTS]
    phrases = []
    for event in spoken:
        moment, all_day = _starts_at(event)
        if moment is None:
            phrases.append(_title(event))
        elif all_day:
            phrases.append(f"{_title(event)}, all day "
                           f"{_day_label(moment.date(), now().date())}")
        else:
            phrases.append(f"{_title(event)} {_spoken_moment(moment)}")
    more = "" if len(matches) == len(spoken) else \
        f" And {len(matches) - len(spoken)} more."
    return None, (f"There are {len(matches)} that match: {_join(phrases)}.{more} "
                  "Which one did you mean?"), matches


def _clashes(start, end, ignore_id: str = ""):
    """Events already occupying that slot. All-day events don't count - a
    holiday shouldn't block a meeting - and neither does anything the
    calendar itself marks as free."""
    events, problem = _load(start, end)
    if problem:
        return [], problem
    busy = []
    for event in events:
        if event.get("id") == ignore_id:
            continue
        if event.get("transparency") == "transparent":
            continue
        moment, all_day = _starts_at(event)
        if all_day or moment is None:
            continue
        busy.append(event)
    return busy, ""


def _ask(question: str, refusal: str):
    """Put the yes/no question. Returns (approved, what_to_say_if_not).

    With no hook wired there is no way to ask, so the answer is no. A write
    that cannot be confirmed must not happen."""
    if _confirm is None:
        print("[calendar] no confirm hook wired - refusing to write")
        return False, ("I can't confirm that with you right now, so I've left "
                       "your calendar alone.")
    try:
        approved = bool(_confirm(question))
    except Exception as exc:
        print(f"[calendar] confirm failed: {exc!r}")
        return False, refusal
    return (True, "") if approved else (False, refusal)


def _clash_question(busy: list, action: str) -> str:
    """'You already have Standup at nine o'clock then. Shall I ...anyway?'"""
    clashing = _join([f"{_title(e)} at {_spoken_clock(_starts_at(e)[0])}"
                      for e in busy[:3]])
    return f"You already have {clashing} then. Shall I {action} anyway?"


# --- creating, moving and cancelling ---------------------------------------

def create_event(title: str, start: str, end: str | None = None,
                 description: str | None = None) -> str:
    """Put a new event in the calendar, after reading it back for a yes."""
    name = (title or "").strip()
    if not name:
        return "What should I call it?"

    service, problem = _service()
    if service is None:
        return problem

    when, problem = resolve_when(start)
    if problem:
        return problem

    if end:
        finish, problem = _resolve_end(end, when)
        if problem:
            return problem
        if finish <= when:
            return (f"That would end before it starts. When should "
                    f"{name} finish?")
    else:
        finish = when + datetime.timedelta(minutes=DEFAULT_DURATION_MINUTES)

    minutes = int((finish - when).total_seconds() // 60)
    length = "" if minutes == DEFAULT_DURATION_MINUTES else \
        f", for {_spoken_duration(minutes)}"
    question = f"Create {name} {_spoken_moment(when)}{length}?"

    # A clash doesn't stop the booking - it changes the question, so a double
    # booking is something the user says yes to rather than discovers later.
    busy, problem = _clashes(when, finish)
    if problem:
        return problem
    if busy:
        question = _clash_question(busy, f"add {name} {_spoken_moment(when)}")

    approved, refusal = _ask(question, f"I haven't added {name}.")
    if not approved:
        return refusal

    body = {
        "summary": name,
        # No timeZone field: the stamps carry their own offset, which the API
        # accepts and which needs no IANA name from a machine that has none.
        "start": {"dateTime": when.isoformat()},
        "end": {"dateTime": finish.isoformat()},
    }
    if description:
        body["description"] = description

    try:
        service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    except Exception as exc:
        return _failure_sentence(exc, "add that to your calendar")

    print(f"[calendar] created {name!r} at {when.isoformat()}")
    return f"Done - {name} is in your calendar {_spoken_moment(when)}."


def reschedule_event(event_identifier: str, new_start: str,
                     new_end: str | None = None, event: dict | None = None) -> str:
    """Move an existing event, after reading the move back for a yes.

    `event` skips the title lookup when the caller already holds the exact
    event. followup.py needs that: it is asking about ONE past occurrence,
    and a title like "Standup" also matches tomorrow's, so looking it up by
    name would either move the wrong one or stall on "which did you mean?".
    Everything after the lookup - the read-back, the gate, the write - is
    the same single code path either way."""
    service, problem = _service()
    if service is None:
        return problem

    if event is None:
        event, problem, _ = _find_by_title(event_identifier)
        if event is None:
            return problem

    when, problem = resolve_when(new_start)
    if problem:
        return problem

    was, all_day = _starts_at(event)
    if new_end:
        finish, problem = _resolve_end(new_end, when)
        if problem:
            return problem
        if finish <= when:
            return "That would end before it starts. When should it finish?"
    else:
        # Keep however long it already ran, so moving a two-hour meeting
        # doesn't quietly shrink it to the default hour.
        finish = when + (_length_of(event)
                         or datetime.timedelta(minutes=DEFAULT_DURATION_MINUTES))

    name = _title(event)
    moving_from = "all day" if all_day or was is None else _spoken_moment(was)
    question = f"Move {name} from {moving_from} to {_spoken_moment(when)}?"

    busy, problem = _clashes(when, finish, ignore_id=event.get("id", ""))
    if problem:
        return problem
    if busy:
        question = _clash_question(
            busy, f"move {name} to {_spoken_moment(when)}")

    approved, refusal = _ask(question, f"I've left {name} where it was.")
    if not approved:
        return refusal

    try:
        service.events().patch(
            calendarId=CALENDAR_ID, eventId=event["id"],
            # patch replaces each named object wholesale, so an all-day event
            # moved to a real time loses its bare "date" cleanly.
            body={"start": {"dateTime": when.isoformat()},
                  "end": {"dateTime": finish.isoformat()}}).execute()
    except Exception as exc:
        return _failure_sentence(exc, "move that event")

    print(f"[calendar] moved {name!r} to {when.isoformat()}")
    return f"Moved - {name} is now {_spoken_moment(when)}."


def cancel_event(event_identifier: str) -> str:
    """Delete an event, after reading it back for a yes."""
    service, problem = _service()
    if service is None:
        return problem

    event, problem, _ = _find_by_title(event_identifier)
    if event is None:
        return problem

    name = _title(event)
    moment, all_day = _starts_at(event)
    if moment is None:
        when_said = ""
    elif all_day:
        when_said = f", all day {_day_label(moment.date(), now().date())}"
    else:
        when_said = f" {_spoken_moment(moment)}"

    approved, refusal = _ask(f"Cancel {name}{when_said}?",
                             f"I've left {name} in your calendar.")
    if not approved:
        return refusal

    try:
        service.events().delete(calendarId=CALENDAR_ID,
                                eventId=event["id"]).execute()
    except Exception as exc:
        return _failure_sentence(exc, "cancel that event")

    print(f"[calendar] cancelled {name!r}")
    return f"Cancelled - {name}{when_said} is off your calendar."
