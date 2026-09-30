from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_url: str
    canonical_url: str
    platform: str


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "paragraph",
        "heading",
        "quote",
        "unordered_list",
        "ordered_list",
        "code",
        "table",
        "image",
        "video",
        "divider",
        "link",
    ]
    text: str = ""
    level: int | None = Field(default=None, ge=1, le=6)
    language: str | None = None
    items: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    url: str | None = None
    alt: str | None = None
    media_id: str | None = None


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: str
    lease_id: str | None = None
    type: Literal["photo", "video", "animation", "document", "live_photo"] = "document"
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int = Field(default=0, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    video_media_id: str | None = None
    video_mime_type: str | None = None
    video_size_bytes: int = 0
    source_url: str | None = None


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceInfo
    title: str = ""
    description: str = ""
    result_type: Literal["post", "richtext", "media", "unknown"] = "unknown"
    blocks: list[Block] = Field(default_factory=list)
    markdown: str = ""
    html: str = ""
    media: list[MediaAsset] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_result_hash: str
    content_hash: str

    @staticmethod
    def digest(value: object) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
