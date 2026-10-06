"""Explicit public allowlist: no Worker filesystem/credential metadata enters state."""

import re
from typing import Any, cast

from .blocks import parse_html, parse_markdown
from .media import media_kind
from .models import Article, Block, MediaAsset, SourceInfo
from .render import article_to_html, article_to_markdown
from .urls import safe_url

_TOPIC = re.compile(r"#([^#\[\]\n]+?)\[话题]#")
_TAG = re.compile(r"#[^\s#]+(?:\[话题])?#?")
_BOILERPLATE = ("复制后打开", "复制这段描述", "打开小红书", "打开【小红书】")


def _hashtag_only(line: str) -> bool:
    if "#" not in line:
        return False
    rest = _TAG.sub("", line).replace("[话题]", "")
    rest = re.sub(r"[\s#]+", "", rest)
    return rest == "" or all(not char.isalnum() and not "\u4e00" <= char <= "\u9fff" for char in rest)


def _boilerplate(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    return any(phrase in compact for phrase in _BOILERPLATE) and len(compact) <= 48


def clean_xiaohongshu(text: str, title: str) -> str:
    """Drop share boilerplate and tag-only lines. Keep a tag that sits inside a sentence."""
    kept: list[str] = []
    for line in _TOPIC.sub(r"#\1", text).splitlines():
        stripped = line.strip()
        if not stripped or _hashtag_only(stripped) or _boilerplate(stripped):
            continue
        kept.append(stripped)
    if len(kept) > 1 and kept[0] == title.strip():
        kept = kept[1:]
    return "\n\n".join(kept)


PUBLIC_FIELDS = {
    "sourceUrl",
    "canonicalUrl",
    "platform",
    "title",
    "description",
    "content",
    "plainContent",
    "markdownContent",
    "htmlContent",
    "contentFormat",
    "resultType",
    "mediaFailureCount",
    "leaseId",
}
MEDIA_FIELDS = {
    "mediaId",
    "type",
    "mimeType",
    "filename",
    "sizeBytes",
    "width",
    "height",
    "durationSeconds",
    "videoMediaId",
    "videoMimeType",
    "videoSizeBytes",
    "sourceUrl",
    "originalUrl",
}


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    output = {k: v for k, v in result.items() if k in PUBLIC_FIELDS}
    media = result.get("media", [])
    if not isinstance(media, list) or any(not isinstance(item, dict) for item in media):
        raise ValueError("invalid_media")
    output["media"] = [{k: v for k, v in m.items() if k in MEDIA_FIELDS} for m in media]
    for media in output["media"]:
        if media.get("filename"):
            media["filename"] = str(media["filename"]).replace("\\", "/").split("/")[-1]
    return output


def normalize_worker_result(result: dict[str, Any]) -> Article:
    result = public_result(result)
    source = safe_url(str(result.get("sourceUrl") or ""))
    if not source:
        raise ValueError("invalid_source")
    canonical = safe_url(result.get("canonicalUrl")) or source
    text = str(result.get("markdownContent") or result.get("plainContent") or result.get("content") or "")
    plain = not (
        result.get("markdownContent")
        or result.get("contentFormat") in {"markdown", "html"}
        or result.get("htmlContent")
    )
    if str(result.get("platform") or "") == "xhs" and plain:
        text = clean_xiaohongshu(text, str(result.get("title") or ""))
    elif str(result.get("platform") or "") == "xhs":
        text = _TOPIC.sub(r"#\1", text)
    if result.get("markdownContent") or result.get("contentFormat") == "markdown":
        blocks = parse_markdown(text, canonical)
    elif result.get("htmlContent"):
        blocks = parse_html(result["htmlContent"], canonical)
    elif result.get("contentFormat") == "html":
        blocks = parse_html(text, canonical)
    else:
        blocks = [Block(type="paragraph", text=t) for t in text.split("\n\n") if t.strip()]
    def all_blocks(values: list[Block]):
        for block in values:
            yield block
            yield from all_blocks(block.children)

    media = []
    for item in result["media"]:
        if not item.get("mediaId"):
            continue
        asset = MediaAsset.model_validate(
            {
                "media_id": item["mediaId"],
                "lease_id": result.get("leaseId"),
                "type": item.get("type", "document"),
                "mime_type": item.get("mimeType"),
                "filename": item.get("filename"),
                "size_bytes": item.get("sizeBytes", 0),
                "width": item.get("width"),
                "height": item.get("height"),
                "duration_seconds": item.get("durationSeconds"),
                "video_media_id": item.get("videoMediaId"),
                "video_mime_type": item.get("videoMimeType"),
                "video_size_bytes": item.get("videoSizeBytes", 0),
                "source_url": safe_url(item.get("sourceUrl")),
            }
        )
        media.append(asset)
        original = safe_url(item.get("originalUrl")) or asset.source_url
        kind = media_kind(asset.type, asset.mime_type)
        matches = [b for b in all_blocks(blocks)
                   if original and b.url == original and not b.media_id
                   and b.type in {"image", "video", "audio", "file"}]
        if matches:
            for block in matches:
                block.media_id = asset.media_id
        else:
            blocks.append(Block(type=cast(Any, kind), media_id=asset.media_id,
                                url=original, text=asset.filename or "附件"))
        if asset.video_media_id:
            motion = Block(type="video", media_id=asset.video_media_id)
            # Attach motion next to its matching photo, including inside a nested block.
            def insert_motion(values: list[Block], asset_id: str, motion: Block) -> bool:
                for index, block in enumerate(values):
                    if block.media_id == asset_id:
                        values.insert(index + 1, motion)
                        return True
                    if insert_motion(block.children, asset_id, motion):
                        return True
                return False
            insert_motion(blocks, asset.media_id, motion)
    identity = {
        "source": canonical,
        "title": result.get("title"),
        "content": text,
        "html": result.get("htmlContent"),
        "media": [m.model_dump(exclude={"media_id", "lease_id", "video_media_id"}) for m in media],
    }
    article = Article(
        source=SourceInfo(
            original_url=source, canonical_url=canonical, platform=str(result.get("platform") or "unknown")
        ),
        title=str(result.get("title") or ""),
        description=str(result.get("description") or ""),
        result_type="richtext" if result.get("resultType") == "richtext" else "media" if media else "post",
        blocks=blocks,
        media=media,
        raw_result_hash=Article.digest(result),
        content_hash=Article.digest(identity),
        warnings=["media_failed"] if result.get("mediaFailureCount") else [],
    )
    article.markdown, article.html = article_to_markdown(article), article_to_html(article)
    return article
