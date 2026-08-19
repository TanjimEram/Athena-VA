"""Which provider Athena is talking to, and which ones are resting.

The chain lives in config.PROVIDERS, ordered best first. This module tracks
which of them have a key, which are in cooldown after a rate limit, and
therefore which one a request should go to right now.

A provider that returns 429 is put aside for however long the response says
to wait - retry-after if it's there, the reset header if not, a sensible
default otherwise. While it's cooling it is skipped. When the wait is up it
rejoins the chain at its usual place, so a temporary throttle on the fast
provider doesn't demote it for the rest of the session.

Cooldown lives in memory and dies with the process. That's deliberate: the
windows it tracks are seconds to minutes, and a stale cooldown read off disk
at startup would skip a provider that had recovered hours ago.

Nothing here makes a network call or decides anything about a reply. It
answers "who's available" and is told "that one just failed"."""

import os
import threading
import time

from athena import config

# What to wait when a provider says it's throttled but not for how long.
DEFAULT_COOLDOWN = 60.0
# Never rest a provider longer than this, however alarming the header. A
# provider parked for an hour is one we've effectively lost for the session.
MAX_COOLDOWN = 15 * 60.0

class NoToolProvider(Exception):
    """Raised when an action is needed but every provider that can still be
    reached is one we haven't confirmed does tool calling. Saying so is the
    point - quietly answering in words would look like Athena deciding not
    to act, which is a different and much more confusing failure."""


_clients: dict = {}
_lock = threading.Lock()
# name -> monotonic time when it becomes usable again
_cooling: dict = {}
# name -> why it was last put aside, for the dashboard and the log
_reasons: dict = {}


def _entries() -> list:
    try:
        return list(config.PROVIDERS)
    except Exception:
        return []


def has_key(provider: dict) -> bool:
    """True if this provider's key is actually present in the environment."""
    try:
        return bool(os.getenv(provider.get("key_env", "")))
    except Exception:
        return False


def configured() -> list:
    """Every provider with a key, in chain order - cooling or not."""
    return [p for p in _entries() if has_key(p)]


def _cooling_for(name: str, now: float) -> float:
    """Seconds this provider still has to rest, 0 if it's ready."""
    until = _cooling.get(name)
    if until is None:
        return 0.0
    return max(0.0, until - now)


def available() -> list:
    """Providers with a key that aren't resting, in chain order. May be
    empty - that means everything is cooling, which the caller must handle
    rather than treating as 'no providers configured'."""
    now = time.monotonic()
    with _lock:
        return [p for p in configured() if _cooling_for(p["name"], now) <= 0]


def current() -> dict | None:
    """The provider a request would go to right now, or None if they're all
    resting or none has a key."""
    ready = available()
    return ready[0] if ready else None


def current_name() -> str:
    """The active provider's name, or a word saying why there isn't one."""
    provider = current()
    if provider:
        return provider["name"]
    return "cooling" if configured() else "none"


def for_tools() -> dict | None:
    """The first available provider that can actually do tool calling.

    supports_tools is None for providers we haven't measured yet - those are
    treated as NOT tool-capable here, because guessing wrong means Athena
    silently stops taking actions. Phase 5's test is what turns a None into
    a True."""
    for provider in available():
        if provider.get("supports_tools") is True:
            return provider
    return None


def mark_cooldown(name: str, seconds: float | None = None,
                  reason: str = "") -> float:
    """Put a provider aside. Returns how long it will actually rest, which
    may be less than asked for - see MAX_COOLDOWN."""
    try:
        wait = DEFAULT_COOLDOWN if not seconds or seconds <= 0 else float(seconds)
        wait = min(wait, MAX_COOLDOWN)
        with _lock:
            _cooling[name] = time.monotonic() + wait
            _reasons[name] = reason or "rate limited"
        print(f"[providers] {name} resting {wait:.0f}s ({_reasons[name]})")
        return wait
    except Exception as exc:
        print(f"[providers] couldn't set a cooldown for {name}: {exc!r}")
        return 0.0


def reset(name: str | None = None) -> None:
    """Bring a provider back early, or all of them if name is None."""
    with _lock:
        if name is None:
            _cooling.clear()
            _reasons.clear()
        else:
            _cooling.pop(name, None)
            _reasons.pop(name, None)


def shortest_wait() -> float | None:
    """When the first provider becomes usable again. None if one already is,
    or if none is configured at all."""
    now = time.monotonic()
    with _lock:
        waits = [_cooling_for(p["name"], now) for p in configured()]
    waits = [w for w in waits if w > 0]
    if not waits or len(waits) < len(configured()):
        return None          # at least one is ready, so there's no waiting
    return min(waits)


def status() -> dict:
    """The picture for the dashboard. Safe to serialise, never raises."""
    try:
        now = time.monotonic()
        with _lock:
            rows = []
            for provider in _entries():
                name = provider["name"]
                keyed = has_key(provider)
                resting = _cooling_for(name, now) if keyed else 0.0
                rows.append({
                    "name": name,
                    "model": provider.get("model", ""),
                    "configured": keyed,
                    "cooling": resting > 0,
                    "cooling_s": round(resting, 1) if resting > 0 else None,
                    "reason": _reasons.get(name, "") if resting > 0 else "",
                    "supports_tools": provider.get("supports_tools"),
                    "trains_on_prompts": bool(provider.get("trains_on_prompts")),
                })
        return {
            "enabled": bool(getattr(config, "PROVIDER_FALLBACK_ENABLED", False)),
            "active": current_name(),
            "shortest_wait_s": shortest_wait(),
            "providers": rows,
        }
    except Exception as exc:
        print(f"[providers] status failed: {exc!r}")
        return {"enabled": False, "active": "none", "shortest_wait_s": None,
                "providers": []}


def client_for(provider: dict):
    """An API client pointed at this provider, built once and reused.

    The OpenAI SDK, for every provider including Groq. The groq SDK looks
    like it would work - it accepts a base_url - but it hardcodes
    "/openai/v1/chat/completions" onto whatever you give it, so it can only
    ever reach Groq. Mistral is at /v1/chat/completions and Gemini at
    /v1beta/openai/chat/completions, and neither is reachable that way. That
    was found by provider_tools_test.py returning 404s from every provider,
    which is what that script is for.

    The cost of using a second SDK is that these raise openai.* exceptions,
    not groq.*, and brain.py's handlers expect groq.*. brain._request
    translates at the boundary so nothing upstream has to know."""
    name = provider["name"]
    with _lock:
        if name in _clients:
            return _clients[name]
    import openai
    client = openai.OpenAI(api_key=os.getenv(provider["key_env"]),
                           base_url=provider["base_url"], max_retries=0,
                           timeout=45.0)
    with _lock:
        _clients[name] = client
    return client


def cooldown_from_headers(headers, default: float = DEFAULT_COOLDOWN) -> float:
    """How long to rest, read out of a 429's headers. retry-after first, then
    whichever reset header is present, then the default. Reuses usage.py's
    parser so "2m59.56s" and "120ms" are understood the same way here."""
    try:
        from athena import usage
        head = usage._lower_keys(headers)
        for key in ("retry-after", "x-ratelimit-reset-requests",
                    "x-ratelimit-reset-tokens"):
            raw = head.get(key)
            if raw:
                seconds = usage.parse_reset(raw)
                if seconds > 0:
                    return seconds
    except Exception as exc:
        print(f"[providers] couldn't read a cooldown from headers: {exc!r}")
    return default
