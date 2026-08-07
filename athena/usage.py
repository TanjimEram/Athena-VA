"""How much API budget is left, read from Groq's own rate-limit headers.

Groq puts the answer on EVERY response, not just on a 429: how many tokens
remain this minute, how many requests remain today, and how long until each
window refills. This module parses those headers and holds the latest picture
so the dashboard can show it.

It is observation only. Nothing here retries, queues, throttles or delays
anything - the existing backoff in brain.py is untouched. Every entry point
swallows its own errors and leaves the meter reading "--", because a budget
display must never be the reason a conversation breaks.

One thing worth knowing: the headers report what is REMAINING, an absolute
figure from Groq, not a running total we keep. So a call somewhere else that
we don't capture doesn't make this wrong - it makes it stale until the next
call we do capture. Rate limits are also per MODEL, so this describes the
budget for whichever model produced the headers, not the account as a whole."""

import re
import threading
import time

# "7.66s", "120ms", "2m59.56s", "1h2m3s", or a bare number of seconds.
_RESET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "": 1.0}

_lock = threading.Lock()

_state = {
    # tokens per minute
    "tokens_left": None,
    "tokens_cap": None,
    "tokens_reset_at": None,     # monotonic deadline, not a duration
    # requests per day
    "requests_left": None,
    "requests_cap": None,
    "requests_reset_at": None,
    # throttling (only set from a 429)
    "throttled_until": None,
    # session totals, from the response body rather than the headers
    "session_tokens": 0,
    "turns": 0,
    "updated_at": None,
}


def parse_reset(value) -> float:
    """A Groq reset string as seconds. Handles every form they send:
    "1.2s" -> 1.2, "120ms" -> 0.12, "2m59.56s" -> 179.56. Anything
    unrecognised is 0.0 rather than an exception."""
    try:
        if value is None:
            return 0.0
        text = str(value).strip().lower()
        if not text:
            return 0.0
        total, found = 0.0, False
        for number, unit in _RESET_RE.findall(text):
            if not number:
                continue
            total += float(number) * _UNITS.get(unit, 1.0)
            found = True
        return total if found else 0.0
    except Exception:
        return 0.0


def _as_int(value):
    """Header values are strings; anything unparseable is None, not zero -
    'unknown' and 'none left' must not look the same."""
    try:
        if value is None:
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _lower_keys(headers) -> dict:
    """Normalise whatever the SDK hands us. httpx.Headers is already
    case-insensitive, a plain dict is not, and either may turn up."""
    try:
        return {str(k).lower(): v for k, v in dict(headers).items()}
    except Exception:
        try:
            return {str(k).lower(): headers[k] for k in headers}
        except Exception:
            return {}


def record(headers, total_tokens=None) -> None:
    """Take the rate-limit picture from one response. Never raises.

    `total_tokens` is the response body's usage.total_tokens, which the
    headers don't carry - it's what lets us show a running session total.
    """
    try:
        head = _lower_keys(headers)
        now = time.monotonic()

        tokens_left = _as_int(head.get("x-ratelimit-remaining-tokens"))
        tokens_cap = _as_int(head.get("x-ratelimit-limit-tokens"))
        requests_left = _as_int(head.get("x-ratelimit-remaining-requests"))
        requests_cap = _as_int(head.get("x-ratelimit-limit-requests"))
        tokens_reset = head.get("x-ratelimit-reset-tokens")
        requests_reset = head.get("x-ratelimit-reset-requests")

        with _lock:
            if tokens_left is not None:
                _state["tokens_left"] = tokens_left
            if tokens_cap is not None:
                _state["tokens_cap"] = tokens_cap
            if requests_left is not None:
                _state["requests_left"] = requests_left
            if requests_cap is not None:
                _state["requests_cap"] = requests_cap
            if tokens_reset is not None:
                _state["tokens_reset_at"] = now + parse_reset(tokens_reset)
            if requests_reset is not None:
                _state["requests_reset_at"] = now + parse_reset(requests_reset)
            if total_tokens is not None:
                counted = _as_int(total_tokens)
                if counted is not None:
                    _state["session_tokens"] += counted
                    _state["turns"] += 1
            _state["updated_at"] = now
    except Exception as exc:                 # never let the meter bite
        print(f"[usage] couldn't read rate-limit headers: {exc!r}")


def record_throttled(retry_after) -> None:
    """A 429 happened. `retry_after` is the header of the same name, present
    only on a 429. Observation only - the caller's own backoff is unchanged."""
    try:
        wait = parse_reset(retry_after)
        with _lock:
            _state["throttled_until"] = time.monotonic() + max(wait, 0.0)
    except Exception as exc:
        print(f"[usage] couldn't record the throttle: {exc!r}")


def _remaining(deadline, now) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - now)


def _percent(left, cap):
    if left is None or not cap:
        return None
    try:
        return max(0.0, min(100.0, (left / cap) * 100.0))
    except (TypeError, ZeroDivisionError):
        return None


def snapshot() -> dict:
    """The current picture, safe to serialise straight to the dashboard.
    Anything unknown is None so the UI can show "--" rather than a wrong
    number. Never raises."""
    try:
        now = time.monotonic()
        with _lock:
            state = dict(_state)

        throttled_for = _remaining(state["throttled_until"], now)
        throttled = throttled_for is not None and throttled_for > 0

        return {
            "tokens_left": state["tokens_left"],
            "tokens_cap": state["tokens_cap"],
            "tokens_reset_s": _remaining(state["tokens_reset_at"], now),
            "requests_left": state["requests_left"],
            "requests_cap": state["requests_cap"],
            "requests_reset_s": _remaining(state["requests_reset_at"], now),
            "pct_tokens": _percent(state["tokens_left"], state["tokens_cap"]),
            "pct_requests": _percent(state["requests_left"], state["requests_cap"]),
            "throttled": throttled,
            "wait_s": throttled_for if throttled else None,
            "session_tokens": state["session_tokens"],
            "turns": state["turns"],
            # How long since anything was captured. The dashboard can use
            # this to grey out a picture that's gone old.
            "stale_s": (now - state["updated_at"]
                        if state["updated_at"] is not None else None),
        }
    except Exception as exc:
        print(f"[usage] snapshot failed: {exc!r}")
        return {"tokens_left": None, "tokens_cap": None, "tokens_reset_s": None,
                "requests_left": None, "requests_cap": None,
                "requests_reset_s": None, "pct_tokens": None,
                "pct_requests": None, "throttled": False, "wait_s": None,
                "session_tokens": 0, "turns": 0, "stale_s": None}


def reset() -> None:
    """Forget everything. Used between runs and by the demo script."""
    with _lock:
        _state.update({
            "tokens_left": None, "tokens_cap": None, "tokens_reset_at": None,
            "requests_left": None, "requests_cap": None,
            "requests_reset_at": None, "throttled_until": None,
            "session_tokens": 0, "turns": 0, "updated_at": None,
        })
