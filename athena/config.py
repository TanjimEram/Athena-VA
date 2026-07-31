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

# Model names live here so swapping models is a one-line change.
BRAIN_MODEL = "llama-3.3-70b-versatile"

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
    "open apps, open websites, search the web, set the system volume, "
    "lock the screen, report battery and system status, and look at your "
    "screen to answer questions about what's on it"
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
