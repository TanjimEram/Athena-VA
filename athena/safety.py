"""The safety gate. Every tool call the brain wants to make passes through
classify() first: 'free' runs immediately, 'confirm' needs a spoken yes from
the user, 'blocked' is refused. Change a tool's risk level by moving its name
between the sets below."""

FREE = {"open_app", "open_website", "web_search", "get_system_info", "see_screen",
        "look_up", "research", "guide_me", "open_dashboard", "close_dashboard",
        # Reading is safe: these only look at documents and email.
        "read_document", "find_document",
        "list_recent_emails", "summarize_emails"}
CONFIRM = {"set_volume", "lock_screen",
           # These write something the user will see and have to undo.
           "create_document", "write_to_document", "write_local_docx",
           # draft_email cannot send, but it still puts a message in Gmail.
           "draft_email",
           # send_email is the only tool that leaves the machine. It is also
           # in brain.NEVER_BATCHED (never runs inside a multi-step chain) and
           # does its own recipient/subject read-back inside mail.py.
           "send_email"}
BLOCKED: set[str] = set()  # nothing blocked yet


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
