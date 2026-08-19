"""Simulate rate limits and check the chain advances and recovers.

    python providers_test.py

No network. Keys are faked in the environment so the chain can be exercised
whatever you actually have configured, and the real environment is restored
at the end."""

import os
import sys
import time

from athena import config, providers

results = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


def names(rows):
    return [p["name"] for p in rows]


if __name__ == "__main__":
    saved = {p["key_env"]: os.environ.get(p["key_env"]) for p in config.PROVIDERS}
    try:
        # Pretend every provider has a key, so the whole chain is in play.
        for provider in config.PROVIDERS:
            os.environ[provider["key_env"]] = "test-key"
        providers.reset()

        print("=== the chain, all keys present ===")
        check("all five configured", len(providers.configured()) == 5,
              str(names(providers.configured())))
        check("order is the config order",
              names(providers.available()) ==
              [p["name"] for p in config.PROVIDERS])
        check("current is the first", providers.current_name() == "groq",
              providers.current_name())

        print("\n=== a 429 on groq advances to gemini ===")
        providers.mark_cooldown("groq", 30, "429")
        check("groq is skipped", "groq" not in names(providers.available()))
        check("current is now gemini", providers.current_name() == "gemini",
              providers.current_name())
        check("the rest are untouched",
              names(providers.available()) ==
              ["gemini", "mistral", "nvidia_nim", "groq_small"],
              str(names(providers.available())))

        print("\n=== cascading failures walk down the chain ===")
        for name, expected in [("gemini", "mistral"), ("mistral", "nvidia_nim"),
                               ("nvidia_nim", "groq_small")]:
            providers.mark_cooldown(name, 30, "429")
            check(f"{name} down -> {expected}",
                  providers.current_name() == expected, providers.current_name())

        print("\n=== everything cooling ===")
        providers.mark_cooldown("groq_small", 45, "429")
        check("no provider available", providers.available() == [])
        check("current() is None", providers.current() is None)
        check("current_name says cooling",
              providers.current_name() == "cooling", providers.current_name())
        wait = providers.shortest_wait()
        check("shortest wait is the soonest one",
              wait is not None and 29 <= wait <= 30.1, str(wait))

        print("\n=== recovery: it rejoins at its usual place, not the end ===")
        providers.reset("groq")
        check("groq is back at the front", providers.current_name() == "groq",
              providers.current_name())
        check("shortest_wait is None once one is ready",
              providers.shortest_wait() is None)

        print("\n=== a cooldown really does expire on its own ===")
        providers.reset()
        providers.mark_cooldown("groq", 0.6, "429")
        check("skipped immediately", providers.current_name() == "gemini",
              providers.current_name())
        time.sleep(0.8)
        check("back by itself after the wait",
              providers.current_name() == "groq", providers.current_name())

        print("\n=== tool-capable selection ===")
        providers.reset()
        check("first tool-capable is groq",
              providers.for_tools()["name"] == "groq")
        providers.mark_cooldown("groq", 30, "429")
        check("unverified providers are NOT assumed tool-capable",
              providers.for_tools()["name"] == "nvidia_nim",
              providers.for_tools()["name"] + " (gemini/mistral are None)")
        providers.mark_cooldown("nvidia_nim", 30, "429")
        check("falls to groq_small",
              providers.for_tools()["name"] == "groq_small")
        providers.mark_cooldown("groq_small", 30, "429")
        check("none tool-capable -> None", providers.for_tools() is None)
        check("but a conversational provider is still available",
              providers.current_name() == "gemini", providers.current_name())

        print("\n=== cooldown length from headers ===")
        providers.reset()
        for headers, want, label in [
            ({"retry-after": "8"}, 8.0, "retry-after wins"),
            ({"x-ratelimit-reset-requests": "2m59.56s"}, 179.56, "reset-requests"),
            ({"x-ratelimit-reset-tokens": "7.66s"}, 7.66, "reset-tokens"),
            ({"retry-after": "5", "x-ratelimit-reset-tokens": "99s"}, 5.0,
             "retry-after beats reset"),
            ({}, providers.DEFAULT_COOLDOWN, "nothing -> default"),
            (None, providers.DEFAULT_COOLDOWN, "no headers -> default"),
            ({"retry-after": "garbage"}, providers.DEFAULT_COOLDOWN, "junk -> default"),
        ]:
            got = providers.cooldown_from_headers(headers)
            check(f"{label}: {got:.2f}s", abs(got - want) < 0.01, f"wanted {want}")

        print("\n=== a wild header value is capped, not obeyed ===")
        actual = providers.mark_cooldown("groq", 99999, "429")
        check(f"capped at {providers.MAX_COOLDOWN:.0f}s",
              actual == providers.MAX_COOLDOWN, f"{actual}s")
        providers.reset()

        print("\n=== missing keys drop out of the chain ===")
        del os.environ["MISTRAL_API_KEY"]
        del os.environ["NVIDIA_API_KEY"]
        check("only keyed providers are configured",
              names(providers.configured()) == ["groq", "gemini", "groq_small"],
              str(names(providers.configured())))
        providers.mark_cooldown("groq", 30, "429")
        check("skips straight past the unkeyed ones",
              providers.current_name() == "gemini", providers.current_name())
        providers.reset()

        print("\n=== no keys at all ===")
        for provider in config.PROVIDERS:
            os.environ.pop(provider["key_env"], None)
        check("configured is empty", providers.configured() == [])
        check("current_name says none",
              providers.current_name() == "none", providers.current_name())
        check("shortest_wait is None, not an error",
              providers.shortest_wait() is None)
        check("for_tools is None", providers.for_tools() is None)

        print("\n=== status() is dashboard-safe ===")
        for provider in config.PROVIDERS:
            os.environ[provider["key_env"]] = "test-key"
        providers.reset()
        providers.mark_cooldown("gemini", 20, "429 from gemini")
        import json
        state = providers.status()
        print("  " + json.dumps(state)[:200] + "...")
        check("serialises", isinstance(json.dumps(state), str))
        check("active is groq", state["active"] == "groq", state["active"])
        check("gemini marked cooling",
              any(r["name"] == "gemini" and r["cooling"] for r in state["providers"]))
        check("reason carried through",
              any(r["reason"] == "429 from gemini" for r in state["providers"]))
        check("enabled reflects the flag",
              state["enabled"] == config.PROVIDER_FALLBACK_ENABLED)
        check("training-data flags exposed",
              {r["name"] for r in state["providers"] if r["trains_on_prompts"]}
              == {"gemini", "mistral"})

        print("\n=== hostile input never raises ===")
        for bad in [None, "", "not-a-provider", 42]:
            providers.mark_cooldown(bad, 5, "x")
            providers.reset(bad)
        providers.mark_cooldown("groq", None, "")
        providers.mark_cooldown("groq", -5, "")
        providers.reset()
        check("survived", True)

    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        providers.reset()

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
