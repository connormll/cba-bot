"""Retrieval-augmented answer streaming."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

from cba_bot.index import Source, retrieve_sources
from cba_bot.prompt import build_messages
from cba_bot.runtime import Runtime

_CITE = re.compile(r"\[S(\d+)\]")


def linkify_citations(text: str, answer_id: int, source_count: int) -> str:
    def replace(match: re.Match) -> str:
        number = int(match.group(1))
        if 1 <= number <= source_count:
            return f"[[S{number}]](cba-source:{answer_id}:{number})"
        return match.group(0)

    return _CITE.sub(replace, text)


async def stream_answer(
    runtime: Runtime,
    question: str,
    history: list[dict[str, str]],
    *,
    current_only: bool,
    sector: str,
    jurisdiction: str,
) -> AsyncIterator[tuple[str, object]]:
    sources: list[Source] = await asyncio.to_thread(
        retrieve_sources,
        runtime.store,
        runtime.embedder,
        question,
        top_k=runtime.settings.top_k,
        current_only=current_only,
        sector=sector,
        jurisdiction=jurisdiction,
    )
    yield "sources", sources
    if not sources:
        yield "token", (
            "No indexed contract addresses that question. Try a broader phrase, "
            "turn off `/fresh` to include expired agreements, or run "
            "`cba-bot scrape` followed by `cba-bot index` to build the corpus."
        )
        return
    messages = build_messages(
        question, sources, history, history_turns=runtime.settings.history_turns
    )
    async for token in runtime.llm.stream(messages):
        yield "token", token
