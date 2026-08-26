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

NEVER_BATCHED: set     # send_email, every sheet edit, every calendar write.
                       # Skipped unless it is the ONLY action of the turn -
                       # bundled in one reply, chained after something else,
                       # or retried by the model with drifted arguments.
                       # `in_batch` alone missed the last two; the calendar
                       # found that, and a retry had run create_event three
                       # times in one turn before it was closed.
SELF_CONFIRMING: set   # the skill asks in its own words. Honoured by BOTH
                       # run_agent and think() - think() used to ask its own
                       # generic question first, so these tools were confirmed
                       # twice and the first read back the raw arguments.
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
SCOPES: list[str]      # documents, drive.file, gmail.readonly, gmail.compose,
                       # spreadsheets, calendar.events

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

### athena/sheet_builder.py — assembling multi-stage changes (DONE)
Builder pattern. A multi-stage spreadsheet change is assembled, validated in
full, described in plain English, then executed all at once or not at all.
Written because "delete all the rows that are coloured orange" needs three
stages — read the formats, decide which rows match, delete them DESCENDING so
an earlier delete doesn't shift a later one — and one batchUpdate can't
express that.

```python
SheetRequestBuilder(facts=None)   # ConcreteBuilder; no API calls while chaining
  .on_sheet(tab)  .sort(column, order, a1_range=None)
  .filter(column, condition, value)  .clear_filter()
  .colour(a1_range, colour)  .highlight_where(column, condition, value, colour)
  .insert_rows(at, count)  .delete_rows(start, end)  .delete_rows_where(predicate)
  .insert_columns(at, count)  .delete_columns(start, end)
  .move_rows(from_start, from_end, to_index)
  .set_formula(cell, formula)  .freeze(rows)  .autosize()  .export(path)
  .resolve(reader=None) -> self    # the ONLY step that reads the sheet
  .build() -> SheetPlan            # validates, or raises PlanError

SheetPlan          # Product: frozen, every field a tuple
SheetFacts         # headers, row/column counts - no network needed
PlanDirector.tidy / clean_rows_by_colour / report
execute(plan, reader=None, applier=None) -> str
snapshot_for(plan) / _restore(snapshot) / export_csv(plan)
match_colour_name(rgb) -> str | None
```

**A1 conversion is DELEGATED** to `sheets._a1_to_grid` rather than duplicated,
so one implementation exists in the codebase. The tab prefix is stripped first
and an explicit `sheet_id` passed, because that function looks an unfamiliar
tab name up over the network and chaining must never touch it.

**Colour matching is by HUE, not RGB distance.** Palette entries sit only
0.103 apart in RGB (orange/red), so any threshold loose enough for a user's
own shade also confuses them — measured against real Google swatches,
nearest-RGB got two of five wrong. Nearest palette hue; saturation below 0.06
is grey, or nothing when near-white; beyond 55° is rejected. Asking for a
colour that isn't present reports which colours **are**.

**Execution is all-or-nothing**, and most of that guarantee is the API's:
`batchUpdate` validates every request up front and applies none if any is
invalid. Every plan is exactly one batchUpdate. The snapshot still makes a
successful change undoable, and on failure the sheet is restored anyway —
including its row COUNT, since a delete leaves the grid shorter.

`sheets.apply_sheet_operation` now asks the model for a **structured plan** —
a list of named steps — which our code translates into builder calls, so an
invented step or a bad argument is rejected here rather than sent to Google.
All thirteen mutating named operations are thin wrappers over the same builder
via `sheets._via_builder`, so there is one code path rather than two.

Verify with `python sheet_builder_test.py` (119 checks, offline) and
`python sheet_builder_live_test.py <url>` (19 checks on a scratch tab it
creates and deletes).

Patterns used across the project are recorded in **docs/design-patterns.md**.

### athena/calendar_skill.py — the calendar, spoken (Phases 1–2 DONE)
Google Calendar through the API. Named `calendar_skill.py` so it can never be
confused with the standard library's `calendar`. Reading is free; creating,
moving and cancelling are gated (below).

**Saying it, not printing it.** The API deals in `2026-08-26T14:30:00+06:00`;
a listener hears "half past two this afternoon". That translation is most of
the module and it lives in pure `_spoken_*` functions of a datetime, which is
what lets 39 phrasing checks run offline with no sign-in and no network. The
four spoken rules are structural: never a raw timestamp, group by day once an
answer spans more than one, say "you have nothing scheduled" rather than
returning an empty list, and stop at `MAX_SPOKEN_EVENTS` (5) then say how many
are left.

**Time zone.** The machine's own, from `datetime.now().astimezone()` —
Bangladesh Standard Time, UTC+06:00 here. There is no IANA database on this
machine (`zoneinfo.TZPATH` is empty), and none is needed: the Calendar API
accepts RFC3339 stamps that carry their own offset. `timezone_name()` reports
what was detected.

**A week ends on Friday.** The weekend here is Friday and Saturday, so
"this week" is today through the coming Friday inclusive. Asked on a Friday
that is just today; asked on a Saturday it runs the full seven days round to
next Friday.

**Scope.** `calendar.events`, deliberately not the full `calendar` scope —
Athena reads and books events and has no business managing calendars
themselves. Adding it invalidates any earlier sign-in on purpose
(`google_auth._load_cached`), so the first run after this change needs
`python google_auth_test.py --reset` and fresh consent. A token that predates
the scope gets its own sentence ("signed in, but not yet for your calendar"),
kept separate from "not signed in" because the fix is different.

**"Tomorrow at 3" is resolved by `resolve_when`, in this module** — not by the
model, and not by a library. That is a measured decision, not a preference.
Run from a Wednesday at 13:15:

| phrase | dateutil | dateparser | `resolve_when` |
|---|---|---|---|
| "tomorrow at 3" | 2026-08-03 13:15 | 2026-08-27 13:15 | Thu 15:00 |
| "next Monday morning" | 2026-08-31 13:15 | `None` | Mon 09:00 |
| "in two hours" | `ParserError` | 2026-08-26 15:15 | Wed 15:15 |
| "Friday at 7pm" | 2026-08-28 19:15 | 2026-08-28 19:00 | Fri 19:00 |
| "at 3" | 2026-08-03 13:15 | 2027-03-26 00:00 | Wed 15:00 |

dateutil read the "3" of "tomorrow at 3" as a day of the month and produced a
date three weeks in the **past**; dateparser dropped the "at 3" entirely.
Neither raised. Silently wrong beats loudly unsure only if you never look, so
this is ours: stdlib `datetime` + `re`, returning **either a moment or a
question**, never a guess it can't justify. A day with no time asks "what time
tomorrow?" rather than inventing 9am. Its one deliberate guess is the bare
hour — **1–6 is the afternoon, 7–11 the morning, 12 is midday** — and the
read-back exists to catch exactly that.

**The confirm gate lives in this module** (`set_confirm`, as `mail.py` does),
not only in `safety.py`. It has to: the model passes the raw words "tomorrow
at 3", and only this module knows they mean three o'clock tomorrow afternoon.
Reading the raw arguments back through `brain.CONFIRM_PHRASING` would confirm
the mishearing rather than catch it. So these three are `SELF_CONFIRMING` —
still confirm-tier, just asking in their own words. **With no hook wired every
write refuses.** A clash doesn't block a booking; it changes the question, so
a double-booking is something you agree to rather than discover. All-day
events and anything the calendar marks free never count as a clash.

```python
get_current_time() -> str            # local time and date, spoken naturally
read_schedule(when="today") -> str   # today / tomorrow / this week / a date
next_event() -> str
find_event(query) -> str             # title match, not Google's whole-event q

set_confirm(confirm_fn=None) -> None      # main.py injects confirm(q) -> bool
create_event(title, start, end=None, description=None) -> str
reschedule_event(event_identifier, new_start, new_end=None) -> str
cancel_event(event_identifier) -> str

resolve_when(text, base=None, roll_past=True)  # -> (datetime, "") | (None, q)
timezone_name() -> str               # what zone we detected, for setup notes
```

Events are identified by **title**: one match acts, none says so, several read
the candidates back and ask which — never a pick made on the user's behalf.
No end given means an hour; `end` also takes a duration ("for two hours").
A reschedule keeps whatever length the event already had, so moving a two-hour
meeting doesn't silently shrink it. No `timeZone` field is ever sent — the
RFC3339 stamps carry their own offset.

Recurring events are expanded with `singleEvents=True`, so a weekly standup is
a meeting on Tuesday rather than one row with a rule attached. All-day events
come back as a bare date and are read as "all day", never as midnight.
Unreachable, unauthorised, or an API switched off in the Cloud Console each
get one honest sentence — never a stack trace, and **a write that failed is
never reported as a success**.

Verify with `python calendar_test.py` (41 checks, `--offline` needs nothing at
all) and `python calendar_write_test.py` (60 checks against a fake calendar
that records every write, so a write escaping its gate shows up as a recorded
call; `--live` creates, moves and deletes one throwaway event for real).

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
back). One shared client is created lazily and reused (never per call);
`shared_client()` hands it to `event_status.py`, which must not build a
second.

### athena/event_status.py — what happened to an event (DONE)
Supabase `event_status` table; the schema SQL is in the module docstring, as
memory.py's is. **Google Calendar has no "done" flag** — an event whose time
has passed looks identical to one that never happened — so if Athena is to
ask "did you actually go?", she has to remember having asked. One row per
event id (unique), so a question is asked once and never again.

Four statuses, and no others: `done` and `skipped` are the user answering,
`rescheduled` is written when an event is moved out of the past, `deferred`
is what a changed subject records so she drops it and stops chasing. Anything
else is refused before it reaches the table.

`record` is an **upsert** — an event answered "deferred" this morning and
"done" this evening ends as one row saying done, not two rows disagreeing.
Writes go through the same fire-and-forget thread as
`memory.log_interaction_async`, so a slow network never delays speech.
`prune()` drops rows over `PRUNE_AFTER_DAYS` (30); it runs once per session
on a background thread the first time the store is used — never on a timer.

**One client, not two.** It comes from `memory.shared_client()`; see the
Singleton entry in docs/design-patterns.md. Degrades exactly as memory.py
does: unconfigured or unreachable means one warning and empty results
forever after, and Athena simply doesn't follow up. Not gated by the
`memory_enabled` setting — switching off conversation logging shouldn't make
her start re-asking about events she already asked about.

```python
record(event_id, status) -> bool          # upsert; unknown status refused
record_async(event_id, status) -> None    # fire-and-forget thread
recorded_ids(event_ids) -> set            # which of these are answered for
status_of(event_id) -> str                # "" if never recorded
recent(n=10) -> list[dict]
prune(days=30) -> int
forget(event_id) -> bool                  # test cleanup only
```

`recorded_ids` is the query phase 4 actually needs ("what have I *not* asked
about"), so it filters on the ids in hand rather than reading the table. When
the store is unavailable it returns an empty set, meaning every event looks
unanswered — the right way round, since the alternative silently swallows
follow-ups.

Verify with `python event_status_test.py` (`--offline` for the degradation
checks alone, which need nothing). The live half writes one row, overwrites
it, prunes and deletes itself; it distinguishes an unreachable project from a
missing table rather than blaming the table for both.

### athena/followup.py — asking whether you did it (DONE)
An event whose time has passed says nothing about whether it happened. This
closes the loop: find past events nobody has answered for, ask about them one
at a time, write the answer to `event_status.py` so the question is asked
once and never again.

**The restraint is the feature**, and each piece of it is a check in the test
script:

- ONE event per question, oldest first. Never a batch, never a list.
- `MAX_PER_SESSION` = 3. A week away earns three questions, not an
  interrogation. Re-asking in the same session doesn't reopen the cap.
- Startup and on request only. **There is no timer in this module** and there
  must not be one — a question that arrives mid-conversation is an
  interruption however well worded. A test greps the source for
  `threading.Timer`, `sched.`, `time.sleep` and `schedule.`.
- Silence or a changed subject records `deferred` and stops **the whole run**,
  not just that event — continuing to the next question after someone has
  moved on is chasing.
- "No" records `skipped` and moves on **without comment**. No consolation, no
  encouragement; a check asserts nothing at all is said.
- "Yes" gets one short warm sentence from `ACKNOWLEDGEMENTS`, never repeated
  within a session. A fixed phrase heard three times stops being an answer
  and becomes a tic.

`_yes_or_no` returns `True`/`False`/**`None`**, and `None` is the load-bearing
one: "anything that isn't a clear yes or no" is how a changed subject is
detected, so it must not stretch to fit. No is tested before yes, because
"I didn't" contains "did".

Speech is injected via `set_io`, the same hook `guide.py` uses, so the whole
exchange runs in tests with no microphone. Rescheduling goes through
`calendar_skill.reschedule_event(..., event=...)` — **by event, not by title**,
since "Standup" also matches tomorrow's — and that write keeps its own
read-back and its own yes rather than riding along on the follow-up. A move
that fails records nothing.

```python
set_io(speak_fn=None, listen_fn=None) -> None
reset_session() -> None                   # main.py calls this per session
pending_followups() -> list               # past, unanswered, oldest first
run(limit=3) -> str                       # the exchange; one spoken sentence
on_startup() -> str                       # gated by followup_on_startup
```

`pending_followups` skips all-day events (holidays and birthdays, not tasks),
declined invitations, and anything still in progress. `[]` whenever anything
is unavailable, which is what makes "she simply does not follow up" the
failure mode. An unreachable store leaves every event looking unanswered —
the right way round, since the alternative silently swallows follow-ups.

Setting: `followup_on_startup` (default True) in `settings.py`, read at call
time so a demo can switch it off without a restart.

Verify with `python followup_test.py` — 52 checks that script the entire
conversation offline; `--live` creates one real past event, confirms it is
found, and deletes it.

**Wired in** (the four-step rule): all eight are in `skills.SKILLS`, have a
`brain.TOOLS` schema, and carry a tier — free for `get_current_time`,
`read_schedule`, `next_event`, `find_event` and `pending_followups`, confirm
for `create_event`, `reschedule_event` and `cancel_event`. The three writes
are also in `brain.NEVER_BATCHED` and `brain.SELF_CONFIRMING`. `main.py`
wires `calendar_skill.set_confirm`, `followup.set_io`,
`followup.reset_session`, and calls `followup.on_startup()` exactly once at
the top of `assistant_loop`.

**The schemas pass times through as spoken.** `create_event.start` is
documented as "in the user's own words … Never a timestamp", because
`resolve_when` is the only thing that knows what "tomorrow at 3" means on
this machine — a model computing a date computes it in its own time zone.
Verified live: "book a dentist appointment tomorrow at 3" arrives as
`{'title': 'Dentist appointment', 'start': 'tomorrow at 3'}`.

Verify with `python calendar_wiring_test.py` (86 checks, offline). It also
checks the whole project for four-step gaps — a skill with no schema is
invisible to the model, and a schema with no tier is *blocked* by
`safety.classify` at the moment someone asks for it.

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
