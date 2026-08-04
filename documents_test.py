"""Test document writing without the voice loop: create a Google Doc, write
to it, find it, read it back, then write the local Word fallback.

    python documents_test.py          # Google Docs round trip + local fallback
    python documents_test.py --local  # only the offline .docx fallback

Run google_auth_test.py first - this needs a working Google sign-in. The
Google part creates a real document in your Drive called "Athena test
document"; delete it afterwards if you like.

Browser tabs are suppressed during the round trip so you don't get a pile of
them, then the finished document is opened once at the end. The .docx
fallback always opens in Word - that's the behaviour being tested."""

import sys

from athena import documents, google_auth

TITLE = "Athena test document"
FIRST = "This is the first thing Athena wrote."
SECOND = "And this line was appended afterwards."
REPLACED = "Everything above was replaced by this single line."


def show(label: str, spoken: str) -> None:
    print(f"\n--- {label} ---")
    print(f"  she says: {spoken}")


if __name__ == "__main__":
    local_only = "--local" in sys.argv

    if not local_only:
        check = google_auth.check_connection()
        if not check["ok"]:
            print(f"Google isn't connected: {check['detail']}")
            print("Run  python google_auth_test.py  first, or use --local.")
            sys.exit(1)
        print(f"Signed in as {check['account']}")

        documents.OPEN_IN_BROWSER = False      # quiet during the round trip

        show("create_document", documents.create_document(TITLE))
        show("write_to_document (append, empty doc)",
             documents.write_to_document(TITLE, FIRST))
        show("write_to_document (append again)",
             documents.write_to_document(TITLE, SECOND))
        show("read_document", documents.read_document(TITLE))
        show("find_document", documents.find_document(TITLE))
        show("write_to_document (replace)",
             documents.write_to_document(TITLE, REPLACED, mode="replace"))
        show("read_document (after replace)", documents.read_document(TITLE))

        print("\n--- error handling ---")
        print(f"  missing doc: {documents.read_document('a document that does not exist')}")
        print(f"  empty text:  {documents.write_to_document(TITLE, '')}")

        documents.OPEN_IN_BROWSER = True       # open the finished doc once
        show("write_to_document (opens the browser)",
             documents.write_to_document(TITLE, "One last line, and this time it opens."))

    print("\n=== local .docx fallback (no internet needed) ===")
    show("write_local_docx", documents.write_local_docx(
        TITLE, "This file was written locally.\n\nIt needs no internet and "
        "no Google account."))

    print("\nDone. Check the document that opened, and the .docx in your "
          "Documents folder.")
