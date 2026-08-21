"""Speech to text. transcribe() sends a WAV file to Groq's Whisper model
and returns what was said as plain text. Uses the same API key as the brain
(from config.py). Returns "" if transcription fails."""

import groq

from athena import config


def transcribe(wav_path: str) -> str:
    """Turn a spoken WAV recording into text. Returns "" on failure."""
    import time
    started = time.monotonic()
    payload_bytes = 0
    try:
        with open(wav_path, "rb") as f:
            payload = f.read()
            payload_bytes = len(payload)
            result = config.get_groq_client().audio.transcriptions.create(
                file=(wav_path, payload),
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

    # Timing only. This one number is connect + upload + inference together:
    # the SDK does all three inside that call and doesn't report the split.
    # wav_bytes goes alongside so upload size can at least be reasoned about.
    try:
        from athena import latency
        latency.mark("stt", time.monotonic() - started)
        latency.meta("wav_bytes", payload_bytes)
    except Exception:
        pass

    return (result.text or "").strip()
