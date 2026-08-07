"""Check consultant mode: entered and left only when asked, no tools, memory
in context, and a slower voice.

    python consultant_test.py

No model is called and nothing is spoken - Groq and Supabase are both stubbed.
The point of this test is the entry and exit rules: Athena must never decide
on her own that someone needs this mode, and never decide they're finished."""

import sys

from athena import agents, brain, config, tts

results = []
sent = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    line = f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else "")
    print(line.encode("ascii", "backslashreplace").decode("ascii"))


class FakeCompletions:
    def create(self, **kwargs):
        sent.append(kwargs)
        msg = type("M", (), {"content": "Mm.", "tool_calls": None})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


# Things that sound like distress but are NOT a request for the mode. Athena
# must not enter on any of these - inferring it is exactly the mistake.
NOT_ENTRY = [
    "i'm really stressed about work",
    "today was awful",
    "i feel terrible",
    "everything is going wrong",
    "i'm exhausted and fed up",
    "my boss is driving me mad",
    "i'm so sad about it",
]

ENTRY = [
    "consultant mode",
    "i need to talk",
    "can we talk",
    "let's talk",
    "i need to vent",
]

EXIT = ["back to normal", "that's enough", "i'm done talking", "we're done"]


if __name__ == "__main__":
    print("=== the agent exists and has no tools ===")
    check("consultant is registered", agents.exists("consultant"))
    check("it has NO tools", agents.tool_names("consultant") == set(),
          str(agents.tool_names("consultant")))
    check("tools_for returns an empty list", agents.tools_for("consultant") == [])
    check("prompt is the placeholder awaiting your text",
          "PLACEHOLDER" in agents.prompt_for("consultant"))

    print("\n=== entered only when asked ===")
    for text in ENTRY:
        check(f"{text!r:40} enters", agents.route(text) == "consultant",
              agents.route(text))

    print("\n=== NEVER entered from how someone sounds ===")
    for text in NOT_ENTRY:
        got = agents.route(text)
        check(f"{text!r:40} -> {got}", got != "consultant", got)

    print("\n=== once in it, ordinary talk does not pull you out ===")
    for text in ["sort the spreadsheet by score", "open notepad",
                 "check my email", "what's my battery", "look up the weather",
                 "i feel a bit better now", "anyway"]:
        got = agents.route(text, "consultant")
        check(f"{text!r:40} stays", got == "consultant", got)

    print("\n=== left only when asked ===")
    for text in EXIT:
        got = agents.route(text, "consultant")
        check(f"{text!r:40} exits", got == "general", got)

    print("\n=== it is never scored, so it can't be routed into by accident ===")
    check("not in the scoring table",
          "consultant" not in agents.score("i need to talk about the spreadsheet"))

    print("\n=== memory goes in front of it ===")
    from athena import memory
    memory.recent = lambda n=5: [
        {"user_text": "i've been putting off the thesis again",
         "athena_reply": "You mentioned that last week too."},
        {"user_text": "my supervisor pushed the deadline", "athena_reply": "Okay."},
    ]
    memory.search = lambda q, n=3: [
        {"user_text": "i keep avoiding the thesis chapter"}]
    memory.log_interaction_async = lambda *a, **k: None

    context = agents.extra_context("consultant", "i still haven't started")
    print(f"  context:\n    " + context.replace("\n", "\n    ")[:300])
    check("recent exchanges are included", "thesis" in context)
    check("a search hit is included", "avoiding the thesis" in context)
    check("other agents get nothing", agents.extra_context("analyst", "x") == "")

    print("\n=== memory being down doesn't break the mode ===")
    def boom(*a, **k): raise RuntimeError("supabase unreachable")
    memory.recent, memory.search = boom, boom
    context = agents.extra_context("consultant", "hello")
    check("survives a memory failure", isinstance(context, str))
    check("and says so rather than pretending to remember",
          "don't imply that you remember" in context, context[:80])

    print("\n=== the request that actually goes out ===")
    memory.recent = lambda n=5: [{"user_text": "the thesis again",
                                  "athena_reply": "Mm."}]
    memory.search = lambda q, n=3: []
    config.get_groq_client = lambda: FakeClient()
    config.AGENTS_ENABLED = True
    sent.clear()
    brain.run_agent("i still haven't started", agent="consultant")
    body = sent[-1]
    check("no tools key at all", "tools" not in body, str(list(body.keys())))
    check("no tool_choice", "tool_choice" not in body)
    check("consultant prompt is in the system message",
          "PLACEHOLDER" in body["messages"][0]["content"])
    check("memory is in the messages",
          any("thesis" in str(m.get("content", "")) for m in body["messages"]))

    print("\n=== the voice slows down ===")
    from athena import settings
    saved_rate = settings.get("voice_rate", 0)
    saved_pitch = settings.get("voice_pitch", 0)
    settings.set("voice_rate", 0)
    settings.set("voice_pitch", 0)
    try:
        tts.set_mode(None)
        check("normal mode is unmodified", tts.mode() is None)
        tts.set_mode("consultant")
        check("mode is set", tts.mode() == "consultant")
        # The strings edge-tts would receive, built the same way speak does.
        rate = tts._pct(int(settings.get("voice_rate", config.TTS_RATE))
                        + config.CONSULTANT_TTS_RATE)
        pitch = tts._hz(int(settings.get("voice_pitch", config.TTS_PITCH))
                        + config.CONSULTANT_TTS_PITCH)
        print(f"  rate={rate!r} pitch={pitch!r}")
        check("rate is slower and correctly signed", rate == "-8%", rate)
        check("pitch is lower and correctly signed", pitch == "-4Hz", pitch)
        check("no equals-sign workaround needed (Python API, not CLI)",
              not rate.startswith("="))
        # user settings compose rather than being overwritten
        settings.set("voice_rate", 10)
        composed = tts._pct(int(settings.get("voice_rate", config.TTS_RATE))
                            + config.CONSULTANT_TTS_RATE)
        check("stacks on the user's own rate", composed == "+2%", composed)
        tts.set_mode(None)
    finally:
        settings.set("voice_rate", saved_rate)
        settings.set("voice_pitch", saved_pitch)

    config.AGENTS_ENABLED = False
    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
