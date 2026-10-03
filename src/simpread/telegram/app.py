"""Single update receiver with explicit external-write confirmation and durable state."""

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from pyrogram import Client, enums, filters, idle
from pyrogram.errors import RPCError
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from simpread.config import Settings
from simpread.domain import Article
from simpread.integrations.notion import NotionError, NotionService, NotionStore
from simpread.integrations.notion.client import notion_id
from simpread.integrations.openai import ActionPlan, Executor, LLMError, ResponsesClient
from simpread.integrations.openai.client import redact, sanitize
from simpread.oauth.server import OAuthServer
from simpread.storage import PendingStore
from simpread.worker import WorkerClient, WorkerError

from .callbacks import make_callback, parse_callback, truncate
from .guidance import ERRORS, HELP
from .input import message_urls
from .media import DeliveryUncertain, send_preview
from .presentation import ai_rich
from .ui import Activity, Panel, Progress, buttons, menu, safe_filename

BOT_COMMANDS = [
    BotCommand("start", "开始使用与授权说明"),
    BotCommand("help", "查看使用帮助"),
    BotCommand("id", "查看自己的 Telegram 用户 ID"),
    BotCommand("read", "解析链接：/read <网址>"),
    BotCommand("status", "查看当前任务和进度"),
    BotCommand("articles", "查看最近解析的文章"),
    BotCommand("platforms", "查看当前支持的平台"),
    BotCommand("translate", "翻译当前选中的文章"),
    BotCommand("cancel", "取消当前任务或输入，保留文章"),
    BotCommand("settings", "查看个人设置"),
    BotCommand("summary", "摘要当前选中的文章"),
    BotCommand("agent", "规划文章处理任务（需启用 AI）"),
    BotCommand("notion", "Notion 授权与保存设置（需配置）"),
]

MESSAGES = {
    "unsupported_url": "暂不支持这个链接，请查看支持的平台后重试。",
    "too_many_links": "一次最多处理 10 个链接，请分批发送。",
    "worker_unavailable": "解析服务暂时无法连接，请稍后重新发送链接。",
    "parse_failed": "这次解析未成功；请确认链接可公开访问，或检查平台登录凭据。",
    "llm_failed": "AI 暂时未返回有效结果。原文已保留，可重试或导出原文。",
    "llm_auth_failed": "AI Key 无效或已过期，请管理员更新接口配置。",
    "llm_timeout": "AI 生成超时，原文已保留；可以重试或换一篇较短的文章。",
    "input_too_large": "文章超出当前 AI 处理上限，仍可导出完整原文。",
    "llm_config_incomplete": "AI 服务尚未配置完成，请联系管理员。",
    "worker_version_mismatch": "Worker 与 Reader 的 ParseHub 版本不匹配。",
    "authorization_required": "请先 /notion connect 授权。",
    "authorization_expired": "Notion 授权已失效，请重新连接。",
    "target_required": "请先 /notion targets 并用 /notion target <id> 选择目标。",
    "schema_confirmation_required": "需要确认添加数据库属性，请重新点击保存按钮。",
    "write_outcome_unknown": "上次写入结果不明，已禁止重复创建。请在 Notion 核对页面，再用 /notion recover <page-id>。",
    "rate_limited": "Notion 暂时限流，请稍后重试。",
    "llm_disabled": "LLM 未启用；请检查服务器开关或在私聊使用 /settings llm on。",
}


def private(message: Any) -> bool:
    return message is not None and message.chat.type in {"private", enums.ChatType.PRIVATE}


async def reply(message: Any, text: str, **kwargs: Any) -> Any:
    return await message.reply_text(
        truncate(text, 3800),
        parse_mode=enums.ParseMode.DISABLED,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        **kwargs,
    )


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pending = PendingStore(settings.reader_database_path, settings.reader_pending_ttl)
        self.worker = WorkerClient(
            settings.parsehub_worker_url,
            settings.parsehub_worker_secret.get_secret_value(),
            settings.reader_bot_token.get_secret_value().split(":", 1)[0],
            settings.parsehub_worker_timeout_seconds,
        )
        self.secrets = (
            NotionStore(settings.notion_database_path, settings.notion_credentials_key.get_secret_value())
            if settings.notion_credentials_key.get_secret_value()
            else None
        )
        self.notion = NotionService(self.secrets, self.worker) if self.secrets else None
        self.oauth = OAuthServer(settings, self.secrets) if settings.notion_client_id and self.secrets else None
        self.locks: dict[int, asyncio.Lock] = {}
        self.active: dict[int, asyncio.Task[Any]] = {}
        self.active_chat: dict[int, int] = {}
        self.activities: dict[int, Activity] = {}
        self.model_catalog: dict[int, tuple[float, list[str]]] = {}
        self.bot: Any = None
        self.capacity = asyncio.Semaphore(2)

    def token_button(self, user_id: int, chat_id: int, label: str, prefix: str,
                     payload: dict[str, Any]) -> tuple[str, str]:
        return label, prefix + ':' + self.pending.approve(user_id, chat_id, payload)

    async def problem(self, user_id: int, message: Any, code: str, key: str | None = None) -> None:
        title, detail = ERRORS.get(code, ('暂时无法完成操作', MESSAGES.get(code, '请稍后重试，已有内容仍保留。')))
        rows: list[list[tuple[str, str]]] = []
        if private(message):
            if code in {'authorization_required', 'authorization_expired'}:
                rows.append([('连接 Notion', f'ui:{user_id}:connect')])
            elif code == 'target_required':
                rows.append([('选择保存位置', f'ui:{user_id}:targets')])
            elif code.startswith('llm_'):
                rows.append([('AI 设置', f'ui:{user_id}:settings')])
            elif code == 'unsupported_url':
                rows.append([('支持的平台', f'ui:{user_id}:platforms')])
            rows.append([('最近文章', f'ui:{user_id}:articles'), ('返回首页', f'ui:{user_id}:home')])
        if key and self.pending.get(user_id, message.chat.id, key):
            rows.insert(0, [('查看原文', make_callback(key, 'open')), ('导出全文', make_callback(key, 'export'))])
        await reply(message, title + '\n\n' + detail, reply_markup=buttons(rows) if rows else None)

    async def task_status(self, user_id: int, message: Any) -> None:
        task = self.active.get(user_id)
        activity = self.activities.get(user_id)
        running = bool(task and not task.done() and task is not asyncio.current_task())
        if running and activity and activity.chat_id == message.chat.id:
            text = f'当前任务\n\n{activity.label}\n已用时 {activity.elapsed()}\n完成后会在原位置显示结果。'
            rows = [[('刷新状态', f'ui:{user_id}:status'), ('取消任务', f'ui:{user_id}:cancel')]]
        elif running:
            text = '你在另一个聊天中的任务仍在进行。\n请回到原聊天查看进度或取消。'
            rows = []
        else:
            text = '当前没有正在执行的任务。\n发送链接开始阅读，或继续之前的文章。'
            rows = []
        if private(message):
            rows.append([('最近文章', f'ui:{user_id}:articles'), ('返回首页', f'ui:{user_id}:home')])
        await reply(message, text, reply_markup=buttons(rows))

    async def busy(self, user_id: int, message: Any) -> None:
        rows: list[list[tuple[str, str]]] = []
        if private(message):
            rows.append([('查看当前任务', f'ui:{user_id}:status'), ('取消任务', f'ui:{user_id}:cancel')])
        try:
            urls = message_urls(message)
        except WorkerError:
            urls = []
        if urls:
            rows.append([self.token_button(user_id, message.chat.id, '任务结束后解析这批链接', 'retry',
                                           {'kind': 'retry_parse', 'urls': urls})])
        await reply(message, '当前任务还在进行，这条请求尚未开始。\n可以查看进度，或等任务结束后继续。',
                    reply_markup=buttons(rows))

    async def help_section(self, user_id: int, message: Any, section: str = '') -> None:
        title, body = HELP.get(section, ('使用帮助', '按当前需要选择一项。所有常用操作都可以从按钮进入。'))
        rows = [[('解析与阅读', f'ui:{user_id}:guide:read'), ('AI 操作', f'ui:{user_id}:guide:ai')],
                [('Notion 保存', f'ui:{user_id}:guide:notion'), ('任务与导出', f'ui:{user_id}:guide:controls')],
                [('返回首页', f'ui:{user_id}:home')]]
        await reply(message, title + '\n\n' + body, reply_markup=buttons(rows) if private(message) else None)

    async def model_choices(self, user_id: int, message: Any, offset: int = 0, refresh: bool = False) -> None:
        snapshot = self.model_catalog.get(user_id)
        if refresh or not snapshot or time.monotonic() - snapshot[0] > 300:
            key = ((self.secrets.secret(user_id, 'openai') if self.secrets else '')
                   or self.settings.openai_api_key.get_secret_value())
            if not key:
                await self.problem(user_id, message, 'llm_config_incomplete')
                return
            client = ResponsesClient(key, self.settings.openai_model, 'model-list',
                                     base_url=self.settings.openai_base_url, timeout=15)
            try:
                page = await client.client.models.list()
                models = sorted({item.id for item in page.data if re.fullmatch(r'[\w./:-]{1,100}', item.id)})
                if self.settings.openai_model in models:
                    models.remove(self.settings.openai_model)
                    models.insert(0, self.settings.openai_model)
                self.model_catalog[user_id] = time.monotonic(), models[:200]
            except Exception:
                await reply(message, '暂时无法读取模型列表。\n可以重试，或手动输入服务支持的模型名。',
                            reply_markup=buttons([
                    [('重试列表', f'ui:{user_id}:models_refresh'), ('手动输入', f'ui:{user_id}:model_input')],
                    [('返回设置', f'ui:{user_id}:settings')],
                ]))
                return
            finally:
                await client.close()
        models = self.model_catalog[user_id][1]
        offset = min(max(0, offset), max(0, ((len(models) - 1) // 6) * 6))
        current = self.pending.preferences(user_id).get('model') or self.settings.openai_model
        rows = [[self.token_button(user_id, message.chat.id,
                                   ('✓ ' if model == current else '') + truncate(model, 45), 'choice',
                                   {'kind': 'model', 'model': model})] for model in models[offset:offset + 6]]
        nav = []
        if offset:
            nav.append(('上一页', f'ui:{user_id}:models:{offset - 6}'))
        if offset + 6 < len(models):
            nav.append(('下一页', f'ui:{user_id}:models:{offset + 6}'))
        if nav:
            rows.append(nav)
        rows.extend([[('刷新列表', f'ui:{user_id}:models_refresh'), ('手动输入', f'ui:{user_id}:model_input')],
                     [('默认模型', f'ui:{user_id}:default_model'), ('返回设置', f'ui:{user_id}:settings')]])
        await reply(message, f'选择模型 · 当前 {current}\n\n列表来自你的接口。模型须支持结构化文本输出。',
                    reply_markup=buttons(rows))

    def recovery_button(self, user_id: int, message: Any, key: str, target_id: str) -> tuple[str, str]:
        return self.token_button(user_id, message.chat.id, '核对 Notion 页面并恢复', 'recover',
                                 {'kind': 'recover_input', 'key': key, 'target': target_id})

    async def request_recovery(self, user_id: int, message: Any, state: dict[str, Any], value: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme:
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or not (
                parsed.hostname in {'notion.so', 'notion.site', 'www.notion.so', 'www.notion.site',
                                    'notion.com', 'www.notion.com', 'app.notion.com'}
                or parsed.hostname.endswith('.notion.site')
            ):
                raise ValueError('invalid_notion_link')
            value = parsed.path.rstrip('/').rsplit('/', 1)[-1][-36:]
            match = re.search(r'([0-9a-fA-F]{32}|[0-9a-fA-F-]{36})$', value)
            if not match:
                raise ValueError('invalid_notion_link')
            value = match[0]
        page_id = notion_id(value)
        _, article, _ = self.selected(user_id, message, state['key'])
        job = self.secrets.export_status(user_id, article.content_hash, state['target']) if self.secrets else None
        if not job or job.get('status') != 'unknown':
            raise ValueError('recovery_not_needed')
        self.pending.preferences(user_id, {'input': None})
        await self.approval(message, user_id,
                            {'kind': 'recover', 'key': state['key'], 'target': state['target'], 'page': page_id},
                            '确认核对这篇 Notion 页面？\n只会检查已有内容与保存进度，不会重新创建页面。')

    async def close(self) -> None:
        await self.worker.close()
        self.pending.close()
        if self.secrets:
            self.secrets.close()

    def keyboard(self, key: str, user_id: int = 0, *, private_chat: bool = True,
                 expanded: bool = False) -> InlineKeyboardMarkup:
        actions: list[tuple[str, str]] = []
        if private_chat and self.settings.llm_enabled and self.pending.preferences(user_id).get("llm"):
            actions.extend([("摘要", "summary"), ("翻译", "translate")])
        if private_chat and self.notion:
            actions.append(("保存到 Notion", "save"))
        actions.append(("导出全文", "export"))
        if expanded and private_chat and self.settings.llm_enabled:
            actions.extend([("提取标签", "tags"), ("标题建议", "title"), ("整理排版", "normalize_markdown")])
        if expanded and private_chat:
            actions.append(("重新解析链接", "refresh"))
            actions.append(("移除这篇文章", "remove"))
        if private_chat:
            actions.append(("收起" if expanded else "更多操作", "back" if expanded else "more"))
        return buttons([
            [(label, make_callback(key, action)) for label, action in actions[i:i + 2]]
            for i in range(0, len(actions), 2)
        ])

    async def home(self, user_id: int, message: Any, *, help_page: bool = False) -> None:
        if help_page:
            await self.help_section(user_id, message)
            return
        if not help_page:
            ai = "已开启" if self.settings.llm_enabled and self.pending.preferences(user_id).get("llm") else "已关闭"
            credential = self.secrets.credential(user_id) if self.secrets else None
            targets = self.secrets.targets(user_id) if self.secrets and credential else []
            chosen = next((t for t in targets if t.get("default")), None)
            notion = ("已连接 · " + (chosen["title"] or "已选保存位置") if chosen else
                      "已连接 · 待选择保存位置" if credential else "等待连接" if self.oauth else "管理员尚未配置")
            text = ("阅读助手\n\n发送一个文章或视频链接，开始解析。\n"
                    "结果中可直接阅读、查看媒体、生成摘要或导出全文。\n\n"
                    f"AI：{ai}\nNotion：{notion}\n"
                    f"文章与按钮有效期约 {self.settings.reader_pending_ttl // 60} 分钟。")
        if not private(message):
            text = "阅读助手\n\n在群内发送链接即可解析和导出。\nAI、个人设置和 Notion 保存请私聊机器人。"
        markup = menu(user_id)
        latest = self.current_key(user_id, message.chat.id)
        if latest and private(message):
            _, current, _ = self.selected(user_id, message, latest)
            markup.inline_keyboard.insert(0, [InlineKeyboardButton(
                "继续阅读 · " + truncate(current.title or "最近文章", 28),
                callback_data=make_callback(latest, "open"))])
        await reply(message, text, reply_markup=markup if private(message) else None)

    async def articles(self, user_id: int, message: Any, offset: int = 0) -> None:
        entries = self.pending.recent(user_id, message.chat.id)
        offset = min(max(0, offset), max(0, ((len(entries) - 1) // 8) * 8))
        page = entries[offset:offset + 8]
        current = self.current_key(user_id, message.chat.id)
        rows = [[(("▶ " if item["id"] == current else "") + truncate(item["title"] or "无标题", 32)
                  + f" · {max(1, int((item['expires'] - time.time()) / 60))}分",
                  make_callback(item["id"], "open"))] for item in page]
        nav = []
        if offset:
            nav.append(("上一页", f"ui:{user_id}:articles:{max(0, offset - 8)}"))
        if offset + 8 < len(entries):
            nav.append(("下一页", f"ui:{user_id}:articles:{offset + 8}"))
        if nav:
            rows.append(nav)
        rows.append([("返回首页", f"ui:{user_id}:home")])
        text = (f"最近文章 · {len(entries)} 篇\n▶ 表示当前文章；时间为剩余可操作时间。" if entries else
                "还没有可用文章。\n请直接发送链接，解析后会出现在这里。")
        await reply(message, text, reply_markup=buttons(rows))

    async def platforms(self, user_id: int, message: Any) -> None:
        snapshot = await self.worker.capabilities()
        await reply(message, "当前支持的平台\n\n" + snapshot.names +
                    "\n\n直接发送原文链接即可。部分平台需要管理员配置 Cookie，登录受限内容可能无法解析。",
                    reply_markup=menu(user_id) if private(message) else None)

    async def approval(self, message: Any, user_id: int, payload: dict[str, Any], description: str) -> None:
        key = self.pending.approve(user_id, message.chat.id, payload)
        label = {"save": "确认保存", "remove": "确认移除", "recover": "确认核对",
                 "disconnect_notion": "确认断开"}.get(payload.get("kind", ""), "确认执行")
        await reply(
            message,
            description,
            reply_markup=buttons([[(label, f"confirm:{key}"), ("取消", f"dismiss:{key}")]]),
        )

    def current_key(self, user_id: int, chat_id: int) -> str | None:
        selected = self.pending.preferences(user_id).get("selected_articles", {}).get(str(chat_id))
        if selected and self.pending.get(user_id, chat_id, selected):
            return str(selected)
        return self.pending.latest(user_id, chat_id)

    def selected(self, user_id: int, message: Any, key: str | None = None) -> tuple[str, Article, tuple[str, ...]]:
        key = key or self.current_key(user_id, message.chat.id)
        result = self.pending.get(user_id, message.chat.id, key) if key else None
        if not key or not result:
            raise ValueError("内容已过期，请重新解析。")
        selections = self.pending.preferences(user_id).get("selected_articles", {})
        if selections.get(str(message.chat.id)) != key:
            selections.pop(str(message.chat.id), None)
            selections[str(message.chat.id)] = key
            self.pending.preferences(user_id, {"selected_articles": dict(list(selections.items())[-32:])})
        return key, result[0], result[1]

    async def read(self, user_id: int, message: Any, urls: list[str], *, force: bool = False) -> None:
        progress = Progress(message, user_id, self.activities.get(user_id))
        succeeded = 0
        failures: list[str] = []
        retry_urls: list[str] = []
        for index, url in enumerate(urls, 1):
            prepared = None
            key = None
            try:
                await progress.update(f"正在解析 {index}/{len(urls)}\n{url}\n页面和媒体准备可能需要一些时间。")
                cached_key = self.pending.find_url(user_id, message.chat.id, url) if not force else None
                if cached_key:
                    _, cached_article, _ = self.selected(user_id, message, cached_key)
                    await progress.update(f"正在打开已有结果 {index}/{len(urls)}")
                    await send_preview(message, cached_article, self.worker,
                                       self.keyboard(cached_key, user_id, private_chat=private(message)),
                                       self.pending.minutes_left(user_id, message.chat.id, cached_key))
                    succeeded += 1
                    continue
                if self.pending.count(user_id) >= self.settings.reader_max_pending_per_user:
                    failures.append("待处理文章已达上限。可在文章「更多操作」中移除旧文章，或等待其过期。")
                    break
                async with self.capacity:
                    prepared = await self.worker.prepare(url)
                    await progress.update(
                        f"正在整理 {index}/{len(urls)} · {prepared.article.title or '文章'}\n正文和媒体会合并展示。")
                    key = self.pending.put(user_id, message.chat.id, prepared.article,
                                           prepared.lease_ids, prepared.raw_result)
                    self.selected(user_id, message, key)
                    await send_preview(message, prepared.article, self.worker,
                                       self.keyboard(key, user_id, private_chat=private(message)),
                                       self.settings.reader_pending_ttl // 60)
                succeeded += 1
            except DeliveryUncertain:
                failures.append(f"第 {index} 篇发送结果未确认，请先查看聊天；可从 /articles 找回原文。")
            except WorkerError as error:
                retry_urls.append(url)
                failures.append(f"第 {index} 篇：" + MESSAGES.get(error.code, "解析未成功，请稍后重试。"))
            except asyncio.CancelledError:
                if key:
                    self.pending.delete(key)
                    key = None
                await progress.update("解析已取消。已发送的其他文章仍可使用。", done=True)
                return
            except Exception:
                retry_urls.append(url)
                failures.append(f"第 {index} 篇未能完成发送，请稍后重试。")
                if key:
                    self.pending.delete(key)
                    key = None
            finally:
                if prepared and key is None:
                    for lease in prepared.lease_ids:
                        try:
                            await self.worker.release(lease)
                        except WorkerError:
                            pass
        if failures:
            rows = [[self.token_button(user_id, message.chat.id, "重试未完成的链接", "retry",
                                       {"kind": "retry_parse", "urls": retry_urls})]] if retry_urls else []
            if private(message):
                rows.append([("最近文章", f"ui:{user_id}:articles"), ("支持的平台", f"ui:{user_id}:platforms")])
            await progress.update(f"处理完成：成功 {succeeded}/{len(urls)}\n" + "\n".join(failures),
                                  done=True, markup=buttons(rows))
        else:
            await progress.clear()

    async def file(self, message: Any, article: Article, action: str) -> None:
        content = article.markdown if action == "markdown" else article.html
        stream = io.BytesIO(content.encode("utf-8"))
        stream.name = safe_filename(article.title, "md" if action == "markdown" else "html")
        label = "Markdown · 适合编辑和复制" if action == "markdown" else "HTML · 可在浏览器离线阅读"
        await message.reply_document(stream, file_name=stream.name,
                                     caption=truncate(article.title or "文章", 250) + "\n完整原文 · " + label,
                                     parse_mode=enums.ParseMode.DISABLED)

    def llm_client(self, user_id: int, text: str) -> ResponsesClient:
        prefs = self.pending.preferences(user_id)
        if not self.settings.llm_enabled or not prefs.get("llm"):
            raise LLMError("llm_disabled")
        if len(text) > self.settings.llm_max_input_chars:
            raise LLMError("input_too_large")
        model = prefs.get("model") or self.settings.openai_model
        key = (
            self.secrets.secret(user_id, "openai") if self.secrets else ""
        ) or self.settings.openai_api_key.get_secret_value()
        if not key or not model:
            raise LLMError("llm_config_incomplete")
        return ResponsesClient(
            key,
            model,
            self.settings.reader_bot_token.get_secret_value(),
            timeout=self.settings.openai_timeout_seconds,
            max_input_chars=self.settings.llm_max_input_chars,
            max_output_tokens=self.settings.llm_max_output_tokens,
            reasoning_effort=prefs.get("reasoning_effort") or self.settings.llm_reasoning_effort,
            base_url=self.settings.openai_base_url,
        )

    async def show_ai(self, message: Any, key: str, operation: str, title: str, value: Any) -> None:
        text = " · ".join(value) if isinstance(value, list) else str(value)
        await message.reply_rich(ai_rich(title, text), reply_markup=buttons([
            [("重新生成", make_callback(key, "regen_" + operation)), ("导出 AI 结果", make_callback(key, "ai_file"))],
            [("查看原文", make_callback(key, "open"))],
        ]))

    async def enhance(self, user_id: int, message: Any, key: str, operation: str,
                      language: str | None = None, *, regenerate: bool = False) -> None:
        _, article, _ = self.selected(user_id, message, key)
        labels = {"summary": ("文章摘要", "summary"), "translate": ("全文翻译", "translated_markdown"),
                  "tags": ("文章标签", "tags"), "title": ("标题建议", "suggested_title"),
                  "normalize_markdown": ("整理后的文章", "normalized_markdown")}
        title, field = labels[operation]
        prefs = self.pending.preferences(user_id)
        target_language = language or prefs["language"]
        signature = json.dumps([prefs.get("model") or self.settings.openai_model,
                                prefs.get("reasoning_effort") or self.settings.llm_reasoning_effort,
                                target_language, self.settings.openai_base_url])
        cached = self.pending.cached_ai(user_id, key, field, signature)
        if cached and not regenerate:
            await self.show_ai(message, key, operation, title, cached)
            return
        client = self.llm_client(user_id, sanitize(article))
        progress = Progress(message, user_id)
        try:
            await progress.update(f"正在生成{title}…\n{article.title}\n复杂内容可能需要更长时间。")
            result = await client.enhance(article, operation, user_id, target_language)
            value = result.model_dump()[field]
            if not value or (isinstance(value, str) and not value.strip()):
                raise LLMError("llm_failed")
            self.selected(user_id, message, key)
            self.pending.save_ai(user_id, key, field, signature, value)
            await self.show_ai(message, key, operation, title, value)
            await progress.clear()
        except asyncio.CancelledError:
            await progress.update("AI 任务已取消，原文保留。", done=True)
        except LLMError as error:
            title, detail = ERRORS.get(str(error), ("AI 暂时未完成", "原文已保留，可稍后重试。"))
            await progress.update(title + "\n" + detail, done=True, markup=buttons([
                [("重试生成", make_callback(key, "regen_" + operation)), ("导出原文", make_callback(key, "export"))],
                [("AI 设置", f"ui:{user_id}:settings")],
            ]))
        except Exception:
            await progress.update("AI 结果未能展示；可用文章的「更多操作 → 导出 AI 结果」查看已保存内容。", done=True)
        finally:
            await client.close()

    async def save(self, user_id: int, message: Any, key: str, target_id: str) -> None:
        key, article, leases = self.selected(user_id, message, key)
        if not self.notion:
            raise NotionError("authorization_required")
        progress = Progress(message, user_id)
        await progress.update("正在保存到 Notion…\n大图或长文需要更多时间，请勿重复点击。")
        try:
            result = await self.notion.export_page(
                user_id, article, target_id, leases=leases, derived=self.pending.derived(user_id, key)
            )
        except asyncio.CancelledError:
            await progress.update("已停止等待 Notion 保存。部分写入可能已完成，"
                                  "请检查 Notion 后再决定是否重试。", done=True, markup=buttons([
                                      [self.recovery_button(user_id, message, key, target_id)],
                                      [("查看原文", make_callback(key, "open")),
                                       ("Notion 设置", f"ui:{user_id}:notion")],
                                  ]))
            return
        except NotionError as error:
            retry = self.token_button(user_id, message.chat.id, "重新确认保存", "saveagain",
                                      {"kind": "continue_save", "key": key, "target": target_id})
            rows = ([[self.recovery_button(user_id, message, key, target_id)]]
                    if error.code == "write_outcome_unknown" else [[retry]])
            rows.append([("Notion 设置", f"ui:{user_id}:notion"), ("查看原文", make_callback(key, "open"))])
            title, detail = ERRORS.get(error.code, ("Notion 保存未完成", "原文和媒体保留，请检查授权后重试。"))
            await progress.update(title + "\n" + detail, done=True, markup=buttons(rows))
            return
        await progress.update("已保存到 Notion。" + ("\n部分媒体以外链或说明保留。" if result["warnings"] else ""),
                              done=True, markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton("打开 Notion 页面", url=result["url"])],
                                  [InlineKeyboardButton("查看原文", callback_data=make_callback(key, "open"))],
                              ]))
        for lease in leases:
            try:
                await self.worker.release(lease)
            except WorkerError:
                pass
        self.pending.clear_leases(key)

    async def request_save(self, user_id: int, message: Any, key: str, target_id: str | None = None) -> None:
        _, article, _ = self.selected(user_id, message, key)
        self.pending.preferences(user_id, {"notion_resume": {"key": key, "chat_id": message.chat.id}})
        if not self.notion:
            raise NotionError("authorization_required")
        if self.secrets and not self.secrets.credential(user_id):
            await self.notion_command(user_id, message, ["connect"])
            return
        try:
            target = self.notion.target(user_id, target_id)
        except NotionError:
            await self.notion_targets(user_id, message)
            return
        missing = await self.notion.schema(user_id, target["id"])
        self.pending.preferences(user_id, {"notion_resume": None})
        description = f"确认保存到 Notion\n\n文章：{article.title or '无标题'}\n位置：{target['title'] or target['id']}"
        if missing:
            description += "\n同时添加缺少属性：" + "、".join(missing)
        await self.approval(
            message, user_id, {"kind": "save", "key": key, "target": target["id"], "add_schema": missing}, description
        )

    async def notion_command(self, user_id: int, message: Any, args: list[str]) -> None:
        if not self.notion or not self.secrets:
            await reply(message, "Notion 尚未由管理员配置。\n目前可以阅读、使用 AI 和导出文件。\n"
                        "管理员配置授权后，再从这里连接并选择保存位置。", reply_markup=menu(user_id))
            return
        command = args[0] if args else "status"
        if command == "connect":
            if not self.oauth:
                await reply(message, "管理员尚未配置 Notion OAuth。")
            else:
                await reply(message, "连接你的 Notion\n授权后返回这里，点击「选择保存位置」。",
                            reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("打开 Notion 授权", url=self.oauth.begin(user_id))],
                    [InlineKeyboardButton("选择保存位置", callback_data=f"ui:{user_id}:targets")],
                ]))
        elif command == "disconnect":
            self.secrets.disconnect(user_id)
            self.pending.preferences(user_id, {"notion_resume": None})
            await reply(message, "已移除本地授权与目标；可在 Notion 中撤销 Integration 权限。")
        elif command == "targets":
            await self.notion_targets(user_id, message)
        elif command == "target" and len(args) == 2:
            await self.select_notion_target(user_id, message, args[1])
        elif command == "recover" and len(args) == 2:
            key, _, _ = self.selected(user_id, message)
            target = self.notion.target(user_id)
            await self.approval(
                message,
                user_id,
                {"kind": "recover", "key": key, "target": target["id"], "page": notion_id(args[1])},
                "核对该页面的导出标记并恢复进度；随后可重试保存。",
            )
        else:
            credential = self.secrets.credential(user_id)
            await reply(message, ("已连接：" + credential[1] + "\n选择保存位置后，回到文章点击「保存到 Notion」。")
                        if credential else "尚未连接 Notion。请点击下方按钮完成授权。",
                        reply_markup=buttons([[(("选择保存位置" if credential else "连接 Notion"),
                                                 f"ui:{user_id}:" + ("targets" if credential else "connect"))],
                                              *([[("断开 Notion", f"ui:{user_id}:disconnect")]] if credential else []),
                                              [("返回首页", f"ui:{user_id}:home")]]))

    async def settings_command(self, user_id: int, message: Any, args: list[str]) -> None:
        update: dict[str, Any] = {}
        if len(args) == 2 and args[0] == "llm" and args[1] in {"on", "off"}:
            update["llm"] = args[1] == "on"
        elif len(args) == 2 and args[0] == "reasoning" and args[1] in {"low", "medium", "high"}:
            update["reasoning_effort"] = args[1]
        elif len(args) == 2 and args[0] == "language" and re.fullmatch(r"[\w-]{1,40}", args[1]):
            update["language"] = args[1]
        elif len(args) == 2 and args[0] == "model":
            if not re.fullmatch(r"[\w./:-]{1,100}", args[1]):
                raise ValueError("模型名无效。")
            update = {"model": args[1]}
        elif len(args) == 2 and args[0] == "key":
            try:
                await message.delete()
            except Exception:
                pass
            if not self.secrets:
                raise ValueError("管理员需先配置凭据加密密钥。")
            self.secrets.secret(user_id, "openai", "" if args[1] == "clear" else args[1])
            self.model_catalog.pop(user_id, None)
            await self.bot.send_message(user_id, "个人 OpenAI Key 已更新。", parse_mode=enums.ParseMode.DISABLED)
            return
        if args and not update:
            await reply(message, "设置格式：/settings model gpt-6.1-sol\n语言：/settings language zh-CN\n"
                        "也可以直接点击下方按钮。", reply_markup=menu(user_id))
            return
        prefs = self.pending.preferences(user_id, update)
        await reply(
            message,
            f"AI：{'开启' if self.settings.llm_enabled and prefs['llm'] else '关闭'}；语言：{prefs['language']}；"
            f"模型：{prefs['model'] or self.settings.openai_model}；"
            f"思考强度：{prefs.get('reasoning_effort') or self.settings.llm_reasoning_effort or '模型默认'}\n"
            "/settings llm on|off\n/settings language zh-CN\n/settings model <模型>\n"
            "默认无需个人 Key。修改模型：/settings model <模型名>",
            reply_markup=buttons([
                [("关闭 AI" if prefs["llm"] else "开启 AI", f"ui:{user_id}:llm:" + ("off" if prefs["llm"] else "on"))],
                [("翻译为中文", f"ui:{user_id}:language:zh-CN"), ("翻译为英文", f"ui:{user_id}:language:en")],
                [("选择模型", f"ui:{user_id}:models"), ("思考强度", f"ui:{user_id}:reasoning")],
                [("恢复默认设置", f"ui:{user_id}:reset_settings"), ("返回首页", f"ui:{user_id}:home")],
            ]),
        )

    async def plan(self, user_id: int, message: Any, intent: str) -> None:
        if not intent.strip():
            await reply(message, "请说明要如何处理文章，例如：/agent 总结文章并导出 Markdown。")
            return
        key, _, _ = self.selected(user_id, message)
        client = self.llm_client(user_id, redact(intent))
        progress = Progress(message, user_id)
        try:
            await progress.update("正在整理你的处理目标…\n生成计划后会请你确认。")
            plan = await client.plan(intent, user_id)
        finally:
            await client.close()
            await progress.clear()
        target_id = None
        missing = {}
        if plan.requires_confirmation:
            if not self.notion:
                raise NotionError("authorization_required")
            target = self.notion.target(user_id)
            target_id = target["id"]
            missing = await self.notion.schema(user_id, target_id)
        action_labels = {"prepare_parse": "检查文章", "render_markdown": "导出 Markdown", "render_html": "导出 HTML",
                         "summarize_article": "生成摘要", "translate_article": "翻译文章", "generate_tags": "提取标签",
                         "list_notion_targets": "列出 Notion 保存位置", "export_notion": "保存到 Notion"}
        description = "将依次执行：\n" + "\n".join(
            f"{i}. {action_labels[a.name]}" for i, a in enumerate(plan.actions, 1))
        if target_id:
            description += "\n保存目标：" + target_id
        if missing:
            description += "\n添加属性：" + "、".join(missing)
        await self.approval(
            message,
            user_id,
            {"kind": "plan", "key": key, "target": target_id, "plan": plan.model_dump(), "add_schema": missing},
            description,
        )

    async def execute_plan(self, user_id: int, message: Any, payload: dict[str, Any]) -> None:
        key, article, _ = self.selected(user_id, message, payload["key"])

        async def prepared(**_: Any) -> None:
            if not await self.worker.supported(article.source.original_url):
                raise WorkerError("unsupported_url")

        async def markdown(**_: Any) -> None:
            await self.file(message, article, "markdown")

        async def html(**_: Any) -> None:
            await self.file(message, article, "html")

        async def summary(language: str) -> None:
            await self.enhance(user_id, message, key, "summary", language)

        async def translate(language: str) -> None:
            await self.enhance(user_id, message, key, "translate", language)

        async def tags(language: str) -> None:
            await self.enhance(user_id, message, key, "tags", language)

        async def targets(**_: Any) -> None:
            await self.notion_command(user_id, message, ["targets"])

        async def export(**_: Any) -> None:
            if not payload.get("target"):
                raise NotionError("target_required")
            await self.save(user_id, message, key, payload["target"])

        executor = Executor(
            {
                "prepare_parse": prepared,
                "render_markdown": markdown,
                "render_html": html,
                "summarize_article": summary,
                "translate_article": translate,
                "generate_tags": tags,
                "list_notion_targets": targets,
                "export_notion": export,
            },
            self.settings.llm_max_tool_calls,
        )
        await executor.run(ActionPlan.model_validate(payload["plan"]), confirmed=True)

    async def private_prompt(self, message: Any) -> None:
        user = getattr(self.bot, "me", None) if self.bot else None
        username = getattr(user, "username", None)
        markup = (InlineKeyboardMarkup([[InlineKeyboardButton("打开私聊", url="https://t.me/" + username)]])
                  if username else None)
        await reply(message, "这个功能需要在私聊中使用。请打开机器人私聊，再发送文章链接或选择已有文章。",
                    reply_markup=markup)

    async def unsupported_input(self, client: Any, message: Any) -> None:
        user_id = int(message.from_user.id) if message.from_user else 0
        if user_id not in self.settings.allowed_users:
            await self.dispatch(client, message)
            return
        await reply(message, "请发送网页链接，或在分享图片/视频时附上原文链接。\n"
                    "目前不能直接解析上传文件或截图中的正文。",
                    reply_markup=menu(user_id))

    async def dispatch(self, client: Any, message: Any) -> None:
        user_id = int(message.from_user.id) if message.from_user else 0
        text = message.text or getattr(message, "caption", None) or ""
        tokens = text.split()
        command = tokens[0].split("@")[0] if tokens else ""
        if user_id and private(message) and command == "/id":
            await reply(message, f"你的 Telegram 用户 ID：{user_id}")
            return
        if user_id not in self.settings.allowed_users:
            if user_id and private(message):
                await reply(
                    message,
                    f"欢迎使用阅读助手。你的 Telegram 用户 ID：{user_id}\n"
                    "请将此 ID 提供给管理员，加入白名单后即可发送链接解析和导出文章。\n"
                    "无需注册账号；/id 可随时查看自己的 ID。",
                )
            return
        if command == "/cancel":
            task = self.active.get(user_id)
            if task and not task.done() and self.active_chat.get(user_id) == message.chat.id:
                task.cancel()
                await reply(message, "已请求取消。")
                return
            pending_input = self.pending.preferences(user_id).get("input") or {}
            if pending_input.get("chat_id") == message.chat.id:
                self.pending.preferences(user_id, {"input": None})
                await reply(message, "已取消输入，原来的设置和文章都已保留。", reply_markup=menu(user_id))
            else:
                await reply(message, "当前没有正在执行的任务，已有文章已保留。", reply_markup=menu(user_id))
            return
        if command == "/status":
            await self.task_status(user_id, message)
            return
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            if command in {"/start", "/help"}:
                await self.home(user_id, message, help_page=command == "/help")
            else:
                await self.busy(user_id, message)
            return
        async with lock:
            task = asyncio.current_task()
            if task:
                self.active[user_id] = task
                self.active_chat[user_id] = message.chat.id
                self.activities[user_id] = Activity(message.chat.id)
            try:
                pending_input = self.pending.preferences(user_id).get("input") or {}
                if (pending_input and pending_input.get("chat_id") == message.chat.id
                        and pending_input.get("expires", 0) <= time.time()):
                    self.pending.preferences(user_id, {"input": None})
                    await reply(message, "这次输入已过期，本条内容未被处理。\n请重新打开设置或核对恢复入口。",
                                reply_markup=menu(user_id))
                    return
                if (private(message) and pending_input.get("kind") == "notion_recover"
                        and pending_input.get("chat_id") == message.chat.id
                        and pending_input.get("expires", 0) > time.time() and not command.startswith("/")):
                    try:
                        await self.request_recovery(user_id, message, pending_input, text.strip())
                    except (ValueError, NotionError):
                        await reply(message, "请发送那篇 Notion 页面的链接或页面 ID。\n要退出可发送 /cancel。")
                    return
                if (private(message) and pending_input.get("chat_id") == message.chat.id
                        and pending_input.get("expires", 0) > time.time() and not command.startswith("/")
                        and not message_urls(message)):
                    model = text.strip()
                    if not re.fullmatch(r"[\w.:-]{1,100}", model):
                        await reply(message, "请输入模型名，例如 gpt-6.1-sol；要退出请发送 /cancel。")
                        return
                    self.pending.preferences(user_id, {"input": None})
                    await self.settings_command(user_id, message, ["model", model])
                    return
                if pending_input and pending_input.get("chat_id") == message.chat.id:
                    self.pending.preferences(user_id, {"input": None})
                if command == "/start" and tokens[1:] == ["notion"] and private(message):
                    await self.notion_targets(user_id, message)
                elif command in {"/start", "/help"}:
                    await self.home(user_id, message, help_page=command == "/help")
                elif command == "/articles":
                    await self.articles(user_id, message)
                elif command == "/platforms":
                    await self.platforms(user_id, message)
                elif command == "/read" and len(tokens) == 1:
                    await reply(message, "请直接发送链接，或使用 /read <网址>。也支持分享消息中的隐藏链接。",
                                reply_markup=menu(user_id))
                elif command in {"/notion", "/settings", "/summary", "/translate", "/agent"}:
                    if not private(message):
                        await self.private_prompt(message)
                    elif command == "/notion":
                        await self.notion_command(user_id, message, tokens[1:])
                    elif command == "/settings":
                        await self.settings_command(user_id, message, tokens[1:])
                    elif command in {"/summary", "/translate"}:
                        key, _, _ = self.selected(user_id, message)
                        await self.enhance(user_id, message, key, "summary" if command == "/summary" else "translate")
                    else:
                        await self.plan(user_id, message, text.partition(" ")[2])
                else:
                    urls = message_urls(message)
                    if urls:
                        await self.read(user_id, message, urls)
                    elif private(message) and not command.startswith("/"):
                        if self.pending.latest(user_id, message.chat.id):
                            await self.plan(user_id, message, text)
                        else:
                            await self.home(user_id, message)
                    else:
                        await self.problem(user_id, message, "invalid_input")
            except asyncio.CancelledError:
                await reply(message, "任务已取消。")
            except (WorkerError, NotionError, LLMError) as error:
                code = error.code if isinstance(error, (WorkerError, NotionError)) else str(error)
                await self.problem(user_id, message, code, self.current_key(user_id, message.chat.id))
            except ValueError:
                await self.problem(user_id, message, "article_expired")
            except Exception:
                await self.problem(user_id, message, "unknown")
            finally:
                self.active.pop(user_id, None)
                self.active_chat.pop(user_id, None)
                self.activities.pop(user_id, None)

    async def notion_targets(self, user_id: int, message: Any, offset: int = 0) -> None:
        if not self.notion or not self.secrets:
            await self.notion_command(user_id, message, [])
            return
        if not self.secrets.credential(user_id):
            await self.notion_command(user_id, message, ['connect'])
            return
        values = await self.notion.targets(user_id)
        values.sort(key=lambda item: (not item.get("default", False), item.get("title", "")))
        offset = min(max(0, offset), max(0, ((len(values) - 1) // 8) * 8))
        rows = [[(('✓ ' if t.get('default') else '') + truncate(t['title'] or '未命名页面', 32)
                  + (' · 数据源' if t.get('kind') == 'data_source' else ' · 页面'), f"ui:{user_id}:target:{t['id']}")]
                for t in values[offset:offset + 8]]
        nav = []
        if offset:
            nav.append(('上一页', f'ui:{user_id}:targets:{max(0, offset - 8)}'))
        if offset + 8 < len(values):
            nav.append(('下一页', f'ui:{user_id}:targets:{offset + 8}'))
        if nav:
            rows.append(nav)
        rows.append([('刷新位置', f'ui:{user_id}:targets'), ('Notion 设置', f'ui:{user_id}:notion')])
        if self.pending.preferences(user_id).get('notion_resume'):
            rows.append([('暂不保存', f'ui:{user_id}:skip_save')])
        await reply(message, '选择 Notion 保存位置\n点击一个页面或数据源。' if values else
                    '暂无可保存的位置。\n请在 Notion 中把目标页面授权给此 Integration，再重新加载。',
                    reply_markup=buttons(rows))

    async def select_notion_target(self, user_id: int, message: Any, target_id: str) -> None:
        if not self.secrets:
            raise NotionError("authorization_required")
        self.secrets.select(user_id, notion_id(target_id))
        resume = self.pending.preferences(user_id).get("notion_resume") or {}
        if (resume.get("chat_id") == message.chat.id
                and self.pending.get(user_id, message.chat.id, resume.get("key", ""))):
            await self.request_save(user_id, message, resume["key"])
            return
        self.pending.preferences(user_id, {"notion_resume": None})
        await reply(message, "保存位置已设置。\n现在发送一个链接，或从最近文章选择要保存的内容。",
                    reply_markup=menu(user_id))

    async def menu_action(self, user_id: int, message: Any, data: str) -> None:
        parts = data.split(':')
        if len(parts) not in {3, 4} or parts[1] != str(user_id) or not private(message):
            raise PermissionError
        action = parts[2]
        arg = parts[3] if len(parts) == 4 else ''
        panel = Panel(message)
        if action not in {'model_input', 'input_cancel'}:
            pending_input = self.pending.preferences(user_id).get('input') or {}
            if pending_input.get('chat_id') == message.chat.id:
                self.pending.preferences(user_id, {'input': None})
        if action in {'home', 'help'}:
            await self.home(user_id, panel, help_page=action == 'help')
        elif action == 'articles':
            await self.articles(user_id, panel, max(0, int(arg or 0)))
        elif action == 'read':
            await reply(panel, "把链接发到当前聊天即可。\n支持直接粘贴链接、转发带链接的文字，或发送带链接的图片说明。",
                        reply_markup=buttons([[('支持的平台', f'ui:{user_id}:platforms'),
                                               ('返回首页', f'ui:{user_id}:home')]]))
        elif action in {'models', 'models_refresh'}:
            await self.model_choices(user_id, panel, int(arg or 0), refresh=action == 'models_refresh')
        elif action == 'status':
            await self.task_status(user_id, panel)
        elif action == 'guide':
            await self.help_section(user_id, panel, arg)
        elif action == 'reset_settings':
            self.pending.preferences(user_id, {'model': '', 'reasoning_effort': '', 'language': 'zh-CN', 'llm': True})
            await self.settings_command(user_id, panel, [])
        elif action == 'skip_save':
            self.pending.preferences(user_id, {'notion_resume': None, 'input': None})
            await reply(panel, '已退出保存设置，原文章已保留。', reply_markup=menu(user_id))
        elif action == 'disconnect':
            if not self.secrets or not self.secrets.credential(user_id):
                await self.notion_command(user_id, panel, [])
            else:
                await self.approval(panel, user_id, {'kind': 'disconnect_notion'},
                                    '断开 Notion 连接？\n将移除本机授权和保存位置，已保存的 Notion 页面不会被删除。')
        elif action == 'model_input':
            self.pending.preferences(user_id, {'input': {'chat_id': message.chat.id, 'expires': time.time() + 300}})
            await reply(panel, "请直接发送模型名，例如 gpt-6.1-sol。\n五分钟内有效；发送链接会退出设置并开始解析。",
                        reply_markup=buttons([[('取消输入', f'ui:{user_id}:input_cancel')]]))
        elif action == 'input_cancel':
            state = self.pending.preferences(user_id).get('input') or {}
            self.pending.preferences(user_id, {'input': None})
            if state.get('kind') == 'notion_recover':
                await reply(panel, '已退出核对，原文和已有保存进度保留。', reply_markup=menu(user_id))
            else:
                await self.settings_command(user_id, panel, [])
        elif action == 'reasoning':
            await reply(panel, "选择思考强度\n高：适合复杂文章\n中：平衡深度和等待时间\n低：适合简单任务",
                        reply_markup=buttons([[('高', f'ui:{user_id}:effort:high'),
                                               ('中', f'ui:{user_id}:effort:medium'),
                                               ('低', f'ui:{user_id}:effort:low')],
                                              [('返回设置', f'ui:{user_id}:settings')]]))
        elif action == 'effort' and arg in {'low', 'medium', 'high'}:
            await self.settings_command(user_id, panel, ['reasoning', arg])
        elif action == 'platforms':
            await self.platforms(user_id, panel)
        elif action == 'settings':
            await self.settings_command(user_id, panel, [])
        elif action == 'llm' and arg in {'on', 'off'}:
            await self.settings_command(user_id, panel, ['llm', arg])
        elif action == 'language' and arg in {'zh-CN', 'en'}:
            await self.settings_command(user_id, panel, ['language', arg])
        elif action == 'default_model':
            self.pending.preferences(user_id, {'model': '', 'input': None})
            await self.settings_command(user_id, panel, [])
        elif action in {'notion', 'connect'}:
            await self.notion_command(user_id, panel, ['connect'] if action == 'connect' else [])
        elif action == 'targets':
            await self.notion_targets(user_id, panel, max(0, int(arg or 0)))
        elif action == 'target' and self.secrets:
            await self.select_notion_target(user_id, panel, arg)
        else:
            raise ValueError('unknown_menu')

    async def callback(self, client: Any, query: Any) -> None:
        uid = int(query.from_user.id)
        message = query.message
        answered = False

        async def ack(text: str = "", *, show_alert: bool = False) -> None:
            nonlocal answered
            if answered:
                if text and message is not None:
                    await reply(message, text, reply_markup=menu(uid) if private(message) else None)
                return
            answered = True
            try:
                await query.answer(text, show_alert=show_alert)
            except RPCError:
                pass

        if uid not in self.settings.allowed_users or message is None:
            await ack("无权限", show_alert=True)
            return
        data = str(query.data or "")
        if data == f"ui:{uid}:cancel":
            task = self.active.get(uid)
            if task and not task.done() and self.active_chat.get(uid) == message.chat.id:
                task.cancel()
                await ack("正在取消")
            else:
                await ack("当前聊天没有正在执行的任务。")
            return
        if private(message) and data in {f"ui:{uid}:status", f"ui:{uid}:home", f"ui:{uid}:help"}:
            await ack()
            await self.menu_action(uid, message, data)
            return
        lock = self.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            await ack("任务仍在进行，可点击进度中的查看状态或取消。", show_alert=True)
            return
        async with lock:
            task = asyncio.current_task()
            if task:
                self.active[uid] = task
                self.active_chat[uid] = message.chat.id
                self.activities[uid] = Activity(message.chat.id)
            try:
                data = str(query.data or "")
                if data.startswith(("retry:", "choice:", "recover:", "saveagain:")):
                    payload = self.pending.consume(uid, message.chat.id, data.split(":", 1)[1])
                    kinds = {"retry": "retry_parse", "choice": "model",
                             "recover": "recover_input", "saveagain": "continue_save"}
                    expected = kinds[data.split(":", 1)[0]]
                    if not payload or payload.get("kind") != expected:
                        raise ValueError("expired")
                    if expected != "retry_parse" and not private(message):
                        raise PermissionError
                    await ack()
                    if expected == "retry_parse":
                        await self.read(uid, message, payload["urls"])
                    elif expected == "continue_save":
                        await self.request_save(uid, message, payload["key"], payload["target"])
                    elif expected == "model":
                        await self.settings_command(uid, Panel(message), ["model", payload["model"]])
                    else:
                        self.selected(uid, message, payload["key"])
                        self.pending.preferences(uid, {"input": {**payload, "kind": "notion_recover",
                                                                "chat_id": message.chat.id,
                                                                "expires": time.time() + 300}})
                        await reply(message, "请把需要核对的 Notion 页面链接发到这里。\n"
                                    "只会核对保存进度，确认后才恢复。发送 /cancel 可退出。", reply_markup=buttons([
                                        [("取消核对", f"ui:{uid}:input_cancel")],
                                    ]))
                    return
                if data.startswith("ui:"):
                    if data.split(":")[1] != str(uid):
                        await ack("这是其他用户的操作面板，请发送 /start 打开自己的面板。", show_alert=True)
                        return
                    if not private(message):
                        raise PermissionError
                    await ack()
                    await self.menu_action(uid, message, data)
                    return
                if data.startswith("dismiss:"):
                    payload = self.pending.consume(uid, message.chat.id, data.split(":", 1)[1])
                    if not payload:
                        raise ValueError("expired")
                    await ack("已取消")
                    await reply(Panel(message), "已取消，本次不会写入或执行计划。",
                                reply_markup=menu(uid))
                    return
                if data.startswith("confirm:"):
                    if not private(message):
                        raise PermissionError
                    payload = self.pending.consume(uid, message.chat.id, data.split(":", 1)[1])
                    if not payload:
                        raise ValueError("expired")
                    if payload["kind"] != "disconnect_notion":
                        self.selected(uid, message, payload["key"])
                    await ack("正在执行")
                    try:
                        await message.edit_reply_markup(None)
                    except RPCError:
                        pass
                    if payload["kind"] == "disconnect_notion":
                        if self.secrets:
                            self.secrets.disconnect(uid)
                        self.pending.preferences(uid, {"notion_resume": None})
                        await self.notion_command(uid, Panel(message), [])
                        return
                    if payload.get("add_schema"):
                        if not self.notion:
                            raise NotionError("authorization_required")
                        # Re-read schema, and require a new confirmation if missing fields changed.
                        missing = await self.notion.schema(uid, payload["target"])
                        if missing != payload["add_schema"]:
                            raise NotionError("schema_confirmation_required")
                        await self.notion.schema(uid, payload["target"], add=True)
                    if payload["kind"] == "remove":
                        _, _, leases = self.selected(uid, message, payload["key"])
                        for lease in leases:
                            try:
                                await self.worker.release(lease)
                            except WorkerError:
                                pass
                        self.pending.delete(payload["key"])
                        await message.edit_text("文章已从最近列表移除，不影响之前导出的文件或 Notion 页面。",
                                                reply_markup=menu(uid))
                    elif payload["kind"] == "save":
                        await self.save(uid, message, payload["key"], payload["target"])
                    elif payload["kind"] == "recover":
                        if not self.notion:
                            raise NotionError("authorization_required")
                        _, article, _ = self.selected(uid, message, payload["key"])
                        await self.notion.reconcile(uid, article, payload["target"], payload["page"])
                        await reply(message, "已核对保存进度。\n点击下方按钮继续，仍会先确认保存位置。",
                                    reply_markup=buttons([[self.token_button(
                                        uid, message.chat.id, "继续确认保存", "saveagain",
                                        {"kind": "continue_save", "key": payload["key"],
                                         "target": payload["target"]})]]))
                    else:
                        await self.execute_plan(uid, message, payload)
                    return
                parsed = parse_callback(data)
                if not parsed:
                    raise ValueError("invalid_callback")
                key, action = parsed
                _, article, _ = self.selected(uid, message, key)
                if action not in {"markdown", "html", "export", "more", "back", "open"} and not private(message):
                    raise PermissionError
                await ack("正在处理")
                if action in {"more", "back"}:
                    markup = self.keyboard(key, uid, private_chat=private(message), expanded=action == "more")
                    if action == "more" and private(message) and self.pending.derived(uid, key):
                        markup.inline_keyboard.append([
                            InlineKeyboardButton("导出 AI 结果", callback_data=make_callback(key, "ai_file"))])
                    await message.edit_reply_markup(markup)
                elif action == "refresh":
                    await self.read(uid, message, [article.source.original_url], force=True)
                elif action == "export":
                    await message.edit_reply_markup(buttons([
                        [("Markdown · 编辑", make_callback(key, "markdown")),
                         ("HTML · 阅读", make_callback(key, "html"))],
                        [("返回操作", make_callback(key, "back"))],
                    ]))
                elif action == "open":
                    await send_preview(message, article, self.worker,
                                       self.keyboard(key, uid, private_chat=private(message)),
                                       self.pending.minutes_left(uid, message.chat.id, key))
                elif action == "ai_file":
                    values = self.pending.derived(uid, key)
                    labels = {"summary": "摘要", "translated_markdown": "翻译", "tags": "标签",
                              "suggested_title": "标题建议", "normalized_markdown": "整理排版"}
                    content = "\n\n".join("## " + labels.get(k, k) + "\n" +
                                             (" · ".join(v) if isinstance(v, list) else str(v))
                                             for k, v in values.items() if v)
                    if not content:
                        raise ValueError("no_ai_result")
                    stream = io.BytesIO(content.encode())
                    stream.name = safe_filename(article.title + "-AI结果", "md")
                    await message.reply_document(stream, file_name=stream.name,
                                                 caption=truncate(article.title or "文章", 250)
                                                 + "\n已生成的 AI 结果汇总",
                                                 parse_mode=enums.ParseMode.DISABLED)
                elif action in {"markdown", "html"}:
                    await self.file(message, article, action)
                elif action == "remove":
                    await self.approval(message, uid, {"kind": "remove", "key": key},
                                        "移除这篇文章？\n它将不再出现在最近列表中，之后可以重新发送链接解析。")
                elif action == "save":
                    await self.request_save(uid, message, key)
                else:
                    await self.enhance(uid, message, key, action.removeprefix("regen_"),
                                       regenerate=action.startswith("regen_"))
            except asyncio.CancelledError:
                await reply(message, "任务已取消。")
            except PermissionError:
                await ack()
                await self.private_prompt(message)
            except ValueError:
                await ack()
                await self.problem(uid, message, "article_expired" if data.startswith("article:") else "action_expired")
            except (NotionError, WorkerError, LLMError) as error:
                code = error.code if isinstance(error, (NotionError, WorkerError)) else str(error)
                await self.problem(uid, message, code, self.current_key(uid, message.chat.id))
            except Exception:
                await self.problem(uid, message, "unknown", self.current_key(uid, message.chat.id))
            finally:
                self.active.pop(uid, None)
                self.active_chat.pop(uid, None)
                self.activities.pop(uid, None)

    async def maintain(self) -> None:
        while True:
            for entry in self.pending.entries():
                expired = entry["expires"] <= time.time() or entry["user_id"] not in self.settings.allowed_users
                for lease in json.loads(entry["leases"]):
                    try:
                        await self.worker.release(lease) if expired else await self.worker.renew(lease)
                    except WorkerError:
                        pass
                if expired:
                    self.pending.delete(entry["id"])
            await asyncio.sleep(90)

    def http_app(self) -> web.Application:
        app = web.Application(client_max_size=8192)

        async def health(request: web.Request) -> web.Response:
            ready = bool(self.bot and self.bot.is_initialized and self.bot.is_connected)
            return web.json_response({"service": "simpread", "ready": ready}, status=200 if ready else 503)

        app.router.add_get("/health", health)
        if self.oauth:

            async def authorized(uid: int) -> None:
                await self.bot.send_message(uid, "Notion 已连接。\n下一步选择保存位置；完成后可继续保存刚才的文章。",
                                            reply_markup=buttons([[("选择保存位置", f"ui:{uid}:targets")],
                                                                  [("返回首页", f"ui:{uid}:home")]]))

            self.oauth.notify = authorized
            self.oauth.routes(app)
        return app

    async def run(self) -> None:
        sessions = self.settings.reader_data_path / "sessions"
        sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bot = Client(
            "reader_" + self.worker.account_id,
            api_id=self.settings.reader_api_id,
            api_hash=self.settings.reader_api_hash.get_secret_value(),
            bot_token=self.settings.reader_bot_token.get_secret_value(),
            workdir=sessions,
            no_updates=False,
            plugins=None,
            parse_mode=enums.ParseMode.DISABLED,
        )
        self.bot.add_handler(MessageHandler(self.dispatch, filters.incoming & (filters.text | filters.caption)))
        self.bot.add_handler(MessageHandler(
            self.unsupported_input, filters.incoming & filters.private & ~filters.caption &
            (filters.photo | filters.video | filters.document | filters.voice | filters.audio | filters.sticker)))
        self.bot.add_handler(CallbackQueryHandler(self.callback))
        runner = None
        task = None
        try:
            await self.worker.startup()
            runner = web.AppRunner(self.http_app(), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, self.settings.reader_host, self.settings.reader_port).start()
            task = asyncio.create_task(self.maintain())
            await self.bot.start()
            if self.oauth:
                self.oauth.bot_username = self.bot.me.username or ""
            await self.bot.set_bot_commands(BOT_COMMANDS)
            await idle()
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if runner:
                await runner.cleanup()
            try:
                if self.bot.is_initialized:
                    await self.bot.stop()
                elif self.bot.is_connected:
                    await self.bot.disconnect()
            finally:
                await self.close()


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    settings.reader_data_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.reader_data_path / "receiver.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("同一数据目录已有 Reader 运行。") from None
        asyncio.run(App(settings).run())
