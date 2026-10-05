"""Ordered, bounded albums of Worker-prepared media; no re-encoding or source fetching."""

import io
from typing import Any

from pyrogram.types import InputMediaPhoto, InputMediaVideo

from simpread.domain import Article
from simpread.domain.media import media_kind
from simpread.worker import WorkerClient, WorkerError


async def send_preview(message: Any, article: Article, worker: WorkerClient) -> int:
    failed = 0
    album: list[InputMediaPhoto | InputMediaVideo] = []
    album_bytes = 0

    async def flush() -> None:
        nonlocal failed, album_bytes
        if not album:
            return
        try:
            if len(album) > 1:
                await message.reply_media_group(album)
            elif isinstance(album[0], InputMediaPhoto):
                await message.reply_photo(album[0].media)
            else:
                await message.reply_video(album[0].media)
        except Exception:
            # A failed response may have been delivered. Do not replay the album and duplicate it.
            failed += len(album)
        finally:
            album.clear()
            album_bytes = 0

    for asset in article.media:
        parts = [(asset.media_id, asset.mime_type, asset.size_bytes, asset.type in {"photo", "live_photo"})]
        if asset.video_media_id:
            parts.append((asset.video_media_id, asset.video_mime_type or "video/mp4", asset.video_size_bytes, False))
        for media_id, mime, size, photo in parts:
            try:
                if not asset.lease_id:
                    raise WorkerError("media_unavailable")
                kind = media_kind(asset.type, mime)
                if size > 20_000_000:
                    await flush()
                    async with worker.media_file(asset.lease_id, media_id, mime=mime) as path:
                        if kind == "video":
                            await message.reply_video(str(path))
                        elif kind == "audio":
                            await message.reply_audio(str(path))
                        else:
                            await message.reply_document(str(path), file_name=asset.filename or "attachment")
                    continue
                if album and (len(album) == 10 or album_bytes + size > 20_000_000):
                    await flush()
                try:
                    data = await worker.download_media(asset.lease_id, media_id, mime=mime)
                except WorkerError as error:
                    if error.code != "media_too_large":
                        raise
                    await flush()
                    async with worker.media_file(asset.lease_id, media_id, mime=mime) as path:
                        if kind == "video":
                            await message.reply_video(str(path))
                        elif kind == "audio":
                            await message.reply_audio(str(path))
                        else:
                            await message.reply_document(str(path), file_name=asset.filename or "attachment")
                    continue
                if album and album_bytes + len(data) > 20_000_000:
                    await flush()
                content = io.BytesIO(data)
                content.name = asset.filename or ("preview.jpg" if photo else "preview.mp4")
                if not photo and asset.video_media_id == media_id:
                    content.name = "preview.mp4"
                if asset.type == "animation" and mime in {"image/gif", "video/mp4"}:
                    await flush()
                    await message.reply_animation(content)
                elif photo:
                    album.append(InputMediaPhoto(content))
                    album_bytes += len(data)
                elif mime and mime.startswith("video/"):
                    album.append(InputMediaVideo(content))
                    album_bytes += len(data)
                else:
                    await flush()
                    if kind == "audio":
                        await message.reply_audio(content)
                        continue
                    await message.reply_document(content, file_name=asset.filename or "attachment")
            except Exception:
                failed += 1
    await flush()
    return failed
