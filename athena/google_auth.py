"""Signing in to Google, once, so the document and email skills can work.

This module owns the whole OAuth dance and nothing else. It caches the
sign-in to a gitignored token file, refreshes it silently when it expires,
and hands out ready-made API clients (`get_service("docs", "v1")`). Every
other module just asks for a client and gets one, or gets None.

The consent screen only ever opens when someone passes interactive=True -
that is `google_auth_test.py`, never a voice turn. A browser prompt in the
middle of a spoken request would block the assistant thread with nothing on
screen to explain why, so skills use the cached token or fail politely.

Nothing here raises into the assistant loop: on any problem it prints a
[google_auth] line and returns None or an honest error string."""

import json
import os

from athena import config

# What Athena is allowed to do with the account. Docs to write documents,
# drive.file so she only ever sees files SHE created (not the whole Drive),
# gmail.readonly to read the inbox, gmail.compose to draft and send.
# Changing this list invalidates an existing token on purpose - see
# _scopes_match below.
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    # Full spreadsheets access - unlike drive.file this reaches any of the
    # user's sheets, which is what lets us open one by URL. There is no
    # "list spreadsheets" call in the Sheets API though, so finding one by
    # NAME still goes through Drive and so still only sees our own files.
    "https://www.googleapis.com/auth/spreadsheets",
]

_creds = None
_services = {}
_failed = False


def is_configured() -> bool:
    """True if the OAuth client credentials are present in .env. Without them
    there is nothing to sign in WITH, let alone a token."""
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def _client_config() -> dict:
    """The client config google-auth-oauthlib normally reads from a
    client_secret.json. We build it in memory from .env instead, so no secret
    is ever written to a file that could be committed."""
    return {
        "installed": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def granted_scopes() -> list:
    """What the saved sign-in ACTUALLY covers, straight from the token file.

    Read it from the file, never from a Credentials object: passing SCOPES to
    from_authorized_user_file overwrites creds.scopes with what we ASKED for,
    so the object always claims to have everything and can never reveal a
    mismatch."""
    path = config.GOOGLE_TOKEN_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return list(json.load(fh).get("scopes") or [])
    except Exception as exc:
        print(f"[google_auth] couldn't read the saved sign-in: {exc}")
        return []


def missing_scopes() -> list:
    """Scopes in SCOPES that the saved sign-in doesn't cover."""
    granted = set(granted_scopes())
    return [scope for scope in SCOPES if scope not in granted]


def _load_cached():
    """Read the token file, or None if it's missing/unreadable/stale."""
    path = config.GOOGLE_TOKEN_FILE
    if not os.path.exists(path):
        return None
    # A token saved before a new scope was added still LOOKS valid but fails
    # deep inside an API call with a confusing error - catch it here instead.
    if missing_scopes():
        print("[google_auth] saved sign-in is missing newer permissions - "
              "run google_auth_test.py --reset to sign in again")
        return None
    try:
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_file(path, SCOPES)
    except Exception as exc:
        print(f"[google_auth] couldn't read the saved sign-in: {exc}")
        return None


def _save(creds) -> None:
    try:
        with open(config.GOOGLE_TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
    except OSError as exc:
        print(f"[google_auth] couldn't save the sign-in: {exc}")


def get_credentials(interactive: bool = False):
    """Valid Google credentials, or None.

    Order: the cached token -> a silent refresh if it expired -> (only when
    interactive) the browser consent screen. interactive stays False for
    everything in the assistant loop; the consent flow BLOCKS on a local
    web server and must never run inside a voice turn."""
    global _creds, _failed
    if _creds is not None and _creds.valid:
        return _creds
    if _failed and not interactive:
        return None
    if not is_configured():
        _failed = True
        print("[google_auth] GOOGLE_CLIENT_ID/SECRET not set in .env - "
              "documents and email are disabled")
        return None

    creds = _load_cached()

    if creds and creds.expired and creds.refresh_token:
        try:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            _save(creds)
        except Exception as exc:
            # Most often: the 7-day Testing-mode expiry, or access revoked.
            print(f"[google_auth] the saved sign-in expired ({exc}) - "
                  "run google_auth_test.py to sign in again")
            creds = None

    if (creds is None or not creds.valid) and interactive:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
            # port=0 lets the OS pick a free port for the one-shot callback.
            creds = flow.run_local_server(port=0, prompt="consent")
            _save(creds)
        except Exception as exc:
            print(f"[google_auth] sign-in failed: {exc}")
            creds = None

    if creds is None or not creds.valid:
        if not interactive:
            _failed = True
        return None

    _creds = creds
    return _creds


def get_service(api: str, version: str):
    """A Google API client, e.g. get_service("docs", "v1") or
    get_service("gmail", "v1"). Built once per api/version and reused -
    building one per call costs a round trip every time. None if not
    signed in."""
    key = (api, version)
    if key in _services:
        return _services[key]
    creds = get_credentials()
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build
        service = build(api, version, credentials=creds, cache_discovery=False)
    except Exception as exc:
        print(f"[google_auth] couldn't build the {api} client: {exc}")
        return None
    _services[key] = service
    return service


def not_connected_message() -> str:
    """The one spoken sentence every Google-backed skill says when there is
    no sign-in. Kept here so all of them word it the same way."""
    if not is_configured():
        return ("I'm not set up for Google yet - the account details are "
                "missing from my configuration.")
    return ("I'm not signed in to your Google account yet. Run the Google "
            "sign-in test once and I'll be able to help with that.")


def check_connection(interactive: bool = False) -> dict:
    """Verify the sign-in actually works, end to end. Signs in if needed
    (when interactive), then really calls Drive and Gmail - a token can look
    valid while an API is switched off in the Cloud Console, and this is
    what catches that.

    Returns {"ok", "detail", "account", "scopes", "drive", "gmail"}."""
    result = {"ok": False, "detail": "", "account": "", "scopes": [],
              "drive": False, "gmail": False}

    if not is_configured():
        result["detail"] = ("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not in "
                            ".env - see .env.example for how to get them")
        return result

    creds = get_credentials(interactive=interactive)
    if creds is None:
        missing = missing_scopes()
        if missing and granted_scopes():
            result["detail"] = (f"the saved sign-in is missing {len(missing)} "
                                "newer permission(s) - sign in again with "
                                "python google_auth_test.py --reset")
        else:
            result["detail"] = ("not signed in - run this with interactive=True "
                                "(python google_auth_test.py) to sign in")
        return result
    # Report what was really granted, not what we asked for.
    result["scopes"] = granted_scopes()

    try:
        drive = get_service("drive", "v3")
        about = drive.about().get(fields="user(emailAddress)").execute()
        result["account"] = about.get("user", {}).get("emailAddress", "")
        result["drive"] = True
    except Exception as exc:
        result["detail"] = f"Drive check failed: {_short(exc)}"
        return result

    try:
        gmail = get_service("gmail", "v1")
        profile = gmail.users().getProfile(userId="me").execute()
        result["account"] = result["account"] or profile.get("emailAddress", "")
        result["gmail"] = True
    except Exception as exc:
        result["detail"] = f"Gmail check failed: {_short(exc)}"
        return result

    result["ok"] = True
    result["detail"] = f"signed in as {result['account']}"
    return result


def sign_out() -> str:
    """Forget the cached sign-in. The next interactive run asks for consent
    again - use this after changing SCOPES or to switch accounts."""
    global _creds, _services, _failed
    _creds = None
    _services = {}
    _failed = False
    path = config.GOOGLE_TOKEN_FILE
    if not os.path.exists(path):
        return "There was no saved Google sign-in to remove."
    try:
        os.remove(path)
        return "Removed the saved Google sign-in."
    except OSError as exc:
        return f"Couldn't remove the saved sign-in: {exc}"


def _short(exc: Exception) -> str:
    """Google's errors are long HTML/JSON blobs; keep the useful bit."""
    text = str(exc)
    try:                                  # HttpError carries a JSON body
        body = getattr(exc, "content", None)
        if body:
            msg = json.loads(body).get("error", {}).get("message")
            if msg:
                text = msg
    except Exception:
        pass
    return text[:160]
