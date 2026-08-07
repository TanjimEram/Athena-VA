"""Prove wiring agents into run_agent changed nothing when the flag is off,
and narrows correctly when it's on.

    python agents_wiring_test.py

No model is called and no skill runs - Groq is replaced with a stub that
records exactly what would have been sent. The important check is the first
one: with AGENTS_ENABLED False, the request body must be identical to what
Athena sent before agents existed."""

import json
import sys

from athena import agents, brain, config, safety, skills

results = []
sent = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


class FakeMessage:
    def __init__(self, content="All done.", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeResponse:
    def __init__(self):
        self.choices = [type("C", (), {"message": FakeMessage()})()]


class FakeCompletions:
    def create(self, **kwargs):
        sent.append(kwargs)
        return FakeResponse()


class FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


def last_tools():
    """The tool names in the most recent request, or None if no tools key."""
    if not sent:
        return None
    body = sent[-1]
    if "tools" not in body:
        return None
    return sorted(t["function"]["name"] for t in body["tools"])


def last_system():
    return sent[-1]["messages"][0]["content"] if sent else ""


if __name__ == "__main__":
    config.get_groq_client = lambda: FakeClient()
    # Memory would try Supabase; the summary path isn't what we're testing.
    from athena import memory
    memory.log_interaction_async = lambda *a, **k: None

    all_enabled = sorted(t["function"]["name"] for t in brain._active_tools())
    print(f"=== baseline: {len(all_enabled)} tools enabled in settings ===\n")

    print("=== AGENTS_ENABLED False: nothing changes ===")
    config.AGENTS_ENABLED = False
    sent.clear()
    brain.run_agent("open notepad")
    check("sends the full tool list", last_tools() == all_enabled,
          f"{len(last_tools() or [])} tools")
    baseline_system = last_system()
    baseline_body = json.dumps(sent[-1].get("tools"), sort_keys=True)

    sent.clear()
    brain.run_agent("open notepad", agent="operator")
    check("an agent argument is IGNORED when the flag is off",
          last_tools() == all_enabled, f"{len(last_tools() or [])} tools")
    check("the tools payload is byte-identical",
          json.dumps(sent[-1].get("tools"), sort_keys=True) == baseline_body)
    check("the system prompt is untouched", last_system() == baseline_system)
    check("no agent prompt leaked in",
          "You are running the user's PC" not in last_system())

    print("\n=== AGENTS_ENABLED True: the list narrows ===")
    config.AGENTS_ENABLED = True
    for name in ("operator", "scribe", "analyst", "researcher", "general"):
        sent.clear()
        brain.run_agent("do the thing", agent=name)
        expected = sorted(n for n in agents.tool_names(name)
                          if n in set(all_enabled))
        got = last_tools()
        check(f"{name:11} offers {len(got or [])} tools", got == expected,
              f"wanted {len(expected)}")
        check(f"{name:11} prompt is prepended",
              last_system().startswith(agents.prompt_for(name)[:40]),
              last_system()[:50])
        check(f"{name:11} still has the base system prompt",
              "voice assistant" in last_system())

    print("\n=== no agent passed, flag on: unchanged ===")
    sent.clear()
    brain.run_agent("open notepad")
    check("full list when no agent given", last_tools() == all_enabled)

    print("\n=== an unknown agent falls back rather than breaking ===")
    sent.clear()
    brain.run_agent("do the thing", agent="wibble")
    check("unknown agent gets general's tools",
          last_tools() == sorted(n for n in agents.tool_names("general")
                                 if n in set(all_enabled)),
          f"{len(last_tools() or [])} tools")

    print("\n=== an empty tool set sends NO tools key, not an empty array ===")
    agents.AGENTS["_empty_test"] = {"prompt": "You have no tools.",
                                    "tools": set(), "model": None,
                                    "description": "test"}
    sent.clear()
    brain.run_agent("just talk to me", agent="_empty_test")
    check("no 'tools' key at all", "tools" not in sent[-1],
          str(list(sent[-1].keys())))
    check("no 'tool_choice' either", "tool_choice" not in sent[-1])
    check("last_tools() is None (not [])", last_tools() is None)
    del agents.AGENTS["_empty_test"]

    print("\n=== settings still win: a disabled skill stays out ===")
    from athena import settings
    victim = "open_app"
    was = settings.is_skill_enabled(victim)
    settings.set_skill_enabled(victim, False)
    try:
        sent.clear()
        brain.run_agent("open notepad", agent="operator")
        check(f"{victim} excluded even though operator claims it",
              victim not in (last_tools() or []), str(last_tools()))
        check("operator still offers its other tools",
              len(last_tools() or []) > 0, str(last_tools()))
    finally:
        settings.set_skill_enabled(victim, was)

    print("\n=== membership is not permission ===")
    confirm_in_agents = sorted({t for name in agents.AGENTS
                                for t in agents.tool_names(name)
                                if safety.classify(t) == "confirm"})
    print(f"  {len(confirm_in_agents)} confirm-tier tools sit in agent sets")
    check("safety.py still calls them confirm",
          all(safety.classify(t) == "confirm" for t in confirm_in_agents))
    check("still excluded from batching where they were",
          all(t in brain.NEVER_BATCHED for t in
              ("send_email", "delete_rows", "apply_sheet_operation")))

    print("\n=== signature is additive only ===")
    import inspect
    params = inspect.signature(brain.run_agent).parameters
    check("run_agent keeps its original parameters",
          list(params)[:4] == ["user_text", "history", "on_step", "confirm"],
          str(list(params)))
    check("agent is optional with a None default",
          params["agent"].default is None)
    chat_params = inspect.signature(brain._chat).parameters
    check("_chat's tools param is optional", chat_params["tools"].default is None)
    agent_chat_params = inspect.signature(brain._agent_chat).parameters
    check("_agent_chat's tools param is optional",
          agent_chat_params["tools"].default is None)

    config.AGENTS_ENABLED = False
    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
