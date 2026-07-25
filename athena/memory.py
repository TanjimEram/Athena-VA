"""Athena's long-term memory, stored in Supabase (free cloud Postgres).
Every exchange is logged to the `interactions` table; recent() and search()
pull past conversations back so "what did I ask you earlier" works across
sessions. Everything degrades gracefully: no credentials or no internet
means no memory, never a crash - Athena just lives in the moment.

Table schema - run this once in the Supabase SQL editor:

    create table if not exists interactions (
      id bigint generated always as identity primary key,
      created_at timestamptz not null default now(),
      user_text text not null,
      athena_reply text,
      tool_called text,
      tool_args jsonb
    );

    alter table interactions enable row level security;

    create policy "athena can read" on interactions
      for select to anon using (true);
    create policy "athena can write" on interactions
      for insert to anon with check (true);
"""

import threading

from athena import config

_client = None
_client_failed = False
_warned = False


def _warn_once(reason: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"[memory] Running without long-term memory: {reason}")


def _get_client():
    """The one shared Supabase client (created once, stored at module level,
    never per call), or None if memory isn't configured/reachable."""
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        _client_failed = True
        _warn_once("SUPABASE_URL / SUPABASE_KEY not set in .env")
        return None
    try:
        from supabase import create_client
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        return _client
    except Exception as exc:
        _client_failed = True
        _warn_once(f"couldn't connect ({exc})")
        return None


def log_interaction(user_text: str, reply: str,
                    tool_called: str | None = None,
                    tool_args: dict | None = None) -> bool:
    """Write one exchange to the interactions table. Returns True if saved,
    False (with a one-time console warning) if memory is unreachable."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.table("interactions").insert({
            "user_text": user_text,
            "athena_reply": reply,
            "tool_called": tool_called,
            "tool_args": tool_args or None,
        }).execute()
        return True
    except Exception as exc:
        _warn_once(f"couldn't save ({exc})")
        return False


def log_interaction_async(user_text: str, reply: str,
                          tool_called: str | None = None,
                          tool_args: dict | None = None) -> None:
    """Fire-and-forget logging on a background thread, so writing to the
    cloud never adds latency to Athena's reply."""
    threading.Thread(
        target=log_interaction,
        args=(user_text, reply, tool_called, tool_args),
        daemon=True,
    ).start()


def recent(n: int = 5) -> list[dict]:
    """The last n interactions, newest first. [] if memory is unavailable."""
    client = _get_client()
    if client is None:
        return []
    try:
        result = (
            client.table("interactions")
            .select("created_at, user_text, athena_reply, tool_called")
            .order("created_at", desc=True)
            .limit(n)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        _warn_once(f"couldn't read ({exc})")
        return []


def search(query: str, n: int = 3) -> list[dict]:
    """Simple text match on what the user said, newest first. [] if none."""
    client = _get_client()
    if client is None:
        return []
    try:
        result = (
            client.table("interactions")
            .select("created_at, user_text, athena_reply, tool_called")
            .ilike("user_text", f"%{query}%")
            .order("created_at", desc=True)
            .limit(n)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        _warn_once(f"couldn't search ({exc})")
        return []
