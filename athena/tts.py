"""Athena's voice - now a streaming pipeline built for time-to-first-word.

speak_stream(chunks) is a 3-stage producer/consumer chain:
    stage 1 (caller thread)  consume the LLM token stream, cut it into
                             sentences (. ! ? or ~120 chars)
    stage 2 (synth thread)   edge-tts each sentence to an in-memory mp3,
                             strictly in order
    stage 3 (player thread)  play clips back-to-back with no gap
so the first sentence is playing while the second is still synthesizing
and the model is still writing the third.

Fixed overhead is cut up front: the pygame mixer is initialised once at
24 kHz (edge-tts' native rate) with a small buffer; one persistent asyncio
loop thread serves every synthesis (no per-call event loop); a shared
aiohttp connector keeps DNS + TLS session state warm between calls (its
close() is a no-op because edge-tts closes its per-call session, which
would otherwise tear the pool down); warmup() does a throwaway synthesis
at startup so the first real sentence doesn't pay the handshake. Audio
lives in BytesIO - no temp files, no disk, no cleanup.

speak(text) remains as the simple non-streaming path (same machinery,
one clip)."""

import asyncio
import io
import math
import os
import queue
import threading
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import aiohttp
import edge_tts
import pygame

from athena import config

SENTENCE_MAX_CHARS = 120
SENTENCE_ENDS = ".!?"

_mixer_ready = False


def _init_mixer() -> None:
    """One mixer for the app's lifetime: 24 kHz mono-ish (edge-tts native)
    with a small buffer for low playback latency."""
    global _mixer_ready
    if not _mixer_ready:
        pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=512)
        _mixer_ready = True


try:
    _init_mixer()
except pygame.error:
    pass  # no audio device yet; retried lazily


# ------------------------------------------------- persistent asyncio loop

_loop = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop


async def _synth_async(text: str) -> io.BytesIO:
    # NOTE: no shared connector on purpose. edge-tts speaks over a
    # websocket, and websocket-upgraded connections can't be pooled - a
    # shared keepalive connector hands the next call a dead connection
    # that hangs to timeout (measured, not theory). Each call gets a
    # fresh session; the persistent loop + warmup carry the savings.
    buf = io.BytesIO()
    # Read voice + prosody from settings AT CALL TIME, so changing the voice
    # in the dashboard applies on the very next sentence with no restart.
    from athena import settings
    voice = settings.get("tts_voice", config.TTS_VOICE)
    rate = _pct(settings.get("voice_rate", 0))
    volume = _pct(settings.get("voice_volume", 0))
    pitch = _hz(settings.get("voice_pitch", 0))
    # Short connect timeout: the service's connects occasionally hang, and
    # failing fast + retrying on a fresh connection beats waiting 10 s.
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, volume=volume, pitch=pitch,
        connect_timeout=4, receive_timeout=15,
    )
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf


def _pct(v) -> str:
    """edge-tts rate/volume string, e.g. 10 -> '+10%', -5 -> '-5%'."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return f"{'+' if v >= 0 else ''}{v}%"


def _hz(v) -> str:
    """edge-tts pitch string, e.g. 10 -> '+10Hz', -5 -> '-5Hz'."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return f"{'+' if v >= 0 else ''}{v}Hz"


def _synthesize_clip(text: str) -> io.BytesIO | None:
    """Sentence -> in-memory mp3. Timeouts and empty answers (the service
    is occasionally flaky in bursts) get one retry; hard connection
    failures don't. None (with a printed reason) on failure."""
    for attempt in (1, 2):
        try:
            future = asyncio.run_coroutine_threadsafe(_synth_async(text), _get_loop())
            buf = future.result(timeout=12)
            if buf.getbuffer().nbytes > 0:
                buf.seek(0)
                return buf
            raise edge_tts.exceptions.NoAudioReceived("empty audio")
        # ORDER MATTERS: aiohttp's ServerTimeoutError subclasses BOTH
        # TimeoutError and ClientError - timeouts must be caught first so
        # they get their retry instead of being treated as fatal.
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if attempt == 2:
                print(f"[tts] Speech service timed out twice, skipping: {text[:40]}...")
                return None
            time.sleep(0.2)
        except (aiohttp.ClientError, OSError) as exc:
            print(f"[tts] Can't reach the speech service - check the internet connection. ({type(exc).__name__})")
            return None
        except Exception as exc:  # e.g. empty audio: worth one retry
            if attempt == 2:
                print(f"[tts] Speech synthesis failed: {type(exc).__name__}: {exc}")
                return None
            time.sleep(0.3)
    return None


def _play_clip(clip: io.BytesIO, on_level=None) -> None:
    """Play one mp3 clip from memory, blocking, feeding on_level a 0..1
    speech-cadence level (pygame can't expose the real samples)."""
    pygame.mixer.music.load(clip, "mp3")
    pygame.mixer.music.play()
    started = time.monotonic()
    while pygame.mixer.music.get_busy():
        if on_level is not None:
            t = time.monotonic() - started
            level = 0.25 + 0.55 * abs(math.sin(t * 6.3)) * (0.65 + 0.35 * math.sin(t * 1.9 + 1))
            try:
                on_level(level)
            except Exception:
                on_level = None
        time.sleep(0.03)
    pygame.mixer.music.unload()


def warmup() -> None:
    """Call once at app start: initialises the mixer, spins up the asyncio
    loop, and does one throwaway synthesis so DNS/TLS are already warm
    before the first real reply."""
    try:
        _init_mixer()
    except pygame.error:
        pass
    _synthesize_clip("Hi")  # discarded on purpose


# ----------------------------------------------------------- public speech

def speak(text: str, on_level=None) -> None:
    """Say a complete string. Blocking. on_level as in speak_stream."""
    if not text or not text.strip():
        return
    try:
        _init_mixer()
    except pygame.error as exc:
        print(f"[tts] No audio output device: {exc}")
        return
    clip = _synthesize_clip(text)
    if clip is not None:
        _play_clip(clip, on_level)
        if on_level is not None:
            try:
                on_level(0.0)
            except Exception:
                pass


def speak_stream(chunks, on_level=None, on_sentence=None) -> float | None:
    """Speak a stream of text pieces (e.g. brain.think_stream's generator)
    sentence by sentence. Blocks until everything has been spoken.

    on_level(0..1)        called while audio plays, for the UI
    on_sentence(text)     called the moment each sentence STARTS playing -
                          push it to the transcript here and the words land
                          together with the voice
    Returns time-to-first-audio in milliseconds (None if nothing played)."""
    try:
        _init_mixer()
    except pygame.error as exc:
        print(f"[tts] No audio output device: {exc}")
        for _ in chunks:   # still drain the stream so the brain finishes
            pass
        return None

    started = time.monotonic()
    first_audio = {"ms": None}
    sentence_q: queue.Queue = queue.Queue()
    clip_q: queue.Queue = queue.Queue(maxsize=3)

    def synth_worker():
        while True:
            sentence = sentence_q.get()
            if sentence is None:
                break
            clip = _synthesize_clip(sentence)
            if clip is not None:
                clip_q.put((sentence, clip))
        clip_q.put(None)

    def player():
        while True:
            item = clip_q.get()
            if item is None:
                break
            sentence, clip = item
            if on_sentence is not None:
                try:
                    on_sentence(sentence)
                except Exception:
                    pass
            if first_audio["ms"] is None:
                first_audio["ms"] = (time.monotonic() - started) * 1000
            _play_clip(clip, on_level)
        if on_level is not None:
            try:
                on_level(0.0)
            except Exception:
                pass

    synth_thread = threading.Thread(target=synth_worker, daemon=True)
    player_thread = threading.Thread(target=player, daemon=True)
    synth_thread.start()
    player_thread.start()

    # Stage 1, in this thread: token stream -> sentences.
    buffer = ""
    try:
        for piece in chunks:
            buffer += str(piece)
            while True:
                sentence, buffer = _take_sentence(buffer)
                if sentence is None:
                    break
                sentence_q.put(sentence)
    finally:
        tail = buffer.strip()
        if tail:
            sentence_q.put(tail)
        sentence_q.put(None)
        synth_thread.join()
        player_thread.join()

    return first_audio["ms"]


def _take_sentence(buffer: str) -> tuple[str | None, str]:
    """Pull one speakable sentence off the front of the buffer, or (None,
    buffer) if it should keep accumulating. Flushes on . ! ? followed by
    whitespace (mid-stream '3.' + '5' stays intact), or a soft cut past
    ~120 chars so a long clause doesn't stall the pipeline."""
    for i, ch in enumerate(buffer):
        if ch in SENTENCE_ENDS and i + 1 < len(buffer) and buffer[i + 1] in " \n\t":
            sentence = buffer[: i + 1].strip()
            return (sentence or None), buffer[i + 1:]
    if len(buffer) > SENTENCE_MAX_CHARS:
        cut = buffer.rfind(" ", 40, SENTENCE_MAX_CHARS)
        if cut == -1:
            cut = SENTENCE_MAX_CHARS
        return buffer[:cut].strip(), buffer[cut:]
    return None, buffer
