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

NEVER_BATCHED: set     # {"send_email"} - skipped if bundled with other calls
SELF_CONFIRMING: set   # {"send_email"} - the skill asks in its own words
CONFIRM_PHRASING: dict # short yes/no wording per tool; the generic phrasing
                       # joins every argument, which would read a whole
                       # document or email body aloud before writing it
```

### athena/safety.py — permission gate (DONE)

```python
classify(tool_name: str) -> str        # 'free' | 'confirm' | 'blocked'
confirm_needed(tool_name: str) -> bool
```

Current policy — free: open_app, open_website, web_search, get_system_info,
see_screen, look_up, research, guide_me, open/close_dashboard, read_document,
find_document, list_recent_emails, summarize_emails (all read-only);
confirm: set_volume, lock_screen, create_document, write_to_document,
write_local_docx, draft_email, send_email; blocked: none (unknown tools are
blocked).

`send_email` carries two extra guards beyond its tier: it's in
`brain.NEVER_BATCHED` so it can never run inside a multi-step chain, and it's
in `brain.SELF_CONFIRMING` so the read-back question comes from `mail.py`
(recipient + subject) rather than the generic one. That delegation cannot
weaken the gate — `mail.send_email` refuses to send without an explicit yes
regardless of what the safety layer did, including when
`confirm_before_acting` is switched off in settings.

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
create_document(title) / write_to_document(title, text, mode) /
find_document(title) / read_document(title) / write_local_docx(title, text)
list_recent_emails(n) / summarize_emails(n) /
draft_email(to, subject, body) / send_email(to, subject, body)
SKILLS: dict[str, callable]   # name -> function, used by brain to dispatch
```

The document and email skills are thin wrappers that lazily import
`documents.py` / `mail.py` — the Google libraries are heavy and most turns
never touch them. The PROSE for a document or an email is composed by the
brain and passed in as an argument; the skills only carry it.

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

### athena/google_auth.py — Google sign-in (DONE)
Owns the whole OAuth2 desktop flow for Docs and Gmail, and nothing else.
Client ID/secret come from `config.GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`
and are assembled into an in-memory client config — there is no
`client_secret.json` on disk. The sign-in is cached to
`config.GOOGLE_TOKEN_FILE` (`google_token.json`, gitignored) and refreshed
silently when it expires.

`interactive` defaults to **False everywhere in the assistant loop**: the
consent screen blocks on a local web server, which would hang a voice turn
with nothing on screen to explain it. Only `google_auth_test.py` passes
`interactive=True`. Skills that find no sign-in speak
`not_connected_message()` instead of failing. Scopes are compared against the
cached token on load, so adding a scope later gives a clear "sign in again"
message rather than an error deep inside an API call.

```python
SCOPES: list[str]      # documents, drive.file, gmail.readonly, gmail.compose

is_configured() -> bool                 # client id + secret present in .env
get_credentials(interactive=False)      # -> Credentials | None
get_service(api: str, version: str)     # -> API client | None, cached per pair
check_connection(interactive=False) -> dict
# {"ok", "detail", "account", "scopes", "drive", "gmail"} - really calls
# Drive and Gmail, so an API switched off in the Cloud Console is caught
not_connected_message() -> str          # the one shared "not signed in" line
sign_out() -> str                       # delete the cached token
```

Verify with `python google_auth_test.py` (`--reset` to re-consent). While the
Cloud project is in Testing mode Google expires the sign-in after 7 days.

### athena/documents.py — writing documents (DONE)
Google Docs through the API (never a driven browser: Docs renders to canvas,
so there is no DOM to type into, and Google blocks automated sign-in). These
functions only move text in and out — the PROSE always comes from the brain,
which composes the text and hands it to `write_to_document`.

A document is named by id or by title; anything not matching an id is looked
up by name via Drive. The `drive.file` scope means Athena only ever sees
documents SHE created. Ambiguous titles ask which one rather than guessing;
the last document touched is remembered, so "add a line to it" works without
repeating the title. After a successful write the doc opens with
`webbrowser.open` (module flag `OPEN_IN_BROWSER`, which the test script turns
off). `read_document` is capped at `MAX_SPOKEN_CHARS` because it's spoken.

```python
create_document(title) -> str
write_to_document(doc_id_or_title, text, mode="append"|"replace") -> str
find_document(title) -> str          # search by name, report the matches
read_document(doc_id_or_title) -> str
write_local_docx(title, text) -> str
# offline fallback: a real .docx via python-docx, saved to the user's
# Documents folder and opened with os.startfile - no internet, no Google
```

Verify with `python documents_test.py` (`--local` for the fallback only).

### athena/mail.py — email (DONE)
Gmail through the API. Reading and writing are deliberately kept apart:
`draft_email` only ever creates a DRAFT sitting in Gmail, and `send_email` is
the only function that can put a message on the wire.

`send_email` will not fire on its own. It reads the recipient and subject
back and waits for an explicit yes through the confirm hook main.py wires in
with `set_confirm()` — the same yes/no the safety gate uses, so voice or a
dashboard click both work. **With no hook wired it refuses to send and saves
a draft instead**; same if the user says no, or if the confirm call itself
raises. That gate lives INSIDE this module on purpose, so a send is still
safe if it is ever called from somewhere that skipped `safety.py`. Bad
recipients and empty bodies are refused before the gate is even reached.

Summaries call Groq directly rather than through `brain.py`, so
`skills -> mail -> brain -> skills` never becomes an import loop.

```python
set_confirm(confirm_fn=None) -> None    # main.py injects confirm(question)->bool

list_recent_emails(n=10) -> str    # senders + subjects only, never contents
summarize_emails(n=10) -> str      # fetches bodies, the LLM condenses them
draft_email(to, subject, body) -> str   # saves a draft; NEVER sends
send_email(to, subject, body) -> str    # gated; drafts instead of sending
                                        # whenever it isn't explicitly approved
```

One recipient per message; a name instead of an address, or a comma-separated
list, is refused with a spoken explanation rather than guessed at.

Verify with `python mail_test.py` — it sends nothing by default and proves
the gate holds. `--send-real` sends one mail to your own address.

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
