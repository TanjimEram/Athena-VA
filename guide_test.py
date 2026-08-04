"""Test guided mode on something simple before the demo.

Open a File Explorer window with a file in it, then run this and follow along:
Athena reads your screen and speaks one step at a time. After each step, say
"next" (or just do the action and stay quiet), and say "done" or "stop" to
end. Manual "next" is the reliable default.

    python guide_test.py
"""

from athena import guide

if __name__ == "__main__":
    print("Starting guided mode. Listen, and say 'next' after each step.\n")
    result = guide.start_guide("rename a file in File Explorer")
    print("\nWrap-up:", result)
