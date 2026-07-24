"""The core decision-maker. think() sends the user's words to the LLM with a
list of available tools. If the model picks a tool, the safety gate decides
whether to run it now, ask the user to confirm, or refuse. Everything the
rest of the pipeline needs comes back in one dict."""

import json

import groq

from athena import config, safety, skills

SYSTEM_PROMPT = (
    f"You are {config.ASSISTANT_NAME}, a voice assistant on the user's Windows "
    "PC. You are calm, warm, and intelligent, with a composed presence like "
    "FRIDAY from Iron Man. Everything you say is read aloud by a voice, so it "
    "must sound natural when spoken: one or two short sentences, never "
    "paragraphs. No markdown, no bullet points, no lists, no emoji. Be "
    "concise and never repeat the user's request back to them. When you "
    "perform an action, confirm it briefly and naturally, like 'Chrome's "
    "open.' rather than 'I have successfully opened Google Chrome for you.' "
    "If you can't do something, say so plainly in one sentence. When the "
    "user asks you to do something on the PC, call the matching tool; when "
    "they're just talking, simply answer. Never invent tools you don't have."
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
            "description": "Search the web for current information the assistant doesn't know.",
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
]


def _chat(messages: list, use_tools: bool):
    """One completion call to Groq, with or without the tools array."""
    kwargs = dict(
        model=config.BRAIN_MODEL,
        messages=messages,
        temperature=config.BRAIN_TEMPERATURE,
        max_tokens=config.BRAIN_MAX_TOKENS,
    )
    if use_tools:
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = "auto"
    return config.get_groq_client().chat.completions.create(**kwargs)


def think(user_text: str, history: list | None = None) -> dict:
    """Turn the user's words into a spoken reply and (maybe) an action.

    history is an optional list of prior {'role': ..., 'content': ...} dicts.
    Returns {'reply_text', 'tool_called', 'args', 'needs_confirmation'}.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-config.HISTORY_MAX_MESSAGES:])
    messages.append({"role": "user", "content": user_text})

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


def run_confirmed(tool_name: str, args: dict) -> str:
    """Execute a tool the user has already said yes to. Called by the layer
    that handles the user's confirmation (main.py or the test CLI)."""
    if safety.classify(tool_name) == "blocked" or tool_name not in skills.SKILLS:
        return "Sorry, that action isn't allowed."
    try:
        return skills.SKILLS[tool_name](**args)
    except Exception as exc:
        print(f"[brain] skill {tool_name} failed: {exc!r}")
        return (
            f"I tried to {tool_name.replace('_', ' ')} but it failed: "
            f"{type(exc).__name__}: {exc}."
        )


def _result(reply_text: str, tool_called: str | None = None,
            args: dict | None = None, needs_confirmation: bool = False) -> dict:
    """Build the standard dict every call to think() returns."""
    return {
        "reply_text": reply_text,
        "tool_called": tool_called,
        "args": args or {},
        "needs_confirmation": needs_confirmation,
    }
