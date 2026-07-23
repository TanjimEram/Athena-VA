# Athena

A voice assistant for Windows 11, built in Python. Say **"hey Jarvis"**, ask
in plain words, and Athena answers in an Irish voice — or actually does it:
opens apps, searches the web, sets the volume, locks the screen. The brain is
a cloud LLM (Groq, free tier) using tool calling; risky actions need a spoken
confirmation; failures are reported honestly, never faked.

**Pipeline:** wake word (openWakeWord, local) → speech-to-text (Groq Whisper)
→ brain (llama-3.3-70b + tool calling) → safety gate → real Windows skills →
voice (edge-tts, en-IE-EmilyNeural).

## Status

Working end to end today: the always-on spoken loop (`python -m athena.main`),
all individual test scripts, real skills, wake word, and the orb UI
(standalone). Planned next: orb wired into the main loop, persistent memory
(Supabase), a custom "Athena" wake word, PyInstaller packaging.

## Quickstart

```
git clone <repo-url> athena-va
cd athena-va
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # then paste your free Groq key into .env
python run_brain_test.py  # typed test, no mic needed
python -m athena.main     # the real thing: say "hey Jarvis"
```

Full details — how every module works, setup from zero, troubleshooting, and
why each tool was chosen: **[docs/Athena_Build_Documentation.md](docs/Athena_Build_Documentation.md)**.
