# Athena — Demo Guide

Everything you need to run a smooth live demo, plus what to do if something
hiccups.

## Launch

```powershell
.\.venv\Scripts\python.exe -m athena.main
```

The orb appears at the right screen edge. Say **"hey Jarvis"**, wait for the
chirp, then speak. Click the orb to open the dashboard; press Esc (or the
minimise button) to collapse back. Close the window or say "go to sleep" to
quit.

## A demo script that flows well

1. **Simple chat** — *"hey Jarvis … what can you do?"*
   Fast spoken reply, proves the voice loop.
2. **Single action** — *"hey Jarvis … what's my battery level?"*
   Real system info, spoken.
3. **Open the dashboard** — click the orb. Show the tool log, transcript,
   status strip (groq/supabase/mic dots, CPU, battery, latency).
4. **Multi-step chain** — *"hey Jarvis … open notepad, check my battery, and
   search the web for lofi focus music."*
   Watch three steps stream into the tool log, then one natural spoken
   summary. This is the headline feature.
5. **Screen vision** — put an error on screen, then *"hey Jarvis … look at my
   screen and tell me what this error means."*
6. **Safety confirmation** — *"hey Jarvis … set the volume to 30."*
   Athena asks "Set volume to 30? Yes or no." Say **"yes"** (or click Approve
   on the dashboard card). Say "no" to show it skipping safely.
7. **Sleep** — *"hey Jarvis … that's all."* She signs off.

Keep a few seconds between requests (see rate limits below).

## What's been hardened

- **Multi-step agent** runs actions one at a time (most reliable), feeds
  results back for dependent steps, caps at 5 steps, dedupes repeats.
- **Never fakes success** — the spoken summary is grounded in what actually
  happened; a skipped/failed step is reported honestly.
- **Malformed tool-call recovery** — if the model garbles a tool call, the
  intended action is parsed from the error and run anyway.
- **Responsive confirmations** — a card click answers instantly (no waiting
  out the voice window).
- **Crash-proof** — any unexpected error in a turn is caught; Athena says
  "sorry, something went wrong, I'm still here" and keeps listening. The
  wake-word loop can't die.

## Realistic expectations (be honest with yourself)

- **Latency**: ~2–3 seconds to first spoken word is normal — it's a cloud
  round-trip (mic → Whisper → brain → voice). Streaming TTS starts the reply
  as soon as the first sentence is ready.
- **Internet required**: Groq (brain, speech-to-text) and edge-tts (voice)
  are online services. Test your connection before presenting.

## If something hiccups mid-demo

- **"I've hit my rate limit / per-minute limit"** — the free Groq tier caps
  both requests-per-minute (~30) and tokens-per-minute (12,000). A
  multi-step chain now uses only ~2 requests (independent actions are
  batched into one model call), so at a normal demo pace — one command,
  wait for the reply, talk to the audience, next command — you won't hit it.
  Athena now tells you roughly how long to wait ("give me about 20 seconds")
  and does NOT retry-hammer. If throttled, just wait that long and continue.
  **Best insurance: use a fresh Groq API key for the presentation and don't
  run test loops beforehand** (rapid-fire requests are what trip the limit).
- **She doesn't hear the wake word** — lower `WAKE_THRESHOLD` in
  `athena/config.py` (e.g. 0.4). Too many false wakes → raise it (0.6).
- **She doesn't catch your request** — speak right after the chirp; the mic
  auto-stops when you pause. Quiet room helps.
- **No voice** — edge-tts needs internet; check the connection.
- **Orb invisible** — set `TRANSPARENT = False` at the top of `athena/ui.py`
  for the opaque fallback.
- **Worst case** — you can always type into the dashboard transcript box
  instead of speaking; it runs the exact same pipeline.

## Pre-demo checklist

- [ ] Internet connected
- [ ] `.env` has a working `GROQ_API_KEY` (ideally a fresh one)
- [ ] Speakers on, mic enabled (Windows mic privacy allows access)
- [ ] Run once quietly to warm up, then leave a minute before presenting
- [ ] Don't run large test loops right before — save your token budget
