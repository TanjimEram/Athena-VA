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
        "describe_sheet", "count_matching",
        # Calendar: reading only. pending_followups belongs here because
        # ASKING is not a write - and if the user then says "move it", that
        # move goes through reschedule_event and is gated on its own.
        "get_current_time", "read_schedule", "next_event", "find_event",
        "pending_followups"}
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
           "delete_rows", "delete_columns", "apply_sheet_operation",
           # Every calendar write. Like send_email these are in
           # brain.NEVER_BATCHED, so an event can never be created as an
           # incidental step in a chain approved for something else. They are
           # also in brain.SELF_CONFIRMING: the read-back happens inside
           # calendar_skill, because only that module knows what "tomorrow at
           # 3" resolved to - confirming the raw words would confirm the
           # mishearing rather than catch it.
           "create_event", "reschedule_event", "cancel_event"}
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
