"""Prove the fallback chain works underneath brain, and that with the flag
off nothing about the old behaviour moved.

    python fallback_test.py

No network. Every provider is replaced by a stub that can be told to succeed
or to raise a real groq.RateLimitError."""

import os
import sys

import groq

from athena import brain, config, providers

results = []
calls = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


class Raw:
    headers = {"x-ratelimit-limit-tokens": "12000",
               "x-ratelimit-remaining-tokens": "9000"}

    def __init__(self, name):
        self.name = name

    def parse(self):
        msg = type("M", (), {"content": f"reply from {self.name}",
                             "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()],
                              "usage": type("U", (), {"total_tokens": 100})()})()


def rate_limited(retry_after="4"):
    """A genuine groq.RateLimitError, the way the SDK builds one."""
    import httpx
    request = httpx.Request("POST", "https://example.invalid/v1/chat")
    response = httpx.Response(429, headers={"retry-after": retry_after},
                              request=request)
    return groq.RateLimitError("rate limited", response=response, body=None)


def stub_client(name, fail_with=None):
    def create(_self, **kwargs):
        calls.append((name, kwargs.get("model")))
        if fail_with is not None:
            raise fail_with
        return Raw(name)
    return type("Client", (), {
        "chat": type("Chat", (), {
            "completions": type("Comp", (), {
                "with_raw_response": type("W", (), {"create": create})()})()})()})()


if __name__ == "__main__":
    saved_flag = config.PROVIDER_FALLBACK_ENABLED
    saved_keys = {p["key_env"]: os.environ.get(p["key_env"])
                  for p in config.PROVIDERS}
    behaviour = {}          # provider name -> exception or None

    try:
        for provider in config.PROVIDERS:
            os.environ[provider["key_env"]] = "test-key"
        providers.client_for = lambda p: stub_client(p["name"], behaviour.get(p["name"]))

        # ---------- flag OFF ----------
        print("=== flag OFF: the old path, untouched ===")
        config.PROVIDER_FALLBACK_ENABLED = False
        providers.reset(); calls.clear()
        default_used = []
        config.get_groq_client = lambda: stub_client("DEFAULT_CLIENT")
        out = brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
        check("uses the default Groq client", calls[-1][0] == "DEFAULT_CLIENT",
              calls[-1][0])
        check("reply comes back", "DEFAULT_CLIENT" in out.choices[0].message.content)
        check("no provider was put in cooldown",
              all(not r["cooling"] for r in providers.status()["providers"]))

        # ---------- flag ON, happy path ----------
        print("\n=== flag ON: primary answers, no fallback ===")
        config.PROVIDER_FALLBACK_ENABLED = True
        providers.reset(); calls.clear(); behaviour.clear()
        out = brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
        # Read the model from config rather than hardcoding it - providers
        # retire models, and a test that pins one rots silently.
        primary = config.PROVIDERS[0]["model"]
        check("went to groq with its configured model",
              calls == [("groq", primary)], str(calls))
        check("reply from groq", "groq" in out.choices[0].message.content)
        check("nothing announced a switch", brain.took_fallback() is False)

        # ---------- one provider throttled ----------
        print("\n=== groq is 429: falls through to gemini ===")
        providers.reset(); calls.clear()
        behaviour["groq"] = rate_limited("4")
        out = brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
        check("tried groq then gemini", [c[0] for c in calls] == ["groq", "gemini"],
              str([c[0] for c in calls]))
        check("used gemini's own model name",
              calls[-1][1] == config.PROVIDERS[1]["model"], str(calls[-1][1]))
        check("reply came from gemini", "gemini" in out.choices[0].message.content)
        check("groq is now cooling",
              any(r["name"] == "groq" and r["cooling"]
                  for r in providers.status()["providers"]))
        check("cooldown came from retry-after (~4s)",
              3 < [r for r in providers.status()["providers"]
                   if r["name"] == "groq"][0]["cooling_s"] <= 4.1)
        check("the switch is announced once", brain.took_fallback() is True)
        check("and not twice", brain.took_fallback() is False)

        # ---------- a cooling provider is skipped without being tried ----------
        print("\n=== the next call skips the cooling one entirely ===")
        calls.clear()
        brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
        check("groq not even attempted", "groq" not in [c[0] for c in calls],
              str([c[0] for c in calls]))

        # ---------- cascade ----------
        print("\n=== every provider 429s ===")
        providers.reset(); calls.clear()
        for name in ("groq", "gemini", "mistral", "nvidia_nim", "groq_small"):
            behaviour[name] = rate_limited("6")
        try:
            brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
            check("raised when the chain is spent", False, "no exception")
        except groq.RateLimitError:
            check("raised the ORIGINAL groq.RateLimitError", True)
        except Exception as exc:
            check("raised the ORIGINAL groq.RateLimitError", False,
                  type(exc).__name__)
        check("tried all five once each", len(calls) == 5, str(len(calls)))
        check("did not loop", [c[0] for c in calls] ==
              ["groq", "gemini", "mistral", "nvidia_nim", "groq_small"],
              str([c[0] for c in calls]))

        print("\n=== with everything cooling, it says how long ===")
        calls.clear()
        try:
            brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
            check("stopped rather than calling", False, "it called anyway")
        except brain._AllProvidersCooling as exc:
            check("stopped without a single call", not calls, str(calls))
            check("carries the shortest wait",
                  exc.wait_s is not None and 5 < exc.wait_s <= 6.1, str(exc.wait_s))

        # ---------- tool calling ----------
        print("\n=== tool calls only go to providers we've measured ===")
        providers.reset(); calls.clear(); behaviour.clear()
        behaviour["groq"] = rate_limited("5")
        brain._chat([{"role": "user", "content": "hi"}], use_tools=True,
                    tools=[{"type": "function",
                            "function": {"name": "x", "parameters": {}}}])
        check("skipped gemini and mistral (supports_tools None)",
              [c[0] for c in calls] == ["groq", "nvidia_nim"],
              str([c[0] for c in calls]))

        print("\n=== no tool-capable provider left ===")
        providers.reset()
        for name in ("groq", "nvidia_nim", "groq_small"):
            providers.mark_cooldown(name, 60, "429")
        calls.clear()
        try:
            brain._chat([{"role": "user", "content": "hi"}], use_tools=True,
                        tools=[{"type": "function",
                                "function": {"name": "x", "parameters": {}}}])
            check("refused to guess", False, "it called something")
        except providers.NoToolProvider:
            check("raised NoToolProvider rather than answering in words", True)
            check("made no call at all", not calls, str(calls))
        check("a conversational call still works",
              "gemini" in brain._chat([{"role": "user", "content": "hi"}],
                                      use_tools=False).choices[0].message.content)

        # ---------- errors we must NOT fall through on ----------
        print("\n=== a malformed request is our bug, not a provider's ===")
        providers.reset(); calls.clear(); behaviour.clear()
        import httpx
        req = httpx.Request("POST", "https://example.invalid/v1/chat")
        behaviour["groq"] = groq.BadRequestError(
            "tool_use_failed", response=httpx.Response(400, request=req), body=None)
        try:
            brain._chat([{"role": "user", "content": "hi"}], use_tools=False)
            check("propagated", False, "swallowed")
        except groq.BadRequestError:
            check("propagates immediately", True)
        check("only groq was tried", [c[0] for c in calls] == ["groq"],
              str([c[0] for c in calls]))
        check("groq NOT put in cooldown for our own bad request",
              not any(r["name"] == "groq" and r["cooling"]
                      for r in providers.status()["providers"]))

    finally:
        config.PROVIDER_FALLBACK_ENABLED = saved_flag
        for key, value in saved_keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        providers.reset()

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
