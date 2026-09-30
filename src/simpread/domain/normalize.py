"""Explicit public allowlist: no Worker filesystem/credential metadata enters state."""

from typing import Any

from .blocks import parse_html, parse_markdown
from .models import Article, Block, MediaAsset, SourceInfo
from .render import article_to_html, article_to_markdown
from .urls import safe_url

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
}


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    output = {k: v for k, v in result.items() if k in PUBLIC_FIELDS}
    output["media"] = [{k: v for k, v in m.items() if k in MEDIA_FIELDS} for m in result.get("media", [])]
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
    if result.get("markdownContent") or result.get("contentFormat") == "markdown":
        blocks = parse_markdown(text)
    elif not text and result.get("htmlContent"):
        blocks = parse_html(result["htmlContent"])
    else:
        blocks = [Block(type="paragraph", text=t) for t in text.split("\n\n") if t.strip()]
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
        blocks.append(
            Block(
                type="image" if asset.type in {"photo", "live_photo"} else "video",
                media_id=asset.media_id,
                url=asset.source_url,
                text=asset.filename or "附件",
            )
        )
        if asset.video_media_id:
            blocks.append(Block(type="video", media_id=asset.video_media_id))
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
