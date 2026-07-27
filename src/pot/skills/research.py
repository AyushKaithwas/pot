"""Research skill: hands a question to a text model with web search and reads back the answer.

The realtime model stays on the call while this runs, so it can say "checking" and then
summarise. Sources are printed to the terminal rather than read aloud.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from agents import function_tool
from openai import AsyncOpenAI

from pot.console import console
from pot.runtime import Runtime, runtime
from pot.skills.base import Skill

log = logging.getLogger(__name__)

PROMPT = """
Research: use `research` for anything you don't reliably know, anything time-sensitive
(weather, news, prices, schedules, "is X open"), or anything needing a source. Ask a full
question, not keywords. Give the answer in one sentence; offer detail only if asked. Never
claim you searched when you didn't.
Leave depth at "quick" for everyday questions; it handles them fine and costs a fraction of
"deep". Only pass depth="deep" when the question genuinely needs digging across sources.
""".strip()

_INSTRUCTIONS = (
    "Answer the question directly and briefly, in plain prose meant to be read aloud. "
    "Lead with the answer. Include concrete numbers, dates and places when they matter. "
    "No markdown, no bullet lists, no preamble."
)

_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=runtime().settings.openai_api_key or None)
    return _client


@function_tool
async def research(question: str, depth: str = "quick") -> str:
    """Look something up on the web and return a short spoken-style answer.

    Args:
        question: The full question to answer, e.g. "will it rain in Lisbon tomorrow".
        depth: "quick" for everyday lookups, "deep" only when the answer needs real digging.
    """
    rt = runtime()
    settings = rt.settings
    if not settings.openai_api_key:
        return "I can't search: OPENAI_API_KEY isn't set."

    deep = depth.strip().lower() == "deep"
    research_config = settings.research
    model = research_config.deep_model if deep else research_config.model
    context_size = research_config.deep_search_context_size if deep else research_config.search_context_size
    try:
        response = await _openai().responses.create(
            model=model,
            tools=[{"type": "web_search", "search_context_size": context_size}],
            instructions=_INSTRUCTIONS,
            input=question,
            reasoning={"effort": "medium" if deep else "low"},
        )
    except Exception as exc:
        log.warning("research failed: %s", exc)
        return f"The search failed: {exc}"

    _record_usage(rt, model, response)
    answer = (response.output_text or "").strip()
    sources = _sources(response)
    if sources:
        # Full URLs wrap over several lines and bury the conversation; --verbose has them.
        log.info("research sources for %r: %s", question, ", ".join(sources))
        console.print(f"[dim]sources: {', '.join(_domains(sources))}[/dim]", highlight=False)
    return answer or "The search came back empty."


def _domains(urls: list[str]) -> list[str]:
    seen: list[str] = []
    for url in urls:
        host = urlparse(url).netloc.removeprefix("www.")
        if host and host not in seen:
            seen.append(host)
    return seen


def _record_usage(rt: Runtime, model: str, response: object) -> None:
    """A search feeds thousands of tokens of page content in, so it's worth counting."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    cost = rt.usage.record_text(
        model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        cached_tokens=cached,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    log.info("research via %s cost $%.4f", model, cost)


def _sources(response: object) -> list[str]:
    urls: list[str] = []
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            for annotation in getattr(block, "annotations", []) or []:
                url = getattr(annotation, "url", None)
                if url and url not in urls:
                    urls.append(url)
    return urls


SKILL = Skill(
    name="research",
    description="Answer questions from the live web with sources",
    tools=(research,),
    prompt=PROMPT,
)
