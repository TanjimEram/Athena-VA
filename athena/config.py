"""One place for all settings. Loads the .env file and exposes the API key
and model names so no other file ever touches secrets or hardcodes a model."""

import os

from dotenv import load_dotenv

# Look for .env in the project root (the folder above athena/).
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Model names live here so swapping models is a one-line change.
BRAIN_MODEL = "llama-3.3-70b-versatile"

# Voice for text-to-speech: Irish female, our FRIDAY sound.
TTS_VOICE = "en-IE-EmilyNeural"

# Speech-to-text model on Groq.
STT_MODEL = "whisper-large-v3-turbo"

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
BRAIN_TEMPERATURE = 0.6
BRAIN_MAX_TOKENS = 512

# How many past messages think() keeps when given a long history.
HISTORY_MAX_MESSAGES = 20


def check_config() -> None:
    """Raise a clear error early if the API key is missing."""
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Create a .env file in the project root "
            "with a line like: GROQ_API_KEY=your_key_here"
        )
