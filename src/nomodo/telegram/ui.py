"""Small reusable status messages and owner-bound navigation panels."""

import re
import time
from dataclasses import dataclass, field
from typing import Any

from pyrogram import enums, types
from pyrogram.errors import BadRequest, MessageNotModified

from .callbacks import truncate


def buttons(rows: list[list[tuple[str, str]]]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([
        [types.InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows
    ])


def menu(user_id: int) -> types.InlineKeyboardMarkup:
    prefix = f'ui:{user_id}:'
    return buttons([
        [('发送链接', prefix + 'read'), ('最近文章', prefix + 'articles')],
        [('AI 设置', prefix + 'settings'), ('Notion 连接与设置', prefix + 'notion')],
        [('当前任务', prefix + 'status'), ('使用帮助', prefix + 'help')],
    ])


def safe_filename(title: str, extension: str) -> str:
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', title).strip(' .')[:70].rstrip(' .')
    name = name.encode()[:180].decode('utf-8', errors='ignore').rstrip(' .')
    return (name or '文章') + '.' + extension


@dataclass
class Activity:
    chat_id: int
    label: str = '准备处理'
    started: float = field(default_factory=time.monotonic)

    def elapsed(self) -> str:
        seconds = max(0, int(time.monotonic() - self.started))
        return f'{seconds // 60} 分 {seconds % 60} 秒' if seconds >= 60 else f'{seconds} 秒'


class Panel:
    """Menus edit their existing message; content results remain separate."""
    def __init__(self, message: Any) -> None:
        self.original = message
        self.chat = message.chat

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        try:
            return await self.original.edit_text(text, **kwargs)
        except MessageNotModified:
            return self.original
        except BadRequest as error:
            if error.ID not in {'MESSAGE_ID_INVALID', 'MESSAGE_EDIT_TIME_EXPIRED', 'MESSAGE_AUTHOR_REQUIRED'}:
                raise
            return await self.original.reply_text(text, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


class Progress:
    def __init__(self, message: Any, user_id: int, activity: Activity | None = None) -> None:
        self.origin, self.user_id, self.message = message, user_id, None
        self.activity = activity

    async def update(self, text: str, *, done: bool = False, markup: Any = None) -> None:
        if self.activity:
            self.activity.label = text.split('\n', 1)[0]
        kwargs = {
            'parse_mode': enums.ParseMode.DISABLED,
            'link_preview_options': types.LinkPreviewOptions(is_disabled=True),
            'reply_markup': markup if done else buttons([[('取消任务', f'ui:{self.user_id}:cancel'),
                                                          ('查看状态', f'ui:{self.user_id}:status')]]),
        }
        if self.message is None:
            self.message = await self.origin.reply_text(truncate(text, 3500), **kwargs)
        else:
            try:
                await self.message.edit_text(truncate(text, 3500), **kwargs)
            except MessageNotModified:
                pass
            except Exception:
                # A missing progress message must not cancel a successful content operation.
                pass

    async def clear(self) -> None:
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                await self.update('已完成，结果见下方。', done=True)
