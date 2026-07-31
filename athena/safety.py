"""The safety gate. Every tool call the brain wants to make passes through
classify() first: 'free' runs immediately, 'confirm' needs a spoken yes from
the user, 'blocked' is refused. Change a tool's risk level by moving its name
between the sets below."""

FREE = {"open_app", "open_website", "web_search", "get_system_info", "see_screen",
        "open_dashboard", "close_dashboard"}
CONFIRM = {"set_volume", "lock_screen"}
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
