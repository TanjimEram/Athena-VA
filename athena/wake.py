"""The always-on ear. listen_for_wake() streams the microphone through
openWakeWord's detector and blocks until the wake phrase is heard above the
threshold, then releases the mic so audio_io.record can use it.

The model is whatever settings says, falling back to config.WAKE_MODEL - a
pretrained name ("hey_jarvis"), the bare name of a model in models/
("hey_athena"), or a path to a .onnx file. resolve_model handles all three.
Reading it from settings is what makes the dashboard's wake-model dropdown
actually do something; it used to read config directly, so choosing a model
there changed nothing even after the restart it asked for.

No API key, all local."""

import os

import numpy as np
import sounddevice as sd

from athena import config

# openWakeWord wants 80 ms frames: 1280 samples at 16 kHz.
CHUNK = 1280

_model = None
_loaded_from = None          # which model the cached one actually is


def _configured_model() -> str:
    """What the user has chosen, read at call time so the dashboard's
    setting is real rather than decorative."""
    from athena import settings
    return str(settings.get("wake_model", config.WAKE_MODEL) or config.WAKE_MODEL)


def resolve_model(name: str) -> str:
    """A pretrained name, a bare custom name, or a path - all to something
    openWakeWord can load.

    A bare name is looked up in models/ first, so the dashboard dropdown can
    hold "hey_athena" rather than an absolute path, and a name that isn't
    there falls through to openWakeWord's pretrained list."""
    name = (name or "").strip()
    if not name:
        return config.WAKE_MODEL
    if name.lower().endswith(".onnx") or os.sep in name or "/" in name:
        return name
    local = os.path.join(config.MODELS_DIR, f"{name}.onnx")
    return local if os.path.exists(local) else name


def _get_model():
    """Load the detector once. Reloads if the chosen model has changed, and
    auto-downloads the pretrained files on a machine without them."""
    global _model, _loaded_from
    wanted = resolve_model(_configured_model())
    if _model is not None and _loaded_from == wanted:
        return _model

    from openwakeword.model import Model
    kwargs = dict(
        wakeword_models=[wanted],
        inference_framework="onnx",  # no tflite runtime on Windows
    )
    try:
        _model = Model(**kwargs)
    except Exception:
        from openwakeword.utils import download_models
        names = [] if wanted.lower().endswith(".onnx") else [wanted]
        download_models(model_names=names)
        _model = Model(**kwargs)
    _loaded_from = wanted
    print(f"[wake] listening for {os.path.basename(wanted)}")
    return _model


def preload() -> None:
    """Load the wake model up front so the first detection isn't slow."""
    _get_model()


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
            from athena import settings
            while True:
                audio, _overflowed = stream.read(CHUNK)
                scores = model.predict(np.squeeze(audio))
                # Sensitivity read every loop so a slider change is live.
                threshold = float(settings.get("wake_threshold", config.WAKE_THRESHOLD))
                if max(scores.values()) >= threshold:
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
