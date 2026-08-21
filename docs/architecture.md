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

### athena/agents.py — specialist agents (DONE)
An agent is a system prompt, the subset of tools it may be OFFERED, and
optionally a model. Narrowing the tool list improves tool choice (42 schemas
is a lot to pick from) and cuts ~50–90% of the per-request token cost.

**Membership is not permission.** An agent holding a tool only means that tool
is put in front of the model; `safety.py` remains the single authority and its
tier check still runs before execution. `_agent_tools` also intersects with
`settings.is_skill_enabled`, so a skill switched off in the dashboard stays off
inside an agent.

```python
AGENTS: dict[str, dict]      # name -> {prompt, tools, model, description}
tools_for(agent) -> list     # brain.TOOLS filtered by name
tool_names(agent) -> set  /  missing_tools(agent) -> set
prompt_for(agent) / model_for(agent) / exists(agent) / get(agent)
extra_context(agent, user_text) -> str
route(user_text, current_agent=None) -> str
score(user_text) -> dict     # what the router matched, for debugging
wants_exit / wants_consultant / wants_consultant_exit
```

Agents: `general` (derived from `safety.FREE`, so never empty or stale),
`operator`, `scribe`, `analyst`, `researcher`, `consultant`.

**Routing is cheap first.** Weighted keyword tables decide; a model call
(`llama-3.1-8b-instant`, one word out) happens only when two or more agents
genuinely compete. A weak signal nobody contests is not ambiguous — "open
notepad" scores 1 and needs no help. Measured: 35/35 test utterances decided
with no model call. Routing is **sticky** across turns, so a follow-up like
"now sort it by score" stays put. It never raises and never returns a name
that isn't an agent.

### athena/contacts.py — people Athena can offer to reach (DONE)
Two ways in, both deliberate: typed into `settings["trusted_contacts"]`, or
`pull_from_google()`, which loads the contacts the user **starred** via the
People API (`contacts.readonly`). Starred only — the full contact list is
everyone you've ever emailed, which is a different question from who you'd
want called, and inferring closeness from message frequency is not something
Athena does.

```python
list_contacts() / add_contact(name, email, relationship) / remove_contact(name)
find(name) -> dict | None  /  reachable() -> list  /  describe_contacts() -> str
pull_from_google() -> str
reach_out(name, owner="") -> str
```

`reach_out` goes through `mail.send_email`, which reads the recipient back and
refuses without an explicit yes — so nothing is ever sent autonomously. The
message says only "can you check in on me", never what the user said.

### athena/latency.py — where the time goes (MEASUREMENT ONLY)
Times every stage of a turn and appends it to `latency_log.jsonl`
(gitignored). Changes nothing about how anything runs; every entry point
swallows its own errors, so a broken stopwatch can't break a conversation.
Behind `config.LATENCY_TRACE` (default on; `LATENCY_TRACE=0` takes not a
single mark).

```python
start_turn() / mark(stage, seconds) / anchor(name, ts=None) / meta(k, v)
end_turn(user_text="") -> dict | None
table(records=None) -> str      # per-turn columns plus a median
medians() -> dict  /  turns() -> list  /  clear()
```

Marks are taken in four places: `audio_io` (the end-of-speech anchor and the
silence wait), `stt` (call time + `wav_bytes`), `brain._request` (completion
time, in a `finally` so a slow failure is measured too), and `main` (first
audio, and closing the turn). No signature changed anywhere.

**TOTAL is anchored on when the last loud frame was heard, not when the
recorder stopped** — the wait through a pause is one of the stages being
measured, so it belongs inside the total.

Two rows are honest about what they can't separate. `stt` is connect, upload
and inference together, because the SDK does all three in one opaque call.
`brain_total` is also the time-to-first-token: **the live path does not
stream.** `run_agent`'s completion call is blocking and `main` hands TTS the
finished reply (`iter([reply])`), so synthesis cannot begin until the whole
answer exists. `brain.think_stream` does stream but is dead code, superseded
by `run_agent`. DEMO.md's claim that "the first sentence plays while the
model is still writing" describes that dead path, not the live one.

Measure with `python latency_test.py [n]` — the real pipeline minus the wake
word, printing a per-stage table with medians.

### athena/providers.py — the fallback chain (DONE)
When one provider's rate limit is hit, Athena falls through to the next
instead of failing. `config.PROVIDERS` is the ordered chain; each entry is a
base URL, a key env var, a model and a `supports_tools` flag.
`PROVIDER_FALLBACK_ENABLED` (default **False**) is the rollback path — off,
every call is a single Groq call exactly as before.

```python
available() -> list        # keyed and not resting, in chain order
current() / current_name() -> the provider a request would use now
for_tools() -> dict | None # first provider MEASURED to do tool calling
mark_cooldown(name, seconds, reason) -> float
reset(name=None) / shortest_wait() -> float | None
status() -> dict           # for the dashboard
client_for(provider)       # OpenAI SDK, pointed at that provider
cooldown_from_headers(headers) -> float
```

**Why the OpenAI SDK and not the groq one.** `groq.Groq` accepts a `base_url`
and looks like it would work, but it hardcodes `/openai/v1/chat/completions`
onto whatever you give it — so it can reach Groq and nothing else. Mistral is
at `/v1/…`, Gemini at `/v1beta/openai/…`. This was found by
`provider_tools_test.py` returning 404s from every provider. The cost is that
the chain raises `openai.*` exceptions while the rest of Athena raises
`groq.*`; `brain._as_groq_error` translates at the boundary so
`_agent_chat`'s existing handlers keep catching rate limits.

`brain._request` is the only new seam. On 429 or an outage it cools that
provider for what the response says, advances, and retries once. A malformed
request propagates immediately and cools nothing — that's our bug, and every
provider would reject it. When everything is cooling it stops without a call
and says the shortest wait. Tool calls only go to providers whose support has
been **measured**; `supports_tools=None` counts as no, and if an action is
needed with nothing capable reachable she says so rather than answering in
words.

`run_agent`'s step logic, batching, dedup, ledger and malformed-tool-call
recovery are untouched — only the transport beneath them changed.

**Privacy:** the Gemini and Mistral free tiers may use prompts for training.
`trains_on_prompts` marks them, and document or spreadsheet content should
not be routed there.

Verify with `python providers_test.py`, `python fallback_test.py` (both
offline) and `python provider_tools_test.py` (real calls, prints a per-
provider table; it never edits config).

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

run_agent(user_text, history=None, on_step=None, confirm=None,
          agent=None) -> dict
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

# agent= names a specialist from agents.py: its prompt is prepended to the
# system message and its tools replace the full list. IGNORED unless
# config.AGENTS_ENABLED (default False) - with the flag off the request body
# is byte-identical to before agents existed, which is the rollback path.
# An agent with NO tools sends no `tools` key at all, not an empty array.

_distress_turn(user_text) -> dict
# safety.is_distress() is checked FIRST, before the messages are even built,
# so no persona, agent prompt or personality setting can sit on top of it.
# Persona dropped, no tools, and this turn is NOT logged to Supabase. The
# pointer to a real person is appended deterministically rather than left to
# the model, so it can't go missing.

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

is_distress(text: str) -> bool         # the floor
distress_matches(text: str) -> list    # which phrases tripped it, for debugging
DISTRESS_PHRASES / DISTRESS_EXCLUSIONS
```

**The distress floor** is a hard rule on the words themselves — the same kind
of thing as a tool tier, and for the same reason: a judgement call can be
talked around, and this one shouldn't be. `DISTRESS_EXCLUSIONS` are blanked
out of the text *before* the triggers are looked for, so "this spreadsheet is
killing me" and "that cake was to die for" don't fire. Both tenses of each
self-harm phrase are listed — a missed inflection is a missed turn.

Deliberately excluded: "can't go on", "can't do this anymore". Said about a
bad week far more often than a life, and the false-positive rate would train
the user to talk around the mode.

Known limits, stated plainly: substring matching on a speech transcript will
miss indirect phrasing and will occasionally fire on a song lyric. The
asymmetry is deliberate — firing wrongly costs one warm reply.

Current policy — free: open_app, open_website, web_search, get_system_info,
see_screen, look_up, research, guide_me, open/close_dashboard, read_document,
find_document, list_recent_emails, summarize_emails (all read-only);
confirm: set_volume, lock_screen, create_document, write_to_document,
write_local_docx, draft_email, send_email; blocked: none (unknown tools are
blocked).

Spreadsheets — free: open_spreadsheet, list_tabs, use_tab, read_range,
describe_sheet, count_matching. confirm: every mutating operation, including
undo_last_change, plus delete_rows/delete_columns/apply_sheet_operation which
read back exactly what will happen first.

**Every mutating sheet tool is in `brain.NEVER_BATCHED`.** A sheet edit
bundled with other actions would get one blanket approval covering things the
user can't separate, so they only ever run as a request of their own. The
read-only sheet tools stay batchable. `apply_sheet_operation` is also in
`SELF_CONFIRMING` — `sheets.py` does its own plan read-back.

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

The document, email and spreadsheet skills are thin wrappers that lazily
import `documents.py` / `mail.py` / `sheets.py` — the Google libraries are
heavy and most turns never touch them.

**Note on size:** there are now 42 registered tools, and their schemas are
~3,900 tokens sent with *every* brain call — about a third of the Groq free
tier's 12,000 tokens/minute before any conversation or sheet data. If tool
definitions need trimming, the sheet tools are the obvious candidates to fold
behind `apply_sheet_operation`. The PROSE for a document or an email is composed by the
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

set_thumbnail_sink(sink_fn=None) -> None    # main.py wires ui.show_thumbnail
```

**Dashboard thumbnail.** When a capture happens, a small JPEG data URI
(`THUMBNAIL_MAX_EDGE` 360px, quality 62 — ~22KB worst case, since it crosses
to the page as a string inside `evaluate_js`) is pushed to
`ui.show_thumbnail`, which calls `window.showThumbnail(uri, label)`. It fires
**before** the model call, so the audience sees what Athena is looking at
while she's still thinking about it. Entirely best-effort: with no sink wired,
or a sink that raises, vision behaves exactly as it did before — a thumbnail
must never be the reason a spoken answer doesn't arrive. On the page the card
only appears once the browser has decoded the image, so a corrupt URI hides
it rather than showing a broken frame.

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

### athena/sheets.py — spreadsheets (Phase 1: READ-ONLY)
Google Sheets through the API, never a driven browser. Athena keeps a
**current spreadsheet and current tab** in module state, so you name the sheet
once and then just give commands; every function falls back to that state.

**A1 vs GridRange.** The API mixes two incompatible systems and this is where
such code rots, so the rule is explicit: *every public function takes A1
notation* (1-based, end **inclusive** — what a person says out loud).
GridRange (0-based, end **exclusive**) exists only inside `_a1_to_grid`, which
is the single place the off-by-one is converted. Nothing else does index
arithmetic. Column letters are capped at three (`ZZZ` is the last real
column), which is what stops `read_range("banana")` being taken as a column.

**Scope consequence.** Opening by URL or id reaches any of the user's
spreadsheets (the `spreadsheets` scope is not per-file). Opening by NAME goes
through Drive, which under `drive.file` only sees files Athena created — so a
spreadsheet the user made themselves must be opened by URL the first time.
The not-found message says so.

```python
open_spreadsheet(name_or_url_or_id) -> str   # resolve, remember, describe
list_tabs() -> str
use_tab(name) -> str                         # switch the current tab
read_range(a1_range) -> str                  # compact summary, never a dump
describe_sheet() -> str                      # headers, row count, other tabs
current() -> dict                            # {id, title, tab, sheet_id}

# internal, the only range arithmetic in the module:
_col_to_index("C") -> 2      /  _index_to_col(2) -> "C"
_parse_a1("Tab!B3:D10") -> ("Tab", "B3:D10")
_a1_to_grid(a1) -> GridRange /  _grid_to_a1(grid) -> str
```

Adding the `spreadsheets` scope invalidates an existing sign-in — run
`python google_auth_test.py --reset` and approve all five permissions.
Verify with `python sheets_test.py <spreadsheet-url-or-id>`; the target is
required so a test can never wander into real data.

**Undo (DONE).** The Sheets API has no undo — undo is a browser-editor
feature and nothing done through the API goes near that stack, so sheets.py
keeps its own. Before any mutating call, capture the affected range's values
AND formatting; `undo_last_change` writes the captured state back via
`updateCells` with `fields="userEnteredValue,userEnteredFormat"`. Cells the
saved rows don't cover get cleared, which is what undoes anything the change
added beyond the original extent. Stack depth 5, in memory, dies with the
process — a safety net for "no, put that back", not a history.

Ranges are `_bounded()` before snapshotting: `"B:B"` has no row bounds, and
an unbounded restore range would let the API decide what to clear. A failed
restore stays on the stack rather than being silently dropped. Undoing in a
spreadsheet the user has since navigated away from names that spreadsheet.

A snapshot may carry an `"inverse"` key — a list of batchUpdate requests to
run instead of restoring cells. Structural edits (inserting or deleting rows)
shift the grid, so writing old values back does **not** reverse them; those
need an inverse request. Phase 3's insert/delete/move will use it.

```python
_snapshot(a1_range) -> dict | None   # None means DON'T mutate: no way back
_push_undo(label, snapshot) -> bool  # False means nothing was recorded
undo_last_change() -> str
undo_depth() -> int
_apply(requests, spreadsheet_id="") -> str | None   # batchUpdate, None = ok
_bounded(grid) -> dict               # fill unbounded edges from the tab size
```

Verify with `python sheets_undo_test.py <url> [range]` — it writes to a
far-off scratch block, proves values and background colours both come back,
and restores that block to how it found it.

**Named operations (DONE).** Each takes typed arguments, snapshots before
mutating, and returns one short sentence.

```python
sort_range(a1_range, column, order="asc"|"desc") -> str
filter_rows(column, condition, value="") -> str      # sets the basic filter
clear_filter() -> str
color_range(a1_range, color_name) -> str
highlight_rows_where(column, condition, value, color_name) -> str
add_formula(cell, formula) -> str                    # entered as if typed
count_matching(column, condition, value="") -> str   # READ-ONLY
insert_rows(at_index, count=1) / insert_columns(at_index, count=1) -> str
delete_rows(start, end=0) / delete_columns(start, end=0) -> str
move_rows(from_start, from_end, to_index) -> str
freeze_header(rows=1) -> str
autosize_columns() -> str
```

**Ranges take A1; rows and columns take spoken numbers** — 1-based and
INCLUSIVE, so "delete rows 3 to 5" is the three rows labelled 3, 4, 5 in the
sheet's own margin. `_dim_range` is the only place that converts, alongside
`_a1_to_grid`.

`column` accepts a header name ("Score"), a column letter ("C"), or a spoken
number ("2"). **Header names win over letters** — someone saying "sort by C"
almost certainly means a column headed C if one exists.

`CONDITIONS` maps each condition to both a Sheets `ConditionType` (for the
basic filter) and a local predicate (for `count_matching` and
`highlight_rows_where`). `CONDITION_ALIASES` absorbs the many ways a person
says the same thing ("is", "=", "more than", "above", "is blank"). A number
comparison against text is False, not an error. `COLORS` is six named
shades as RGB floats, plus aliases; users speak colour names, never hex.

**`_mutate(label, snapshot, requests, success)` is the shape every mutating
function takes** and the place the undo invariant is enforced: if
`_push_undo` returns False nothing is attempted at all, and if the change
fails the undo entry is popped again — a phantom entry would make the next
undo restore something that was never changed.

Which ops need an inverse rather than a cell snapshot: filter/clear_filter (a
filter isn't cell data), insert/delete/move (they shift the grid),
freeze_header and autosize_columns (sheet and column properties). `delete_*`
captures the doomed cells first and its inverse is *insert then write back*;
it refuses to delete at all if that capture fails.

Verify with `python sheets_ops_test.py <url>` — builds its own table in a
far-off scratch block, runs and undoes every operation, restores the block.

**The escape hatch (DONE).** `apply_sheet_operation(natural_language_request)`
for the unusual request no named operation covers. The model writes the
batchUpdate body; **none of it is trusted.**

```python
set_confirm(confirm_fn=None) -> None      # main.py injects confirm(q) -> bool
apply_sheet_operation(natural_language_request) -> str
```

The order is: generate → validate → describe → confirm → snapshot → apply.

*Validation* refuses anything that isn't a list of single-key objects whose
keys are in `KNOWN_REQUEST_TYPES` (the real batchUpdate types — an invented
key means the model made something up, and inventions don't run), more than
`MAX_GENERATED_REQUESTS`, any `spreadsheetId`/`destinationSpreadsheetId`
anywhere in the tree that isn't the current file, or any `sheetId` that isn't
a tab of the current file. The scan walks the whole nested structure, so a
buried key can't slip past.

*Description* is generated **from the validated JSON, not from anything the
model said about it** — that matters, because the sentence the user approves
has to describe what will actually run. Requests in `STRUCTURAL_TYPES` add a
spoken warning that the undo will only be partial, since a cell snapshot
can't reverse a shape change.

*Confirmation* goes through `set_confirm`, like `mail.send_email`. **With no
hook wired it states the plan and does nothing**; a "no", or a confirm call
that raises, leaves the sheet alone. Only then is the whole used tab
snapshotted and the body applied through `_mutate`.

Verify with `python sheets_hatch_test.py <url>` — a dry run that answers no
to everything and prints each generated body and its description, so the
descriptions can be read against the requests. `--execute` runs one operation
for real in a scratch block and undoes it.

**Provider routing (DONE).** `apply_sheet_operation` is the only place in
sheets.py that uses a model at all; everything else is plain API calls. That
one call is token-heavy (it carries the sheet's shape and headers), so it can
be moved off Groq's 12,000-tokens-per-minute free tier:

```python
_sheets_provider() -> str      # settings["sheets_provider"], read at call time
_ask_model(system, user, max_tokens=900) -> (text, error)
_ask_gemini(...) / _ask_groq(...)
```

Set `SHEETS_PROVIDER=gemini` and `GEMINI_API_KEY` in `.env` (or flip
`sheets_provider` in settings, which is read at call time so it needs no
restart). **The routing lives at this call site — `brain.py` is deliberately
untouched** and the voice loop stays on Groq.

Gemini is reached by plain REST over `urllib`; one call site doesn't justify
another dependency, and the key goes in the `x-goog-api-key` header rather
than the URL. **Any Gemini failure — no key, bad key, HTTP error, timeout,
unusable response — falls back to Groq with a printed reason**, so switching
providers can never leave the feature broken.

Verify with `python sheets_provider_test.py` (add a URL to also generate a
real body on each provider). Writes nothing.

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
