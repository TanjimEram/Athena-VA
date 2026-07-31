"""Live web research - Athena searches the web, reads the results, and speaks
the answer (as opposed to skills.web_search, which just opens a browser tab).

look_up(query)  - a quick 1-3 sentence spoken answer to a factual/current
                  question, via Tavily's search with a synthesized answer.
research(topic) - a slightly deeper multi-source summary: search, optionally
                  pull the top page, then have the LLM condense it into a
                  short spoken summary and offer to go deeper.

Both are bounded and fail gracefully with a spoken message (no internet, no
key, or API error) so they never break the assistant loop."""

import urllib.parse

from athena import config

_client = None
_client_failed = False


def _get_client():
    """The Tavily client, or None if research isn't configured/reachable."""
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        return None
    if not config.TAVILY_API_KEY:
        _client_failed = True
        print("[research] TAVILY_API_KEY not set in .env - web research disabled")
        return None
    try:
        from tavily import TavilyClient
        _client = TavilyClient(api_key=config.TAVILY_API_KEY)
        return _client
    except Exception as exc:
        _client_failed = True
        print(f"[research] couldn't create Tavily client: {exc}")
        return None


_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}


def _site_name(url: str) -> str:
    """The speakable brand of a URL: en.wikipedia.org -> wikipedia,
    bbc.co.uk -> bbc, reuters.com -> reuters."""
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return parts[0] if parts else ""
    # the label before the TLD, skipping ccTLD second levels like co.uk
    idx = -2
    if parts[-2] in _SECOND_LEVEL and len(parts) >= 3:
        idx = -3
    return parts[idx]


def _source_names(results: list, limit: int = 2) -> str:
    """Turn result URLs into speakable site names, e.g. 'reuters and bbc'."""
    names = []
    for r in results[:limit]:
        name = _site_name(r.get("url", ""))
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return " and ".join(names)


def _trim_sentences(text: str, max_sentences: int = 3) -> str:
    """Keep the first few sentences so the spoken answer stays short."""
    text = " ".join(text.split())
    out, count = "", 0
    for ch in text:
        out += ch
        if ch in ".!?":
            count += 1
            if count >= max_sentences:
                break
    return out.strip()


def look_up(query: str) -> str:
    """A short spoken answer to a factual or current-events question."""
    client = _get_client()
    if client is None:
        return "I can't reach the web right now to look that up."
    try:
        resp = client.search(
            query=query, include_answer=True, max_results=4,
            search_depth="basic",
        )
    except Exception as exc:
        print(f"[research] look_up failed: {exc!r}")
        return "I couldn't reach the web to look that up. Check the connection."

    results = resp.get("results", []) or []
    answer = (resp.get("answer") or "").strip()
    if not answer and results:
        answer = (results[0].get("content") or "").strip()
    if not answer:
        return f"I searched, but couldn't find a clear answer about {query}."

    spoken = _trim_sentences(answer, 3)
    sources = _source_names(results, 2)
    if sources:
        spoken += f" That's according to {sources}."
    return spoken


def _summarize(topic: str, material: str) -> str:
    """Have the LLM condense collected web text into a short spoken summary."""
    material = material[:6000]  # keep the call fast and cheap
    try:
        resp = config.get_groq_client().chat.completions.create(
            model=config.BRAIN_MODEL,
            messages=[
                {"role": "system", "content": (
                    "You are Athena summarizing web research to be read aloud. "
                    "From the material below, give the key points in 3 or 4 "
                    "short spoken sentences - natural to say out loud, no "
                    "markdown, no lists, no citations. Be accurate; don't "
                    "invent anything not in the material.")},
                {"role": "user", "content": (
                    f"Topic: {topic}\n\nMaterial:\n{material}\n\n"
                    "Give the concise spoken summary now.")},
            ],
            temperature=0.4,
            max_tokens=220,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"[research] summarize failed: {exc!r}")
        return ""


def research(topic: str) -> str:
    """A slightly deeper, multi-source spoken summary of a topic."""
    client = _get_client()
    if client is None:
        return "I can't reach the web right now to research that."
    try:
        resp = client.search(
            query=topic, include_answer=True, max_results=6,
            search_depth="advanced",
        )
    except Exception as exc:
        print(f"[research] research failed: {exc!r}")
        return "I couldn't reach the web to research that. Check the connection."

    results = resp.get("results", []) or []
    if not results and not resp.get("answer"):
        return f"I searched, but couldn't find much about {topic}."

    # Collect the synthesized answer + each result's snippet as raw material.
    material = (resp.get("answer") or "") + "\n\n"
    for r in results[:5]:
        material += f"- {r.get('title', '')}: {r.get('content', '')}\n"

    # Pull the top page's full text for a bit more depth (best-effort).
    try:
        top_url = results[0].get("url")
        if top_url:
            ex = client.extract(urls=[top_url])
            pages = ex.get("results", []) if isinstance(ex, dict) else []
            if pages:
                material += "\n" + (pages[0].get("raw_content") or "")[:3000]
    except Exception as exc:
        print(f"[research] extract skipped: {exc!r}")

    summary = _summarize(topic, material)
    if not summary:
        # Fall back to Tavily's own answer if the LLM summary failed.
        summary = _trim_sentences(resp.get("answer") or "", 4) or \
            f"Here's a bit on {topic}, but I couldn't summarize it cleanly."
    return summary + " Want me to go deeper on any part of that?"
