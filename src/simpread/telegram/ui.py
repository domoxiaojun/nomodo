"""Small reusable status messages and owner-bound navigation panels."""

from typing import Any

from pyrogram import enums, types
from pyrogram.errors import MessageNotModified

from .callbacks import truncate


def buttons(rows: list[list[tuple[str, str]]]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([
        [types.InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows
    ])


def menu(user_id: int) -> types.InlineKeyboardMarkup:
    prefix = f'ui:{user_id}:'
    return buttons([
        [('发送链接', prefix + 'read'), ('最近文章', prefix + 'articles')],
        [('AI 设置', prefix + 'settings'), ('Notion', prefix + 'notion')],
        [('使用帮助', prefix + 'help'), ('支持的平台', prefix + 'platforms')],
    ])


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

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


class Progress:
    def __init__(self, message: Any, user_id: int) -> None:
        self.origin, self.user_id, self.message = message, user_id, None

    async def update(self, text: str, *, done: bool = False) -> None:
        kwargs = {
            'parse_mode': enums.ParseMode.DISABLED,
            'link_preview_options': types.LinkPreviewOptions(is_disabled=True),
            'reply_markup': None if done else buttons([[('取消任务', f'ui:{self.user_id}:cancel')]]),
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
