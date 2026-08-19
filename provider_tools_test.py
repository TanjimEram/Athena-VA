"""Does each provider actually do tool calling with OUR schema?

    python provider_tools_test.py                # every configured provider
    python provider_tools_test.py gemini mistral # just these

This sends the real brain.TOOLS schema and three fixed utterances to each
provider that has a key, and reports whether the right tool came back with
the right arguments. It makes real API calls - a handful per provider - so
it costs a little budget. Run it once, read the table, then decide.

It does NOT edit config. supports_tools stays whatever it is until you
change it yourself, having seen this output."""

import json
import sys
import time

from athena import brain, config, providers

# One utterance per shape that matters: a plain action, an action with a
# number to get right, and something that must NOT call a tool at all.
CASES = [
    {"say": "open notepad",
     "want_tool": "open_app",
     "check": lambda a: "notepad" in str(a.get("name", "")).lower(),
     "why": "simple action"},
    {"say": "set the volume to 30",
     "want_tool": "set_volume",
     "check": lambda a: str(a.get("level")) in ("30", "30.0"),
     "why": "action with an argument"},
    {"say": "what can you do?",
     "want_tool": None,
     "check": lambda a: True,
     "why": "must NOT call a tool"},
]

SYSTEM = ("You are Athena, a voice assistant. Call a tool only when the user "
          "gives a clear command to perform an action. If they are asking a "
          "question or chatting, answer in words and call no tool.")


def run_case(client, model, case, tools):
    """(ok, detail) for one utterance against one provider."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": case["say"]}],
            tools=tools, tool_choice="auto",
            temperature=0, max_tokens=300)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"

    try:
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None) or []

        if case["want_tool"] is None:
            if calls:
                return False, f"called {calls[0].function.name} (should not have)"
            return True, "answered in words"

        if not calls:
            return False, "no tool call"
        name = calls[0].function.name
        if name != case["want_tool"]:
            return False, f"called {name}"
        try:
            args = json.loads(calls[0].function.arguments or "{}")
        except json.JSONDecodeError:
            return False, f"unparseable args: {calls[0].function.arguments[:40]}"
        if not case["check"](args):
            return False, f"wrong args: {args}"
        return True, f"{name}({args})"
    except Exception as exc:
        return False, f"unreadable response: {type(exc).__name__}"


if __name__ == "__main__":
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    tools = brain.TOOLS
    print(f"Sending the real schema: {len(tools)} tools, "
          f"~{len(json.dumps(tools)) // 4} tokens per call.\n")

    rows = []
    for provider in config.PROVIDERS:
        name = provider["name"]
        if wanted and name not in wanted:
            continue
        if not providers.has_key(provider):
            rows.append((name, provider["model"], "NO KEY", [], None))
            print(f"--- {name}: no {provider['key_env']} set, skipping ---\n")
            continue

        print(f"--- {name} ({provider['model']}) ---")
        client = providers.client_for(provider)
        outcomes, elapsed = [], []
        for case in CASES:
            started = time.monotonic()
            ok, detail = run_case(client, provider["model"], case, tools)
            elapsed.append(time.monotonic() - started)
            outcomes.append(ok)
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {case['why']:<24} {case['say']!r}")
            print(f"         -> {detail}")
            time.sleep(1.2)          # 1 req/sec on mistral; be polite to all
        avg = sum(elapsed) / len(elapsed) if elapsed else 0
        rows.append((name, provider["model"], None, outcomes, avg))
        print()

    print("=" * 72)
    print(f"{'provider':<12} {'passed':<8} {'avg':<8} {'config says':<13} verdict")
    print("=" * 72)
    for name, model, skipped, outcomes, avg in rows:
        declared = next(p["supports_tools"] for p in config.PROVIDERS
                        if p["name"] == name)
        declared_text = {True: "True", False: "False", None: "None (unset)"}[declared]
        if skipped:
            print(f"{name:<12} {'-':<8} {'-':<8} {declared_text:<13} not tested (no key)")
            continue
        passed = sum(outcomes)
        verdict = ("tool calling works" if passed == len(CASES)
                   else "partial - read above" if passed else "NO tool calling")
        print(f"{name:<12} {str(passed) + '/' + str(len(CASES)):<8} "
              f"{avg:.1f}s{'':<3} {declared_text:<13} {verdict}")
    print("=" * 72)
    print("\nNothing was changed. If a provider passed 3/3 and config says")
    print("None, that's the one to flip to True in config.PROVIDERS.")
