"""The always-on ear. listen_for_wake() streams the microphone through
openWakeWord's detector and blocks until the wake phrase is heard above
config.WAKE_THRESHOLD, then releases the mic so audio_io.record can use it.
The model comes from config.WAKE_MODEL - a pretrained name ("hey_jarvis")
or a path to a custom .onnx file. No API key, all local."""

import numpy as np
import sounddevice as sd

from athena import config

# openWakeWord wants 80 ms frames: 1280 samples at 16 kHz.
CHUNK = 1280

_model = None


def _get_model():
    """Load the detector once. Auto-downloads the pretrained files on a
    machine that doesn't have them yet."""
    global _model
    if _model is None:
        from openwakeword.model import Model
        kwargs = dict(
            wakeword_models=[config.WAKE_MODEL],
            inference_framework="onnx",  # tflite isn't available on Windows
        )
        try:
            _model = Model(**kwargs)
        except Exception:
            from openwakeword.utils import download_models
            names = [] if config.WAKE_MODEL.endswith(".onnx") else [config.WAKE_MODEL]
            download_models(model_names=names)
            _model = Model(**kwargs)
    return _model


def listen_for_wake(timeout: float | None = None) -> bool:
    """Block until the wake word is heard, then return True with the mic
    released. Returns False if the mic can't open, or if `timeout` seconds
    pass without a detection (timeout=None means wait forever)."""
    model = _get_model()
    model.reset()  # forget audio from last time so we don't retrigger

    frames_heard = 0
    max_frames = None if timeout is None else int(timeout * 16000 / CHUNK)

    try:
        # The with-block guarantees the stream is closed and the mic freed
        # on detection, timeout, and Ctrl+C alike.
        with sd.InputStream(
            samplerate=16000, channels=1, dtype="int16", blocksize=CHUNK
        ) as stream:
            while True:
                audio, _overflowed = stream.read(CHUNK)
                scores = model.predict(np.squeeze(audio))
                if max(scores.values()) >= config.WAKE_THRESHOLD:
                    return True
                frames_heard += 1
                if max_frames is not None and frames_heard >= max_frames:
                    return False
    except sd.PortAudioError as exc:
        print(
            "[wake] Couldn't open the microphone. Check Windows mic privacy "
            "settings (Settings > Privacy & security > Microphone) and the "
            f"default input device. ({exc})"
        )
        return False
