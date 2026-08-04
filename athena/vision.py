"""Athena's eyes. Grabs the screen (or just the focused window), shrinks and
JPEG-encodes it, and asks Groq's vision model about it - so you can say
"what's this error?" or "read this to me" and get a short spoken answer.

Capture is via mss (fast, no temp files); the image is downscaled so its
longest edge is <= config.VISION_MAX_EDGE and base64-encoded inline, which
keeps the upload small and the reply quick. Everything degrades to a spoken
error string instead of crashing the voice loop."""

import base64
import ctypes
import io
import re
import time

import mss
from PIL import Image

from athena import config

# The vision model is a reasoning model that can emit <think>...</think>
# before its answer - strip it so none of that reaches the voice.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Make this process DPI-aware so mss (physical pixels) and pygetwindow's
# window bounds agree on a high-DPI display; otherwise window crops misalign.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def capture_screen() -> Image.Image:
    """Grab the primary monitor as a PIL RGB image."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # [0] is the whole virtual desktop; [1] primary
        shot = sct.grab(monitor)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture_active_window() -> Image.Image:
    """Grab just the focused window on Windows. Falls back to the full
    primary screen if there's no usable active window (e.g. desktop,
    minimized, or a zero-size window)."""
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win is not None and win.width > 0 and win.height > 0:
            with mss.mss() as sct:
                virtual = sct.monitors[0]  # bounding box of all monitors
                region = {
                    "left": max(win.left, virtual["left"]),
                    "top": max(win.top, virtual["top"]),
                    "width": win.width,
                    "height": win.height,
                }
                # Clamp so we never ask mss for pixels past the desktop edge.
                region["width"] = min(region["width"],
                                      virtual["left"] + virtual["width"] - region["left"])
                region["height"] = min(region["height"],
                                       virtual["top"] + virtual["height"] - region["top"])
                if region["width"] > 0 and region["height"] > 0:
                    shot = sct.grab(region)
                    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:
        print(f"[vision] active-window capture failed, using full screen: {exc}")
    return capture_screen()


def to_base64_jpeg(image: Image.Image, max_edge: int = config.VISION_MAX_EDGE) -> str:
    """Downscale so the longest edge <= max_edge, JPEG-encode, base64."""
    w, h = image.size
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / longest
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


VISION_SYSTEM_PROMPT = (
    "You are the vision system for Athena, a spoken voice assistant. You are "
    "shown a screenshot of the user's screen. Answer their question about it "
    "in one or two short, natural sentences meant to be read aloud - no "
    "markdown, no lists, no bullet points. If they're asking about an error, "
    "say what the error is and, briefly, the likely fix. If you can't tell, "
    "say so plainly."
)


def analyze_screen(question: str, system_prompt: str = VISION_SYSTEM_PROMPT,
                   active_window_only: bool = False, max_tokens: int = 200) -> str:
    """Capture the screen (or active window) and ask the vision model about it
    with a caller-supplied system prompt. Returns the model's text (with any
    <think> reasoning stripped), or a spoken error string. Shared by
    ask_about_screen and the guided-mode loop so both reuse one capture path."""
    try:
        image = capture_active_window() if active_window_only else capture_screen()
    except Exception as exc:
        print(f"[vision] screen capture failed: {exc}")
        return "I couldn't capture your screen just now."

    try:
        b64 = to_base64_jpeg(image)
    except Exception as exc:
        print(f"[vision] image encoding failed: {exc}")
        return "I grabbed your screen but couldn't process the image."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": question or "What's on the screen right now?"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]},
    ]
    client = config.get_groq_client()
    try:
        try:
            # reasoning_effort="none" keeps this fast and skips the <think>
            # block, which we must never speak.
            response = client.chat.completions.create(
                model=config.VISION_MODEL, messages=messages,
                temperature=0.3, max_tokens=max_tokens, reasoning_effort="none",
            )
        except Exception:
            # A future model rotation might not accept reasoning_effort;
            # retry without it (and strip any think block below).
            response = client.chat.completions.create(
                model=config.VISION_MODEL, messages=messages,
                temperature=0.3, max_tokens=max_tokens * 2,
            )
        answer = (response.choices[0].message.content or "")
        return _THINK_RE.sub("", answer).strip()
    except Exception as exc:
        print(f"[vision] model call failed: {exc!r}")
        return ""


def ask_about_screen(question: str, active_window_only: bool = False) -> str:
    """Capture the screen and return a concise spoken answer to `question`."""
    answer = analyze_screen(question, VISION_SYSTEM_PROMPT, active_window_only)
    if answer.startswith("I couldn't") or answer.startswith("I grabbed"):
        return answer  # capture/encode error already phrased for speech
    return answer or "I couldn't reach my vision model to look at your screen."


def save_screenshot(path: str | None = None, active_window_only: bool = False) -> str | None:
    """Save a screenshot to disk (PNG) and return the path, for when you
    want the image itself - e.g. to attach it somewhere. None on failure."""
    try:
        image = capture_active_window() if active_window_only else capture_screen()
        if path is None:
            path = f"athena_screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        image.save(path)
        return path
    except Exception as exc:
        print(f"[vision] couldn't save screenshot: {exc}")
        return None
