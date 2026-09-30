"""Send bounded Worker-prepared media unchanged. No local re-encoding or parsing."""

import io
from typing import Any

from simpread.domain import Article
from simpread.worker import WorkerClient, WorkerError


async def send_preview(message: Any, article: Article, worker: WorkerClient) -> int:
    failed = 0
    for asset in article.media[:10]:
        parts = [(asset.media_id, asset.mime_type, asset.size_bytes, asset.type in {"photo", "live_photo"})]
        if asset.video_media_id:
            parts.append((asset.video_media_id, asset.video_mime_type, asset.video_size_bytes, False))
        for media_id, mime, size, photo in parts:
            try:
                if not asset.lease_id or size > 20_000_000:
                    raise WorkerError("media_too_large")
                data = await worker.download_media(asset.lease_id, media_id, mime=mime)
                content = io.BytesIO(data)
                content.name = "preview." + ("jpg" if photo else "mp4")
                if photo:
                    await message.reply_photo(content)
                elif mime and mime.startswith("video/"):
                    await message.reply_video(content)
                else:
                    await message.reply_document(content, file_name=asset.filename or "attachment")
            except Exception:
                failed += 1
    return failed + max(0, len(article.media) - 10)
