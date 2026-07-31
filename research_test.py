"""Test Athena's live web research without voice: run a quick look_up and a
deeper research, and print both spoken answers.

    python research_test.py

Needs TAVILY_API_KEY in .env. Without it you'll get the graceful
"can't reach the web" message instead of a crash."""

from athena import research

if __name__ == "__main__":
    print("=== look_up: who is the current president of France ===")
    print(research.look_up("who is the current president of France"))

    print("\n=== research: latest developments in AI voice assistants ===")
    print(research.research("latest developments in AI voice assistants"))
