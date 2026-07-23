"""Microphone test: records 5 seconds, sends it to Whisper on Groq, and
prints what you said. Speak as soon as you see 'Recording...'."""

import os

from athena import audio_io, stt

if __name__ == "__main__":
    print("Recording for 5 seconds - speak now...")
    wav_path = audio_io.record(seconds=5)
    if wav_path is None:
        raise SystemExit(1)
    print("Transcribing...")
    text = stt.transcribe(wav_path)
    os.remove(wav_path)
    print(f"You said: {text!r}")
