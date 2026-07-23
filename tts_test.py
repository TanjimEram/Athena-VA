"""Hearing test: Athena introduces herself in her Irish voice.
Run it, turn your speakers on, and listen."""

from athena import tts

if __name__ == "__main__":
    print("Synthesizing... you should hear Athena in a moment.")
    tts.speak("Hello, I am Athena. Your assistant is online.")
    print("Done.")
