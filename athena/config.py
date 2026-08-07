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

# Specialist agents (athena/agents.py). OFF by default: with this False,
# run_agent takes exactly the path it took before agents existed, sending the
# same full tool list. This is the rollback switch - don't remove it.
AGENTS_ENABLED = os.getenv("AGENTS_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on")

# Speak a warning when the API budget runs low. OFF by default: the meter on
# the dashboard is silent and always available, and a voice that interrupts
# to talk about itself is worse than one that doesn't.
USAGE_VOICE_ALERTS = os.getenv("USAGE_VOICE_ALERTS", "").strip().lower() in (
    "1", "true", "yes", "on")
# Fraction of the per-minute token budget below which she mentions it.
USAGE_WARN_AT = 0.25

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Where the signed-in token is cached (gitignored). Per-machine, like
# settings.json - one file next to the project, not in the package.
GOOGLE_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "google_token.json",
)

# Model names live here so swapping models is a one-line change.
BRAIN_MODEL = "llama-3.3-70b-versatile"

# Voice for text-to-speech: Irish female, our FRIDAY sound.
TTS_VOICE = "en-IE-EmilyNeural"
# Baseline prosody. Percent for rate, Hz for pitch, 0 = the voice's own.
# (The edge-tts CLI needs --rate=-8% with an equals sign for negatives; the
# Python API we use takes "-8%" directly, so there's nothing to work around.)
TTS_RATE = 0
TTS_PITCH = 0
# Consultant mode speaks slower and a little lower. Not a gimmick: the
# default delivery is brisk, and brisk is wrong for that conversation.
CONSULTANT_TTS_RATE = -8
CONSULTANT_TTS_PITCH = -4

# ==========================================================================
# >>> FILL THESE IN <<<
# Crisis lines Athena can name when the distress floor trips. These are
# PUBLIC information, which is why they live here in git rather than in
# settings.json - your own contacts are personal and go there instead.
#
# Look yours up at https://findahelpline.com or https://befrienders.org and
# paste in verified numbers for YOUR country. They are left blank on purpose:
# a wrong crisis number is worse than none, and I'm not guessing at digits.
#
# Entries still containing FILL_ME are skipped, so an unfinished list makes
# Athena speak generally rather than read a placeholder out loud.
# ==========================================================================
DISTRESS_HOTLINES = [
    # ("what to call it", "how to reach it")
    ("FILL_ME: your local crisis line", "FILL_ME: number"),
    ("FILL_ME: emergency services", "FILL_ME: number"),
]


def hotlines() -> list:
    """The hotlines that have actually been filled in."""
    return [(label, contact) for label, contact in DISTRESS_HOTLINES
            if "FILL_ME" not in label and "FILL_ME" not in contact]

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

# Wake word (openWakeWord, no API key). WAKE_MODEL is either a pretrained
# name like "hey_jarvis" or a path to a custom .onnx file — swapping in our
# own "Athena" model later is just changing this one line.
WAKE_MODEL = "hey_jarvis"
# 0..1 confidence needed to trigger. Lower = more sensitive, more false wakes.
WAKE_THRESHOLD = 0.5

# How the assistant behaves. Kept here so tuning doesn't mean editing brain.py.
ASSISTANT_NAME = "Athena"

# What Athena can do, in plain words. Used to answer "what can you do?"
# accurately - update this one line when you add a skill.
CAPABILITIES = (
    "open apps, open websites, search the web and read you the answer, set the "
    "system volume, lock the screen, report battery and system status, look at "
    "your screen to answer questions about it, guide you through on-screen "
    "tasks step by step, write documents for you in Google Docs or Word, "
    "read, summarize, draft and send your email, and work on your Google "
    "Sheets - sorting, filtering, colouring, formulas, adding and removing "
    "rows, with an undo"
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
