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

### athena/settings.py + settings_api.py — settings dashboard (DONE)
Runtime settings store backed by `settings.json` (gitignored, per-machine).
`settings.py`: `get(key, default)`, `set(key, value)` (writes immediately),
`all()`, `reset_to_defaults()`, plus skill toggles; created from config.py
defaults on first run. Modules read via `settings.get(...)` AT CALL TIME, so
changes apply on the next interaction with no restart — verified for voice,
prosody, personality, brain model/max-tokens, skill enable/disable, confirm
toggle, wake sensitivity, follow-up window, sleep phrases, memory on/off.
`RESTART_KEYS` (wake_model, orb_size) are flagged in the UI. `settings_api.py`
backs the webview API: `get_settings()`, `save_setting`/`save_skill`,
`save_key` (writes masked keys to .env), `test_connection` (live-pings
Groq/Tavily/Supabase; honest "not wired" for others), `recent_memory` /
`clear_memory`, `reset_defaults`. The settings view is #settings-view in
`app.html`, reached by the dashboard gear — one page, no new window.

### athena/guide.py — guided mode (DONE)
Athena reads the screen (reuses `vision.analyze_screen`) and talks the user
through any task one step at a time. Loop: capture -> ask the vision model for
the SINGLE next action (or exactly `DONE`) -> speak it -> wait. Wait mode is a
setting: `manual` (say "next"/"done", the reliable demo default) or `auto`
(re-check on a timer). Tracks the last instruction so it never repeats a step;
stops on DONE or `guide_max_steps`. She only INSTRUCTS - never clicks. Presets
map phrases like "train your wake word" to a detailed openWakeWord-Colab goal.
Skill `guide_me(goal)` (free tier); main.py wires `guide.set_io` so the orb
reacts (speaking/listening) during a guide.

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
# low-latency single-action path: generator yields text pieces as the model
# writes; feed to tts.speak_stream. (Used before the multi-step upgrade.)

run_agent(user_text, history=None, on_step=None, confirm=None) -> dict
# MULTI-STEP: independent actions are batched into ONE model call (few
# tokens/requests - matters on the free tier); dependent actions chain one
# at a time with results fed back. Capped at 5 steps, stops after a batch or
# a repeat-only round. Safety per step: free runs, blocked refused,
# confirm-tier calls `confirm(question)->bool` and can be skipped without
# killing the chain. Already-run/declined actions are deduped. on_step(step)
# fires live for UI logging. Returns {"reply_text", "steps"}; reply_text is
# ONE summary grounded in the real per-step ledger (never spins a
# skipped/failed step as success). This is what main.py uses.
# Hardened for live use: tool-call model calls retry with rising temperature
# on the intermittent 'tool_use_failed'; if it still won't parse, the
# intended call is RECOVERED from the error's failed_generation and run
# anyway. Rate limits back off; connection errors fail fast with a spoken
# message. main.py wraps each turn so no error can crash the loop.

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
see_screen(question: str, focus="screen"|"window") -> str   # via vision.py
SKILLS: dict[str, callable]   # name -> function, used by brain to dispatch
```

### athena/vision.py — screen understanding (DONE)
Captures the screen (or active window) with mss, downscales to
`config.VISION_MAX_EDGE` and base64-JPEGs it, and asks Groq's
`config.VISION_MODEL` (a reasoning model - called with
`reasoning_effort="none"` and `<think>` stripped so nothing leaks to TTS).
Groq rotates vision models: Llama 4 Scout was deprecated June 2026, so this
uses `qwen/qwen3.6-27b`. Powers the `see_screen` skill.

```python
capture_screen() -> PIL.Image
capture_active_window() -> PIL.Image        # falls back to full screen
ask_about_screen(question, active_window_only=False) -> str
save_screenshot(path=None, active_window_only=False) -> str | None
```

Adding a skill = write the function, add it to `SKILLS`, add its schema to
`brain.TOOLS`, and put its name in one of the sets in `safety.py`.

### athena/research.py — live web research, spoken (DONE)
Tavily (key in `config.TAVILY_API_KEY`). Unlike `web_search` (which only
OPENS a browser tab), these fetch and SPEAK the answer:
- `look_up(query)` - short 1-3 sentence answer via Tavily search
  (include_answer), plus the source site names.
- `research(topic)` - deeper: multi-source search + top-page extract, then
  the LLM condenses it to a concise spoken summary and offers to go deeper.
Both fail gracefully with a spoken message (no key / no internet / API error)
and are bounded so they don't hang the turn. Skills `look_up` / `research`
(free tier) dispatch to them; the brain routes factual/"latest" questions to
look_up, "research X"/"tell me about X" to research, and only "open a search"
to web_search.

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

### athena/ui.py — the two-mode UI (DONE: single-page app.html)
ONE pywebview window loading ONE page, `assets/ui/app.html`, which holds
both `#orb-view` (96x96 colour-keyed chathead) and `#dashboard-view` (1150x700
HUD). Switching modes NEVER reloads: Python resizes/moves the window and
calls the page's `showDashboard()`/`showOrb()` to toggle a body class, so the
DOM (log, transcript, state) persists across switches - no cache/replay
needed. Switches animate window bounds over ~200 ms.

Reliability rebuild: all JS handlers that call Python are wired only after
the `pywebviewready` event, and every call goes through a guarded, logged
`callApi()`. The orb drags via `api.move_window(dx,dy)` (physical-pixel
deltas moved via Win32, DPI-correct); on mouseup, movement <6 px = CLICK ->
`api.expand()`, otherwise DRAG -> snap to the nearest edge of the current
monitor (Win32 work areas; multi-monitor + taskbar aware), spot saved to
.athena_ui_state.json and restored next session. Escape / minimise button /
rail "orb mode" -> `api.collapse()`. Voice: skills `open_dashboard` /
`close_dashboard` call `ui.expand()` / `ui.collapse()` (main.py injects the
ui ref via `skills.set_ui`). Diagnostics: `[app]` console logs on
mousedown/up/click/drag; `[ui]` terminal prints on expand/collapse.

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
