"""Athena's voice. speak(text) synthesizes speech with Microsoft Edge's
online TTS (edge-tts, voice set in config.py), plays it through pygame,
and blocks until the sentence finishes. Needs internet; if there's none,
it prints a clear message instead of crashing."""

import asyncio
import os
import tempfile
import time

# Keep pygame's banner out of our console.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import aiohttp
import edge_tts
import pygame

from athena import config

_mixer_ready = False


def _init_mixer() -> None:
    """Initialize pygame's mixer once and keep it alive between speak()
    calls - re-initializing per sentence adds audible startup lag."""
    global _mixer_ready
    if not _mixer_ready:
        pygame.mixer.init()
        _mixer_ready = True


# Warm the mixer at import (app startup). If no audio device is ready yet,
# stay quiet - speak() retries lazily.
try:
    _init_mixer()
except pygame.error:
    pass


async def _synthesize(text: str, path: str) -> None:
    await edge_tts.Communicate(text, config.TTS_VOICE).save(path)


def speak(text: str) -> None:
    """Say the text out loud. Returns when Athena has finished speaking."""
    if not text or not text.strip():
        return

    # Synthesize to a temp mp3. edge-tts is async; wrap it so we stay sync.
    fd, mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="athena_tts_")
    os.close(fd)
    try:
        try:
            asyncio.run(_synthesize(text, mp3_path))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            print(f"[tts] Can't reach the speech service - check the internet connection. ({type(exc).__name__})")
            return
        except edge_tts.exceptions.NoAudioReceived:
            print("[tts] The speech service sent no audio for that text.")
            return
        except Exception as exc:
            print(f"[tts] Speech synthesis failed: {exc}")
            return

        _init_mixer()
        pygame.mixer.music.load(mp3_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
        # Release the file handle so Windows lets us delete the mp3.
        pygame.mixer.music.unload()
    finally:
        try:
            os.remove(mp3_path)
        except OSError:
            pass
