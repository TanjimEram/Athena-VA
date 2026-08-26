"""One place for all settings. Loads the .env file and exposes the API key
and model names so no other file ever touches secrets or hardcodes a model."""

import os

from dotenv import load_dotenv

# Look for .env in the project root (the folder above athena/).
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Supabase (cloud memory). Optional: without these Athena still works,
# she just doesn't remember across sessions.
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Tavily (live web research - search, read, summarize). Optional: without it
# the look_up/research skills say they can't reach the web instead of failing.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# Google (Docs + Gmail). Optional: without these the document and email
# skills say they aren't connected instead of failing. These are the
# "Desktop app" OAuth client credentials from the Google Cloud Console -
# they are NOT an API key and must never be committed.
# Gemini. Used ONLY for spreadsheet reasoning, and only when
# SHEETS_PROVIDER is "gemini" - everything else stays on Groq. Sheet data is
# token-heavy and Groq's free tier caps at 12,000 tokens a minute, so moving
# just this one call off Groq keeps the voice loop's budget intact.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
# "groq" (default) or "gemini". Falls back to Groq if Gemini isn't usable,
# so setting this can never leave the sheets feature broken.
SHEETS_PROVIDER = os.getenv("SHEETS_PROVIDER", "groq")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Where the signed-in token is cached (gitignored). Per-machine, like
# settings.json - one file next to the project, not in the package.
GOOGLE_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "google_token.json",
)

# Model names live here so swapping models is a one-line change.
# Groq retired the Llama 3.x models - llama-3.3-70b-versatile 404s ("model
# does not exist"). Verified against the live model list, August 2026.
# openai/gpt-oss-120b replaces it and was measured selecting the right tool
# with the right arguments on our real tool schema.
# This is also settings.DEFAULTS["brain_model"], so leaving it stale meant
# one click of "Reset defaults" in the dashboard would kill the assistant.
# To re-check what's available:
#   python -c "from athena import config; print([m.id for m in
#              config.get_groq_client().models.list().data])"
BRAIN_MODEL = "openai/gpt-oss-120b"

# Voice for text-to-speech: Irish female, our FRIDAY sound.
TTS_VOICE = "en-IE-EmilyNeural"

# Speech-to-text model on Groq.
STT_MODEL = "whisper-large-v3-turbo"

# Vision model on Groq (screen understanding). Groq rotates these - only
# reference it from here. As of 2026-07, Llama 4 Scout is DEPRECATED (gone
# June 2026); qwen/qwen3.6-27b is the live multimodal model. If vision calls
# start failing, run:  python -c "from athena import config;
# print([m.id for m in config.get_groq_client().models.list().data])"
# and swap in whatever vision model is current.
VISION_MODEL = "qwen/qwen3.6-27b"
# Longest image edge sent to the model - smaller = faster + cheaper.
VISION_MAX_EDGE = 1536

# Microphone recording defaults: 16 kHz mono is what speech models want.
AUDIO_SAMPLERATE = 16000

# Wake word (openWakeWord, no API key, all local).
#
# WAKE_MODEL is a pretrained name ("hey_jarvis"), a bare name of a model in
# MODELS_DIR ("hey_athena"), or a full path to a .onnx file. wake.py resolves
# all three, so the dashboard dropdown can hold a friendly name.
#
# ONNX rather than TFLite on purpose: onnxruntime is already a dependency and
# there is no TFLite runtime on this machine. The trained model ships in both
# formats; only the .onnx is used.
MODELS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
WAKE_MODEL = "hey_athena"
# 0..1 confidence needed to trigger. Lower = more sensitive, more false wakes.
WAKE_THRESHOLD = 0.5


def wake_label(model: str | None = None) -> str:
    """The wake phrase as a person reads it: "hey_athena" -> "Hey Athena".

    Derived, never typed. The GUI said "hey jarvis" for weeks after the custom
    model replaced it, because the phrase was hardcoded in three files and the
    model name was somewhere else entirely. Anything that displays the wake
    word asks for it here, so it can only ever be wrong in one place.

    Takes a pretrained name, a bare model name, or a full path to a .onnx."""
    name = str(model or WAKE_MODEL).strip()
    name = os.path.splitext(os.path.basename(name))[0]     # a path -> the name
    words = name.replace("-", " ").replace("_", " ").split()
    return " ".join(word.capitalize() for word in words) or "the wake word"


# The phrase for the current model, for anything that just wants the default.
# Read wake_label(settings.get("wake_model")) instead where the live setting
# matters - the dashboard can change the model without a restart.
WAKE_LABEL = wake_label()

# How the assistant behaves. Kept here so tuning doesn't mean editing brain.py.
ASSISTANT_NAME = "Athena"

# What Athena can do, in plain words. Used to answer "what can you do?"
# accurately - update this one line when you add a skill.
CAPABILITIES = (
    "open apps, open websites, search the web and read you the answer, set the "
    "system volume, lock the screen, report battery and system status, look at "
    "your screen to answer questions about it, guide you through on-screen "
    "tasks step by step, write documents for you in Google Docs or Word, "
    "read, summarize, draft and send your email, work on your Google "
    "Sheets - sorting, filtering, colouring, formulas, adding and removing "
    "rows, with an undo - and manage your calendar: read your schedule, book "
    "and move and cancel events, and follow up on things whose time has "
    "passed"
)
BRAIN_TEMPERATURE = 0.6
# Low on purpose: replies are spoken aloud, so a hard cap keeps her to a
# sentence or two. (Tool-call arguments also fit - they're short.)
BRAIN_MAX_TOKENS = 100

# How many past messages think() keeps: 12 = the last 6 user/assistant
# exchanges. Enough for "what about tomorrow?", small enough to stay fast.
HISTORY_MAX_MESSAGES = 12


def check_config() -> None:
    """Raise a clear error early if the API key is missing."""
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Create a .env file in the project root "
            "with a line like: GROQ_API_KEY=your_key_here"
        )


_groq_client = None


def get_groq_client():
    """The one shared Groq client (brain + speech-to-text). Created on first
    use and reused forever - building it per call wastes time on every turn."""
    global _groq_client
    if _groq_client is None:
        import groq
        check_config()
        _groq_client = groq.Groq(api_key=GROQ_API_KEY)
    return _groq_client
