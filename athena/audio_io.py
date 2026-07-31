"""The microphone. record_until_silence() listens, starts capturing when you
start talking, and stops the moment you've been quiet for a beat - so Athena
responds as soon as you finish instead of waiting out a timer. It measures
the room's ambient noise for ~0.4 s first, so the silence threshold adapts
to a quiet bedroom or a noisy cafe. The old fixed-length record() stays as
a fallback. Both return the path of a temp 16 kHz mono WAV file."""

import os
import tempfile
from collections import deque

import numpy as np
import sounddevice as sd
from scipy.io import wavfile

from athena import config

# Analyze audio in 30 ms frames - small enough to react quickly.
FRAME_MS = 30

_MIC_HINT = (
    "[audio] Couldn't open the microphone. Check that Windows allows "
    "microphone access (Settings > Privacy & security > Microphone) "
    "and that an input device is plugged in and set as default."
)


def record_until_silence(
    max_seconds: float = 12,
    silence_threshold: float | None = None,
    silence_duration: float = 0.8,
    samplerate: int = config.AUDIO_SAMPLERATE,
    on_level=None,
    stop_event=None,
) -> str | None:
    """Record speech and stop automatically after `silence_duration` seconds
    of quiet (hard stop at `max_seconds`). The threshold auto-calibrates
    from ambient noise unless `silence_threshold` is given. Returns the WAV
    path, or None if no speech was heard (or the mic failed).

    on_level, if given, is called every 30 ms frame with the raw RMS level -
    the UI uses it to animate the orb/waveform while you talk.
    stop_event, if given, is checked every frame - when set, recording stops
    immediately and releases the mic (used to abort a listen when the user
    answers a confirmation by clicking instead of speaking)."""
    frame_len = int(samplerate * FRAME_MS / 1000)
    max_frames = int(max_seconds * 1000 / FRAME_MS)
    silence_frames_needed = max(1, int(silence_duration * 1000 / FRAME_MS))
    calibration_frames = 0 if silence_threshold else max(1, int(400 / FRAME_MS))

    threshold = silence_threshold
    ambient: list[float] = []
    # Keep ~0.3 s of audio from just before speech was detected, so the
    # first syllable doesn't get clipped off.
    preroll: deque = deque(maxlen=int(300 / FRAME_MS))
    captured: list[np.ndarray] = []
    speech_started = False
    loud_run = 0
    silent_run = 0

    try:
        with sd.InputStream(
            samplerate=samplerate, channels=1, dtype="int16", blocksize=frame_len
        ) as stream:
            for i in range(max_frames):
                if stop_event is not None and stop_event.is_set():
                    return None  # aborted (e.g. confirmation answered by click)
                frame, _overflowed = stream.read(frame_len)
                frame = np.squeeze(frame)
                rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
                if on_level is not None:
                    try:
                        on_level(rms)
                    except Exception:
                        on_level = None  # never let a UI hiccup kill recording

                # Phase 1: listen to the empty room to learn its noise level.
                if i < calibration_frames:
                    ambient.append(rms)
                    preroll.append(frame)
                    continue
                if threshold is None:
                    noise = float(np.mean(ambient))
                    # Speech must clearly rise above the room: several times
                    # the ambient energy, with a floor low enough for quiet
                    # laptop mics (this one idles near RMS 0-1).
                    threshold = max(noise * 4.0, noise + 100.0, 150.0)

                # Phase 2: wait for speech, then capture until silence.
                # Two consecutive loud frames (60 ms) are required to start,
                # so a single click or pop can't false-trigger.
                if not speech_started:
                    preroll.append(frame)
                    if rms >= threshold:
                        loud_run += 1
                        if loud_run >= 2:
                            speech_started = True
                            captured.extend(preroll)
                    else:
                        loud_run = 0
                else:
                    captured.append(frame)
                    # Hysteresis: ending speech requires dropping well below
                    # the start threshold, so trailing soft syllables count.
                    if rms < threshold * 0.8:
                        silent_run += 1
                        if silent_run >= silence_frames_needed:
                            break
                    else:
                        silent_run = 0
    except sd.PortAudioError as exc:
        print(f"{_MIC_HINT} ({exc})")
        return None

    if not speech_started:
        return None
    return _write_wav(np.concatenate(captured), samplerate)


def record(seconds: float = 5, samplerate: int = config.AUDIO_SAMPLERATE) -> str | None:
    """Fallback: record a fixed number of seconds from the default mic.
    Blocks for the full duration. Returns None if recording failed."""
    frames = int(seconds * samplerate)
    try:
        audio = sd.rec(frames, samplerate=samplerate, channels=1, dtype="int16")
        sd.wait()
    except sd.PortAudioError as exc:
        print(f"{_MIC_HINT} ({exc})")
        return None
    return _write_wav(np.squeeze(audio), samplerate)


def _write_wav(audio: np.ndarray, samplerate: int) -> str:
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="athena_mic_")
    os.close(fd)
    wavfile.write(wav_path, samplerate, audio)
    return wav_path
