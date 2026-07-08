"""The assistant's hands. Every action Athena can take lives here.
For now these are stubs: they print what they WOULD do and return a short
sentence for the voice to speak. Real Windows automation replaces the
function bodies later without changing any signatures."""


def open_app(name: str) -> str:
    """Launch a program on this PC by its friendly name."""
    print(f"[skills] would launch app: {name}")
    return f"Opened {name}."


def open_website(url: str) -> str:
    """Open a URL in the default browser."""
    print(f"[skills] would open browser at: {url}")
    return f"Opened {url}."


def web_search(query: str) -> str:
    """Search the web and summarize what comes back."""
    print(f"[skills] would search the web for: {query}")
    return f"Here's what I found for {query}."


def set_volume(level: int) -> str:
    """Set the system volume to a percentage from 0 to 100."""
    level = max(0, min(100, int(level)))
    print(f"[skills] would set system volume to: {level}%")
    return f"Volume set to {level} percent."


def lock_screen() -> str:
    """Lock the Windows session."""
    print("[skills] would lock the screen")
    return "Screen locked."


def get_system_info() -> str:
    """Report basics like CPU, memory, and battery."""
    print("[skills] would gather system info")
    return "Everything looks healthy. CPU and memory are in normal range."


# The brain dispatches tool calls through this table, so adding a skill means
# writing the function and adding one line here (plus its schema in brain.py).
SKILLS = {
    "open_app": open_app,
    "open_website": open_website,
    "web_search": web_search,
    "set_volume": set_volume,
    "lock_screen": lock_screen,
    "get_system_info": get_system_info,
}
