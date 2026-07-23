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

# still to build, for the always-on wake-word listener:
start_input_stream(callback) -> None   # feeds mic chunks (bytes) to callback
stop_input_stream() -> None
```

### athena/wake.py — wake-word detection
Listens to mic chunks and fires when it hears "Athena".

```python
init() -> None
process_chunk(pcm_bytes: bytes) -> bool   # True the moment the wake word is heard
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

### athena/skills.py — actions (STUBS DONE)
Each returns a short sentence for the voice to speak.

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

### main.py — the conductor
Because ui.start() blocks, main.py looks like:

```python
def assistant_loop():
    wake.init(); memory setup
    loop: ...the interaction below...

config.check_config()
ui.start(main_fn=assistant_loop)   # blocks here until the orb is closed
```

Call order for one interaction (inside assistant_loop):

```python
loop:
    ui.set_state("idle")
    wait until wake.process_chunk(...) is True        # via audio_io input stream
    ui.set_state("listening")
    text = stt.listen_and_transcribe()
    ui.set_state("thinking")
    memory.add("user", text)
    result = brain.think(text, memory.get_history())
    if result["needs_confirmation"]:
        ui.set_state("speaking"); tts.speak(result["reply_text"])
        answer = stt.listen_and_transcribe()          # listen for "yes"
        if answer says yes:
            reply = brain.run_confirmed(result["tool_called"], result["args"])
        else:
            reply = "Okay, cancelled."
    else:
        reply = result["reply_text"]
    memory.add("assistant", reply)
    ui.set_state("speaking")
    tts.speak(reply)
```

## Ground rules

- Windows 11, Python 3.12.4, virtualenv at `.venv`.
- Secrets only in `.env` (never committed); everything reads them via `config.py`.
- Standard library where possible; new dependencies need a team ping first.
- Every file starts with a short plain-words docstring saying what it's for.
- Skills return short human sentences — whatever a skill returns is what
  Athena says out loud.
