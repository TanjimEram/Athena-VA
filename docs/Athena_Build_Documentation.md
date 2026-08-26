# Athena — Build Documentation

This documents what **actually exists and runs today**, not what was planned.
Anything not built yet is clearly marked **PLANNED**.

---

## 1. What Athena is

Athena is a voice assistant that lives on a Windows laptop, like a homemade
Siri or Alexa. You say "Hey Athena", she answers "Yes?" in an Irish voice,
and you tell her what you want in normal words — "open Chrome", "how's the
battery", "lock the screen". A cloud AI model figures out what you meant and
either answers you or actually does the thing on your PC. Risky actions
(like locking the screen) are only done after she asks you to confirm out
loud. She never pretends: if something fails, she tells you it failed and why.

## 2. How it works — the runtime loop

```
            (always listening, local, free)
  ┌──────────────┐
  │  WAKE WORD   │  "Hey Athena"  — openWakeWord, runs on this laptop
  └──────┬───────┘
         │ mic released, Athena says "Yes?"
         ▼
  ┌──────────────┐
  │ SPEECH→TEXT  │  5 s recording → Groq whisper-large-v3-turbo → plain text
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │    BRAIN     │  Groq llama-3.3-70b-versatile + tool calling:
  │ (LLM+tools)  │  chats, OR picks a skill and fills in its arguments
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ SAFETY CHECK │  free → run now | confirm → ask first | blocked → refuse
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │    SKILLS    │  really does it: launch app, set volume, lock screen...
  └──────┬───────┘  returns an honest one-line result
         ▼
  ┌──────────────┐
  │ TEXT→SPEECH  │  edge-tts (en-IE-EmilyNeural) → mp3 → pygame plays it
  └──────┬───────┘
         │
         └────────► back to waiting for the wake word
```

The orb UI (a glowing status window) exists and works on its own but is
**not wired into this loop yet**.

## 3. Tech stack

Context for the "why": the dev laptop has 16 GB RAM and an AMD Radeon GPU —
no CUDA — on Windows 11, so anything needing an NVIDIA GPU or huge local
models is out. Heavy AI runs in the cloud on free tiers; light AI runs
locally.

| Component | Tool chosen | Why (for this laptop) | Offline fallback |
|---|---|---|---|
| Wake word | openWakeWord, custom-trained `hey_athena` (ONNX) | Tiny model, runs fine on CPU; no account, no API key, never expires | Already offline |
| Speech-to-text | Groq `whisper-large-v3-turbo` | Big-model accuracy with zero local compute; same free API key as the brain | faster-whisper small model on CPU, or Vosk |
| Brain + tool calling | Groq `llama-3.3-70b-versatile` | A 70B model can't fit in 16 GB shared RAM and AMD has no CUDA for local inference; Groq's free tier serves it fast and supports OpenAI-style tool calling | Ollama/llama.cpp with a small (3–8B) model — noticeably dumber |
| Voice (TTS) | edge-tts, voice `en-IE-EmilyNeural` | Free, natural-sounding Irish female voice — the FRIDAY sound; no key needed | pyttsx3 (Windows SAPI voices — robotic but offline) |
| Memory | Supabase — **PLANNED** | Free Postgres in the cloud; conversation memory survives reinstalls and is shared across devices | Local SQLite/JSON file |
| UI | pywebview orb (built, not yet wired into main loop) | One small window rendering HTML/CSS — full animation freedom without a game engine | Already offline |
| Packaging | PyInstaller — **PLANNED** | Turns the project into a single .exe teammates can run without Python installed | n/a |

## 4. Accounts, API keys, and .env

You need exactly **one** account: **Groq** (https://console.groq.com — free,
no credit card). One key covers both the brain and speech-to-text.
(Later, when memory is built: a free Supabase account too — **PLANNED**.)

How keys work here:

- The real key lives in a file called `.env` in the repo root:
  `GROQ_API_KEY=gsk_...`
- `athena/config.py` loads `.env` at startup with python-dotenv. **No other
  file reads environment variables and no key ever appears in code.**
- `.env` is listed in `.gitignore`, so **it is never committed**. What gets
  committed is `.env.example` — the same file with a placeholder instead of
  a real key. New teammates copy it to `.env` and paste their own key.

## 5. Repository structure

Owners — Member 1 (Tahsin): brain, tool calling, skills · Member 2: memory ·
Member 3: execution/safety. Files not in someone's area are shared.

```
athena-va/
├── athena/                  the assistant itself (a Python package)
│   ├── __init__.py          makes the folder importable                [shared]
│   ├── config.py            loads .env; every setting/constant         [shared]
│   ├── brain.py             LLM + tool calling, the decision-maker     [Tahsin]
│   ├── skills.py            real Windows actions (apps/volume/lock...) [Tahsin]
│   ├── safety.py            free/confirm/blocked gate for every tool   [Member 3]
│   ├── wake.py              openWakeWord "Hey Athena" listener         [shared]
│   ├── audio_io.py          microphone recording to 16 kHz WAV         [shared]
│   ├── stt.py               WAV → text via Groq Whisper                [shared]
│   ├── tts.py               text → Emily's voice via edge-tts + pygame [shared]
│   ├── ui.py                the orb window (pywebview wrapper)         [shared]
│   └── main.py              always-on loop: wake→listen→think→speak    [shared]
├── assets/ui/orb.html       the animated orb (self-contained HTML/CSS/JS)
├── docs/
│   ├── architecture.md      module contract the team codes against
│   └── Athena_Build_Documentation.md   this file
├── run_brain_test.py        typed chat test of the brain (no mic needed)
├── run_ui_test.py           opens the orb, cycles its four states
├── stt_test.py              records 5 s and prints what you said
├── tts_test.py              speaks one sentence so you can hear Emily
├── talk_test.py             full spoken conversation (Enter to talk)
├── requirements.txt         every pip dependency
├── .env.example             template for the key file (committed)
├── .env                     your real key (NEVER committed)
└── .gitignore               keeps .env, .venv/ and __pycache__/ out of git
```

(`athena/memory.py` does **not exist yet** — it's Member 2's PLANNED module.)

## 6. Setup from zero (Windows 11)

```
git clone <repo-url> athena-va
cd athena-va
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then open `.env` and replace the placeholder with your own Groq key.
First wake-word run downloads the small openWakeWord model files
automatically (one-time, needs internet).

## 7. How each module works (viva guide)

**config.py — one place for settings.** Loads `.env` once and exposes the
API key plus every constant (model names, voice, wake threshold). Key idea:
*single source of truth* — changing a model or voice is a one-line edit here,
and secrets stay out of source code.

**brain.py — the decision-maker, built on tool calling.** The important
concept: we do NOT hard-code phrases like "if the user says 'open chrome'
then...". Instead, every skill is described to the model in a `tools` array —
its name, what it does in plain English, and what arguments it takes (as a
JSON schema). The model reads the user's sentence and *decides for itself*
whether to just answer with words or to call a tool, and it fills in the
arguments — "fire up the browser, would you" comes back as
`open_app(name="Chrome")` without us ever anticipating that phrasing.
`think()` sends the chat history + tools to Groq, parses any tool call,
routes it through safety, runs the skill if allowed, and returns a dict:
`{reply_text, tool_called, args, needs_confirmation}`. The spoken reply for
an action is **exactly the string the skill returned** — the model never gets
to invent "Done!" for something that failed. Errors (rate limit, no
internet, bad key) each produce a calm spoken fallback instead of a crash.

**skills.py — the hands.** Real implementations, not stubs: `open_app` maps
friendly names (chrome, vscode, spotify...) to executables, tries the PATH
first, then Windows' registered-apps lookup (ShellExecute), so apps not on
PATH still launch; `open_website`/`web_search` use the `webbrowser` module;
`set_volume` talks to the Windows audio COM interface via pycaw and reports
the volume it *read back* after setting; `lock_screen` calls
`user32.LockWorkStation()`; `get_system_info` reads battery, CPU, memory and
volume via psutil + pycaw. Key idea: every function returns a short honest
sentence about what actually happened, and that sentence is what Athena
speaks. Failure returns a clear reason — never a fake success.

**safety.py — the permission gate.** Three sets: FREE runs immediately
(open_app, open_website, web_search, get_system_info), CONFIRM needs a
spoken yes first (set_volume, lock_screen), BLOCKED is refused (empty so
far). Key idea: the *model* never decides what's safe — a dumb, auditable
Python lookup does, and unknown tool names are treated as blocked.

**wake.py — the always-on ear.** Streams the mic in 80 ms frames through
openWakeWord's `hey_athena` detector — a small neural net running locally on
CPU. When the confidence score passes `WAKE_THRESHOLD` (0.5, tunable in
config) it returns True and *releases the microphone* so the recorder can
open it next. Swapping in a custom "Athena" model later = pointing
`WAKE_MODEL` at a `.onnx` file. Windows detail: it must use the ONNX
inference backend, because the default tflite runtime doesn't exist on
Windows.

**audio_io.py — the microphone.** `record(seconds=5)` captures mono audio at
16 kHz with sounddevice and writes a temp WAV with scipy. Key idea: 16 kHz
mono is the format speech models expect. If the mic won't open it prints a
pointer to the Windows microphone privacy setting instead of crashing.

**stt.py — speech to text.** Uploads the WAV to Groq's Whisper
(`whisper-large-v3-turbo`) and returns the plain text, `""` on any failure.
Reuses the same client/key as the brain.

**tts.py — the voice.** edge-tts synthesizes the sentence into a temporary
mp3 (async under the hood, wrapped so `speak()` stays a normal blocking
call), pygame plays it, and the file is unloaded then deleted — the unload
matters because Windows won't delete a file pygame still holds open. No
internet → clear printed message, no crash.

**ui.py + orb.html — the face.** A frameless, always-on-top, draggable
320×320 window showing a glowing orb with four looks: idle (slow pulse),
listening (blue ripples), thinking (amber swirl), speaking (green reactive
pulse). Python switches them by calling the page's `window.setState(...)`.
Key idea: pywebview must own the program's main thread, so `ui.start()`
blocks and your app loop runs in a background thread it spawns. **Built and
demoed, not yet wired into main.py.**

**main.py — the conductor.** The always-on loop gluing it all together:
wait for "Hey Athena" → speak "Yes?" → record 5 s → transcribe → think →
speak the honest reply — and for confirm-level actions, listen for a spoken
"yes" before running them. Keeps conversation history across turns so
follow-up questions work. Ctrl+C exits cleanly.

## 8. Current status

**Works today (all verified by running):**
- Typed brain test: tool calling, safety gating, and chat with no mic.
- Full spoken conversation (talk_test.py): record → Whisper → brain decides
  → real skill runs → Emily speaks the honest result.
- Real skills: apps actually launch, volume actually changes, battery is
  actually read; failures are reported truthfully.
- Wake word: "Hey Athena" detection, local and free, mic handed over cleanly.
- Always-on assistant (`python -m athena.main`): the full loop end to end.
- The orb UI, standalone (run_ui_test.py).

**Next (PLANNED):**
- Wire the orb into main.py (state changes at each pipeline stage).
- `memory.py` with Supabase — persistent memory across sessions (Member 2).
- ~~A custom "Athena" wake-word model to replace hey_jarvis~~ **DONE** —
  `models/hey_athena.onnx` is trained and in use (`config.WAKE_MODEL`).
- Record-until-silence instead of a fixed 5-second window.
- Package as a single .exe with PyInstaller.

## 9. How to run it

From the repo root, venv activated (or prefix with `.venv\Scripts\python.exe`):

| Command | What it tests |
|---|---|
| `python run_brain_test.py` | Brain by typing — no mic. `quit` to exit |
| `python tts_test.py` | Hear Emily speak one sentence |
| `python stt_test.py` | Records 5 s, prints what you said |
| `python talk_test.py` | Spoken conversation — Enter to talk, `q` to quit |
| `python run_ui_test.py` | The orb cycling its four states |
| `python -m athena.main` | The real thing: say "Hey Athena" |

## 10. Troubleshooting (problems we actually hit)

- **VS Code ran the system Python, not the venv** → imports "missing" even
  though installed. Fix: Ctrl+Shift+P → *Python: Select Interpreter* → pick
  `.venv\Scripts\python.exe`.
- **Skills were Week-1 stubs returning fake success** ("Opened Chrome." while
  opening nothing). Fixed by real implementations whose return strings state
  what actually happened; the brain speaks those verbatim.
- **TTS made an mp3 but no sound played.** Playback must wait on
  `pygame.mixer.music.get_busy()` and then `unload()` before deleting the
  temp file — Windows locks the file while pygame holds it.
- **Mic conflict between wake listener and recorder.** Two things can't hold
  the input device at once; wake.py closes its stream (a `with` block
  guarantees it) *before* returning, then audio_io.record opens the mic
  fresh.
- **pycaw's API changed** (release 20251023): the `GetSpeakers().Activate(...)`
  pattern from every tutorial now throws AttributeError. Current API:
  `AudioUtilities.GetSpeakers().EndpointVolume`.
- **openWakeWord defaulted to tflite, which Windows doesn't have.** Pass
  `inference_framework="onnx"` and install onnxruntime.

## 11. Decisions log

- **Brain in the cloud, not local.** The laptop has an AMD Radeon GPU — no
  CUDA, which most local-LLM tooling assumes — and 16 GB shared RAM. A 70B
  model is physically impossible locally; a 3–8B one that fits would be much
  worse at tool calling. Cloud inference gives big-model quality for free.
- **Why Groq.** Free tier with no credit card; genuinely fast; one key and
  one client library cover BOTH speech-to-text (Whisper) and a
  tool-calling-capable LLM (llama-3.3-70b-versatile).
- **Why openWakeWord over Porcupine.** Picovoice/Porcupine wanted an account
  with a company email and its free keys expire; openWakeWord needs no
  account, no key, nothing expires, and it ships a usable pretrained
  "hey_jarvis" model — with a documented path to training our own "Athena"
  word later. That path was taken: `hey_athena` is the model in use, and
  hey_jarvis remains selectable in the settings dashboard.
- **Why edge-tts and Emily.** Free and unlimited, dramatically more natural
  than Windows' built-in voices, and `en-IE-EmilyNeural` — Irish, female —
  is the closest match to FRIDAY, the vibe we want. Trade-off: needs
  internet; pyttsx3 is the offline fallback if that ever matters.

## 12. Glossary

- **Wake word** — the phrase that makes the assistant start listening
  ("Hey Athena"), detected locally so nothing is recorded until you say it.
- **STT / speech-to-text** — turning a voice recording into written words.
- **TTS / text-to-speech** — turning written words into a spoken voice.
- **LLM** — large language model; the AI that understands and generates text.
- **Tool calling** — letting the LLM choose one of our functions and fill in
  its arguments, instead of us hard-coding what each phrasing should do.
- **Inference** — running an AI model to get an answer (as opposed to
  training it).
- **API key** — a secret password that lets our code use a cloud service;
  ours lives only in `.env`.
- **.env file** — a local, never-committed text file holding secrets as
  `NAME=value` lines.
- **venv / virtual environment** — a private folder of Python + packages for
  this project only, so it can't clash with other projects.
- **ONNX** — a portable file format for neural networks; how our wake-word
  model runs on any CPU.
- **16 kHz mono** — audio with 16,000 samples per second on one channel; the
  standard diet of speech models.
- **CUDA** — NVIDIA-only GPU compute tech; our AMD laptop doesn't have it,
  which is why heavy AI runs in the cloud.
- **COM interface** — Windows' internal plumbing for controlling things like
  the master volume; pycaw is our Python doorway to it.
- **Confirmation gate** — the safety rule that risky actions need a spoken
  "yes" before they run.
