"""People Athena can offer to contact for you.

Two ways in, and both are things you did on purpose: type them into settings,
or pull the contacts you STARRED in Google. Starred matters - it's a choice
you made about who those people are, not a guess from how often you message
someone. Athena never works out who you're close to on her own.

Nothing here sends anything by itself. When it matters, Athena names the
person and asks, and the message she sends is "can you check in on me" -
never what you actually said. Telling someone you're in trouble is yours to
decide, and it isn't undoable.

Contacts live in settings.json (gitignored, per-machine) because phone
numbers and addresses are personal. The crisis lines in config.py are public
and live in git."""

from athena import google_auth, settings

# The message sent on your behalf. Deliberately says nothing about what you
# told Athena - it opens a door, it doesn't hand over a transcript.
CHECK_IN_SUBJECT = "Can you check in on me?"
CHECK_IN_BODY = (
    "Hi {name},\n\n"
    "This message was sent by Athena, {owner}'s assistant, because they "
    "asked me to let you know they'd like to hear from you.\n\n"
    "Could you give them a call when you get a chance?\n"
)


def list_contacts() -> list:
    """The saved contacts. Always a list, even if settings holds junk."""
    saved = settings.get("trusted_contacts", [])
    if not isinstance(saved, list):
        return []
    return [c for c in saved if isinstance(c, dict) and c.get("name")]


def save_contacts(contacts: list) -> None:
    settings.set("trusted_contacts", contacts)


def add_contact(name: str, email: str = "", relationship: str = "") -> str:
    """Add or update one person by name."""
    name = (name or "").strip()
    if not name:
        return "I need a name to save."
    contacts = list_contacts()
    for existing in contacts:
        if existing["name"].strip().lower() == name.lower():
            existing["email"] = email or existing.get("email", "")
            existing["relationship"] = relationship or existing.get("relationship", "")
            save_contacts(contacts)
            return f"Updated {name} in your contacts."
    contacts.append({"name": name, "email": email.strip(),
                     "relationship": relationship.strip()})
    save_contacts(contacts)
    return f"Added {name} to your contacts."


def remove_contact(name: str) -> str:
    contacts = list_contacts()
    kept = [c for c in contacts if c["name"].strip().lower() != (name or "").strip().lower()]
    if len(kept) == len(contacts):
        return f"{name} isn't in your contacts."
    save_contacts(kept)
    return f"Removed {name} from your contacts."


def find(name: str) -> dict | None:
    """A saved contact by name, matching loosely - the name arrives spoken."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    contacts = list_contacts()
    for contact in contacts:
        if contact["name"].strip().lower() == wanted:
            return contact
    for contact in contacts:                 # first name is enough
        if wanted in contact["name"].strip().lower():
            return contact
    return None


def pull_from_google() -> str:
    """Load the contacts you STARRED in Google into the saved list.

    Starred only. The full contact list would be everyone you've ever emailed,
    which is not the same question as who you'd want called."""
    service = google_auth.get_service("people", "v1")
    if service is None:
        return google_auth.not_connected_message()

    try:
        response = service.people().connections().list(
            resourceName="people/me", pageSize=500,
            personFields="names,emailAddresses,memberships,relations").execute()
    except Exception as exc:
        detail = google_auth._short(exc)
        if "insufficient" in detail.lower() or "scope" in detail.lower():
            return ("I don't have permission to read your contacts yet. Run "
                    "the Google sign-in test again and approve the contacts "
                    "permission.")
        return f"I couldn't read your contacts: {detail}"

    starred = []
    for person in response.get("connections", []) or []:
        memberships = person.get("memberships", []) or []
        is_starred = any(
            m.get("contactGroupMembership", {}).get("contactGroupId") == "starred"
            for m in memberships)
        if not is_starred:
            continue
        names = person.get("names", []) or []
        emails = person.get("emailAddresses", []) or []
        relations = person.get("relations", []) or []
        name = names[0].get("displayName", "").strip() if names else ""
        if not name:
            continue
        starred.append({
            "name": name,
            "email": emails[0].get("value", "").strip() if emails else "",
            "relationship": relations[0].get("type", "").strip() if relations else "",
        })

    if not starred:
        return ("You haven't starred any contacts in Google, so there was "
                "nothing to bring in. Star the people you'd want me to be "
                "able to reach, then ask me again.")

    existing = {c["name"].strip().lower() for c in list_contacts()}
    contacts = list_contacts()
    added = [c for c in starred if c["name"].strip().lower() not in existing]
    contacts.extend(added)
    save_contacts(contacts)

    if not added:
        return f"Your {len(starred)} starred contacts were already saved."
    names = ", ".join(c["name"] for c in added[:5])
    more = f", and {len(added) - 5} more" if len(added) > 5 else ""
    return (f"I've brought in {len(added)} starred contact"
            f"{'s' if len(added) != 1 else ''}: {names}{more}. "
            "I'll always ask before messaging any of them.")


def describe_contacts() -> str:
    """Who Athena could offer to reach, said aloud."""
    contacts = list_contacts()
    if not contacts:
        return ("You haven't given me anyone to contact yet. You can add "
                "someone, or ask me to pull your starred Google contacts.")
    reachable = [c for c in contacts if c.get("email")]
    names = ", ".join(c["name"] for c in contacts[:6])
    if not reachable:
        return (f"I have {names}, but no email addresses, so I couldn't "
                "actually reach any of them.")
    return f"I could reach out to {names} if you wanted."


def reachable() -> list:
    """Contacts we could actually message."""
    return [c for c in list_contacts() if c.get("email")]


def reach_out(name: str, owner: str = "") -> str:
    """Ask someone to check in. Goes through mail.send_email, which reads the
    recipient back and refuses to send without an explicit yes - so this
    cannot fire on its own."""
    contact = find(name)
    if contact is None:
        return f"I don't have anyone called {name} saved."
    if not contact.get("email"):
        return (f"I have {contact['name']} saved, but no email address, so I "
                "can't reach them. You'd have to call them yourself.")

    from athena import mail
    owner = owner or "your friend"
    body = CHECK_IN_BODY.format(name=contact["name"], owner=owner)
    return mail.send_email(contact["email"], CHECK_IN_SUBJECT, body)
