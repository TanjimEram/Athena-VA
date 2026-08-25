"""Backend for the settings dashboard - the logic behind the webview API in
ui.py. Kept separate so it can be tested without opening a window.

Handles: assembling the settings payload the UI renders, writing API keys to
.env (masked in the UI, never shown in full), live connection tests, and the
memory view/clear. Non-secret tunables live in settings.json (via settings.py);
secrets live only in .env.
"""

import re
from pathlib import Path

from athena import config, safety, settings, skills

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Service -> the .env variable(s) it uses.
KEY_VARS = {
    "GROQ": ["GROQ_API_KEY"],
    "GEMINI": ["GEMINI_API_KEY"],
    "OPENAI": ["OPENAI_API_KEY"],
    "ANTHROPIC": ["ANTHROPIC_API_KEY"],
    "TAVILY": ["TAVILY_API_KEY"],
    "SUPABASE": ["SUPABASE_URL", "SUPABASE_KEY"],
    "PICOVOICE": ["PICOVOICE_ACCESS_KEY"],
}

# Curated dropdown options (kept short; edited here, not in the UI).
VOICES = [
    "en-IE-EmilyNeural", "en-US-AriaNeural", "en-US-JennyNeural",
    "en-US-GuyNeural", "en-GB-SoniaNeural", "en-GB-RyanNeural",
    "en-GB-LibbyNeural", "en-AU-NatashaNeural", "en-CA-ClaraNeural",
]
MODELS = {
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
             "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "openai": ["gpt-4o", "gpt-4o-mini"],
    "anthropic": ["claude-sonnet-4-5", "claude-opus-4-5"],
}
# Our own trained model first, then openWakeWord's pretrained ones. A bare
# name here is resolved by wake.resolve_model - models/<name>.onnx if it
# exists, otherwise a pretrained download.
WAKE_MODELS = ["hey_athena", "hey_jarvis", "alexa", "hey_mycroft"]


def _mask(value: str) -> str:
    if not value:
        return ""
    v = str(value)
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "•" * 6 + v[-4:]


def _read_env() -> dict:
    data = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip()
    return data


def get_settings() -> dict:
    """The full payload the settings view renders from."""
    env = _read_env()
    skill_rows = []
    for name in sorted(skills.SKILLS):
        skill_rows.append({
            "name": name,
            "tier": safety.classify(name),
            "enabled": settings.is_skill_enabled(name),
        })
    keys = {}
    for service, vars_ in KEY_VARS.items():
        primary = env.get(vars_[0], "")
        keys[service] = {"set": bool(primary), "masked": _mask(primary)}
    return {
        "values": settings.all(),
        "restart_keys": sorted(settings.RESTART_KEYS),
        "skills": skill_rows,
        "voices": VOICES,
        "models": MODELS,
        "wake_models": WAKE_MODELS,
        "keys": keys,
    }


def save_setting(key: str, value) -> dict:
    """Persist one non-secret setting. Returns {'ok', 'restart'}."""
    try:
        settings.set(key, value)
        return {"ok": True, "restart": key in settings.RESTART_KEYS}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def save_skill(name: str, enabled: bool) -> dict:
    try:
        settings.set_skill_enabled(name, enabled)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def save_key(service: str, value: str) -> dict:
    """Write an API key to .env (creating it if needed), update the running
    process env, and return the new masked value. Never echoes the full key."""
    service = str(service).upper()
    var = KEY_VARS.get(service, [None])[0]
    if not var:
        return {"ok": False, "error": f"unknown service {service}"}
    value = (value or "").strip()
    try:
        _write_env_var(var, value)
        import os
        os.environ[var] = value
        # Reflect into config's cached attribute so fresh reads see it.
        setattr(config, var, value)
        return {"ok": True, "masked": _mask(value), "restart": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _write_env_var(name: str, value: str) -> None:
    """Update or append NAME=value in .env, preserving other lines."""
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_connection(service: str) -> dict:
    """Actually ping a service with its configured key. {'ok': bool, 'detail'}."""
    service = str(service).upper()
    try:
        if service == "GROQ":
            r = config.get_groq_client().chat.completions.create(
                model=config.BRAIN_MODEL,
                messages=[{"role": "user", "content": "ping"}], max_tokens=1)
            return {"ok": True, "detail": "Groq reachable"}
        if service == "TAVILY":
            from athena import research
            client = research._get_client()
            if client is None:
                return {"ok": False, "detail": "no Tavily key"}
            client.search(query="ping", max_results=1)
            return {"ok": True, "detail": "Tavily reachable"}
        if service == "SUPABASE":
            from athena import memory
            client = memory._get_client()
            if client is None:
                return {"ok": False, "detail": "Supabase not configured"}
            client.table("interactions").select("id").limit(1).execute()
            return {"ok": True, "detail": "Supabase reachable"}
        if service in ("GEMINI", "OPENAI", "ANTHROPIC", "PICOVOICE"):
            env = _read_env()
            has = bool(env.get(KEY_VARS[service][0]))
            return {"ok": False,
                    "detail": ("key saved, but this provider isn't wired into "
                               "Athena yet" if has else "no key set")}
        return {"ok": False, "detail": f"unknown service {service}"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}


def recent_memory(n: int = 8) -> list:
    """Recent interactions for the memory view. [] if memory off/unavailable."""
    try:
        from athena import memory
        rows = memory.recent(n)
        return [{"user": r.get("user_text", ""), "athena": r.get("athena_reply", "")}
                for r in rows]
    except Exception:
        return []


def clear_memory() -> dict:
    """Delete stored interactions. The anon Supabase key only has select/insert
    by default, so this may not be permitted - reported honestly."""
    try:
        from athena import memory
        client = memory._get_client()
        if client is None:
            return {"ok": False, "detail": "memory not configured"}
        client.table("interactions").delete().neq("id", 0).execute()
        return {"ok": True, "detail": "memory cleared"}
    except Exception as exc:
        return {"ok": False,
                "detail": ("couldn't clear - the anon key likely lacks delete "
                           "permission; clear it in the Supabase dashboard "
                           f"({type(exc).__name__})")}


def reset_defaults() -> dict:
    try:
        settings.reset_to_defaults()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
