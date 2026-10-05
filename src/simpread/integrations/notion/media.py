"""Lease-only downloads; no source URL fetching and no filesystem paths from the Worker."""

import asyncio
import io
from typing import Any

from PIL import Image

from simpread.domain import Article
from simpread.domain.media import media_kind
from simpread.domain.urls import safe_url
from simpread.worker import WorkerClient, WorkerError

from .blocks import text_blocks
from .client import NotionClient, NotionError


def validate(data: bytes, mime: str) -> None:
    if mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000 or Image.MIME.get(image.format or "") != mime:
                raise ValueError("invalid_image")
            image.verify()
    elif mime in {"video/mp4", "video/quicktime", "video/webm"}:
        if not (data[4:8] == b"ftyp" or data.startswith(b"\x1aE\xdf\xa3")):
            raise ValueError("invalid_video")
    elif mime == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("invalid_document")
    elif mime.startswith("audio/"):
        if not data:
            raise ValueError("invalid_audio")
    else:
        raise ValueError("unsupported_media")


async def upload_media(
    article: Article, client: NotionClient, worker: WorkerClient
) -> tuple[dict[str, Any], list[str]]:
    output, warnings = {}, []
    for asset in article.media:
        parts = [(asset.media_id, asset.mime_type, asset.size_bytes, media_kind(asset.type, asset.mime_type))]
        if asset.video_media_id:
            parts.append((asset.video_media_id, asset.video_mime_type or "video/mp4", asset.video_size_bytes, "video"))
        for media_id, mime, size, kind in parts:
            if media_id in output:
                continue
            try:
                if not asset.lease_id or not mime:
                    raise ValueError("media_unavailable")
                if size > 20_000_000:
                    raise WorkerError("media_too_large")
                data = await worker.download_media(asset.lease_id, media_id, mime=mime)
                if kind == "file" and mime != "application/pdf":
                    if not data or asset.type != "document":
                        raise ValueError("invalid_document")
                else:
                    await asyncio.to_thread(validate, data, mime)
                # Bytes stay in bounded memory; no user-selected temp path is ever written.
                upload_id = await client.upload(data, mime, f"{media_id}.{mime.split('/')[-1]}")
                output[media_id] = {
                    "object": "block",
                    "type": kind,
                    kind: {"type": "file_upload", "file_upload": {"id": upload_id}},
                }
            except (NotionError, WorkerError, ValueError, OSError) as error:
                if isinstance(error, NotionError) and error.code == "authorization_expired":
                    raise
                warnings.append("media_failed")
                if kind == "image" and (url := safe_url(asset.source_url)):
                    output[media_id] = {
                        "object": "block",
                        "type": "image",
                        "image": {"type": "external", "external": {"url": url}},
                    }
                else:
                    output[media_id] = text_blocks(
                        f"{asset.filename or kind}：未能上传；原文：{article.source.original_url}"
                    )[0]
    return output, warnings
