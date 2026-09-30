import json
from datetime import UTC, datetime
from typing import Any

from simpread.domain import Article
from simpread.domain.urls import safe_url

from .client import NotionError

Json = dict[str, Any]
FIELDS = {
    "Title": "title",
    "Source URL": "url",
    "Platform": "rich_text",
    "Imported At": "date",
    "Summary": "rich_text",
    "Tags": "multi_select",
}
LANGUAGES = {
    "plain text",
    "python",
    "javascript",
    "typescript",
    "json",
    "bash",
    "shell",
    "c",
    "c++",
    "c#",
    "css",
    "html",
    "java",
    "go",
    "rust",
    "sql",
    "yaml",
    "toml",
    "markdown",
}


def rt(text: str) -> list[Json]:
    return [{"type": "text", "text": {"content": text[i : i + 900]}} for i in range(0, len(text), 900)] or []


def text_blocks(text: str, kind: str = "paragraph", **kwargs: Any) -> list[Json]:
    return [
        {"object": "block", "type": kind, kind: {"rich_text": rt(text[i : i + 1800]), **kwargs}}
        for i in range(0, len(text), 1800)
    ]


def article_blocks(article: Article, media: dict[str, Json]) -> list[Json]:
    output = text_blocks(article.description, "quote")
    for b in article.blocks:
        if b.type in {"paragraph", "heading", "code", "quote"}:
            kind = f"heading_{min(b.level or 1, 3)}" if b.type == "heading" else b.type
            extra = {"language": b.language if b.language in LANGUAGES else "plain text"} if kind == "code" else {}
            output.extend(text_blocks(b.text, kind, **extra))
        elif b.type in {"unordered_list", "ordered_list"}:
            for item in b.items:
                output.extend(
                    text_blocks(item, "bulleted_list_item" if b.type == "unordered_list" else "numbered_list_item")
                )
        elif b.type == "divider":
            output.append({"object": "block", "type": "divider", "divider": {}})
        elif b.type in {"image", "video", "link"}:
            if b.media_id and b.media_id in media:
                output.append(media[b.media_id])
            elif url := safe_url(b.url):
                kind = "image" if b.type == "image" else "bookmark"
                payload = {"type": "external", "external": {"url": url}} if kind == "image" else {"url": url}
                output.append({"object": "block", "type": kind, kind: payload})
            else:
                output.extend(text_blocks(f"{b.text or b.type}：附件不可用，请查看原文。"))
        elif b.type == "table" and b.rows:
            width = max(map(len, b.rows))
            if width > 20 or any(len(c) > 1800 for r in b.rows for c in r):
                output.extend(text_blocks("\n".join(" | ".join(r) for r in b.rows)))
            else:
                for start in range(0, len(b.rows), 30):
                    output.append(
                        {
                            "object": "block",
                            "type": "table",
                            "table": {
                                "table_width": width,
                                "has_column_header": start == 0,
                                "has_row_header": False,
                                "children": [
                                    {
                                        "object": "block",
                                        "type": "table_row",
                                        "table_row": {"cells": [rt(c) for c in r + [""] * (width - len(r))]},
                                    }
                                    for r in b.rows[start : start + 30]
                                ],
                            },
                        }
                    )
    output.extend(text_blocks(f"来源：{article.source.original_url}\n平台：{article.source.platform}"))
    return output


def batches(blocks: list[Json]) -> list[list[Json]]:
    output: list[list[Json]] = []
    current: list[Json] = []
    size = count = 0
    for block in blocks:
        length = len(json.dumps(block, ensure_ascii=False).encode())
        nested = 1 + len(block.get("table", {}).get("children", []))
        if length > 400_000:
            raise NotionError("block_too_large")
        if current and (len(current) >= 50 or count + nested > 90 or size + length > 400_000):
            output.append(current)
            current, size, count = [], 0, 0
        current.append(block)
        size, count = size + length, count + nested
    if current:
        output.append(current)
    return output


def mapping(schema: Json, existing: Json) -> tuple[Json, Json]:
    names: Json = {}
    missing: Json = {}
    for name, kind in FIELDS.items():
        selected = existing.get(name, name)
        if kind == "title" and selected not in schema:
            selected = next((n for n, p in schema.items() if p.get("type") == "title"), selected)
        prop = schema.get(selected)
        if prop and prop.get("type") != kind:
            raise NotionError("schema_type_mismatch")
        if not prop:
            if kind == "title":
                raise NotionError("schema_title_missing")
            missing[selected] = {kind: {}}
        names[name] = selected
    return names, missing


def properties(article: Article, target: Json, info: Json, derived: Json) -> Json:
    title = {"title": rt((article.title or "Untitled")[:900])}
    if target["kind"] == "page":
        return {"title": title}
    names, missing = mapping(info["properties"], target.get("mapping", {}))
    if missing:
        raise NotionError("schema_confirmation_required")
    values = {
        "Title": title,
        "Source URL": {"url": safe_url(article.source.original_url)},
        "Platform": {"rich_text": rt(article.source.platform)},
        "Imported At": {"date": {"start": datetime.now(UTC).isoformat()}},
        "Summary": {"rich_text": rt(str(derived.get("summary") or article.description)[:1800])},
        "Tags": {"multi_select": [{"name": str(t)[:80].replace(",", " ")} for t in derived.get("tags", [])[:20]]},
    }
    return {names[n]: v for n, v in values.items()}
