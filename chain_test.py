"""Test the multi-step brain without voice: run a chained typed command
through run_agent and print each step as it happens, plus the final summary.

The demo command mixes tiers on purpose - two free steps and one
confirm-tier step (set_volume) - so you can see safety hold across the
chain. The confirm callback here SKIPS the volume change (returns False),
so nothing on your system is altered; the rest of the chain still runs.

It also runs a CONTROL check first: a plain question must produce NO chain
(no tools) - a chain forms only when the request has clear instructions.

    python chain_test.py
"""

import sys

from athena import brain


def on_step(step: dict) -> None:
    tag = f"[{step['tier']}/{step['status']}]"
    print(f"  STEP {tag} {step['tool']}({step['args']})")
    print(f"         -> {step['result']}")


def confirm(question: str) -> bool:
    print(f"  CONFIRM ASKED: {question}")
    print(f"  (test auto-answers NO, so it's skipped)")
    return False


def control_no_chain() -> None:
    """A single question must NOT expand into a chain of actions."""
    question = "what can you do?"
    print(f"CONTROL - question that must NOT act: {question!r}")
    result = brain.run_agent(question, on_step=on_step, confirm=confirm)
    n = len(result["steps"])
    print(f"  steps: {n}  ({'PASS - answered, no action' if n == 0 else 'FAIL - it acted!'})")
    print(f"  reply: {result['reply_text']}\n")


if __name__ == "__main__":
    command = ("open notepad, then tell me the battery and system status, "
               "then set the volume to 80")
    if len(sys.argv) > 1:
        command = " ".join(sys.argv[1:])
    else:
        control_no_chain()   # only when using the default demo command

    print(f"Command: {command!r}\n")
    result = brain.run_agent(command, on_step=on_step, confirm=confirm)

    print(f"\nSteps run: {len(result['steps'])}")
    print(f"Final spoken summary:\n  {result['reply_text']}")

    # close the Notepad the test opened, if any
    try:
        import subprocess
        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"],
                       capture_output=True)
    except Exception:
        pass
