"""What actually happened to a calendar event, stored in Supabase.

Google Calendar has no "done" flag. An event whose time has passed looks
exactly like one that never happened, so if Athena is to ask "did you
actually go to the dentist?" she has to remember having asked, and what the
answer was. That is the whole job of this module: one row per event, so a
question is asked once and never again.

Four answers are recorded. `done` and `skipped` are the user telling us;
`rescheduled` is written when an event is moved out of the past; `deferred`
is what happens when they change the subject - recorded so we drop it and
stop chasing, which is the polite outcome, not a failure.

**One client, not two.** The Supabase client comes from
`memory.shared_client()`. That singleton is deliberate and documented in
docs/design-patterns.md; a second one would mean a second TLS handshake per
session and a second copy of the "is this configured?" question.

**It degrades exactly as memory.py does.** No credentials, no network, no
table: one warning, then empty results forever after. Athena simply does not
follow up. She must never break because this store is unreachable - a
missing memory is a missing feature, not an error.

Not gated by the `memory_enabled` setting on purpose: switching off
conversation logging shouldn't make Athena start re-asking about events she
already asked about.

Table schema - run this once in the Supabase SQL editor, alongside the
`interactions` table in memory.py:

    create table if not exists event_status (
      id bigint generated always as identity primary key,
      event_id text not null unique,
      status text not null,
      created_at timestamptz not null default now()
    );

    alter table event_status enable row level security;

    create policy "athena can read" on event_status
      for select to anon using (true);
    create policy "athena can write" on event_status
      for insert to anon with check (true);
    -- update: an answer can change (deferred today, done tomorrow), and the
    -- upsert below needs it. delete: prune() clears rows over 30 days old.
    create policy "athena can change" on event_status
      for update to anon using (true);
    create policy "athena can prune" on event_status
      for delete to anon using (true);
"""

import datetime
import threading

from athena import memory

# The only four answers there are. Anything else is a bug in the caller and
# is refused rather than written, so the table can't fill with junk.
STATUSES = ("done", "skipped", "rescheduled", "deferred")

# Rows older than this are dropped. A month is long past the point where
# "did you do that?" is a useful question, and the table stays small.
PRUNE_AFTER_DAYS = 30

_warned = False
_pruned = False


def _warn_once(reason: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"[event_status] Running without follow-ups: {reason}")


def _client():
    """The shared Supabase client, or None. Prunes old rows on the first
    successful call of a session - lazily and on a background thread, so it
    costs nothing and never delays an answer."""
    global _pruned
    client = memory.shared_client()
    if client is None:
        _warn_once("Supabase isn't configured or wasn't reachable")
        return None
    if not _pruned:
        _pruned = True
        threading.Thread(target=prune, daemon=True).start()
    return client


def record(event_id: str, status: str) -> bool:
    """Remember what happened to one event. Returns True if it was saved.

    An upsert, not an insert: an event answered "deferred" this morning and
    "done" this evening should end with one row saying done, not two rows
    disagreeing."""
    if not event_id:
        return False
    if status not in STATUSES:
        print(f"[event_status] refusing to record unknown status {status!r}")
        return False
    client = _client()
    if client is None:
        return False
    try:
        client.table("event_status").upsert(
            {"event_id": event_id,
             "status": status,
             "created_at": datetime.datetime.now(
                 datetime.timezone.utc).isoformat()},
            on_conflict="event_id",
        ).execute()
        return True
    except Exception as exc:
        _warn_once(f"couldn't save ({exc})")
        return False


def record_async(event_id: str, status: str) -> None:
    """Fire-and-forget on a background thread, so writing to the cloud never
    adds latency to what Athena is saying. Same pattern as
    memory.log_interaction_async, and for the same reason.

    Starting a thread can itself fail when a machine is out of them, and this
    is called from the middle of a spoken exchange - so even that is caught.
    Losing a record means one question gets asked twice; raising here would
    stop the conversation dead."""
    try:
        threading.Thread(target=record, args=(event_id, status),
                         daemon=True).start()
    except Exception as exc:
        _warn_once(f"couldn't start the background write ({exc})")


def recorded_ids(event_ids: list) -> set:
    """Which of these events have already been answered for.

    This is the question phase 4 actually asks - "what have I NOT asked about
    yet" - so it is one query against the ids in hand rather than a full table
    read. An empty set when the store is unavailable, which means every event
    looks unanswered; that is the right way round, because the alternative is
    silently swallowing follow-ups."""
    ids = [event_id for event_id in (event_ids or []) if event_id]
    if not ids:
        return set()
    client = _client()
    if client is None:
        return set()
    try:
        result = (client.table("event_status")
                  .select("event_id")
                  .in_("event_id", ids)
                  .execute())
    except Exception as exc:
        _warn_once(f"couldn't read ({exc})")
        return set()
    return {row.get("event_id") for row in (result.data or [])}


def status_of(event_id: str) -> str:
    """What was recorded for one event, or "" if nothing was."""
    if not event_id:
        return ""
    client = _client()
    if client is None:
        return ""
    try:
        result = (client.table("event_status")
                  .select("status")
                  .eq("event_id", event_id)
                  .limit(1)
                  .execute())
    except Exception as exc:
        _warn_once(f"couldn't read ({exc})")
        return ""
    rows = result.data or []
    return rows[0].get("status", "") if rows else ""


def recent(n: int = 10) -> list:
    """The last n answers, newest first. [] if the store is unavailable."""
    client = _client()
    if client is None:
        return []
    try:
        result = (client.table("event_status")
                  .select("event_id, status, created_at")
                  .order("created_at", desc=True)
                  .limit(n)
                  .execute())
        return result.data or []
    except Exception as exc:
        _warn_once(f"couldn't read ({exc})")
        return []


def forget(event_id: str) -> bool:
    """Drop one row. Only used by the test script, to clean up after itself."""
    client = memory.shared_client()
    if client is None or not event_id:
        return False
    try:
        client.table("event_status").delete().eq("event_id", event_id).execute()
        return True
    except Exception as exc:
        _warn_once(f"couldn't delete ({exc})")
        return False


def prune(days: int = PRUNE_AFTER_DAYS) -> int:
    """Delete rows older than `days`. Returns how many went, or 0.

    Called on a background thread the first time the store is used in a
    session - never on a timer, and never anywhere it could delay speech.
    Uses memory.shared_client directly rather than _client() so it cannot
    recurse into the prune it was started by."""
    client = memory.shared_client()
    if client is None:
        return 0
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days)).isoformat()
    try:
        result = (client.table("event_status")
                  .delete()
                  .lt("created_at", cutoff)
                  .execute())
        gone = len(result.data or [])
        if gone:
            print(f"[event_status] pruned {gone} row(s) older than {days} days")
        return gone
    except Exception as exc:
        _warn_once(f"couldn't prune ({exc})")
        return 0
