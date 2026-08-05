"""Check the sheets provider routing: which model does the spreadsheet
thinking, and that a broken Gemini setup falls back to Groq instead of
breaking the feature.

    python sheets_provider_test.py                        # routing only, no sheet needed
    python sheets_provider_test.py <spreadsheet-url-or-id> # also generate a real body

Nothing here writes to a spreadsheet - it only asks the model what it WOULD
do. Only the escape hatch (apply_sheet_operation) uses a model at all;
everything else in sheets.py is plain API calls with no model involved.

To try Gemini: put a free key from https://aistudio.google.com in .env as
GEMINI_API_KEY, and set SHEETS_PROVIDER=gemini."""

import sys
import time

from athena import config, settings, sheets

results = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    print("=== how it's configured ===")
    print(f"  config.SHEETS_PROVIDER : {config.SHEETS_PROVIDER}")
    print(f"  settings override      : {settings.get('sheets_provider', '(unset)')}")
    print(f"  in effect              : {sheets._sheets_provider()}")
    print(f"  GROQ_API_KEY set       : {bool(config.GROQ_API_KEY)}")
    print(f"  GEMINI_API_KEY set     : {bool(config.GEMINI_API_KEY)}")
    print(f"  Gemini model           : {config.GEMINI_MODEL}")

    original = settings.get("sheets_provider", config.SHEETS_PROVIDER)

    print("\n=== the setting is read at call time (no restart) ===")
    settings.set("sheets_provider", "gemini")
    check("switches to gemini", sheets._sheets_provider() == "gemini",
          sheets._sheets_provider())
    settings.set("sheets_provider", "groq")
    check("switches back to groq", sheets._sheets_provider() == "groq",
          sheets._sheets_provider())

    print("\n=== Gemini with no key reports honestly, doesn't crash ===")
    saved_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = None
    text, error = sheets._ask_gemini("You are terse.", "Say OK.", 20)
    print(f"  -> text={text!r} error={error!r}")
    check("refuses without a key", error and "GEMINI_API_KEY" in error, error)
    check("returns no text", text == "", text)

    print("\n=== ...and the router falls back to Groq ===")
    settings.set("sheets_provider", "gemini")
    started = time.monotonic()
    text, error = sheets._ask_model("Reply with exactly: OK", "Go.", 20)
    print(f"  -> {text.strip()[:60]!r} error={error!r} "
          f"({time.monotonic() - started:.1f}s)")
    check("fell back and got an answer", bool(text.strip()) and not error, error)
    config.GEMINI_API_KEY = saved_key

    print("\n=== a bad Gemini key is caught, not raised ===")
    config.GEMINI_API_KEY = "definitely-not-a-real-key"
    text, error = sheets._ask_gemini("You are terse.", "Say OK.", 20)
    print(f"  -> error={error!r}")
    check("bad key reported as an error string", bool(error) and not text, error)
    text, error = sheets._ask_model("Reply with exactly: OK", "Go.", 20)
    check("router still answered via Groq", bool(text.strip()), error)
    config.GEMINI_API_KEY = saved_key

    print("\n=== Groq directly ===")
    settings.set("sheets_provider", "groq")
    started = time.monotonic()
    text, error = sheets._ask_model("Reply with exactly: OK", "Go.", 20)
    print(f"  -> {text.strip()[:60]!r} ({time.monotonic() - started:.1f}s)")
    check("groq answered", bool(text.strip()) and not error, error)

    if config.GEMINI_API_KEY:
        print("\n=== Gemini directly (you have a key) ===")
        started = time.monotonic()
        text, error = sheets._ask_gemini("Reply with exactly: OK", "Go.", 20)
        print(f"  -> {text.strip()[:60]!r} error={error!r} "
              f"({time.monotonic() - started:.1f}s)")
        check("gemini answered", bool(text.strip()) and not error, error)
    else:
        print("\n(no GEMINI_API_KEY, so the live Gemini call is skipped -")
        print(" everything stays on Groq, which is the safe default)")

    # ---- generating a real request body, on each provider ----
    if args:
        print(f"\n{sheets.open_spreadsheet(args[0])}")
        if sheets.current()["id"]:
            request = "colour the header row light blue and freeze it"
            for provider in (["groq", "gemini"] if config.GEMINI_API_KEY
                             else ["groq"]):
                settings.set("sheets_provider", provider)
                print(f"\n--- {provider} generating: {request!r} ---")
                started = time.monotonic()
                generated, gen_error = sheets._generate_requests(request)
                elapsed = time.monotonic() - started
                if gen_error:
                    print(f"  {gen_error}")
                    continue
                clean, verror = sheets._validate_requests(generated)
                print(f"  {elapsed:.1f}s, {len(generated)} request(s)")
                if verror:
                    print(f"  VALIDATOR REFUSED: {verror}")
                else:
                    print(f"  plan: {sheets._describe_requests(clean)}")
                check(f"{provider} produced a valid body", not verror, verror)
    else:
        print("\n(pass a spreadsheet URL to also generate a real request body)")

    settings.set("sheets_provider", original)
    print(f"\nRestored sheets_provider to {original!r}")

    total, passed = len(results), sum(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
