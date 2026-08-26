"""Prove the calendar is actually wired in, all four steps of it.

    python calendar_wiring_test.py

The four-step rule - function, SKILLS dict, brain.TOOLS schema, tier in
safety.py - is the kind of thing that rots quietly. A skill with no schema is
invisible to the model; a schema with no tier is BLOCKED by safety.classify
and fails at the moment someone asks for it, which is the worst time to find
out. So this checks all four for every calendar skill, and checks the whole
project for the same gaps while it's here.

It also checks the things that must NOT be true: that no calendar write can
ride along inside a batch, and that every one of them refuses to run without
a confirmation.

Nothing here touches the network, the calendar, or Supabase.
"""

import sys

from athena import brain, calendar_skill, followup, safety, skills

PASSED = 0
FAILED = 0

READ_SKILLS = ["get_current_time", "read_schedule", "next_event",
               "find_event", "pending_followups"]
WRITE_SKILLS = ["create_event", "reschedule_event", "cancel_event"]
CALENDAR_SKILLS = READ_SKILLS + WRITE_SKILLS


def check(label: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


def tool_names() -> set:
    return {t["function"]["name"] for t in brain.TOOLS}


def four_step_checks() -> None:
    print("\n--- step 1: the function exists and is callable ---")
    for name in CALENDAR_SKILLS:
        check(f"skills.{name}", callable(getattr(skills, name, None)), True)

    print("\n--- step 2: it's in the SKILLS dict ---")
    for name in CALENDAR_SKILLS:
        check(f"SKILLS[{name!r}]", name in skills.SKILLS, True)

    print("\n--- step 3: the model can see it (brain.TOOLS) ---")
    names = tool_names()
    for name in CALENDAR_SKILLS:
        check(f"TOOLS has {name}", name in names, True)

    print("\n--- step 4: it has a tier, so it isn't silently blocked ---")
    for name in READ_SKILLS:
        check(f"{name} is free", safety.classify(name), "free")
    for name in WRITE_SKILLS:
        check(f"{name} needs confirming", safety.classify(name), "confirm")

    print("\n--- and the four steps agree with each other, project-wide ---")
    missing_schema = sorted(set(skills.SKILLS) - names)
    check("every skill the model could call has a schema", missing_schema, [])
    missing_skill = sorted(names - set(skills.SKILLS))
    check("every schema has a skill behind it", missing_skill, [])
    blocked = sorted(name for name in skills.SKILLS
                     if safety.classify(name) == "blocked")
    check("no skill is left untiered (classify would block it)", blocked, [])


def safety_checks() -> None:
    print("\n--- no calendar write rides along in a batch ---")
    for name in WRITE_SKILLS:
        check(f"{name} is never batched", name in brain.NEVER_BATCHED, True)
    check("...for the same reason send_email isn't",
          "send_email" in brain.NEVER_BATCHED, True)

    print("\n--- each one asks in its own words ---")
    for name in WRITE_SKILLS:
        check(f"{name} is self-confirming", name in brain.SELF_CONFIRMING, True)
    check("so the generic phrasing never reads the raw words back",
          any(name in brain.CONFIRM_PHRASING for name in WRITE_SKILLS), False)

    print("\n--- and the guard actually holds, not just the set membership ---")
    # Driving _handle_call directly: deterministic, offline, and it exercises
    # the exact code path run_agent uses. Live model runs found both of the
    # holes below; these keep them shut.
    import json as _json

    class FakeCall:
        def __init__(self, name, args):
            self.function = type("f", (), {"name": name,
                                           "arguments": _json.dumps(args)})()

    def handle(name, args, ran_sigs, in_batch=False):
        return brain._handle_call(FakeCall(name, args), lambda q: True,
                                  set(), ran_sigs, in_batch)

    step, _, _ = handle("create_event", {"title": "X", "start": "tomorrow at 3"},
                        ran_sigs={}, in_batch=True)
    check("bundled into one reply: skipped", step["status"], "skipped")

    # The chained case - a second round after something else already ran.
    step, _, _ = handle("create_event", {"title": "X", "start": "tomorrow at 3"},
                        ran_sigs={"get_system_info:{}": "battery is fine"})
    check("chained after another action: skipped too", step["status"], "skipped")
    check("...and the model is told it did NOT happen",
          "ask me for it on its own" in step["result"], True)

    # The retry case - same tool again with cosmetically different arguments.
    step, _, _ = handle(
        "create_event", {"title": "X", "start": "tomorrow at 3",
                         "description": ""},
        ran_sigs={'create_event:{"start": "tomorrow at 3", "title": "X"}': "Done"})
    check("a retry with drifted arguments cannot double-book",
          step["status"], "skipped")
    check("...and says so plainly",
          "not doing it twice" in step["result"], True)

    step, _, _ = handle("read_schedule", {"when": "today"},
                        ran_sigs={"get_system_info:{}": "fine"})
    check("a READ still chains freely", step["status"], "ran")

    print("\n--- reading is free, but only reading ---")
    check("pending_followups is free - asking is not a write",
          safety.classify("pending_followups"), "free")
    check("...and it is not batched into other work either, since it talks",
          "pending_followups" in brain.NEVER_BATCHED, False)

    print("\n--- with no confirm hook, every write refuses ---")
    real_confirm = calendar_skill._confirm
    real_service = calendar_skill._service
    calendar_skill.set_confirm(None)
    calendar_skill._service = lambda: (object(), "")
    try:
        for name, args in (
                ("create_event", {"title": "X", "start": "tomorrow at 3"}),
                ("reschedule_event", {"event_identifier": "X",
                                      "new_start": "tomorrow at 3"}),
                ("cancel_event", {"event_identifier": "X"})):
            said = skills.SKILLS[name](**args)
            check(f"{name} writes nothing without a way to ask",
                  "left your calendar alone" in said
                  or "couldn't find anything" in said
                  or said.startswith("I couldn't"), True)
    finally:
        calendar_skill._service = real_service
        calendar_skill.set_confirm(real_confirm)


def schema_checks() -> None:
    print("\n--- the schemas tell the model the right thing ---")
    by_name = {t["function"]["name"]: t["function"] for t in brain.TOOLS}

    create = by_name["create_event"]
    check("create_event requires a title and a start",
          sorted(create["parameters"]["required"]), ["start", "title"])
    check("and end is optional, so the default hour applies",
          "end" in create["parameters"]["required"], False)
    check("the schema tells the model NOT to compute a date",
          "user's own words" in create["parameters"]["properties"]["start"]["description"],
          True)
    check("...and says so in the description too",
          "do NOT convert" in create["description"]
          or "Never a timestamp" in
          create["parameters"]["properties"]["start"]["description"], True)

    check("read_schedule takes 'when' and nothing is required",
          by_name["read_schedule"]["parameters"].get("required", []), [])
    for name in ("get_current_time", "next_event", "pending_followups"):
        check(f"{name} takes no arguments",
              by_name[name]["parameters"]["properties"], {})

    print("\n--- the schema arguments match the functions ---")
    import inspect
    for name in CALENDAR_SKILLS:
        params = set(inspect.signature(skills.SKILLS[name]).parameters)
        declared = set(by_name[name]["parameters"]["properties"])
        check(f"{name}: every declared argument really exists",
              sorted(declared - params), [])
        required = set(by_name[name]["parameters"].get("required", []))
        check(f"{name}: every required argument exists", sorted(required - params), [])


def wiring_checks() -> None:
    print("\n--- main.py wires the hooks it has to ---")
    source = open("athena/main.py", encoding="utf-8").read()
    check("calendar_skill.set_confirm is wired to the shared confirm",
          "calendar_skill.set_confirm(_chain_confirm)" in source, True)
    check("followup.set_io is wired so the orb reacts",
          "followup.set_io(" in source, True)
    check("the session counter is reset per session",
          "followup.reset_session()" in source, True)
    check("and the startup trigger is called exactly once",
          source.count("followup.on_startup()"), 1)
    check("nothing schedules it on a timer",
          any(word in source for word in
              ("Timer(", "schedule.every", "followup_interval")), False)

    print("\n--- she says what she can actually do ---")
    from athena import config
    check("'what can you do' now mentions the calendar",
          "calendar" in config.CAPABILITIES.lower(), True)
    check("...and doesn't promise anything about it that isn't built",
          "remind" in config.CAPABILITIES.lower(), False)

    print("\n--- the startup gate is a real setting with a real default ---")
    from athena import settings
    check("followup_on_startup has a default",
          settings.DEFAULTS.get("followup_on_startup"), True)


if __name__ == "__main__":
    print("=== Athena calendar, phase 5: wired in ===")
    four_step_checks()
    safety_checks()
    schema_checks()
    wiring_checks()
    print(f"\n{PASSED} passed, {FAILED} failed.")
    sys.exit(1 if FAILED else 0)
