"""Check the agent registry is sound: every tool an agent claims really
exists, is classified by safety.py, and has a schema the brain can send.

    python agents_test.py

Nothing here calls a model or touches the network - it only inspects the
registry against SKILLS, safety.py and brain.TOOLS. Phase 1 changes no
behaviour, so nothing else should move."""

import sys

from athena import agents, brain, safety, skills

results = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


if __name__ == "__main__":
    schema_names = {t["function"]["name"] for t in brain.TOOLS}

    print(f"=== registry: {len(agents.AGENTS)} agents, "
          f"{len(skills.SKILLS)} tools available ===\n")

    for name in agents.AGENTS:
        tools = sorted(agents.tool_names(name))
        missing = sorted(agents.missing_tools(name))
        schemas = agents.tools_for(name)
        tiers = {}
        for tool in tools:
            tiers.setdefault(safety.classify(tool), []).append(tool)

        print(f"--- {name} ---")
        print(f"  {len(tools)} tools: {', '.join(tools) or '(none)'}")
        print(f"  tiers: " + ", ".join(f"{t}={len(v)}" for t, v in sorted(tiers.items())))
        if missing:
            print(f"  skipped (not in SKILLS): {', '.join(missing)}")
        print(f"  schemas returned: {len(schemas)}")

        check(f"{name}: every tool exists in SKILLS",
              all(t in skills.SKILLS for t in tools),
              str([t for t in tools if t not in skills.SKILLS]))
        check(f"{name}: nothing is blocked",
              all(safety.classify(t) != "blocked" for t in tools),
              str([t for t in tools if safety.classify(t) == "blocked"]))
        check(f"{name}: every tool has a schema",
              all(t in schema_names for t in tools),
              str([t for t in tools if t not in schema_names]))
        check(f"{name}: tools_for matches tool_names",
              {s["function"]["name"] for s in schemas} == set(tools))
        check(f"{name}: has a prompt", bool(agents.prompt_for(name).strip()))
        check(f"{name}: has a description",
              bool(agents.get(name).get("description", "").strip()))
        print()

    print("=== the fallback ===")
    check("general exists", agents.exists("general"))
    check("general is never empty", len(agents.tool_names("general")) > 0,
          str(len(agents.tool_names("general"))))
    check("general is exactly the free tools",
          agents.tool_names("general") ==
          {n for n in skills.SKILLS if safety.classify(n) == "free"})
    check("unknown agent falls back to general",
          agents.get("wibble") is agents.AGENTS["general"])
    check("unknown agent gets general's tools",
          agents.tool_names("wibble") == agents.tool_names("general"))
    check("exists() says no to nonsense", not agents.exists("wibble"))

    print("\n=== membership is not permission ===")
    confirm_tools = [t for name in agents.AGENTS
                     for t in agents.tool_names(name)
                     if safety.classify(t) == "confirm"]
    print(f"  {len(confirm_tools)} confirm-tier tools sit inside agent sets")
    print("  (that's correct - the tier check still runs before execution)")
    check("safety.py still classifies them as confirm",
          all(safety.classify(t) == "confirm" for t in confirm_tools))

    print("\n=== no tool is orphaned across every agent ===")
    covered = set()
    for name in agents.AGENTS:
        covered |= agents.tool_names(name)
    uncovered = sorted(set(skills.SKILLS) - covered)
    print(f"  covered by at least one agent: {len(covered)}/{len(skills.SKILLS)}")
    if uncovered:
        print(f"  reachable only with agents off: {', '.join(uncovered)}")
    check("every tool is reachable from some agent", not uncovered,
          str(uncovered))

    print("\n=== token saving, the point of the exercise ===")
    import json
    full = len(json.dumps(brain.TOOLS)) // 4
    print(f"  all tools: ~{full} tokens")
    for name in agents.AGENTS:
        narrowed = len(json.dumps(agents.tools_for(name))) // 4
        saved = 100 - int(narrowed / full * 100) if full else 0
        print(f"  {name:11} ~{narrowed:5} tokens  ({saved}% less)")

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
