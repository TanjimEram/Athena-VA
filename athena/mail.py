"""Reading and writing email through Gmail.

Reading is easy and safe. Writing is not, so the two are deliberately kept
apart: draft_email only ever creates a DRAFT sitting in Gmail waiting for
you, and send_email is the only function that can actually put a message on
the wire.

send_email will not fire on its own. It reads the recipient and the subject
back and waits for an explicit yes, through the confirm hook main.py wires
in with set_confirm(). If nothing is wired in, it refuses to send and saves
a draft instead - the safe direction to fail. That gate sits INSIDE this
module on purpose, so send_email is still safe if it is ever called from
somewhere that skipped safety.py.

Summaries are composed by the LLM (called directly here, not through
brain.py, so that skills -> mail -> brain never becomes an import loop).
Everything returns one short honest sentence, spoken verbatim."""

import base64
import re
from email.message import EmailMessage

from athena import config, google_auth

# Deliberately strict: if the brain hands over a name instead of an address,
# we say so rather than guessing at somebody's inbox.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")
# Bodies are trimmed before the summary call - keeps it fast and cheap.
MAX_BODY_CHARS = 1500
MAX_MATERIAL_CHARS = 6000

# main.py injects its yes/no asker (spoken + dashboard card). Until it does,
# send_email cannot send.
_confirm = None


def set_confirm(confirm_fn=None) -> None:
    """main.py wires in its confirm(question) -> bool here, the same one the
    safety gate uses, so a send can be approved by voice or by clicking."""
    global _confirm
    _confirm = confirm_fn


def _service():
    return google_auth.get_service("gmail", "v1")


def _header(message: dict, name: str) -> str:
    """One header out of a Gmail message payload, or ''."""
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _sender_name(from_header: str) -> str:
    """'Alice Smith <alice@x.com>' -> 'Alice Smith'; a bare address stays."""
    from_header = (from_header or "").strip()
    match = re.match(r'^\s*"?([^"<]*?)"?\s*<', from_header)
    if match and match.group(1).strip():
        return match.group(1).strip()
    # No display name - use the bare address, without the angle brackets a
    # header like "<alice@x.com>" would otherwise leave in the spoken line.
    return from_header.strip("<>").strip() or "someone"


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
            "utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """The plain-text body out of Gmail's nested MIME parts. Prefers
    text/plain; falls back to text/html with the tags stripped."""
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(_decode(data))
            elif mime == "text/html":
                html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    if plain:
        return "\n".join(plain).strip()
    if html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                      "\n".join(html), flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    return ""


def _list_messages(n: int) -> list:
    """Recent inbox message ids, newest first. [] if none/unavailable."""
    gmail = _service()
    if gmail is None:
        return []
    try:
        result = gmail.users().messages().list(
            userId="me", q="in:inbox", maxResults=max(1, min(int(n), 25))
        ).execute()
        return result.get("messages", []) or []
    except Exception as exc:
        print(f"[mail] couldn't list messages: {google_auth._short(exc)}")
        return []


def list_recent_emails(n: int = 10) -> str:
    """Who recently emailed you and about what - senders and subjects only,
    never the contents."""
    if _service() is None:
        return google_auth.not_connected_message()

    ids = _list_messages(n)
    if not ids:
        return "I couldn't see any recent email in your inbox."

    gmail = _service()
    lines = []
    for entry in ids:
        try:
            message = gmail.users().messages().get(
                userId="me", id=entry["id"], format="metadata",
                metadataHeaders=["From", "Subject"]).execute()
        except Exception as exc:
            print(f"[mail] couldn't read a message: {google_auth._short(exc)}")
            continue
        sender = _sender_name(_header(message, "From"))
        subject = (_header(message, "Subject") or "no subject").strip()
        if len(subject) > 80:
            subject = subject[:80].rstrip() + "..."
        lines.append(f"{sender}, about {subject}")

    if not lines:
        return "I found some email but couldn't read the details."
    count = len(lines)
    word = "email" if count == 1 else "emails"
    return f"Your {count} most recent {word}: " + "; ".join(lines) + "."


def summarize_emails(n: int = 10) -> str:
    """Read the recent emails and say what they're actually about."""
    if _service() is None:
        return google_auth.not_connected_message()

    ids = _list_messages(n)
    if not ids:
        return "I couldn't see any recent email in your inbox."

    gmail = _service()
    material, read = "", 0
    for entry in ids:
        try:
            message = gmail.users().messages().get(
                userId="me", id=entry["id"], format="full").execute()
        except Exception as exc:
            print(f"[mail] couldn't read a message: {google_auth._short(exc)}")
            continue
        sender = _sender_name(_header(message, "From"))
        subject = _header(message, "Subject") or "no subject"
        body = _extract_body(message.get("payload", {}))[:MAX_BODY_CHARS]
        material += f"\nFrom {sender} - {subject}\n{body}\n"
        read += 1
        if len(material) > MAX_MATERIAL_CHARS:
            break

    if not read:
        return "I found some email but couldn't read any of it."

    summary = _summarize(material[:MAX_MATERIAL_CHARS], read)
    if not summary:
        # Honest fallback: we read them, we just couldn't summarize.
        return (f"I read your {read} most recent emails but couldn't put a "
                "summary together just now.")
    return summary


def _summarize(material: str, count: int) -> str:
    """Have the LLM condense the emails into a few spoken sentences."""
    try:
        response = config.get_groq_client().chat.completions.create(
            model=config.BRAIN_MODEL,
            messages=[
                {"role": "system", "content": (
                    "You are Athena summarizing someone's recent email to be "
                    "read aloud. Give the gist in 3 or 4 short spoken "
                    "sentences: who wrote, what they want, and anything that "
                    "clearly needs a reply. Natural to say out loud - no "
                    "markdown, no lists, no email addresses read out. Be "
                    "accurate and never invent a message that isn't there.")},
                {"role": "user", "content": (
                    f"Here are the {count} most recent emails:\n{material}\n\n"
                    "Give the concise spoken summary now.")},
            ],
            temperature=0.4,
            max_tokens=260,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"[mail] summarize failed: {exc!r}")
        return ""


def _build_raw(to: str, subject: str, body: str) -> str:
    """A MIME message, base64url encoded the way Gmail wants it."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject or ""
    message.set_content(body or "")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def _check_recipient(to: str) -> str | None:
    """The spoken complaint about a bad recipient, or None if it's fine."""
    to = (to or "").strip()
    if not to:
        return "I need an email address before I can write that."
    if not _EMAIL_RE.match(to):
        return (f"{to} doesn't look like an email address, so I've not "
                "written anything. Give me the full address.")
    return None


def draft_email(to: str, subject: str, body: str) -> str:
    """Save a draft in Gmail. This NEVER sends - the draft sits in the Drafts
    folder until a human opens it and presses send."""
    gmail = _service()
    if gmail is None:
        return google_auth.not_connected_message()
    complaint = _check_recipient(to)
    if complaint:
        return complaint
    if not (body or "").strip():
        return "There was nothing to put in the email, so I didn't draft it."

    try:
        gmail.users().drafts().create(
            userId="me",
            body={"message": {"raw": _build_raw(to, subject, body)}}).execute()
    except Exception as exc:
        return f"I couldn't save that draft: {google_auth._short(exc)}"

    print(f"[mail] drafted to {to}: {subject!r}")
    about = f" about {subject}" if subject else ""
    return (f"I've saved a draft to {to}{about}. It's in your drafts - "
            "nothing has been sent.")


def send_email(to: str, subject: str, body: str) -> str:
    """Actually send an email. Reads the recipient and subject back and waits
    for an explicit yes first; without a confirm hook wired in it refuses and
    saves a draft instead."""
    gmail = _service()
    if gmail is None:
        return google_auth.not_connected_message()
    complaint = _check_recipient(to)
    if complaint:
        return complaint
    if not (body or "").strip():
        return "There was nothing to put in the email, so I didn't send it."

    about = f", about {subject}" if subject else ", with no subject"
    question = f"Send this email to {to}{about}?"

    if _confirm is None:
        # No way to ask, so we must not send. Fail towards the draft.
        print("[mail] no confirm hook wired - refusing to send, drafting instead")
        drafted = draft_email(to, subject, body)
        if drafted.startswith("I've saved a draft"):
            return (f"I can't confirm a send right now, so I've saved it as a "
                    f"draft to {to} instead. Nothing has been sent.")
        return drafted

    try:
        approved = bool(_confirm(question))
    except Exception as exc:
        print(f"[mail] confirm failed: {exc!r}")
        approved = False

    if not approved:
        drafted = draft_email(to, subject, body)
        if drafted.startswith("I've saved a draft"):
            return (f"I haven't sent it. I've saved it as a draft to {to} "
                    "in case you want it later.")
        return "I haven't sent it."

    try:
        gmail.users().messages().send(
            userId="me",
            body={"raw": _build_raw(to, subject, body)}).execute()
    except Exception as exc:
        return f"I couldn't send that email: {google_auth._short(exc)}"

    print(f"[mail] sent to {to}: {subject!r}")
    return f"Sent to {to}."
