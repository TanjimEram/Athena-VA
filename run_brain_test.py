"""Keyboard test drive for the brain — no microphone needed. Type a line,
see which tool the brain picked, its arguments, and what Athena would say.
If she asks for confirmation, answer 'yes' to run the action. Type 'quit'
to exit."""

from athena import brain


def main() -> None:
    print("Athena brain test. Type a request, or 'quit' to exit.\n")
    history: list[dict] = []
    pending: dict | None = None  # a confirm-level tool waiting for a yes

    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_text:
            continue
        if user_text.lower() in ("quit", "exit"):
            break

        # If Athena just asked "are you sure?", treat yes/no as the answer.
        if pending:
            if user_text.lower() in ("yes", "y", "yes please", "do it", "sure"):
                reply = brain.run_confirmed(pending["tool_called"], pending["args"])
                pending = None
                print(f"athena> {reply}\n")
                history.append({"role": "assistant", "content": reply})
                continue
            print("athena> Okay, cancelled.\n")
            pending = None
            # fall through: treat what they typed as a fresh request

        result = brain.think(user_text, history)

        print(f"  tool: {result['tool_called']}")
        print(f"  args: {result['args']}")
        print(f"  needs_confirmation: {result['needs_confirmation']}")
        print(f"athena> {result['reply_text']}\n")

        if result["needs_confirmation"]:
            pending = result

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": result["reply_text"]})

    print("Goodbye.")


if __name__ == "__main__":
    main()
