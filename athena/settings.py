"""Runtime settings store, backed by settings.json.

Everything the settings dashboard can change lives here rather than hardcoded
in config.py. Modules read values AT CALL TIME via settings.get(...), so a
change takes effect on the next use with no restart - except things that can
only load once (wake model, mic device), which the UI marks "restart to apply".

config.py still owns secrets (loaded from .env) and supplies the canonical
default values below. settings.json holds only non-secret tunables; API keys
never go here - they live in .env.
"""

import json
import threading
from pathlib import Path

from athena import config

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

# Canonical defaults. Secrets are NOT here (they stay in .env / config.py).
DEFAULTS = {
    # --- Voice ---
    "tts_voice": config.TTS_VOICE,
    "voice_rate": 0,          # percent, -50..+50  (edge-tts rate)
    "voice_pitch": 0,         # Hz offset, -50..+50
    "voice_volume": 0,        # percent, -50..+50
    # --- Wake word ---  (model change needs a restart)
    "wake_model": config.WAKE_MODEL,
    "wake_threshold": config.WAKE_THRESHOLD,
    "wake_enabled": True,
    # --- Brain ---
    "brain_provider": "groq",
    "brain_model": config.BRAIN_MODEL,
    # Spreadsheet reasoning only - see config.SHEETS_PROVIDER. Read at call
    # time, so switching it applies to the next request with no restart.
    "sheets_provider": config.SHEETS_PROVIDER,
    "brain_max_tokens": config.BRAIN_MAX_TOKENS,
    "personality": ("calm, warm, and intelligent, with a composed presence "
                    "like FRIDAY from Iron Man"),
    # --- Behavior ---
    "confirm_before_acting": True,
    "followup_seconds": 6,
    "sleep_phrases": ["go to sleep", "goodbye athena", "that's all", "stand down"],
    # --- Skills ---  name -> bool; missing name defaults to enabled
    "skills_enabled": {},
    # --- Appearance ---
    "accent_color": "#4fd6e8",
    "orb_size": 96,           # restart to apply (window size)
    "dock_edge": "right",
    "default_view": "orb",    # orb | dashboard
    # --- Guided mode ---
    "guide_wait_mode": "manual",   # "manual" (say 'next') | "auto" (timer)
    "guide_auto_seconds": 4,
    "guide_max_steps": 12,
    # --- Memory ---
    "memory_enabled": True,
    # --- System ---
    "start_on_boot": False,
    "summon_hotkey": "",
}

# Keys that only take effect after a restart (the UI flags these).
RESTART_KEYS = {"wake_model", "orb_size"}

_lock = threading.RLock()
_data: dict = {}
_loaded = False


def load() -> dict:
    """Read settings.json into memory, merging in any missing defaults.
    Creates the file from defaults on first run. Safe to call repeatedly."""
    global _data, _loaded
    with _lock:
        raw = {}
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raw = {}
            except Exception as exc:
                print(f"[settings] couldn't read settings.json, using defaults: {exc}")
                raw = {}
        merged = dict(DEFAULTS)
        merged.update(raw)
        _data = merged
        _loaded = True
        if not SETTINGS_FILE.exists():
            _save_locked()          # materialize defaults on first run
    return dict(_data)


def _ensure_loaded() -> None:
    if not _loaded:
        load()


def get(key: str, default=None):
    """Current value for `key`. Falls back to the caller's default, then the
    canonical default. Read at CALL TIME so changes apply without a restart."""
    _ensure_loaded()
    with _lock:
        if key in _data:
            return _data[key]
    if default is not None:
        return default
    return DEFAULTS.get(key)


def set(key: str, value) -> None:
    """Update a setting and persist to settings.json immediately."""
    _ensure_loaded()
    with _lock:
        _data[key] = value
        _save_locked()


def all() -> dict:
    """A copy of every current setting."""
    _ensure_loaded()
    with _lock:
        return dict(_data)


def reset_to_defaults() -> dict:
    """Restore every setting to its canonical default and persist."""
    global _data
    with _lock:
        _data = dict(DEFAULTS)
        _save_locked()
        return dict(_data)


def is_skill_enabled(name: str) -> bool:
    """Whether a skill is on. Unknown/unset skills default to enabled."""
    _ensure_loaded()
    with _lock:
        return bool(_data.get("skills_enabled", {}).get(name, True))


def set_skill_enabled(name: str, enabled: bool) -> None:
    _ensure_loaded()
    with _lock:
        table = dict(_data.get("skills_enabled", {}))
        table[name] = bool(enabled)
        _data["skills_enabled"] = table
        _save_locked()


def _save_locked() -> None:
    """Write _data to settings.json (caller holds _lock). Atomic-ish via a
    temp file so a crash mid-write can't corrupt the settings."""
    try:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_data, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception as exc:
        print(f"[settings] couldn't save settings.json: {exc}")


# Load once at import so the store is ready as soon as anything reads it.
load()
