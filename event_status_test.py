"""Confirm the completion store works, and that Athena survives it not working.

    python event_status_test.py            # degradation checks, then the real table
    python event_status_test.py --offline  # degradation checks only

Two halves. The offline half proves the important property: with Supabase
unconfigured or unreachable, every function returns empty after ONE warning
and nothing raises. Athena just doesn't follow up. That path matters more
than the happy one, because it is the one that runs on a bad hotel wifi.

The live half writes one row for a fake event id, reads it back, overwrites
it, prunes, and deletes itself again. It leaves nothing behind.

If the live half says the table is missing, the SQL to create it is printed -
it also lives in the docstring of athena/event_status.py.
"""

import datetime
import sys
import time

from athena import config, event_status, memory

PASSED = 0
FAILED = 0


def check(label: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")


class Unavailable:
    """Supabase missing or unreachable, without touching anyone's .env."""

    def __init__(self, raises=False):
        self.raises = raises

    def __enter__(self):
        self._real = memory.shared_client
        self._warned = event_status._warned
        self._pruned = event_status._pruned
        event_status._warned = False
        event_status._pruned = True          # don't start a prune thread
        if self.raises:
            memory.shared_client = lambda: Exploding()
        else:
            memory.shared_client = lambda: None
        return self

    def __exit__(self, *exc):
        memory.shared_client = self._real
        event_status._warned = self._warned
        event_status._pruned = self._pruned


class Exploding:
    """A client that connects and then fails on every call - the "table isn't
    there" and "connection dropped mid-query" case, which is different from
    having no client at all."""

    def table(self, name):
        return self

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise RuntimeError("relation \"event_status\" does not exist")
        return boom


def degradation_checks() -> None:
    print("\n--- Supabase not configured: empty, never an exception ---")
    with Unavailable():
        check("record returns False rather than pretending",
              event_status.record("evt-1", "done"), False)
        check("recorded_ids is empty, so nothing is silently swallowed",
              event_status.recorded_ids(["evt-1", "evt-2"]), set())
        check("status_of is empty", event_status.status_of("evt-1"), "")
        check("recent is empty", event_status.recent(), [])
        check("prune does nothing", event_status.prune(), 0)
        check("and it warned exactly once", event_status._warned, True)

    print("\n--- Supabase reachable but the query fails ---")
    with Unavailable(raises=True):
        check("a failed write is reported as a failure",
              event_status.record("evt-1", "done"), False)
        check("a failed read is empty, not an exception",
              event_status.recorded_ids(["evt-1"]), set())
        check("status_of survives it", event_status.status_of("evt-1"), "")
        check("recent survives it", event_status.recent(), [])
        check("prune survives it", event_status.prune(), 0)

    print("\n--- the store refuses to hold nonsense ---")
    check("an unknown status is refused before it reaches the table",
          event_status.record("evt-1", "maybe"), False)
    check("...and so is an empty event id",
          event_status.record("", "done"), False)
    check("the four statuses are the four the follow-up uses",
          set(event_status.STATUSES),
          {"done", "skipped", "rescheduled", "deferred"})
    # Inside Unavailable so the fire-and-forget thread can't leave a stray
    # row in the real table just because this half was run.
    with Unavailable():
        check("record_async returns immediately and never raises",
              event_status.record_async("evt-async-check", "deferred"), None)

    print("\n--- one client, not two ---")
    check("event_status has no client of its own",
          hasattr(event_status, "_get_client") or
          hasattr(event_status, "create_client"), False)
    check("it goes through the shared one",
          event_status._client.__doc__ is not None
          and "shared" in event_status._client.__doc__, True)


SCHEMA_HINT = """
  The event_status table doesn't exist yet. In the Supabase SQL editor, run:

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
    create policy "athena can change" on event_status
      for update to anon using (true);
    create policy "athena can prune" on event_status
      for delete to anon using (true);

  (This SQL also lives in the docstring of athena/event_status.py.)
"""


def _unreachable_reason() -> str:
    """Why the live half can't run, or "" if it can.

    Worth doing properly: a failed write has several possible causes and
    "your table is missing" is the wrong thing to print when the truth is
    that the project isn't there any more."""
    import socket
    import urllib.parse

    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return "SUPABASE_URL / SUPABASE_KEY are not set in .env."
    host = urllib.parse.urlparse(config.SUPABASE_URL).hostname or ""
    try:
        socket.gethostbyname(host)
    except OSError:
        try:
            socket.gethostbyname("supabase.com")
        except OSError:
            return "no network - supabase.com doesn't resolve either."
        return (f"{host} doesn't resolve, though supabase.com does. That "
                "project looks like it no longer exists - a free project "
                "that sat idle long enough gets paused and then removed. "
                "Check the Supabase dashboard; a new project means a new "
                "URL and key in .env, and BOTH tables created again.")
    if memory.shared_client() is None:
        return "couldn't build a Supabase client (see the warning above)."
    return ""


def live_checks() -> None:
    print("\n=== the real table ===\n")
    reason = _unreachable_reason()
    if reason:
        print(f"  Skipped: {reason}")
        print("\n  This is exactly the path the degradation checks above "
              "cover:\n  Athena keeps working, she just doesn't follow up.")
        return

    marker = f"athena-test-{int(time.time())}"
    other = f"{marker}-second"
    try:
        saved = event_status.record(marker, "done")
        if not saved:
            print(SCHEMA_HINT)
            check("wrote one row", saved, True)
            return
        check("wrote one row", saved, True)
        check("and it reads back", event_status.status_of(marker), "done")

        event_status.record(marker, "rescheduled")
        check("recording again overwrites rather than duplicating",
              event_status.status_of(marker), "rescheduled")
        rows = [r for r in event_status.recent(50)
                if r.get("event_id") == marker]
        check("...so there is exactly one row for the event", len(rows), 1)

        event_status.record(other, "skipped")
        found = event_status.recorded_ids([marker, other, "never-recorded"])
        check("recorded_ids finds the answered ones", found, {marker, other})
        check("and does not invent the unanswered one",
              "never-recorded" in found, False)

        check("an unknown id has no status",
              event_status.status_of("never-recorded"), "")

        print("\n  Pruning is by age, so nothing written just now should go:")
        before = len(event_status.recorded_ids([marker, other]))
        event_status.prune()
        check("today's rows survive a prune",
              len(event_status.recorded_ids([marker, other])), before)
        check("a prune with a zero-day cutoff would take them",
              event_status.prune(days=0) >= 2, True)
    finally:
        event_status.forget(marker)
        event_status.forget(other)
        left = event_status.recorded_ids([marker, other])
        check("the test cleaned up after itself", left, set())


if __name__ == "__main__":
    print("=== Athena calendar, phase 3: the completion store ===")
    degradation_checks()

    if "--offline" not in sys.argv:
        live_checks()
    else:
        print("\n(Skipping the real table - run without --offline for that.)")

    print(f"\n{PASSED} passed, {FAILED} failed.")
    sys.exit(1 if FAILED else 0)
