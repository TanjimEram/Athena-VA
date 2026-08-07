"""The safety gate. Every tool call the brain wants to make passes through
classify() first: 'free' runs immediately, 'confirm' needs a spoken yes from
the user, 'blocked' is refused. Change a tool's risk level by moving its name
between the sets below."""

FREE = {"open_app", "open_website", "web_search", "get_system_info", "see_screen",
        "look_up", "research", "guide_me", "open_dashboard", "close_dashboard",
        # Reading is safe: these only look at documents and email.
        "read_document", "find_document",
        "list_recent_emails", "summarize_emails",
        # Spreadsheets: looking, not touching. use_tab only moves a pointer.
        "open_spreadsheet", "list_tabs", "use_tab", "read_range",
        "describe_sheet", "count_matching"}
CONFIRM = {"set_volume", "lock_screen",
           # These write something the user will see and have to undo.
           "create_document", "write_to_document", "write_local_docx",
           # draft_email cannot send, but it still puts a message in Gmail.
           "draft_email",
           # send_email is the only tool that leaves the machine. It is also
           # in brain.NEVER_BATCHED (never runs inside a multi-step chain) and
           # does its own recipient/subject read-back inside mail.py.
           "send_email",
           # Every spreadsheet edit. All of these are also in
           # brain.NEVER_BATCHED, so a destructive edit can never ride along
           # inside a chain the user approved for something else.
           "sort_range", "filter_rows", "clear_filter", "color_range",
           "highlight_rows_where", "add_formula",
           "insert_rows", "insert_columns", "move_rows",
           "freeze_header", "autosize_columns", "undo_last_change",
           # These three read their plan back before doing anything:
           # delete_* through brain.CONFIRM_PHRASING, apply_sheet_operation
           # inside sheets.py (so it's in brain.SELF_CONFIRMING too).
           "delete_rows", "delete_columns", "apply_sheet_operation"}
BLOCKED: set[str] = set()  # nothing blocked yet


# --------------------------------------------------------------------------
# The distress floor.
#
# A hard rule, matched on the words themselves - the same kind of thing as a
# tool tier, and for the same reason: a judgement call is a thing that can be
# talked around, and this one shouldn't be. It runs BEFORE any persona or
# agent prompt is applied, so nothing Athena has been told to be can sit on
# top of it.
#
# Known limits, stated plainly: this is substring matching on a speech
# transcript. It will miss indirect phrasing ("I'm tired of everything") and
# it will occasionally fire on a quote or a song lyric. That asymmetry is
# deliberate. Firing when it shouldn't costs one plain, warm reply. Not
# firing when it should costs something else.
# --------------------------------------------------------------------------

DISTRESS_PHRASES = (
    # intent
    "kill myself", "killing myself", "end my life", "ending my life",
    "take my own life", "taking my own life", "commit suicide", "suicidal",
    "end it all",
    # not wanting to be here
    "want to die", "wanna die", "want to be dead", "wish i was dead",
    "wish i were dead", "better off dead", "don't want to be here anymore",
    "dont want to be here anymore", "don't want to exist",
    "dont want to exist", "don't want to live", "dont want to live",
    "no reason to live", "nothing to live for",
    # self-harm. Both tenses of each: people say "I've been hurting myself"
    # at least as often as "I hurt myself", and a missed inflection is a
    # missed turn.
    "hurt myself", "hurting myself", "harm myself", "harming myself",
    "cut myself", "cutting myself",
    "overdose", "overdosing",
    "take all my pills", "taking all my pills",
)

# Blanked out of the text BEFORE the phrases above are looked for, so the
# ordinary figurative uses of these words don't trip anything.
DISTRESS_EXCLUSIONS = (
    "killing me", "killing it", "kill for", "could murder a",
    "dying to", "dying for", "to die for", "dying laughing",
    "dead tired", "dead serious", "drop dead", "dead easy",
    "suicide squad", "suicide mission", "career suicide",
    "political suicide", "social suicide",
)

# Deliberately NOT triggers: "can't go on", "can't do this anymore". They're
# said about a spreadsheet or a bad week far more often than about a life,
# and the false-positive rate would train the user to talk around the mode.


def _normalise(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def is_distress(text: str) -> bool:
    """True if this turn must drop everything else and respond plainly.

    Not a diagnosis and not a model judgement - just whether the words are
    there, with the common figurative uses removed first."""
    cleaned = _normalise(text)
    for idiom in DISTRESS_EXCLUSIONS:
        cleaned = cleaned.replace(idiom, " ")
    return any(phrase in cleaned for phrase in DISTRESS_PHRASES)


def distress_matches(text: str) -> list:
    """Which phrases tripped it. For the test, and for explaining a false
    positive - never spoken to the user."""
    cleaned = _normalise(text)
    for idiom in DISTRESS_EXCLUSIONS:
        cleaned = cleaned.replace(idiom, " ")
    return [phrase for phrase in DISTRESS_PHRASES if phrase in cleaned]


def classify(tool_name: str) -> str:
    """Return 'free', 'confirm', or 'blocked' for a tool name.
    Unknown tools are treated as blocked — safer to refuse than to guess."""
    if tool_name in FREE:
        return "free"
    if tool_name in CONFIRM:
        return "confirm"
    return "blocked"


def confirm_needed(tool_name: str) -> bool:
    """True if this tool must be confirmed by the user before running."""
    return classify(tool_name) == "confirm"
