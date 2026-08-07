"""Check the router sends requests to the right specialist, sticks where it
should, and never fails.

    python router_test.py            # keyword routing only, no model calls
    python router_test.py --model    # also allow the ambiguity fallback call

By default the model call is stubbed out, so this runs offline and free and
tells you how many requests the cheap path decided on its own. That number is
the point: a routing call on every turn would undo the token saving agents
exist for."""

import sys

from athena import agents

results = []
model_calls = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    line = f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else "")
    # The Windows console is cp1252; an emoji in a test case would crash the
    # printing, not the routing. Don't let the harness fail the code.
    print(line.encode("ascii", "backslashreplace").decode("ascii"))


# what the user says -> where it should go
CASES = [
    ("open notepad", "operator"),
    ("launch chrome for me", "operator"),
    ("set the volume to thirty", "operator"),
    ("lock the screen", "operator"),
    ("what's my battery level", "operator"),

    ("write a document called project notes", "scribe"),
    ("check my email", "scribe"),
    ("what's in my inbox", "scribe"),
    ("draft an email to sara about lunch", "scribe"),
    ("add a paragraph to my notes", "scribe"),

    ("open the spreadsheet", "analyst"),
    ("sort it by score descending", "analyst"),
    ("how many rows have a score over eighty", "analyst"),
    ("highlight rows where the city is london", "analyst"),
    ("delete row four", "analyst"),
    ("undo that", "analyst"),

    ("look up who won the match", "researcher"),
    ("what's the latest on the election", "researcher"),
    ("tell me about quantum computing", "researcher"),
    ("what's on my screen right now", "researcher"),

    ("hello", "general"),
    ("what can you do", "general"),
    ("thanks", "general"),
]

STICKY = [
    # (current agent, what they say next, expected)
    ("analyst", "now sort it by the second column", "analyst"),
    ("analyst", "and colour the top row yellow", "analyst"),
    ("analyst", "what about the total", "analyst"),
    ("scribe", "add another line about the budget", "scribe"),
    ("scribe", "now read it back to me", "scribe"),
    ("operator", "now lock the screen", "operator"),
    # clear jumps away
    ("analyst", "check my email", "scribe"),
    ("scribe", "open the spreadsheet and sort by name", "analyst"),
    ("operator", "look up the weather in dhaka", "researcher"),
    # explicit exits
    ("analyst", "back to normal", "general"),
    ("scribe", "that's enough", "general"),
    ("operator", "never mind", "general"),
]

NEVER_FAIL = ["", "   ", "asdfghjkl", "?????", "\n\n",
              "a" * 500, "🙂🙂🙂", "SELECT * FROM users;"]


if __name__ == "__main__":
    allow_model = "--model" in sys.argv

    real_ask = agents._ask_router
    def counting_ask(text):
        model_calls.append(text)
        if not allow_model:
            return None                 # forces the keyword fallback
        return real_ask(text)
    agents._ask_router = counting_ask

    print("=== routing from a standing start (no current agent) ===")
    for text, expected in CASES:
        got = agents.route(text)
        scores = agents.score(text)
        top = ", ".join(f"{k}={v}" for k, v in sorted(scores.items(),
                                                      key=lambda kv: -kv[1]) if v)
        check(f"{text!r:52} -> {got}", got == expected,
              f"wanted {expected}; scores: {top or 'none'}")

    print("\n=== sticky routing ===")
    for current, text, expected in STICKY:
        got = agents.route(text, current)
        check(f"[{current}] {text!r:44} -> {got}", got == expected,
              f"wanted {expected}")

    print("\n=== never fails ===")
    for text in NEVER_FAIL:
        try:
            got = agents.route(text)
            check(f"{text[:24]!r:28} -> {got}", agents.exists(got), got)
        except Exception as exc:
            check(f"{text[:24]!r:28} raised", False, f"{type(exc).__name__}: {exc}")
    try:
        got = agents.route("open notepad", "not-a-real-agent")
        check("unknown current agent survives", agents.exists(got), got)
        got = agents.route("hello", None)
        check("None current agent survives", agents.exists(got), got)
    except Exception as exc:
        check("bad current agent raised", False, str(exc))

    print("\n=== an empty request keeps you where you are ===")
    check("empty text stays in analyst", agents.route("", "analyst") == "analyst")
    check("empty text with no agent -> general", agents.route("") == "general")

    print("\n=== how often the cheap path was enough ===")
    total_routed = len(CASES) + len(STICKY)
    decided_free = total_routed - len(model_calls)
    print(f"  {decided_free}/{total_routed} decided by keywords alone "
          f"({int(decided_free / total_routed * 100)}%)")
    if model_calls:
        print("  needed a model call:")
        for text in model_calls:
            print(f"    - {text!r}")
    check("at least 80% decided without a model call",
          decided_free / total_routed >= 0.8,
          f"{decided_free}/{total_routed}")

    print("\n=== a broken router call still returns a real agent ===")
    def boom(text): raise RuntimeError("network down")
    agents._ask_router = boom
    try:
        got = agents.route("something quite ambiguous about a thing")
        check("survives a raising router", agents.exists(got), got)
    except Exception as exc:
        check("survives a raising router", False, str(exc))
    agents._ask_router = real_ask

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
