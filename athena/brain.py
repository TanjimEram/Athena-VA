"""The core decision-maker. think() sends the user's words to the LLM with a
list of available tools. If the model picks a tool, the safety gate decides
whether to run it now, ask the user to confirm, or refuse. Everything the
rest of the pipeline needs comes back in one dict."""

import json
import re
import time

import groq

from athena import config, memory, safety, skills

DEFAULT_PERSONALITY = ("calm, warm, and intelligent, with a composed presence "
                       "like FRIDAY from Iron Man")


def _system_prompt() -> str:
    """Built at CALL TIME so a personality change in the dashboard applies on
    the next reply without a restart."""
    from athena import settings
    personality = settings.get("personality", DEFAULT_PERSONALITY) or DEFAULT_PERSONALITY
    return (
        f"You are {config.ASSISTANT_NAME}, a voice assistant on the user's Windows "
        f"PC. You are {personality}. Everything you say is read aloud by a voice, "
        "so it must sound natural when spoken: one or two short sentences, never "
        "paragraphs. No markdown, no bullet points, no lists, no emoji. Be "
        "concise and never repeat the user's request back to them. If you can't "
        "do something, say so plainly in one sentence.\n\n"
        "WHEN TO USE A TOOL - read this carefully:\n"
        "Only call a tool when the user gives a CLEAR command to perform an "
        "action (open, close, set, search, play, lock, show me, do X). If the "
        "user is asking a question, chatting, or asking what you can do, DO NOT "
        "call any tool - just answer in words. When you're unsure whether "
        "something is a request for action or just conversation, do NOT act: "
        "answer, and ask if they'd like you to do it. When you do perform an "
        "action, confirm it briefly and naturally, like 'Chrome's open.' rather "
        "than 'I have successfully opened Google Chrome.' Never invent tools you "
        "don't have.\n\n"
        "Examples:\n"
        "- User: \"what can you do?\" -> No tool. Briefly describe your abilities: "
        f"you can {config.CAPABILITIES}.\n"
        "- User: \"open chrome\" -> Call open_app with name Chrome.\n"
        "- User: \"what's the weather like?\" -> No tool; answer in words. Only "
        "search the web if they clearly ask you to look it up.\n"
        "- User: \"how are you?\" -> No tool; just chat.\n"
        "- User: \"lock my screen\" -> Call lock_screen."
    )

# One schema entry per skill. The model reads these descriptions to decide
# which tool fits, so keep them plain and specific.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Launch an application on the user's Windows PC, e.g. Chrome, Notepad, Spotify.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Friendly name of the app to open, e.g. 'Chrome'.",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_website",
            "description": "Open a website in the user's default browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to open, e.g. 'https://youtube.com'.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "OPEN a Google search in the user's browser (opens a tab; does "
                "NOT read or speak the answer). Only use when the user asks to "
                "'open a search', 'search in the browser', 'Google this', or "
                "otherwise wants a browser tab. To answer a question out loud, "
                "use look_up or research instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_up",
            "description": (
                "Answer a factual or current-events question by searching the "
                "web and SPEAKING the answer - for 'what is', 'who is', "
                "'what's the latest on', 'how much is', anything you can't "
                "answer from memory. Reads the results and gives a short "
                "spoken answer; does not open a browser tab."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to answer.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": (
                "A deeper multi-source summary, spoken aloud - for 'research "
                "X', 'tell me about X', 'summarize the latest on X'. Searches "
                "several sources, reads them, and gives a concise spoken "
                "summary of the key points."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic to research.",
                    }
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "Set the system volume to a percentage between 0 and 100.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "Target volume from 0 (mute) to 100 (max).",
                    }
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_screen",
            "description": "Lock the Windows session so a password is needed to get back in.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Report the PC's current status: CPU, memory, battery.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "guide_me",
            "description": (
                "Walk the user through an on-screen task step by step by "
                "reading their screen - use for 'guide me through X', 'help me "
                "do X', 'walk me through X'. Athena reads the screen and speaks "
                "one instruction at a time; she does not click anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "What the user wants to accomplish.",
                    }
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_dashboard",
            "description": ("Open the Athena dashboard - show the full dashboard "
                            "on screen (expands the floating orb into the HUD)."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_dashboard",
            "description": ("Close or minimise the dashboard back to the orb - "
                            "hide the dashboard."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "see_screen",
            "description": (
                "Look at the user's screen or active window and answer a "
                "question about what's shown. Use for 'what's this error', "
                "'what am I looking at', 'read this', 'what does this say', "
                "'take a screenshot and tell me...'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What the user wants to know about the screen.",
                    },
                    "focus": {
                        "type": "string",
                        "enum": ["screen", "window"],
                        "description": "Look at the whole 'screen' or just the active 'window'.",
                    },
                },
                "required": ["question"],
            },
        },
    },
]


def _chat(messages: list, use_tools: bool, max_tokens: int | None = None,
          temperature: float | None = None):
    """One completion call to Groq. Model + reply-length read from settings
    at CALL TIME so dashboard changes apply on the next turn."""
    from athena import settings
    kwargs = dict(
        model=settings.get("brain_model", config.BRAIN_MODEL),
        messages=messages,
        temperature=config.BRAIN_TEMPERATURE if temperature is None else temperature,
        max_tokens=max_tokens or int(settings.get("brain_max_tokens", config.BRAIN_MAX_TOKENS)),
    )
    if use_tools:
        kwargs["tools"] = _active_tools()
        kwargs["tool_choice"] = "auto"
    return config.get_groq_client().chat.completions.create(**kwargs)


def _active_tools() -> list:
    """The tool list minus any skills the user has toggled off in settings -
    so disabled skills are truly removed from the brain's options."""
    from athena import settings
    return [t for t in TOOLS
            if settings.is_skill_enabled(t["function"]["name"])]


_memory_context = None


def _get_memory_context() -> str:
    """Fetch the last few logged interactions ONCE per session and format
    them for the model, so 'what did I ask you earlier' works across
    restarts. Within a session, `history` already covers it."""
    global _memory_context
    if _memory_context is None:
        # recent() is newest-first; reverse to chronological so the summary
        # reads oldest -> newest in the prompt.
        rows = list(reversed(memory.recent(5)))
        if rows:
            lines = [
                f"- They said: {row['user_text']} / You replied: {row['athena_reply']}"
                for row in rows
            ]
            _memory_context = (
                "From your long-term memory, the user's most recent past "
                "interactions with you (possibly from earlier sessions):\n"
                + "\n".join(lines)
            )
        else:
            _memory_context = ""
    return _memory_context


def think(user_text: str, history: list | None = None) -> dict:
    """Turn the user's words into a spoken reply and (maybe) an action,
    then log the exchange to long-term memory.

    history is an optional list of prior {'role': ..., 'content': ...} dicts.
    Returns {'reply_text', 'tool_called', 'args', 'needs_confirmation'}.
    """
    result = _think(user_text, history)
    # Fire-and-forget so saving never delays the spoken reply.
    memory.log_interaction_async(
        user_text, result["reply_text"], result["tool_called"], result["args"] or None
    )
    return result


def _build_messages(user_text: str, history: list | None) -> list:
    messages = [{"role": "system", "content": _system_prompt()}]
    memory_context = _get_memory_context()
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    if history:
        messages.extend(history[-config.HISTORY_MAX_MESSAGES:])
    messages.append({"role": "user", "content": user_text})
    return messages


def _think(user_text: str, history: list | None = None) -> dict:
    messages = _build_messages(user_text, history)

    try:
        response = _chat(messages, use_tools=True)
    except groq.BadRequestError as exc:
        # llama-3.3 sometimes garbles its tool-call syntax and Groq rejects
        # the whole request ("tool_use_failed"). A fresh attempt usually
        # comes out clean; if not, answer in words rather than error out.
        if "tool_use_failed" not in str(exc):
            print(f"[brain] bad request: {exc}")
            return _result("Something went wrong on my end. Try that once more.")
        try:
            response = _chat(messages, use_tools=True)
        except Exception:
            # Two garbled attempts: give up on tools for this turn. The
            # words-only reply must not pretend an action happened.
            retry_messages = messages + [{
                "role": "system",
                "content": (
                    "Your tools are unavailable for this reply. Answer in "
                    "words only. Do not claim to have opened, searched, or "
                    "done anything - just talk."
                ),
            }]
            try:
                response = _chat(retry_messages, use_tools=False)
            except Exception as retry_exc:
                print(f"[brain] retry without tools failed: {retry_exc!r}")
                return _result("Something went wrong on my end. Try that once more.")
    except groq.RateLimitError:
        return _result("I'm being rate-limited right now. Give me a few seconds and ask again.")
    except groq.APIConnectionError:
        return _result("I can't reach my language model — check the internet connection.")
    except groq.AuthenticationError:
        return _result("My API key was rejected. Check GROQ_API_KEY in the .env file.")
    except Exception as exc:
        print(f"[brain] unexpected error: {exc}")
        return _result("Something went wrong on my end. Try that once more.")

    message = response.choices[0].message

    # No tool call: the model just wants to talk.
    if not message.tool_calls:
        return _result(message.content or "I'm not sure what to say to that.")

    # Models occasionally return several calls; we act on the first and note it.
    call = message.tool_calls[0]
    tool_name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}

    verdict = safety.classify(tool_name)

    if verdict == "blocked":
        return _result(
            f"Sorry, I'm not allowed to do that. {tool_name} is off-limits for me.",
            tool_called=tool_name,
            args=args,
        )

    if verdict == "confirm":
        pretty_args = ", ".join(f"{k} {v}" for k, v in args.items()) or "that"
        return _result(
            f"Just to confirm, you want me to {tool_name.replace('_', ' ')}"
            + (f" with {pretty_args}?" if args else "?")
            + " Say yes and I'll do it.",
            tool_called=tool_name,
            args=args,
            needs_confirmation=True,
        )

    # verdict == "free": run the skill now and speak its REAL result.
    # reply_text is exactly what the skill returned - never a made-up success.
    try:
        reply = skills.SKILLS[tool_name](**args)
    except Exception as exc:
        print(f"[brain] skill {tool_name} failed: {exc!r}")
        reply = (
            f"I tried to {tool_name.replace('_', ' ')} but it failed: "
            f"{type(exc).__name__}: {exc}."
        )
    return _result(reply, tool_called=tool_name, args=args)


def think_stream(user_text: str, history: list | None = None):
    """Streaming version of think() for low time-to-first-word.

    Returns (generator, result): the generator yields text pieces as the
    model writes them - feed it straight into tts.speak_stream. `result`
    is the same dict think() returns; it is fully filled in once the
    generator is exhausted (plus 'first_token_s', the model's latency to
    its first piece of text). Tool calls still work: a free tool runs and
    its honest result is yielded as the spoken confirmation; confirm and
    blocked tiers yield the question/refusal. On any streaming failure it
    falls back to the non-streaming think() path and yields that reply."""
    result = {"reply_text": "", "tool_called": None, "args": {},
              "needs_confirmation": False, "first_token_s": None}

    def generate():
        started = time.monotonic()
        tool_acc: dict[int, dict] = {}
        from athena import settings
        try:
            stream = config.get_groq_client().chat.completions.create(
                model=settings.get("brain_model", config.BRAIN_MODEL),
                messages=_build_messages(user_text, history),
                tools=_active_tools(),
                tool_choice="auto",
                temperature=config.BRAIN_TEMPERATURE,
                max_tokens=int(settings.get("brain_max_tokens", config.BRAIN_MAX_TOKENS)),
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    if result["first_token_s"] is None:
                        result["first_token_s"] = time.monotonic() - started
                    result["reply_text"] += delta.content
                    yield delta.content
                for tc in delta.tool_calls or []:
                    slot = tool_acc.setdefault(tc.index, {"name": "", "args": ""})
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
        except Exception as exc:
            # Streaming failed (rate limit, garbled tool syntax, network):
            # fall back to the sturdy non-streaming ladder in one piece.
            print(f"[brain] stream failed, falling back: {type(exc).__name__}")
            fallback = _think(user_text, history)
            result.update(fallback)
            if result["first_token_s"] is None:
                result["first_token_s"] = time.monotonic() - started
            memory.log_interaction_async(
                user_text, result["reply_text"], result["tool_called"],
                result["args"] or None)
            yield fallback["reply_text"]
            return

        if tool_acc:
            slot = tool_acc[min(tool_acc)]
            tool_name = slot["name"]
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result["tool_called"] = tool_name
            result["args"] = args
            if result["first_token_s"] is None:
                result["first_token_s"] = time.monotonic() - started

            verdict = safety.classify(tool_name)
            if verdict == "blocked":
                text = f"Sorry, I'm not allowed to do that. {tool_name} is off-limits for me."
            elif verdict == "confirm":
                pretty = ", ".join(f"{k} {v}" for k, v in args.items()) or "that"
                text = (
                    f"Just to confirm, you want me to {tool_name.replace('_', ' ')}"
                    + (f" with {pretty}?" if args else "?")
                    + " Say yes and I'll do it."
                )
                result["needs_confirmation"] = True
            else:
                try:
                    text = skills.SKILLS[tool_name](**args)
                except Exception as exc:
                    print(f"[brain] skill {tool_name} failed: {exc!r}")
                    text = (
                        f"I tried to {tool_name.replace('_', ' ')} but it failed: "
                        f"{type(exc).__name__}: {exc}."
                    )
            if result["reply_text"]:
                result["reply_text"] += " "
            result["reply_text"] += text
            yield text
        elif not result["reply_text"]:
            text = "I'm not sure what to say to that."
            result["reply_text"] = text
            yield text

        memory.log_interaction_async(
            user_text, result["reply_text"], result["tool_called"],
            result["args"] or None)

    return generate(), result


def run_confirmed(tool_name: str, args: dict) -> str:
    """Execute a tool the user has already said yes to. Called by the layer
    that handles the user's confirmation (main.py or the test CLI)."""
    if safety.classify(tool_name) == "blocked" or tool_name not in skills.SKILLS:
        return "Sorry, that action isn't allowed."
    try:
        reply = skills.SKILLS[tool_name](**args)
    except Exception as exc:
        print(f"[brain] skill {tool_name} failed: {exc!r}")
        reply = (
            f"I tried to {tool_name.replace('_', ' ')} but it failed: "
            f"{type(exc).__name__}: {exc}."
        )
    memory.log_interaction_async(f"(confirmed: {tool_name})", reply, tool_name, args)
    return reply


def _result(reply_text: str, tool_called: str | None = None,
            args: dict | None = None, needs_confirmation: bool = False) -> dict:
    """Build the standard dict every call to think() returns."""
    return {
        "reply_text": reply_text,
        "tool_called": tool_called,
        "args": args or {},
        "needs_confirmation": needs_confirmation,
    }


# ============================ multi-step agent ============================

MAX_AGENT_STEPS = 5  # cap the loop so a runaway plan can't spin forever

AGENT_GUIDANCE = (
    "If - and only if - the user clearly asked you to perform actions, you "
    "may use tools to carry them out. A question, a greeting, or 'what can "
    "you do' is NOT a request for action: answer in words and call no tool. "
    "When the request DOES contain several clear instructions and they are "
    "INDEPENDENT (none needs another's result), emit ALL of their tool calls "
    "together in one reply. Only when a later action depends on an earlier "
    "action's result, do those one at a time so you can react to what came "
    "back. Never repeat an action that already ran, and don't retry one the "
    "user declined or that was blocked. When every requested action has been "
    "attempted, reply with ONE short spoken summary - a sentence or two."
)


def run_agent(user_text: str, history: list | None = None,
              on_step=None, confirm=None) -> dict:
    """Fulfil a possibly multi-step request. The model may return several
    tool calls at once (independent steps) or one at a time (dependent
    steps, results fed back so it can decide the next action). Safety is
    enforced per step: free runs, blocked is refused, confirm asks via the
    `confirm` callback and can be skipped without killing the chain.

    on_step(step: dict)  called as each step finishes, for live UI logging
        step = {tool, args, tier, status, result}
        status in {"ran", "skipped", "blocked", "failed"}
    confirm(question: str) -> bool   asked for each confirm-tier step

    Returns {"reply_text": final_spoken_summary, "steps": [step, ...]}.
    """
    messages = _build_messages(user_text, history)
    messages.insert(1, {"role": "system", "content": AGENT_GUIDANCE})

    steps: list[dict] = []
    declined: set[str] = set()   # tools the user declined - don't re-ask
    ran_sigs: dict[str, str] = {}  # signature -> result, to block re-execution
    model_said = ""              # the model's own text when it stops calling tools

    for _ in range(MAX_AGENT_STEPS):
        response, err = _agent_chat(messages)
        if response is None:
            model_said = model_said or err
            break

        message = response.choices[0].message

        # No tool calls -> the model is done deciding actions.
        if not message.tool_calls:
            model_said = (message.content or "").strip()
            break

        messages.append(_assistant_tool_msg(message))

        new_calls = 0   # NEW (non-duplicate) actions this round
        for call in message.tool_calls:
            step, feedback, duplicate = _handle_call(call, confirm, declined, ran_sigs)
            # A duplicate of an already-run action is fed back (so the model
            # stops repeating) but not re-executed, re-logged, or re-counted.
            if not duplicate:
                new_calls += 1
                steps.append(step)
                if on_step is not None:
                    try:
                        on_step(dict(step))   # logged ONCE, with the real outcome
                    except Exception:
                        pass
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": feedback,
            })

        # Token-saving fast path: if the model emitted a BATCH of independent
        # actions (2+ in one round), assume the request is fulfilled and go
        # straight to the ledger summary - no extra "are you done?" round-trip.
        # The summary call sees every result, so it can still synthesise or
        # compare across them. Single-tool rounds fall through and loop, so a
        # dependent next action can react to what just came back.
        if new_calls >= 2:
            break
        # A round that only repeated already-done/declined actions -> spinning.
        if new_calls == 0:
            break

    # Build the final spoken summary. Crucially, we DON'T trust the model's
    # own free-form recap (it hallucinates "done" for skipped/failed steps).
    #   0 steps  -> plain chat: use what the model said
    #   1 step   -> speak its real result verbatim (honest, fast, no extra call)
    #   2+ steps -> one natural summary GROUNDED in the actual step ledger
    if not steps:
        final_text = model_said or "I'm not sure what to say to that."
    elif len(steps) == 1:
        final_text = steps[0]["result"] or model_said or "Done."
    else:
        final_text = _summarize_from_ledger(user_text, steps) or _fallback_summary(steps)

    memory.log_interaction_async(
        user_text, final_text,
        steps[0]["tool"] if steps else None,
        steps[0]["args"] if steps else None,
    )
    return {"reply_text": final_text, "steps": steps}


# --- minimal stand-ins so a recovered tool call flows through run_agent
#     exactly like a real Groq response would ---
class _RecoveredFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _RecoveredCall:
    def __init__(self, name, arguments):
        self.id = f"recovered_{id(self)}"
        self.type = "function"
        self.function = _RecoveredFn(name, arguments)


class _RecoveredMessage:
    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content


class _RecoveredResponse:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


def _wait_phrase(seconds: int | None) -> str:
    """A natural spoken 'try again in ...' phrase for the rate-limit message."""
    if not seconds:
        return "Give me a moment and ask again."
    if seconds < 90:
        return f"Try again in about {seconds} seconds."
    minutes = round(seconds / 60)
    return f"Try again in about {minutes} minute{'s' if minutes != 1 else ''}."


def _retry_after_seconds(exc) -> int | None:
    """How long to wait after a rate limit, from Groq's 'try again in Xs'
    message or the reset header. Rounded up to whole seconds."""
    import math
    try:
        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
        for key in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            val = headers.get(key)
            if val:
                m = re.match(r"(?:(\d+)m)?([\d.]+)s?", str(val).strip())
                if m:
                    mins = int(m.group(1)) if m.group(1) else 0
                    secs = float(m.group(2)) if m.group(2) else 0
                    total = mins * 60 + secs
                    if total > 0:
                        return max(1, math.ceil(total))
    except Exception:
        pass
    m = re.search(r"try again in ([\d.]+)s", str(exc))
    if m:
        return max(1, math.ceil(float(m.group(1))))
    return None


def _failed_generation(exc) -> str:
    """Pull the model's malformed output out of a tool_use_failed error."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        fg = body.get("error", {}).get("failed_generation")
        if fg:
            return fg
    m = re.search(r"'failed_generation':\s*'(.*?)'\s*\}\s*\}?\s*$", str(exc), re.DOTALL)
    return m.group(1) if m else str(exc)


def _parse_call_args(segment: str) -> dict:
    """Pull arguments out of one malformed tool-call fragment - either a JSON
    blob (`{"name":"Notepad"}`) or a key=value (`query=lofi`)."""
    json_m = re.search(r"\{.*?\}", segment, re.DOTALL)
    if json_m:
        try:
            parsed = json.loads(json_m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    kv = re.search(r"([a-zA-Z_]\w*)\s*=\s*([^<>{}]+)", segment)
    if kv:
        return {kv.group(1): kv.group(2).strip().strip('"').strip("'")}
    return {}


def _recover_tool_calls(exc):
    """Parse the intended tool call(s) from a rejected malformed generation
    and build a synthetic response so the actions still happen - including a
    whole PARALLEL batch. Handles the shapes llama-3.3 emits, e.g.
    <function=web_search>query=...</function> and
    <function=open_app={"name":"Notepad"}>. Returns a response or None."""
    text = _failed_generation(exc)
    calls = []
    for part in re.split(r"(?=<function\s*=)", text):
        nm = re.match(r"<function\s*=\s*([a-zA-Z_]\w*)", part)
        if not nm or nm.group(1) not in skills.SKILLS:
            continue
        name = nm.group(1)
        args = _parse_call_args(part[nm.end():])
        calls.append(_RecoveredCall(name, json.dumps(args)))
    if not calls:
        return None
    print(f"[brain] recovered {len(calls)} malformed tool call(s): "
          f"{[c.function.name for c in calls]}")
    return _RecoveredResponse(_RecoveredMessage(calls))


def _agent_chat(messages: list):
    """One agent model call, hardened against the intermittent failures that
    could wreck a live demo. Returns (response, None) on success, or
    (None, friendly_message) once all options are spent.

    llama-3.3 sometimes emits malformed tool-call syntax Groq rejects
    ('tool_use_failed'). We retry with rising temperature (same prompt at
    the same temperature reproduces the same bad output); if it still won't
    parse, we recover the model's INTENDED call from the error and run it
    anyway. Rate limits back off; connection errors fail fast."""
    for attempt in range(4):
        temp = config.BRAIN_TEMPERATURE + 0.15 * attempt
        try:
            return _chat(messages, use_tools=True, max_tokens=512, temperature=temp), None
        except groq.BadRequestError as exc:
            if "tool_use_failed" not in str(exc):
                print(f"[brain] agent bad request: {str(exc)[:160]}")
                return None, "Sorry, I couldn't work that one out. Could you rephrase it?"
            if attempt < 3:
                continue  # resample
            recovered = _recover_tool_calls(exc)  # last resort: parse intent
            if recovered is not None:
                return recovered, None
            return None, "Sorry, I couldn't work that one out. Could you rephrase it?"
        except groq.RateLimitError as exc:
            # Do NOT retry on a rate limit - more requests only dig the hole
            # deeper. Fail fast with how long to wait (parsed from Groq).
            wait = _retry_after_seconds(exc)
            return None, f"I've hit my usage limit. {_wait_phrase(wait)}"
        except groq.APIConnectionError:
            return None, "I can't reach my language model — check the internet connection."
        except Exception as exc:
            print(f"[brain] agent error: {exc!r}")
            return None, "Something went wrong on my end. Try that once more."
    return None, "Sorry, I couldn't work that one out. Could you rephrase it?"


def _handle_call(call, confirm, declined: set, ran_sigs: dict) -> tuple[dict, str, bool]:
    """Classify one tool call, enforce its safety tier (asking `confirm` for
    confirm-tier steps), run it if allowed, and return (step, feedback,
    duplicate). `feedback` is what the model sees next: the real result for
    steps that ran, or an explicit NOT-DONE notice otherwise. `duplicate` is
    True when this exact action already ran this turn - the caller feeds the
    result back (to stop the model repeating) but doesn't re-run or re-count
    it. `declined` suppresses re-asking a confirm the user rejected;
    `ran_sigs` remembers what already executed to prevent duplicate side
    effects."""
    tool = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):   # model sometimes sends "null" for no-arg tools
        args = {}

    sig = tool + ":" + json.dumps(args, sort_keys=True, default=str)
    tier = safety.classify(tool)
    step = {"tool": tool, "args": args, "tier": tier, "status": "ran", "result": ""}
    NOT_DONE = ("NOT DONE. This action did NOT run. Do not retry it and do "
                "not claim it happened.")

    # Already executed this exact action -> don't do it again.
    if sig in ran_sigs:
        step["result"] = ran_sigs[sig]
        return step, f"Already done earlier: {ran_sigs[sig]} Do not repeat it.", True

    if tier == "blocked" or tool not in skills.SKILLS:
        step["status"] = "blocked"
        step["result"] = f"Not allowed: {tool} is off-limits."
        return step, NOT_DONE, False

    # Confirm-before-acting can be turned off in settings; then confirm-tier
    # actions run straight away (read at call time so the toggle is live).
    from athena import settings
    if tier == "confirm" and settings.get("confirm_before_acting", True):
        if tool in declined:                     # already said no this turn
            step["status"] = "skipped"
            step["result"] = f"Skipped {tool.replace('_', ' ')} (already declined)."
            return step, NOT_DONE, True          # duplicate: don't re-log
        pretty = ", ".join(f"{v}" for v in args.values())
        question = (f"{tool.replace('_', ' ').capitalize()}"
                    + (f" to {pretty}" if pretty else "")
                    + "? Yes or no.")
        approved = bool(confirm(question)) if confirm is not None else False
        if not approved:
            declined.add(tool)
            step["status"] = "skipped"
            step["result"] = f"Skipped {tool.replace('_', ' ')} (you declined)."
            return step, NOT_DONE, False
        # approved -> fall through and execute

    _execute_into(step)
    if step["status"] == "ran":
        ran_sigs[sig] = step["result"]
    return step, step["result"], False


def _execute_into(step: dict) -> None:
    """Run the step's skill, writing result/status into the step in place."""
    tool, args = step["tool"], step["args"]
    try:
        step["result"] = skills.SKILLS[tool](**args)
        step["status"] = "ran"
    except Exception as exc:
        print(f"[brain] skill {tool} failed: {exc!r}")
        step["status"] = "failed"
        step["result"] = (
            f"Tried to {tool.replace('_', ' ')} but it failed: "
            f"{type(exc).__name__}: {exc}."
        )


def _assistant_tool_msg(message) -> dict:
    """Rebuild the assistant message (with its tool calls) for the thread."""
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name,
                          "arguments": tc.function.arguments or "{}"}}
            for tc in message.tool_calls
        ],
    }


_LEDGER_STATUS = {
    "ran": "DONE",
    "failed": "FAILED",
    "skipped": "NOT DONE (user declined it)",
    "blocked": "NOT DONE (blocked, not allowed)",
}


def _summarize_from_ledger(user_text: str, steps: list) -> str:
    """One natural spoken summary, GROUNDED in what actually happened.

    Split by outcome: the model narrates the steps that actually RAN (it is
    accurate about those - it has their real results, including honest
    failure messages). The steps that did NOT happen (skipped/blocked) get a
    DETERMINISTIC honest clause appended by us - the model never gets to
    describe them, so it can never spin a declined action into a success."""
    ran = [s for s in steps if s["status"] in ("ran", "failed")]
    not_done = [s for s in steps if s["status"] in ("skipped", "blocked")]

    summary = ""
    if ran:
        lines = []
        for s in ran:
            args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
            label = f"{s['tool'].replace('_', ' ')}" + (f" ({args})" if args else "")
            lines.append(f"- {label}: {_LEDGER_STATUS[s['status']]} — {s['result']}")
        ledger = "\n".join(lines)
        # NOTE: we deliberately do NOT pass the original request here - it may
        # mention actions that didn't happen (e.g. "set volume to 80"), and
        # the model will confabulate them into the summary. It should narrate
        # ONLY these real results.
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": (
                "Here is exactly what you just did, with the real results:\n"
                + ledger + "\n\nGive one short spoken summary of just these "
                "actions - a sentence or two, no lists. Report the results "
                "exactly as written; do not add or change any numbers or "
                "facts, and don't mention anything not listed here.")},
        ]
        try:
            resp = _chat(messages, use_tools=False, max_tokens=120)
            summary = (resp.choices[0].message.content or "").strip()
        except Exception:
            summary = _fallback_summary(ran)
    summary = " ".join(summary.split())  # collapse stray newlines for TTS

    # Honest, deterministic tail for anything that did NOT happen.
    clauses = []
    for s in not_done:
        action = s["tool"].replace("_", " ")
        if s["status"] == "skipped":
            clauses.append(f"I didn't {action}, since you declined")
        else:
            clauses.append(f"I couldn't {action} — it's not allowed")
    if clauses:
        tail = _join_clauses(clauses).capitalize() + "."
        summary = (summary + " " + tail).strip() if summary else tail
    return summary


def _join_clauses(parts: list) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _fallback_summary(steps: list) -> str:
    """Last resort if the summariser call fails: stitch the real results."""
    if not steps:
        return "I'm not sure what to do with that."
    joined = " ".join(s["result"] for s in steps if s.get("result"))
    return joined[:400] or "Done."
