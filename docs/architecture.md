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
record_until_silence(max_seconds=12, silence_threshold=None,
                     silence_duration=0.8, samplerate=16000) -> str | None
# the default listener: starts capturing when speech begins, stops after
# silence_duration of quiet (auto-calibrated to the room's ambient noise),
# hard stop at max_seconds; None if no speech was heard

record(seconds: float = 5, samplerate: int = 16000) -> str | None
# fixed-length fallback; records mono from the default mic
# both return the path to a temp 16 kHz WAV (caller deletes it)

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

think_stream(user_text, history=None) -> (generator, result)
# low-latency path: generator yields text pieces as the model writes;
# feed it to tts.speak_stream. `result` is think()'s dict (plus
# 'first_token_s'), filled once the generator is exhausted. Tool calls
# still work; falls back to think() internally on stream failures.

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

### athena/memory.py — long-term memory (DONE)
Supabase (free cloud Postgres) `interactions` table; the schema SQL is in
memory.py's docstring. brain.think logs every exchange (async, so it never
delays speech) and injects the last few past interactions into the model's
context once per session. Without SUPABASE_URL/SUPABASE_KEY in .env, or
offline, everything returns empty/False with a single warning — Athena
keeps working, just memoryless. (Short-term history within a session is
still the plain list main.py passes to think.)

```python
log_interaction(user_text, reply, tool_called=None, tool_args=None) -> bool
log_interaction_async(...) -> None      # fire-and-forget thread
recent(n=5) -> list[dict]               # last n rows, newest first
search(query, n=3) -> list[dict]        # case-insensitive match on user_text
```

Verify the connection with `python memory_test.py` (writes one row, reads it
back). One shared client is created lazily and reused (never per call).

### athena/tts.py — text to speech (DONE, streaming pipeline)
edge-tts (online, voice in `config.TTS_VOICE`) + pygame at 24 kHz with a
small buffer. speak_stream is a 3-stage chain (sentence splitter -> synth
worker -> gapless player) so the first sentence plays while the next is
synthesizing and the model is still writing. All audio is in-memory
BytesIO; a persistent asyncio loop serves every synthesis; timeouts and
empty answers retry once. Needs internet; prints instead of crashing.

```python
speak(text: str, on_level=None) -> None      # blocking, one clip
speak_stream(chunks, on_level=None, on_sentence=None) -> float | None
# speaks a text-piece iterator sentence by sentence; on_sentence fires as
# each sentence STARTS playing; returns time-to-first-audio in ms
warmup() -> None   # call at startup: mixer + loop + throwaway synthesis
```

### athena/ui.py — the two-mode UI (DONE: orb + dashboard in one window)
One pywebview window that switches modes: ORB (90x90 colour-keyed chathead,
edge-docked, click to expand) and DASHBOARD (assets/ui/dashboard.html,
1150x700 centred HUD: tool log, streaming transcript + typed input, status
strip, confirm card, collapsible session rail; minimise button or Escape
collapses back). Mode switches animate the window bounds over ~200 ms.
Python caches state/transcript/log/status/pending-confirm and replays them
into whichever page loads, so nothing is lost by switching. The orb drags
manually (JS pointer deltas -> api.move_window; <5 px & <300 ms = click);
on release it snaps to the nearest edge of the monitor it's on (Win32 work
areas: multi-monitor and taskbar aware), any corner sticks, and the spot is
saved to .athena_ui_state.json (gitignored) and restored next session.
Drag the dashboard by its top bar.

```python
STATES: tuple                       # idle, listening, thinking, speaking, confirm
start(main_fn=None) -> None         # blocking; main_fn runs in a worker thread
stop() / expand() / collapse()
set_state(s) / set_amplitude(v)     # work in both modes
show_bubble(text)                   # orb mode only
add_log(text, tier) / add_transcript(who, text) / set_status(dict)
show_confirm(question) / open_sessions() / close_sessions()
on_typed_input / on_confirm / on_orb_click   # assignable hooks
```

### athena/ui_orb.py — the standalone orb (superseded by ui.py)
A 90px always-on-top orb hugging the right screen edge: five states
(idle/listening/thinking/speaking/confirm), amplitude-reactive bars, a
sliding message bubble, drag-anywhere with animated edge snapping, and a
click callback stub (`on_orb_click`). `TRANSPARENT` flag at the top: True
is the pretty transparent-window mode; if that renders invisible on a
machine (WebView2 bug), set False for a dark rounded panel that widens
briefly for bubbles. Same threading contract as ui.py: `start(main_fn)`
blocks.

```python
STATES: tuple            # idle, listening, thinking, speaking, confirm
start(main_fn=None) -> None
set_state(state: str) -> None
set_amplitude(v: float) -> None    # 0..1, drives bars + speaking pulse
show_bubble(text: str) -> None     # auto-fades after ~6 s
stop() -> None
on_orb_click() -> None             # stub; replace to handle clicks
```

### athena/ui.py — the old orb (superseded by ui_orb.py)
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

### athena/main.py — the conductor (DONE, fully wired to the UI)
Run with `python -m athena.main`. pywebview owns the main thread
(`ui.start()`); the assistant loop and a status updater run as daemon
threads, so closing the window always brings the process down. Per turn:
wake word (3 s polling bursts so shutdown is prompt) → beep → VAD listening
with real mic levels fed to `ui.set_amplitude` → whisper → streaming reply
(brain.think_stream piped into tts.speak_stream: first sentence plays while
the model is still writing; measured time-to-first-audio is printed and
logged per turn) with the transcript filling as each sentence is spoken →
orb bubble when collapsed → ~6 s follow-up window. Confirm-tier actions show the dashboard card AND listen for a
spoken yes/no — click wins. Typed input from the dashboard goes straight
into `process_turn` (no mic). The status strip gets connections
(groq/supabase/mic), cpu/battery (psutil), and stt/brain/tts latency every
3 s. Sleep phrases or closing the window shut everything down cleanly.

Still to wire in: session history is a local list (memory.py already logs
to Supabase through brain.think).

## Ground rules

- Windows 11, Python 3.12.4, virtualenv at `.venv`.
- Secrets only in `.env` (never committed); everything reads them via `config.py`.
- Standard library where possible; new dependencies need a team ping first.
- Every file starts with a short plain-words docstring saying what it's for.
- Skills return short human sentences — whatever a skill returns is what
  Athena says out loud.
