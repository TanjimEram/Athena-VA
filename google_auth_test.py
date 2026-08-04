"""Sign in to Google once, and prove it worked - run this before trying any
document or email feature.

    python google_auth_test.py           # sign in (opens your browser once)
    python google_auth_test.py --reset   # forget the sign-in and start over

The first run opens a browser consent screen. Pick the Google account you
want Athena to use and approve all four permissions. After that the sign-in
is cached in google_token.json (gitignored) and no browser opens again.

Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env - see .env.example
for how to create them in the Google Cloud Console.

Note: while your Cloud project is in Testing mode, Google expires the
sign-in after 7 days. If documents or email stop working, run this again."""

import sys

from athena import google_auth

SCOPE_NAMES = {
    "https://www.googleapis.com/auth/documents": "write Google Docs",
    "https://www.googleapis.com/auth/drive.file": "manage the docs it creates",
    "https://www.googleapis.com/auth/gmail.readonly": "read your email",
    "https://www.googleapis.com/auth/gmail.compose": "draft and send email",
}


def line(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


if __name__ == "__main__":
    if "--reset" in sys.argv:
        print(google_auth.sign_out())
        print("Run this again without --reset to sign in fresh.\n")
        sys.exit(0)

    print("=== Google sign-in check ===\n")

    if not google_auth.is_configured():
        line("credentials in .env", False,
             "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are missing")
        print("\nSee .env.example for how to create them, then run this again.")
        sys.exit(1)
    line("credentials in .env", True)

    print("\nSigning in (a browser window may open - approve all four "
          "permissions)...\n")
    result = google_auth.check_connection(interactive=True)

    line("signed in", bool(result["account"]), result["account"] or result["detail"])
    line("Google Drive reachable", result["drive"])
    line("Gmail reachable", result["gmail"])

    if result["scopes"]:
        print("\n  Permissions granted:")
        for scope in result["scopes"]:
            print(f"    - {SCOPE_NAMES.get(scope, scope)}")
        missing = [s for s in google_auth.SCOPES if s not in result["scopes"]]
        for scope in missing:
            print(f"    - MISSING: {SCOPE_NAMES.get(scope, scope)}")

    print()
    if result["ok"]:
        print("All good - Athena can reach Google Docs and Gmail.")
        print("The sign-in is cached, so this won't ask again.")
    else:
        print(f"Not working yet: {result['detail']}")
        print("If a permission is missing above, run with --reset and "
              "approve everything.")
        sys.exit(1)
