"""Stable source units, lossless chunking and deterministic protected-content restoration."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from nomodo.domain import Article
from nomodo.domain.models import Block
from nomodo.domain.render import article_to_html, article_to_markdown

from .client import LLMError, redact
from .schemas import Citation, Transformation
from .tokens import Tokens

PROTECTED = re.compile(
    r"`+[^`\n]+`+|https?://[^\s<>]+|"
    r"\b(?:cookie|authorization|token|access_token|api_key)\s*[:=][^\n]+|"
    r"\b(?:sk-|ntn_|secret_)[\w-]{12,}|"
    r"(?<!\w)(?:/Users/|/home/|/tmp/|/private/|[A-Za-z]:\\)[^\s\"<>]+",
    re.IGNORECASE,
)
MARKER = re.compile(r"⟦SR[0-9a-f]{8}:\d+⟧")


@dataclass
class Unit:
    id: str
    block_id: int
    field: str
    text: str

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "block_id": self.block_id, "text": self.text}


def walk(blocks: list[Block]) -> list[Block]:
    return [item for block in blocks for item in [block, *walk(block.children)]]


def fields(article: Article, transform: bool = False) -> list[tuple[str, int, str]]:
    output = [("title", 0, article.title), ("description", 0, article.description)]
    for index, block in enumerate(walk(article.blocks), 1):
        if block.children:
            continue
        if transform and block.type in {"code", "image", "video", "animation", "audio", "file", "divider"}:
            continue
        if block.inlines and transform:
            output.extend((f"b{index}.inlines.{i}.text", index, run.text)
                          for i, run in enumerate(block.inlines) if not run.code)
        else:
            output.append((f"b{index}.text", index, block.text))
        output.extend((f"b{index}.items.{i}", index, text) for i, text in enumerate(block.items))
        if block.cells and transform:
            output.extend((f"b{index}.cells.{r}.{c}.{i}.text", index, run.text)
                          for r, row in enumerate(block.cells) for c, cell in enumerate(row)
                          for i, run in enumerate(cell) if not run.code)
        else:
            output.extend((f"b{index}.rows.{r}.{c}", index, text)
                          for r, row in enumerate(block.rows) for c, text in enumerate(row))
    return [(key, block, text) for key, block, text in output if text]


def protect(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]

    def replace(match: re.Match[str]) -> str:
        key = f"⟦SR{digest}:{len(mapping)}⟧"
        mapping[key] = match.group()
        return key

    return PROTECTED.sub(replace, text), mapping


def split_text(text: str, tokens: Tokens, budget: int) -> list[str]:
    if budget < 64:
        raise LLMError("llm_context_too_small")
    parts = []
    while text:
        if tokens.count(text) <= budget:
            parts.append(text)
            break
        low, high = 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if tokens.count(text[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        end = low
        boundary = max(text.rfind("\n", 0, end), text.rfind(" ", 0, end))
        if boundary >= end // 2:
            end = boundary + 1
        for marker in MARKER.finditer(text[:end + 40]):
            if marker.start() < end < marker.end():
                end = marker.start()
                break
        if end <= 0 or tokens.count(text[:end]) > budget:
            raise LLMError("llm_context_too_small")
        parts.append(text[:end])
        text = text[end:]
    return parts


def prepare(
    article: Article, tokens: Tokens, budget: int, transform: bool = False
) -> tuple[list[Unit], dict[str, str]]:
    output: list[Unit] = []
    placeholders: dict[str, str] = {}
    for key, block, original in fields(article, transform):
        if transform:
            text, mapping = protect(original)
            placeholders.update(mapping)
        else:
            text = redact(original)
        for index, part in enumerate(split_text(text, tokens, budget - 80)):
            output.append(Unit(f"{key}:{index}", block, key, part))
    return output, placeholders


def pack(units: list[Unit], tokens: Tokens, budget: int) -> list[list[Unit]]:
    output: list[list[Unit]] = []
    current: list[Unit] = []
    size = 2
    for unit in units:
        cost = tokens.json(unit.payload()) + 2
        if cost > budget:
            raise LLMError("llm_context_too_small")
        if current and size + cost > budget:
            output.append(current)
            current, size = [], 2
        current.append(unit)
        size += cost
    if current:
        output.append(current)
    return output


def evidence(article: Article) -> dict[int, str]:
    result: dict[int, str] = {}
    for _, block, text in fields(article):
        result[block] = result.get(block, "") + ("\n" if block in result else "") + text
    return result


def verify_citations(citations: list[Citation], article: Article, allowed: list[Unit] | None = None) -> None:
    originals = evidence(article)
    for cite in citations:
        if not cite.quote.strip() or cite.quote not in originals.get(cite.block_id, ""):
            raise LLMError("invalid_citation")
        if allowed is not None and not any(cite.block_id == u.block_id and cite.quote in u.text for u in allowed):
            raise LLMError("invalid_citation")


def verify_transform(result: Transformation, units: list[Unit], normalize: bool) -> None:
    if [u.id for u in result.units] != [u.id for u in units]:
        raise LLMError("incomplete_transformation")
    for source, target in zip(units, result.units, strict=True):
        if source.text.strip() and not target.text.strip():
            raise LLMError("incomplete_transformation")
        if MARKER.findall(source.text) != MARKER.findall(target.text):
            raise LLMError("protected_content_changed")
        if normalize and re.sub(r"\s+", "", source.text) != re.sub(r"\s+", "", target.text):
            raise LLMError("normalization_changed_content")


def restore(article: Article, units: list[Unit], translations: dict[str, str], placeholders: dict[str, str]) -> Article:
    clone = article.model_copy(deep=True)
    blocks = walk(clone.blocks)
    merged: dict[str, str] = {}
    for unit in units:
        merged[unit.field] = merged.get(unit.field, "") + translations[unit.id]
    for field, text in merged.items():
        text = MARKER.sub(lambda match: placeholders.get(match.group(), match.group()), text)
        path = field.split(".")
        if len(path) == 1:
            setattr(clone, field, text)
        else:
            block = blocks[int(path[0][1:]) - 1]
            if path[1] == "text":
                block.text = text
            elif path[1] == "items":
                block.items[int(path[2])] = text
            elif path[1] == "rows":
                block.rows[int(path[2])][int(path[3])] = text
            elif path[1] == "inlines":
                block.inlines[int(path[2])].text = text
            elif path[1] == "cells":
                block.cells[int(path[2])][int(path[3])][int(path[4])].text = text
    for block in blocks:
        if block.inlines:
            block.text = "".join(run.text for run in block.inlines)
        if block.cells:
            block.rows = [["".join(run.text for run in cell) for cell in row] for row in block.cells]
    clone.markdown, clone.html = article_to_markdown(clone), article_to_html(clone)
    return clone


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
