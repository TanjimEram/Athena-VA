# Design patterns in Athena

Every pattern below was checked against the source before being written
down, and each entry names the file and the class or function you can read.
Where the fit is close rather than exact, that is said plainly — claiming a
pattern the code does not implement would be worse than claiming none.

**A note on branches.** This document describes `feature/sheets-builder`.
One pattern (Strategy) has a fuller second implementation on
`feature/provider-fallback` in `athena/providers.py`, which is **not present
on this branch**. It is described at the end and clearly marked, rather than
being presented as though you could open the file here.

---

## 1. Builder

**Where:** `athena/sheet_builder.py`

| Participant | Class |
|---|---|
| ConcreteBuilder | `SheetRequestBuilder` |
| Product | `SheetPlan` |
| Director | `PlanDirector` |

### The problem

"Delete all the rows that are coloured orange" is not one API request. It is
three stages: read the cell formats, work out which rows match, then delete
them in **descending** index order so that removing row 4 does not shift row
7 out from under you.

The previous design asked a language model for a single `batchUpdate` body.
It could not express any of that, so it correctly refused — and that honest
refusal was right. The capability was what was missing.

There is a second problem underneath. A spreadsheet edit is irreversible
enough that you want to validate the *whole* change before any of it runs,
and describe it to the user in plain English so they can approve it. That
means construction and execution have to be separate steps, which is exactly
what Builder is for.

### How it is applied

Steps are chained onto the builder, which accumulates them and performs **no
API calls at all** while chaining:

```python
plan = (SheetRequestBuilder()
        .delete_rows_where({"kind": "background colour", "colour": "orange"})
        .resolve()        # the only step that reads the sheet
        .build())         # validates, then freezes
```

`build()` returns a `SheetPlan`: a frozen dataclass whose every field is a
tuple. It carries the API requests **and** a plain-English description **and**
the ranges it will touch.

That dual representation is the pattern doing real work rather than
decorating a function. One construction process yields two outputs: the thing
the API executes, and the sentence the user approves. They cannot drift apart,
because they are built from the same validated steps.

`PlanDirector` holds sequences that recur — `tidy()` is freeze-header plus
autosize. The Director knows the *order* of construction; the builder knows
*how* to construct. Separating those is the reason the pattern has a Director
at all.

### Without it

The natural alternative is one function per operation, each assembling and
sending its own requests:

```python
def delete_orange_rows():
    grid = read_formats()
    rows = [i for i, row in enumerate(grid) if is_orange(row)]
    for row in sorted(rows, reverse=True):
        send({"deleteDimension": {...}})     # one call per row
```

Three things go wrong. Each `send` is a separate call, so a failure halfway
leaves the sheet half-edited — the worst outcome available. There is nothing
to show the user before it runs, so approval is a guess. And the descending
sort has to be remembered in every function that deletes anything; forget it
once and rows silently vanish from the wrong places.

The builder makes all three structural rather than remembered: one
`batchUpdate`, one description built from the validated plan, and the
descending sort applied once in `build()` for every plan that ever exists.

---

## 2. Memento

**Where:** `athena/sheets.py` — `_snapshot`, `_push_undo`, `undo_last_change`,
with `athena/sheet_builder.py` — `snapshot_for`, `_restore`

| Participant | In the code |
|---|---|
| Originator | the spreadsheet |
| Memento | the snapshot dict — values, formats, row and column counts |
| Caretaker | `_undo_stack` (depth 5) |

### The problem

**The Google Sheets API has no undo.** Undo is a feature of the browser
editor, and nothing done through the API enters that stack. A voice command
that sorted the wrong column was permanent.

### How it is applied

Before any change, the affected state is captured into an opaque record and
pushed onto a stack. `undo_last_change()` pops one and writes it back. The
caretaker never inspects the memento's contents; it only stores and returns
them.

Two details worth reading:

- The snapshot covers the **whole used range**, not the ranges the plan
  names. A delete shifts every row beneath it, so "what we touched" is not
  "what changed".
- It records the row and column **counts** as well as the contents. Writing
  six rows of values into a grid a delete left at three would silently lose
  the tail, so `_restore` pads or trims the shape first.

### Without it

Undo would have to be the inverse of each operation, computed per operation:
the opposite of a sort (unsortable — the original order is gone), the
opposite of a delete (re-insert *and* rewrite), the opposite of a colour
change (the previous colour, which nobody recorded). Some of those cannot be
computed at all after the fact. Capturing state instead of computing inverses
is what makes undo possible for operations that are not mathematically
invertible.

---

## 3. Singleton

Four instances, each solving the same problem for a different resource.

| Where | What is shared | Why |
|---|---|---|
| `athena/config.py` — `get_groq_client()` | the Groq API client | building one per call adds a TLS handshake to every turn |
| `athena/memory.py` — `_get_client()` | the Supabase client | same, plus a failure flag so a dead connection is diagnosed once, not per call |
| `athena/tts.py` — `_get_loop()` | one persistent asyncio event loop | a new loop per utterance costs startup on every sentence spoken |
| `athena/google_auth.py` — `get_service()` | one client per (api, version) | each build fetches a service discovery document |

### How it is applied

Lazily and behind a function, not as a class attribute:

```python
_groq_client = None

def get_groq_client():
    global _groq_client
    if _groq_client is None:
        _groq_client = groq.Groq(api_key=GROQ_API_KEY)
    return _groq_client
```

The lazy part matters. Athena imports modules it may never use in a given
session; constructing a Supabase client at import time would cost a network
round trip to reach a service the user may not have configured.

`memory._get_client` adds a `_client_failed` flag so a missing configuration
prints **one** warning rather than one per interaction — a detail that only
shows up once something is broken, which is when noise is least welcome.

That one gained a **second consumer** when the calendar arrived.
`athena/event_status.py` stores what happened to a past event in a different
table in the same project, and reaches the client through
`memory.shared_client()` rather than calling `create_client` again. This is
where the pattern pays: a second client would mean a second TLS handshake per
session, a second `_client_failed` flag, and two modules each answering "is
Supabase configured?" separately — and disagreeing the moment one of them is
asked before the other. `shared_client` is a public name added for exactly
this, so the sharing is a documented contract rather than a reach into
another module's private.

### Without it

Every call site builds its own client. On the voice loop that is a TLS
handshake per turn on a high-latency connection, and in `tts` a fresh event
loop per spoken sentence. It also scatters the "is this configured?" question
across every caller instead of answering it once.

### An honest note

This is the *lazy-initialised shared instance* form. Nothing prevents a
caller constructing its own `groq.Groq` — there is no enforced single
instance, and enforcing one would make testing harder, since several test
suites deliberately replace `config.get_groq_client` with a stub. The pattern
is used for resource sharing, not for enforcing uniqueness.

---

## 4. Strategy

**Where:** `athena/sheets.py` — `_ask_model`, `_ask_groq`, `_ask_gemini`,
selected by `_sheets_provider()`

### The problem

Spreadsheet reasoning is token-heavy: the request carries the sheet's shape,
its headers and the user's words. Groq's free tier caps at 12,000 tokens per
minute, and the voice loop needs that budget. The work should be movable to a
different provider **without the calling code knowing or caring**.

### How it is applied

Interchangeable functions behind one interface — each takes
`(system, user, max_tokens)` and returns `(text, error)`:

```python
def _ask_model(system, user, max_tokens=900):
    if _sheets_provider() == "gemini":
        text, error = _ask_gemini(system, user, max_tokens)
        if not error:
            return text, ""
        print(f"[sheets] Gemini unavailable ({error}) - falling back to Groq")
    return _ask_groq(system, user, max_tokens)
```

The strategy is selected at **call time** from settings, so switching
providers needs no restart. Every caller — the planner included — just calls
`_ask_model` and never learns which one answered.

Failure is part of the contract rather than an exception: a strategy that
cannot serve returns an error string, and the selector falls back. Choosing
Gemini therefore cannot leave the feature broken.

### Without it

The provider choice would be an `if` at every call site, repeated wherever a
model is used, with each site handling its own fallback — or not, which is
how one path ends up silently broken when a key expires.

### On the fuller implementation

`feature/provider-fallback` extends this to a five-provider ordered chain in
`athena/providers.py`, with cooldowns and per-provider capability flags. It is
the same pattern with more strategies and a richer selector. **That file does
not exist on this branch**, so read it there rather than looking for it here.

---

## 5. Adapter — a smaller one, claimed carefully

**Where:** `athena/sheet_builder.py` — `LiveSheetReader`

`SheetRequestBuilder.resolve()` needs three things from a sheet: `facts()`,
`values()` and `backgrounds()`. `sheets.py` offers none of those shapes
directly. `LiveSheetReader` adapts one interface to the other, and it is the
only part of `sheet_builder.py` that talks to Google.

The practical payoff is that the entire builder — including predicate
resolution and colour matching — is tested offline against a stub reader,
with no network and no spreadsheet. That is 119 checks that cost nothing to
run.

**Why "carefully":** this is a small, single-purpose adapter written
alongside its client, not a wrapper retrofitted around an incompatible
third-party interface. It is a fair use of the name, but it is not the
textbook case of adapting a class you cannot change.

---

## Patterns deliberately not claimed

Worth recording, since the absence is a decision rather than an oversight.

- **Observer.** `run_agent`'s `on_step` callback looks like it, but there is
  one optional callback rather than a subscriber list, and no registration or
  removal. It is a callback, not the pattern.
- **Facade.** `skills.py` presents one surface over many modules, which is
  facade-shaped, but it exists to give the language model a flat tool list
  rather than to simplify a complex subsystem for callers.
- **Command.** The undo stack stores *state*, not invocable operations. That
  makes it Memento; calling it Command would describe a design we considered
  and did not build, because computing inverse operations is impossible for
  a sort.
- **Factory.** `providers.client_for` caches clients but does not select
  between classes.
