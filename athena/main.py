"""Athena's always-on loop: wait for the wake word, answer "Yes?", listen,
think, act, speak - then go back to waiting. Ctrl+C shuts her down cleanly.
Run from the repo root with:  python -m athena.main"""

import os

from athena import audio_io, brain, config, stt, tts, wake


def hear(seconds: float = 5) -> str:
    """Record once, transcribe, clean up the temp file, return the text."""
    wav_path = audio_io.record(seconds=seconds)
    if wav_path is None:
        return ""
    try:
        return stt.transcribe(wav_path)
    finally:
        os.remove(wav_path)


def is_yes(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in ("yes", "yeah", "yep", "sure", "go ahead", "do it"))


def say(text: str) -> None:
    print(f"Athena: {text}")
    tts.speak(text)


def main() -> None:
    config.check_config()
    print("Athena is online. Say 'hey Jarvis' to wake her. Ctrl+C to quit.")
    history: list[dict] = []

    while True:
        # 1) Sleep until the wake word. The wake listener releases the mic
        #    before returning, so record() below can open it right away.
        if not wake.listen_for_wake():
            print("Wake listener failed - fix the mic and restart.")
            return

        # 2) Acknowledge, then listen for the actual request.
        say("Yes?")
        print("Listening... (5 seconds)")
        user_text = hear()
        if not user_text:
            say("Sorry, I didn't catch that.")
            continue
        print(f"You said: {user_text}")

        # 3) Think, maybe act, and speak the honest result.
        result = brain.think(user_text, history)
        reply = result["reply_text"]
        say(reply)

        # 4) Risky actions: she asked a question, listen for yes or no.
        if result["needs_confirmation"]:
            print("Listening for yes or no... (5 seconds)")
            answer = hear()
            print(f"You said: {answer}")
            if is_yes(answer):
                reply = brain.run_confirmed(result["tool_called"], result["args"])
            else:
                reply = "Okay, cancelled."
            say(reply)

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAthena going offline. Goodbye.")
