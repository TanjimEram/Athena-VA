"""Specialist agents - who Athena is being for a given request.

An agent is three things: a system prompt, the subset of tools it may be
OFFERED, and optionally a model. Narrowing the tool list makes the model pick
better (42 schemas is a lot to choose from) and costs far fewer tokens per
request, which matters on a 12,000-tokens-a-minute free tier.

Membership is NOT permission. An agent holding a tool only means that tool is
put in front of the model. safety.py is still the single authority on what may
actually run, and its tier check happens before execution exactly as before.

Tool sets are declared as intent and filtered against what really exists, so a
name for a feature that hasn't landed is skipped rather than breaking anything.
`general` is derived from whatever is free right now, so it can never be empty
and never goes stale."""

from athena import safety, skills


def _free_tools() -> set:
    """Every tool that runs without asking. Derived, not typed out, so
    `general` stays correct as skills come and go."""
    return {name for name in skills.SKILLS if safety.classify(name) == "free"}


# Written as intent. Anything not in SKILLS is filtered out by tool_names(),
# so a set may safely name a tool from a branch that hasn't landed.
_OPERATOR_TOOLS = {
    "open_app", "open_website", "set_volume", "lock_screen", "get_system_info",
    "open_dashboard", "close_dashboard",
}
_SCRIBE_TOOLS = {
    "create_document", "write_to_document", "find_document", "read_document",
    "write_local_docx",
    "list_recent_emails", "summarize_emails", "draft_email", "send_email",
}
_ANALYST_TOOLS = {
    "open_spreadsheet", "list_tabs", "use_tab", "read_range", "describe_sheet",
    "count_matching", "sort_range", "filter_rows", "clear_filter",
    "color_range", "highlight_rows_where", "add_formula",
    "insert_rows", "insert_columns", "delete_rows", "delete_columns",
    "move_rows", "freeze_header", "autosize_columns", "undo_last_change",
    "apply_sheet_operation",
}
_RESEARCHER_TOOLS = {
    # web_search only OPENS a tab; look_up and research actually fetch and
    # speak an answer, which is what a researcher is for.
    "web_search", "look_up", "research", "see_screen",
}

_SHARED_VOICE = (
    "Everything you say is read aloud, so keep it to one or two short "
    "spoken sentences. No markdown, no lists, no emoji."
)

AGENTS = {
    "general": {
        "prompt": (
            "You are handling a general request. Use a tool only when the "
            f"user clearly asked for an action. {_SHARED_VOICE}"
        ),
        "tools": None,          # None = every free tool, resolved at call time
        "model": None,
        "description": (
            "everyday requests, chat, questions, system status, screen "
            "questions, opening things, anything that doesn't fit a specialist"
        ),
    },
    "operator": {
        "prompt": (
            "You are running the user's PC: launching apps, opening sites, "
            "volume, locking the screen, reporting system status. Act on "
            f"clear commands and confirm briefly. {_SHARED_VOICE}"
        ),
        "tools": _OPERATOR_TOOLS,
        "model": None,
        "description": (
            "controlling this Windows PC - open or launch an app, open a "
            "website, set or change the volume, mute, lock the screen, "
            "battery, CPU, memory, system status"
        ),
    },
    "scribe": {
        "prompt": (
            "You are writing for the user: documents and email. YOU compose "
            "the prose and pass the finished text to the tool - never a "
            "placeholder. Prefer drafting email over sending it. "
            f"{_SHARED_VOICE}"
        ),
        "tools": _SCRIBE_TOOLS,
        "model": None,
        "description": (
            "documents and email - write, draft or read a document, take "
            "notes, check the inbox, summarise email, draft or send a message"
        ),
    },
    "analyst": {
        "prompt": (
            "You are working on the user's spreadsheet. Ranges are A1 "
            "notation; rows and columns are 1-based and inclusive, as the "
            "sheet labels them. Open a spreadsheet before anything else. "
            f"{_SHARED_VOICE}"
        ),
        "tools": _ANALYST_TOOLS,
        "model": None,
        "description": (
            "spreadsheets - open a sheet, read cells or a range, sort, "
            "filter, colour or highlight rows, formulas, count matching rows, "
            "insert or delete rows and columns, freeze headers, undo a change"
        ),
    },
    "researcher": {
        "prompt": (
            "You are finding things out for the user - looking them up and "
            "reading them back. Answer with what you actually found, and say "
            f"so plainly when you couldn't find it. {_SHARED_VOICE}"
        ),
        "tools": _RESEARCHER_TOOLS,
        "model": None,
        "description": (
            "finding things out - look something up, search the web, what is, "
            "who is, latest news, research a topic, read what's on screen"
        ),
    },
}

# ==========================================================================
# >>> PLACEHOLDER - REPLACE THIS PROMPT <<<
# The user is writing the consultant prompt themselves. What's here is a
# minimal stand-in so the mode is testable; it is NOT the intended text.
# Replace the whole string, keep the name.
# ==========================================================================
CONSULTANT_PROMPT = (
    "[PLACEHOLDER PROMPT - to be replaced]\n"
    "You are talking with the user about something that matters to them. "
    "You have no tools and you are not trying to do anything - you are here "
    "to listen and think alongside them. Be specific: you have their past "
    "conversations in context, so refer to what they've actually told you "
    "rather than talking in generalities. Don't perform, don't be clever, "
    "don't rush them to a solution. Short spoken sentences, and it's fine to "
    "just ask a question back."
)

AGENTS["consultant"] = {
    "prompt": CONSULTANT_PROMPT,
    "tools": set(),          # deliberately empty: this mode does not act
    "model": None,
    "description": (
        "talking something through - entered and left by explicit request "
        "only, never inferred"
    ),
}

FALLBACK = "general"
CONSULTANT = "consultant"

# Entering and leaving are EXPLICIT. Athena never decides someone sounds like
# they need this, and never decides they've finished. Guessing either way is
# worse than not offering it.
_CONSULTANT_ENTRY = (
    "consultant mode", "consultant", "i need to talk", "we need to talk",
    "can we talk", "can i talk to you", "i need to vent", "let's talk",
    "lets talk", "talk with me", "i want to talk about something",
)
_CONSULTANT_EXIT = (
    "back to normal", "that's enough", "thats enough", "i'm done talking",
    "im done talking", "enough of that", "let's stop", "lets stop",
    "normal mode", "thanks that's all", "we're done", "were done",
)


def exists(agent: str) -> bool:
    return agent in AGENTS


def get(agent: str) -> dict:
    """An agent by name, falling back to general rather than raising."""
    return AGENTS.get(agent) or AGENTS[FALLBACK]


def tool_names(agent: str) -> set:
    """The tools this agent may be OFFERED - intent filtered down to what
    actually exists. Never a permission check: safety.py still decides what
    may run."""
    wanted = get(agent).get("tools")
    if wanted is None:
        return _free_tools()
    return {name for name in wanted if name in skills.SKILLS}


def missing_tools(agent: str) -> set:
    """Named in the set but not in SKILLS - a branch that hasn't landed, or a
    typo. The test uses this to tell those two apart."""
    wanted = get(agent).get("tools")
    if wanted is None:
        return set()
    return {name for name in wanted if name not in skills.SKILLS}


def tools_for(agent: str) -> list:
    """The tool schemas to send for this agent, filtered from brain.TOOLS.

    Imported lazily: brain imports agents, so a module-level import here
    would close the loop."""
    from athena import brain
    allowed = tool_names(agent)
    return [tool for tool in brain.TOOLS
            if tool["function"]["name"] in allowed]


def prompt_for(agent: str) -> str:
    return get(agent).get("prompt", "")


def model_for(agent: str):
    """The model this agent prefers, or None to use the configured default."""
    return get(agent).get("model")


def describe_all() -> str:
    """One line per agent - what the router matches against, and what a
    'which of these' model call is shown."""
    return "\n".join(f"{name}: {spec['description']}"
                     for name, spec in AGENTS.items())


# --------------------------------------------------------------------------
# routing
#
# Cheap first, model second. Most requests are decided by a keyword or two,
# and a routing call that costs tokens on every turn would undo the saving
# this whole thing exists for. Sticky by default: a follow-up like "now sort
# it by score" should stay where it is without asking anyone.
# --------------------------------------------------------------------------

# Phrases that identify an agent. Longer phrases score higher than bare words,
# so "open the spreadsheet" goes to analyst, not operator on the word "open".
_SIGNALS = {
    "operator": {
        3: ("lock the screen", "lock my screen", "turn the volume",
            "set the volume", "volume to", "battery level", "how much battery",
            "system status", "cpu usage", "open the dashboard"),
        2: ("open app", "launch", "volume", "mute", "unmute", "lock screen",
            "battery", "shut down", "restart"),
        1: ("open", "start", "close", "cpu", "memory", "dashboard"),
    },
    "scribe": {
        3: ("write a document", "new document", "make a document",
            "in my notes", "check my email", "check my inbox", "read my email",
            "draft an email", "send an email", "reply to", "any new email",
            "what's in my inbox", "summarise my email", "summarize my email"),
        2: ("document", "email", "inbox", "draft", "letter", "memo",
            "write up", "take notes", "word file"),
        1: ("write", "notes", "mail", "compose"),
    },
    "analyst": {
        3: ("open the spreadsheet", "open my spreadsheet", "in the sheet",
            "sort by", "sort it by", "filter the rows", "filter by",
            "highlight rows", "add a formula", "how many rows",
            "delete the row", "delete row", "insert a row", "freeze the header",
            "undo that", "put it back", "google sheet"),
        2: ("spreadsheet", "sheet", "column", "cell", "formula", "sort",
            "filter", "row", "tab", "sum of", "total of", "undo"),
        1: ("data", "table", "count", "highlight"),
    },
    "researcher": {
        3: ("look it up", "look up", "search the web", "find out",
            "what's the latest", "tell me about", "research",
            "on my screen", "what does this say", "read the screen",
            "what am i looking at"),
        2: ("who is", "what is", "when did", "where is", "how many",
            "news", "google", "search", "screen"),
        1: ("find", "latest", "explain"),
    },
}

# Said out loud, these mean "stop being a specialist".
_EXIT_PHRASES = ("back to normal", "never mind", "nevermind", "forget that",
                 "that's enough", "thats enough", "stop that", "go back",
                 "normal mode", "general mode")

# How far ahead a new agent must score before we leave the current one. A
# passing mention of a spreadsheet shouldn't drag a document session away.
_STICKY_MARGIN = 2
# Below this, nothing matched clearly enough to be sure.
_CONFIDENT = 3

_ROUTER_MODEL = "llama-3.1-8b-instant"
_ROUTER_SYSTEM = (
    "You label a request with the ONE specialist best suited to it. Reply "
    "with a single word from the list and nothing else - no punctuation, no "
    "explanation. If none clearly fit, reply general."
)


def _normalise(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def score(user_text: str) -> dict:
    """How strongly each agent matches. Exposed so the test can show its
    working when a routing decision looks wrong."""
    text = _normalise(user_text)
    # consultant is never scored - it's entered by asking, never by matching.
    scores = {name: 0 for name in AGENTS
              if name not in (FALLBACK, CONSULTANT)}
    for name, bands in _SIGNALS.items():
        if name not in scores:
            continue
        for weight, phrases in bands.items():
            for phrase in phrases:
                if phrase in text:
                    scores[name] += weight
    return scores


def wants_exit(user_text: str) -> bool:
    text = _normalise(user_text)
    return any(phrase in text for phrase in _EXIT_PHRASES)


def wants_consultant(user_text: str) -> bool:
    """Did the user ASK for this mode? Never inferred from how they sound."""
    text = _normalise(user_text)
    return any(phrase in text for phrase in _CONSULTANT_ENTRY)


def wants_consultant_exit(user_text: str) -> bool:
    text = _normalise(user_text)
    return any(phrase in text for phrase in _CONSULTANT_EXIT)


def extra_context(agent: str, user_text: str) -> str:
    """Anything this agent needs in front of it before it answers.

    For the consultant that's memory: recent exchanges plus anything matching
    what they just said. Specificity is the whole point of the mode - talking
    in generalities to someone who has told you things before is worse than
    not remembering at all."""
    if agent != CONSULTANT:
        return ""
    from athena import memory
    lines = []
    try:
        for row in memory.recent(6):
            user_said = (row.get("user_text") or "").strip()
            reply = (row.get("athena_reply") or "").strip()
            if user_said:
                lines.append(f"- They said: {user_said}")
            if reply:
                lines.append(f"  You replied: {reply}")
    except Exception as exc:
        print(f"[agents] couldn't read recent memory: {exc!r}")
    try:
        for row in memory.search(user_text, 4):
            user_said = (row.get("user_text") or "").strip()
            if user_said and not any(user_said in line for line in lines):
                lines.append(f"- Earlier, they said: {user_said}")
    except Exception as exc:
        print(f"[agents] couldn't search memory: {exc!r}")

    if not lines:
        return ("You have no earlier conversations to draw on, so don't "
                "imply that you remember things you don't.")
    return ("What they've told you before, to be specific about rather than "
            "recite:\n" + "\n".join(lines[:20]))


def _ask_router(user_text: str) -> str | None:
    """One small model call, only when the keywords couldn't decide. Returns
    an agent name or None - it never raises, and never blocks a turn for
    long."""
    from athena import config
    try:
        response = config.get_groq_client().chat.completions.create(
            model=_ROUTER_MODEL,
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM},
                {"role": "user", "content": (
                    f"Specialists:\n{describe_all()}\n\n"
                    f"Request: {user_text}\n\nOne word:")},
            ],
            temperature=0, max_tokens=8,
        )
        answer = (response.choices[0].message.content or "").strip().lower()
    except Exception as exc:
        print(f"[agents] router call failed ({type(exc).__name__}) - using general")
        return None
    # The model sometimes adds a full stop or a word of preamble.
    for name in AGENTS:
        if name in answer:
            return name
    return None


def route(user_text: str, current_agent: str | None = None) -> str:
    """Which specialist should handle this. Never raises, never returns
    something that isn't a real agent."""
    text = _normalise(user_text)
    if not text:
        return current_agent if exists(current_agent or "") else FALLBACK

    # Asking for the consultant gets it, from anywhere.
    if wants_consultant(text):
        return CONSULTANT

    # Once in it, ONLY an explicit exit leaves. No amount of spreadsheet talk
    # pulls someone out of a conversation they asked to have.
    if current_agent == CONSULTANT:
        return FALLBACK if wants_consultant_exit(text) else CONSULTANT

    if wants_exit(text):
        return FALLBACK

    scores = score(text)
    best = max(scores, key=scores.get) if scores else FALLBACK
    best_score = scores.get(best, 0)
    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0

    # Stay put unless the new request clearly belongs somewhere else.
    if exists(current_agent or "") and current_agent != FALLBACK:
        if best == current_agent:
            return current_agent
        if best_score < scores.get(current_agent, 0) + _STICKY_MARGIN:
            return current_agent

    # A clear winner: decide here and spend nothing. "Clear" means either a
    # strong match that leads, OR the only agent that matched at all - a weak
    # signal nobody competes with is not ambiguous. "Open notepad" scores 1
    # and needs no help; it's two agents pulling that needs a tiebreak.
    if best_score > 0 and best_score > runner_up:
        if best_score >= _CONFIDENT or runner_up == 0:
            return best

    # Genuinely nothing matched - general handles it, no model call needed.
    if best_score == 0:
        return FALLBACK

    # Ambiguous: two or more agents competing. Worth one small call.
    asked = _ask_router(user_text)
    if asked and exists(asked):
        return asked
    return best if best_score else FALLBACK
