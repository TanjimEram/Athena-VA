"""Spoken conversation test - the whole pipeline except the wake word.
Each turn: press Enter, talk (recording stops when you pause), hear Athena
answer.
If she asks to confirm an action (like locking the screen), just say yes
or no when she prompts you. Type q + Enter to quit."""

import os

from athena import audio_io, brain, stt, tts


def listen() -> str:
    """Listen until the speaker goes quiet, transcribe, clean up the temp
    file, return the text ("" if nothing was said)."""
    wav_path = audio_io.record_until_silence()
    if wav_path is None:
        return ""
    try:
        return stt.transcribe(wav_path)
    finally:
        os.remove(wav_path)


def is_yes(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in ("yes", "yeah", "yep", "sure", "go ahead", "do it"))


def main() -> None:
    print("Athena talk test. Speak when you see 'Listening...'.\n")
    history: list[dict] = []

    while True:
        command = input("Press Enter to talk (or type q then Enter to quit) ")
        if command.strip().lower() == "q":
            break

        print("Listening... (stops when you stop talking)")
        user_text = listen()
        if not user_text:
            print("Athena: Sorry, I didn't catch that.")
            tts.speak("Sorry, I didn't catch that.")
            continue
        print(f"You said: {user_text}")

        result = brain.think(user_text, history)
        reply = result["reply_text"]
        print(f"Athena: {reply}")
        tts.speak(reply)

        # Confirm-level actions: she asked a question, listen for the answer.
        if result["needs_confirmation"]:
            print("Listening for yes or no...")
            answer = listen()
            print(f"You said: {answer}")
            if is_yes(answer):
                reply = brain.run_confirmed(result["tool_called"], result["args"])
            else:
                reply = "Okay, cancelled."
            print(f"Athena: {reply}")
            tts.speak(reply)

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply})

    print("Goodbye.")


if __name__ == "__main__":
    main()
