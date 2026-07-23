"""Speech to text. transcribe() sends a WAV file to Groq's Whisper model
and returns what was said as plain text. Uses the same API key as the brain
(from config.py). Returns "" if transcription fails."""

import groq

from athena import config

_client = None


def _get_client() -> groq.Groq:
    global _client
    if _client is None:
        config.check_config()
        _client = groq.Groq(api_key=config.GROQ_API_KEY)
    return _client


def transcribe(wav_path: str) -> str:
    """Turn a spoken WAV recording into text. Returns "" on failure."""
    try:
        with open(wav_path, "rb") as f:
            result = _get_client().audio.transcriptions.create(
                file=(wav_path, f.read()),
                model=config.STT_MODEL,
                language="en",
            )
    except FileNotFoundError:
        print(f"[stt] No such audio file: {wav_path}")
        return ""
    except groq.RateLimitError:
        print("[stt] Rate-limited by the speech service, try again in a moment.")
        return ""
    except groq.APIConnectionError:
        print("[stt] Can't reach the speech service - check the internet connection.")
        return ""
    except Exception as exc:
        print(f"[stt] Transcription failed: {exc}")
        return ""

    return (result.text or "").strip()
