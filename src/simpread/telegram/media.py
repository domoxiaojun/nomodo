"""Prepare media without chat sends; publish one native Rich Message per article."""

from __future__ import annotations

import io
from typing import Any

from pyrogram import enums, types
from pyrogram.errors import BadRequest

from simpread.domain import Article
from simpread.worker import WorkerClient, WorkerError

from .callbacks import preview, truncate
from .presentation import article_rich

# Conservative application limits leave room for rendering and other active users.
MAX_MEDIA = 20
MAX_TOTAL_BYTES = 40_000_000


class DeliveryUncertain(RuntimeError):
    """Do not send a second result when Telegram may have accepted the first."""


async def prepare_media(article: Article, worker: WorkerClient) -> tuple[list[Any], list[str]]:
    blocks: list[Any] = []
    failed = total = attempted = 0
    for asset in article.media:
        parts = [(asset.media_id, asset.mime_type, asset.size_bytes, asset.type)]
        if asset.video_media_id:
            parts.append((asset.video_media_id, asset.video_mime_type, asset.video_size_bytes, 'video'))
        for media_id, mime, size, kind in parts:
            attempted += 1
            if attempted > MAX_MEDIA or total >= MAX_TOTAL_BYTES:
                failed += 1
                continue
            try:
                if not asset.lease_id or size > 20_000_000 or size > MAX_TOTAL_BYTES - total:
                    raise WorkerError('media_too_large')
                data = await worker.download_media(asset.lease_id, media_id, mime=mime,
                                                   limit=min(20_000_000, MAX_TOTAL_BYTES - total))
                total += len(data)
                content = io.BytesIO(data)
                if kind in {'photo', 'live_photo'}:
                    content.name = 'photo.jpg'
                    blocks.append(types.InputRichBlockPhoto(types.InputMediaPhoto(content)))
                elif kind == 'animation':
                    content.name = 'animation.mp4'
                    blocks.append(types.InputRichBlockAnimation(types.InputMediaAnimation(content)))
                elif kind == 'video' or (mime or '').startswith('video/'):
                    content.name = 'video.mp4'
                    blocks.append(types.InputRichBlockVideo(types.InputMediaVideo(
                        content, width=asset.width or 0, height=asset.height or 0,
                        duration=int(asset.duration_seconds or 0))))
                else:
                    content.name = 'attachment'
                    blocks.append(types.InputRichBlockDocument(types.InputMediaDocument(content)))
            except WorkerError:
                failed += 1
    warnings = [f'{failed} 个媒体未嵌入（大小限制或下载失败），可通过「查看原文」访问。'] if failed else []
    return blocks, warnings


async def send_preview(message: Any, article: Article, worker: WorkerClient,
                       keyboard: types.InlineKeyboardMarkup, lifetime_minutes: int = 30) -> None:
    blocks, warnings = await prepare_media(article, worker)
    try:
        result = await message.reply_rich(article_rich(article, blocks, warnings, lifetime_minutes),
                                          reply_markup=keyboard)
        if result is None:
            raise DeliveryUncertain('Telegram 未返回发送结果')
    except BadRequest as error:
        # Only definite Rich/media rejection can fall back; transport errors must not resend.
        if not any(word in (error.ID or "") for word in ('RICH', 'MEDIA', 'PHOTO', 'IMAGE', 'VIDEO', 'DOCUMENT')):
            raise
        fallback = await message.reply_text(
            truncate(preview(article), 3000) + '\n\nTelegram 未接受富媒体展示。点击「导出」获取全文，媒体可查看原文。',
            parse_mode=enums.ParseMode.DISABLED, link_preview_options=types.LinkPreviewOptions(is_disabled=True),
            reply_markup=keyboard,
        )
        if fallback is None:
            raise DeliveryUncertain("Telegram 未返回发送结果") from None
    except Exception as error:
        raise DeliveryUncertain('发送结果暂时无法确认，请先查看聊天；原文已保留在「最近文章」。') from error
