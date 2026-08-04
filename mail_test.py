"""Test email handling without the voice loop.

    python mail_test.py                    # read + draft + prove the send gate holds
    python mail_test.py --send-real        # additionally send ONE real email to yourself

By default this NEVER sends anything. It reads your inbox, saves drafts, and
then tries hard to make send_email fire when it shouldn't - with no confirm
hook wired in, and with a confirm hook that says no. Both must end up as
drafts. That is the point of the test.

--send-real sends one real email to your own address, and still asks you to
type yes at the terminal first.

Heads up: this leaves 3 or 4 test drafts in your Gmail drafts folder. Delete
them when you're done. Run google_auth_test.py first."""

import sys

from athena import google_auth, mail

SUBJECT = "Athena test message"
BODY = ("This is a test message written by Athena.\n\n"
        "If you are reading this in your drafts folder, the safety gate "
        "worked exactly as intended.")


def show(label: str, spoken: str) -> None:
    print(f"\n--- {label} ---")
    print(f"  she says: {spoken}")


def expect(label: str, spoken: str, must_not_contain: str) -> bool:
    """Assert a spoken result does NOT claim to have sent anything."""
    ok = must_not_contain.lower() not in spoken.lower()
    print(f"\n--- {label} ---")
    print(f"  she says: {spoken}")
    print(f"  [{'PASS' if ok else 'FAIL'}] nothing was sent")
    return ok


if __name__ == "__main__":
    check = google_auth.check_connection()
    if not check["ok"]:
        print(f"Google isn't connected: {check['detail']}")
        print("Run  python google_auth_test.py  first.")
        sys.exit(1)
    me = check["account"]
    print(f"Signed in as {me}")

    # ---------- reading ----------
    show("list_recent_emails(5)", mail.list_recent_emails(5))
    show("summarize_emails(5)", mail.summarize_emails(5))

    # ---------- drafting (never sends) ----------
    show("draft_email (to yourself)", mail.draft_email(me, SUBJECT, BODY))

    # ---------- bad input is refused before anything is written ----------
    show("draft_email with a name, not an address",
         mail.draft_email("Bob", SUBJECT, BODY))
    show("draft_email with an empty body", mail.draft_email(me, SUBJECT, "   "))

    # ---------- the send gate ----------
    print("\n=== send gate: these must all end as drafts ===")
    passed = True

    mail.set_confirm(None)
    passed &= expect("send_email with NO confirm hook wired",
                     mail.send_email(me, SUBJECT, BODY), "sent to")

    mail.set_confirm(lambda question: False)
    passed &= expect("send_email when you say no",
                     mail.send_email(me, SUBJECT, BODY), "sent to")

    asked = {}
    def spy(question):
        asked["question"] = question
        return False
    mail.set_confirm(spy)
    mail.send_email(me, SUBJECT, BODY)
    question = asked.get("question", "")
    names_both = me in question and SUBJECT in question
    print(f"\n--- the question you'd be asked ---")
    print(f"  {question}")
    print(f"  [{'PASS' if names_both else 'FAIL'}] names the recipient and the subject")
    passed &= names_both

    print(f"\n{'ALL GATE CHECKS PASSED' if passed else 'A GATE CHECK FAILED'}")

    # ---------- the only path that really sends ----------
    if "--send-real" in sys.argv:
        print(f"\nAbout to really send an email to {me}.")
        if input("Type yes to send: ").strip().lower() == "yes":
            mail.set_confirm(lambda question: True)
            show("send_email (for real)", mail.send_email(me, SUBJECT, BODY))
        else:
            print("Nothing sent.")
    else:
        print("\nNothing was sent. Pass --send-real to test a real send.")

    mail.set_confirm(None)
    if not passed:
        sys.exit(1)
