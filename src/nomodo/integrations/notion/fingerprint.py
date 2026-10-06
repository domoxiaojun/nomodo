"""Semantic fingerprints exclude Notion response metadata, never user-visible content."""

import hashlib
import json
from typing import Any

from .client import NotionError

Json = dict[str, Any]


def rich_text(value: Any) -> list[Json]:
    if not isinstance(value, list):
        raise NotionError("write_outcome_unknown")
    output: list[Json] = []
    for run in value:
        if not isinstance(run, dict) or run.get("type") != "text":
            raise NotionError("write_outcome_unknown")
        text = run.get("text", {})
        content = text.get("content")
        if not isinstance(content, str):
            raise NotionError("write_outcome_unknown")
        annotations = run.get("annotations") or {}
        style = {key: annotations.get(key, False) for key in
                 ("bold", "italic", "strikethrough", "underline", "code")}
        style["color"] = annotations.get("color", "default")
        item = {"text": content, "style": style, "link": (text.get("link") or {}).get("url")}
        if output and output[-1]["style"] == style and output[-1]["link"] == item["link"]:
            output[-1]["text"] += content
        elif content:
            output.append(item)
    return output


def content(block: Json) -> Json:
    kind = block.get("type")
    if not isinstance(kind, str):
        raise NotionError("write_outcome_unknown")
    payload = block.get(kind, {})
    if not isinstance(payload, dict) or block.get("archived") or block.get("in_trash"):
        raise NotionError("write_outcome_unknown")
    output: Json = {"type": kind}
    if kind in {"paragraph", "heading_1", "heading_2", "heading_3", "quote",
                "bulleted_list_item", "numbered_list_item", "code"}:
        output.update(rich_text=rich_text(payload.get("rich_text", [])), color=payload.get("color", "default"))
        if kind == "code":
            output["language"] = payload.get("language", "plain text")
            output["caption"] = rich_text(payload.get("caption", []))
        if kind.startswith("heading_"):
            output["is_toggleable"] = payload.get("is_toggleable", False)
    elif kind == "table":
        for key in ("table_width", "has_column_header", "has_row_header"):
            output[key] = payload.get(key)
    elif kind == "table_row":
        output["cells"] = [rich_text(cell) for cell in payload.get("cells", [])]
    elif kind in {"image", "video", "audio", "file", "bookmark"}:
        output["caption"] = rich_text(payload.get("caption", []))
        if kind == "bookmark":
            output["url"] = payload.get("url")
        else:
            resource_type = payload.get("type")
            output["resource_type"] = resource_type
            if resource_type == "external":
                output["url"] = payload.get("external", {}).get("url")
            elif resource_type == "file_upload":
                output["upload_id"] = payload.get("file_upload", {}).get("id")
            else:
                # An expiring hosted URL cannot prove which upload originally produced it.
                raise NotionError("write_outcome_unknown")
    elif kind != "divider":
        raise NotionError("write_outcome_unknown")
    if payload.get("children"):
        output["children"] = [content(child) for child in payload["children"]]
    return output


def fingerprint(block: Json) -> str:
    return hashlib.sha256(json.dumps(content(block), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
