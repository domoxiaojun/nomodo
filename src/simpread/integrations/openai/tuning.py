"""Local retrieval, bounded scheduling, evidence heuristics and translation context."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from simpread.domain import Article

from .client import redact
from .content import Unit, fields, pack, restore, split_text, walk
from .schemas import Answer, Summary
from .tokens import Tokens

STOP = {"the", "and", "what", "which", "where", "when", "does", "this", "that", "with", "from", "about", "article"}


class AdaptiveGate:
    def __init__(self, maximum: int) -> None:
        self.maximum, self.active, self.cooldown = maximum, 0, 0.0
        self.condition = asyncio.Condition()

    def limited(self) -> None:
        self.cooldown = time.monotonic() + 30

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self.condition:
            await self.condition.wait_for(
                lambda: self.active < (1 if time.monotonic() < self.cooldown else self.maximum)
            )
            self.active += 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()


async def ordered_map[T, R](values: list[T], action: Callable[[T], Awaitable[R]], width: int) -> list[R]:
    result: list[R] = []
    async def invoke(value: T) -> R:
        return await action(value)
    # Never create one task per article chunk: only the active batch occupies task memory.
    for start in range(0, len(values), width):
        tasks = [asyncio.create_task(invoke(item)) for item in values[start:start + width]]
        try:
            result.extend(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    return result


def terms(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in STOP]
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words


def build_index(groups: list[list[Unit]]) -> dict[str, Any]:
    documents = [dict(Counter(terms("\n".join(u.text for u in group)))) for group in groups]
    frequency = dict(Counter(word for document in documents for word in document))
    return {"documents": documents, "frequency": frequency}


def retrieval_order(question: str, index: dict[str, Any], initial: int) -> list[list[int]]:
    documents = index["documents"]
    if re.search(
        r"\b(all|compare|overall|every|each|list|total|exceptions|difference|why|how)\b|\bsummar|"
        r"全文|全部|所有|比较|有哪些|总共|整体|总结|概括|原因|区别|例外|逐一|对比|为什么|如何|怎样",
        question, re.I,
    ):
        return [list(range(len(documents)))]
    query = set(terms(question))
    frequency = index.get("frequency") or dict(Counter(word for document in documents for word in document))
    scores = []
    for position, doc in enumerate(documents):
        score = sum(
            (1 + math.log(doc[word])) * math.log(1 + len(documents) / frequency[word])
            for word in query if word in doc
        )
        scores.append((score, position))
    ranked = [i for _, i in sorted(scores, key=lambda pair: (-pair[0], pair[1]))]
    first: set[int] = set()
    for i in ranked[:initial]:
        first.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(documents))
    # This is a first pass, not a truncation: absent/insufficient evidence expands to the remainder.
    rest = [i for i in ranked if i not in first]
    return [sorted(first)] + [rest[i:i + initial] for i in range(0, len(rest), initial)]


def paragraph_groups(units: list[Unit], tokens: Tokens, budget: int) -> list[list[Unit]]:
    paragraphs: list[list[Unit]] = []
    for unit in units:
        if paragraphs and paragraphs[-1][-1].block_id == unit.block_id:
            paragraphs[-1].append(unit)
        else:
            paragraphs.append([unit])
    output: list[list[Unit]] = []
    current: list[Unit] = []
    for paragraph in paragraphs:
        parts = pack(paragraph, tokens, budget)
        if len(parts) > 1:
            if current:
                output.append(current)
                current = []
            output.extend(parts)
        elif current and len(pack(current + paragraph, tokens, budget)) > 1:
            output.append(current)
            current = paragraph.copy()
        else:
            current.extend(paragraph)
    if current:
        output.append(current)
    return output


def clip(text: str, tokens: Tokens, budget: int) -> str:
    return split_text(text, tokens, max(64, budget))[0] if text else ""


def translation_context(article: Article, group: list[Unit], tokens: Tokens, budget: int) -> dict[str, str]:
    blocks = walk(article.blocks)
    first, last = min(u.block_id for u in group), max(u.block_id for u in group)
    headings = [b.text for b in blocks[:max(0, first - 1)] if b.type == "heading"]
    before = blocks[first - 2].text if first > 1 else ""
    after = blocks[last].text if last < len(blocks) else ""
    paragraph = "\n".join(b.text or "\n".join(" | ".join(row) for row in b.rows)
                          for b in blocks[max(0, first - 1):last])
    return {key: clip(redact(value), tokens, budget // 5) for key, value in {
        "title": article.title, "heading": headings[-1] if headings else "", "before": before, "after": after,
        "paragraph": paragraph,
    }.items()}


def glossary_candidates(article: Article) -> list[str]:
    text = "\n".join(redact(t) for _, _, t in fields(article))
    counts = Counter(w for w in re.findall(r"\b[A-Za-z][A-Za-z-]{2,}\b", text) if w.lower() not in STOP)
    return [word for word, count in counts.most_common() if count > 1][:30]


def local_layout(article: Article) -> Article:
    units = []
    for name, block, text in fields(article, True):
        normalized = text.replace("\r\n", "\n")
        # Inline run edge spaces separate words; only strip whitespace immediately before a newline.
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        units.append(Unit(name, block, name, normalized))
    return restore(article, units, {u.id: u.text for u in units}, {})


def evidence_risks(value: Any) -> list[str]:
    if isinstance(value, Summary):
        claims = [(value.conclusion, value.citations)] + [(p.text, p.citations) for p in value.points]
    elif isinstance(value, Answer) and value.found:
        claims = [(value.answer, value.citations)]
    else:
        return []
    risks: set[str] = set()
    number = re.compile(r"\d[\d,]*(?:\.\d+)?")
    negation = re.compile(r"\b(?:not|never|cannot|without)\b|不能|不得|禁止|尚未|并非|未能", re.I)
    units = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|mg|km|cm|mm|ml|ms|%|％|周|天|年|月|元|美元|欧元)", re.I)
    def normalized(text: str) -> set[str]:
        return {n.replace(",", "").lstrip("0") or "0" for n in number.findall(text)}

    for claim, cites in claims:
        source = "\n".join(c.quote for c in cites)
        if normalized(claim) - normalized(source):
            risks.add("numbers_or_dates")
        if negation.search(source) and not negation.search(claim):
            risks.add("negation")
        if set(units.findall(claim)) - set(units.findall(source)):
            risks.add("units")
    return sorted(risks)
