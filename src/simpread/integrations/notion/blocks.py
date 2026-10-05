import json
from datetime import UTC, datetime
from typing import Any

from simpread.domain import Article
from simpread.domain.models import Block, Inline
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


def rich_blocks(runs: list[Inline], kind: str = "paragraph", **extra: Any) -> list[Json]:
    blocks: list[Json] = []
    current: list[Json] = []
    size = 0
    for run in runs:
        for offset in range(0, len(run.text), 900):
            text = run.text[offset:offset + 900]
            if current and (size + len(text) > 1800 or len(current) >= 90):
                blocks.append({"object": "block", "type": kind, kind: {"rich_text": current, **extra}})
                current, size = [], 0
            content: Json = {"content": text}
            if link := safe_url(run.url):
                content["link"] = {"url": link}
            current.append({"type": "text", "text": content, "annotations": {
                "bold": run.bold, "italic": run.italic, "code": run.code, "strikethrough": run.strike}})
            size += len(text)
    if current or not blocks:
        blocks.append({"object": "block", "type": kind, kind: {"rich_text": current, **extra}})
    return blocks


def article_blocks(article: Article, media: dict[str, Json]) -> list[Json]:
    def build(values: list[Block]) -> list[Json]:
        output: list[Json] = []
        for b in values:
            if b.type in {"paragraph", "heading", "code", "quote", "link"}:
                kind = (f"heading_{min(b.level or 1, 3)}" if b.type == "heading"
                        else "paragraph" if b.type == "link" else b.type)
                extra = {"language": b.language if b.language in LANGUAGES else "plain text"} if kind == "code" else {}
                runs = b.inlines or [Inline(text=b.text, url=b.url if b.type == "link" else None)]
                converted = rich_blocks(runs, kind, **extra)
                if b.children:
                    converted[-1][kind]["children"] = build(b.children)
                output.extend(converted)
            elif b.type in {"unordered_list", "ordered_list"}:
                kind = "bulleted_list_item" if b.type == "unordered_list" else "numbered_list_item"
                explicit_numbers = b.type == "ordered_list" and b.start != 1
                if explicit_numbers:
                    # Notion cannot set an ordered list's start. Plain paragraphs avoid auto-renumbering.
                    kind = "paragraph"
                items = b.children or [Block(type="list_item", text=t) for t in b.items]
                for number, item in enumerate(items, b.start):
                    children = list(item.children)
                    first = children.pop(0) if children and children[0].type in {"paragraph", "link"} else item
                    runs = first.inlines or [Inline(text=first.text)]
                    if explicit_numbers:
                        runs = [Inline(text=f"{number}. "), *runs]
                    converted = rich_blocks(runs, kind)
                    # A long item is still one item. Store continuation text as child paragraphs.
                    continuation = [
                        {"object": "block", "type": "paragraph", "paragraph": part[kind]}
                        for part in converted[1:]
                    ]
                    converted = converted[:1]
                    nested = [*continuation, *build(children)]
                    if nested:
                        converted[-1][kind]["children"] = nested
                    output.extend(converted)
            elif b.type == "divider":
                output.append({"object": "block", "type": "divider", "divider": {}})
            elif b.type in {"image", "video", "animation", "audio", "file"}:
                if b.media_id and b.media_id in media:
                    output.append(media[b.media_id])
                elif url := safe_url(b.url):
                    kind = "file" if b.type == "animation" else b.type
                    payload = {"type": "external", "external": {"url": url}}
                    output.append({"object": "block", "type": kind, kind: payload})
                else:
                    output.extend(text_blocks(f"{b.text or b.type}：附件不可用，请查看原文。"))
            elif b.type == "table" and b.rows:
                width = max(map(len, b.rows))
                rows: list[Json] = []
                fallback = False
                for index, row in enumerate(b.rows):
                    cells: list[Json] = []
                    for col, text in enumerate(row + [""] * (width - len(row))):
                        runs = b.cells[index][col] if b.cells and col < len(b.cells[index]) else [Inline(text=text)]
                        # Each cell has the same rich-text object limits as a paragraph.
                        converted = rich_blocks(runs)
                        if len(converted) > 1:
                            cells = []
                            break
                        cells.append(converted[0]["paragraph"]["rich_text"])
                    if not cells:
                        fallback = True
                    else:
                        rows.append({"object": "block", "type": "table_row", "table_row": {"cells": cells}})
                if fallback:
                    output.extend(text_blocks("\n".join(" | ".join(row) for row in b.rows)))
                    continue
                for start in range(0, len(rows), 30):
                    output.append({"object": "block", "type": "table", "table": {
                        "table_width": width, "has_column_header": b.header and start == 0,
                        "has_row_header": False, "children": rows[start:start+30]}})
        return output
    return [*text_blocks(article.description, "quote"), *build(article.blocks),
            *text_blocks(f"来源：{article.source.original_url}\n平台：{article.source.platform}")]


def block_count(block: Json) -> int:
    return 1 + sum(block_count(child) for child in block.get(block["type"], {}).get("children", []))


def batches(blocks: list[Json]) -> list[list[Json]]:
    output: list[list[Json]] = []
    current: list[Json] = []
    size = count = 0
    for block in blocks:
        length = len(json.dumps(block, ensure_ascii=False).encode())
        nested = block_count(block)
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
