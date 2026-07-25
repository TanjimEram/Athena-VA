"""Confirm the Supabase memory connection works end to end: write one test
interaction, read it back with recent(), and print what came back.

Run from the repo root:  python memory_test.py

If SUPABASE_URL / SUPABASE_KEY aren't set in .env (or the project is
unreachable), you'll get a clear message instead of a crash - that's the
graceful-degradation path Athena relies on."""

import time

from athena import config, memory

if __name__ == "__main__":
    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        print("SUPABASE_URL / SUPABASE_KEY are not set in .env - memory is "
              "disabled. Add them and run again to test the connection.")
        raise SystemExit(0)

    stamp = time.strftime("%H:%M:%S")
    user_text = f"memory test at {stamp}"
    reply = "This is a test reply from memory_test.py."

    print("Writing a test interaction...")
    saved = memory.log_interaction(
        user_text, reply, tool_called="get_system_info", tool_args={"note": stamp}
    )
    if not saved:
        print("Write failed - check the console warning above, your keys, and "
              "that the 'interactions' table exists with insert permission.")
        raise SystemExit(1)
    print("  saved.")

    print("\nReading back the last 5 interactions (newest first):")
    rows = memory.recent(5)
    if not rows:
        print("  recent() returned nothing - check read permission on the table.")
        raise SystemExit(1)
    for row in rows:
        marker = "  -> " if row.get("user_text") == user_text else "     "
        print(f"{marker}[{row.get('created_at')}] "
              f"{row.get('user_text')!r} / {row.get('athena_reply')!r} "
              f"(tool: {row.get('tool_called')})")

    if rows[0].get("user_text") == user_text:
        print("\nConnection confirmed: the row we just wrote came back at the top.")
    else:
        print("\nWrote and read successfully, though the newest row wasn't ours "
              "(concurrent writes?). Connection still works.")
