"""The microphone. record() grabs a few seconds of mono audio from the
default input device and saves it as a 16 kHz WAV file, returning the path.
Speech models (Whisper) want exactly this format. Returns None with a clear
message if the mic can't be opened."""

import os
import tempfile

import numpy as np
import sounddevice as sd
from scipy.io import wavfile

from athena import config


def record(seconds: float = 5, samplerate: int = config.AUDIO_SAMPLERATE) -> str | None:
    """Record from the default mic and return the path of a temp WAV file.
    Blocks for the full duration. Returns None if recording failed."""
    frames = int(seconds * samplerate)
    try:
        audio = sd.rec(frames, samplerate=samplerate, channels=1, dtype="int16")
        sd.wait()
    except sd.PortAudioError as exc:
        print(
            "[audio] Couldn't open the microphone. Check that Windows allows "
            "microphone access (Settings > Privacy & security > Microphone) "
            f"and that an input device is plugged in and set as default. ({exc})"
        )
        return None

    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="athena_mic_")
    os.close(fd)
    wavfile.write(wav_path, samplerate, np.squeeze(audio))
    return wav_path
