# Athena — Architecture Contract

This is the contract the team codes against. Each module below lists the exact
functions it must expose. If you need to change a signature, change it here
first and tell the team.

## The pipeline

```
wake word -> speech-to-text -> brain (LLM + tools) -> safety -> skills -> text-to-speech -> orb UI
```

`main.py` owns the loop; every other module is a library that does one job.

## Modules

### athena/config.py  — settings (DONE)
Loads `.env`, holds every constant. No other file reads environment variables.

```python
GROQ_API_KEY: str | None
BRAIN_MODEL: str          # "llama-3.3-70b-versatile"
ASSISTANT_NAME: str
check_config() -> None    # raises RuntimeError if the API key is missing
```

### athena/audio_io.py — microphone plumbing (record() DONE)
Owns the audio devices so wake/stt never touch hardware directly.

```python
record(seconds: float = 5, samplerate: int = 16000) -> str | None
# blocking; records mono from the default mic, returns path to a temp 16 kHz
# WAV (caller deletes it), or None with a printed hint if the mic won't open

# (wake.py opens its own mic stream, so the old start_input_stream/
#  stop_input_stream plan is no longer needed)
```

### athena/wake.py — wake-word detection (DONE)
openWakeWord (local, no API key, no account) with the pretrained
"hey_jarvis" model. Model name/path and threshold live in config
(`WAKE_MODEL`, `WAKE_THRESHOLD`); swapping in a custom "Athena" .onnx later
is a one-line config change. Owns the mic only while listening and releases
it before returning, so audio_io.record can open it immediately after.

```python
listen_for_wake(timeout: float | None = None) -> bool
# blocks until the wake word is heard (True); False on timeout or mic failure
```

### athena/stt.py — speech to text (transcribe() DONE)
Sends recorded audio to Groq's Whisper (`config.STT_MODEL`).

```python
transcribe(wav_path: str) -> str   # plain text; "" on failure

# still to build: record-until-silence on top of audio_io + transcribe
listen_and_transcribe(timeout_seconds: float = 10.0) -> str   # "" if nothing heard
```

### athena/brain.py — LLM + tool calling (DONE)
The decision-maker. Already calls safety and skills internally.

```python
think(user_text: str, history: list | None = None) -> dict
# returns {"reply_text": str, "tool_called": str | None,
#          "args": dict, "needs_confirmation": bool}

run_confirmed(tool_name: str, args: dict) -> str
# executes a confirm-level tool AFTER the user says yes; returns spoken result
```

### athena/safety.py — permission gate (DONE)

```python
classify(tool_name: str) -> str        # 'free' | 'confirm' | 'blocked'
confirm_needed(tool_name: str) -> bool
```

Current policy — free: open_app, open_website, web_search, get_system_info;
confirm: set_volume, lock_screen; blocked: none (unknown tools are blocked).

### athena/skills.py — actions (REAL, DONE)
Real Windows implementations (app launch via PATH + ShellExecute, browser via
webbrowser, volume via pycaw, lock via user32, battery/CPU via psutil). Each
returns a short honest sentence about what actually happened — errors are
reported, never faked as success. The brain speaks these strings verbatim.

```python
open_app(name: str) -> str
open_website(url: str) -> str
web_search(query: str) -> str
set_volume(level: int) -> str
lock_screen() -> str
get_system_info() -> str
SKILLS: dict[str, callable]   # name -> function, used by brain to dispatch
```

Adding a skill = write the function, add it to `SKILLS`, add its schema to
`brain.TOOLS`, and put its name in one of the sets in `safety.py`.

### athena/memory.py — conversation history
Keeps the rolling chat history that gets passed into `brain.think`.

```python
add(role: str, content: str) -> None    # role is 'user' or 'assistant'
get_history() -> list[dict]             # [{"role": ..., "content": ...}, ...]
clear() -> None
```

### athena/tts.py — text to speech (DONE)
Uses edge-tts (online) with the voice in `config.TTS_VOICE`
(en-IE-EmilyNeural) and plays through pygame. Needs internet; prints a
message instead of crashing without it.

```python
speak(text: str) -> None   # blocking: returns when Athena finishes talking
```

### athena/ui.py — the orb (DONE)
Visual state indicator, a frameless always-on-top pywebview window showing
`assets/ui/orb.html`. pywebview must own the main thread, so `start()`
BLOCKS: main.py passes its assistant loop as `main_fn` and that loop runs in
a background thread while the orb is on screen.

```python
STATES: tuple                        # ('idle', 'listening', 'thinking', 'speaking')
start(main_fn=None) -> None          # blocking; runs main_fn in a worker thread
set_state(state: str) -> None        # thread-safe; unknown states fall back to idle
stop() -> None                       # closes the window, which unblocks start()
```

### athena/main.py — the conductor (DONE, headless: no orb/memory yet)
Run with `python -m athena.main`. Current loop per interaction:

```python
loop:
    wake.listen_for_wake()                 # blocks; releases mic on return
    tts.speak("Yes?")
    text = hear()                          # audio_io.record + stt.transcribe
    result = brain.think(text, history)
    tts.speak(result["reply_text"])
    if result["needs_confirmation"]:
        answer = hear()                    # listen for "yes"
        reply = brain.run_confirmed(...) if yes else "Okay, cancelled."
        tts.speak(reply)
    history.append(user + assistant turns)
```

Still to wire in: ui (main loop must run as `ui.start(main_fn=...)`'s worker
thread with `ui.set_state` calls at each stage) and memory (replace the local
`history` list).

## Ground rules

- Windows 11, Python 3.12.4, virtualenv at `.venv`.
- Secrets only in `.env` (never committed); everything reads them via `config.py`.
- Standard library where possible; new dependencies need a team ping first.
- Every file starts with a short plain-words docstring saying what it's for.
- Skills return short human sentences — whatever a skill returns is what
  Athena says out loud.
