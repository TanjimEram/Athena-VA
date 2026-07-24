"""Microphone test with voice activity detection: start talking after
'Listening...', and it stops on its own once you go quiet, then prints how
long it actually recorded and what you said."""

import os

from athena import audio_io, stt
from scipy.io import wavfile

if __name__ == "__main__":
    print("Listening... speak now - recording stops when you stop. (12 s max)")
    wav_path = audio_io.record_until_silence()
    if wav_path is None:
        print("No speech detected (or the mic failed).")
        raise SystemExit(1)

    samplerate, data = wavfile.read(wav_path)
    print(f"Recorded {len(data) / samplerate:.1f} seconds of audio.")

    print("Transcribing...")
    text = stt.transcribe(wav_path)
    os.remove(wav_path)
    print(f"You said: {text!r}")
