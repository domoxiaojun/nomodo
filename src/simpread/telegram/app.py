"""Single update receiver with explicit external-write confirmation and durable state."""

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import re
import time
from typing import Any

from aiohttp import web
from pyrogram import Client, enums, filters, idle
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
from .input import message_urls
from .media import DeliveryUncertain, send_preview
from .presentation import ai_rich
from .ui import Panel, Progress, buttons, menu

BOT_COMMANDS = [
    BotCommand("start", "开始使用与授权说明"),
    BotCommand("help", "查看使用帮助"),
    BotCommand("id", "查看自己的 Telegram 用户 ID"),
    BotCommand("read", "解析链接：/read <网址>"),
    BotCommand("articles", "查看最近解析的文章"),
    BotCommand("platforms", "查看当前支持的平台"),
    BotCommand("translate", "翻译最近文章"),
    BotCommand("cancel", "取消任务或清理最近文章"),
    BotCommand("settings", "查看个人设置"),
    BotCommand("summary", "摘要最近文章（需启用 AI）"),
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
        self.bot: Any = None
        self.capacity = asyncio.Semaphore(2)

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
        if private_chat and (self.settings.llm_enabled or self.pending.derived(user_id, key)):
            actions.append(("收起" if expanded else "更多操作", "back" if expanded else "more"))
        return buttons([
            [(label, make_callback(key, action)) for label, action in actions[i:i + 2]]
            for i in range(0, len(actions), 2)
        ])

    async def home(self, user_id: int, message: Any, *, help_page: bool = False) -> None:
        if help_page:
            text = (
                "使用指南\n\n"
                "1. 直接发送链接或带链接的分享消息，可一次发送多个链接。\n"
                "2. 每篇文章以一条富消息展示，包含正文、媒体和操作按钮。\n"
                "3. 点击「摘要」「翻译」处理内容，点击「导出全文」获取 Markdown / HTML。\n"
                "4. 点击「最近文章」重新打开仍在有效期内的内容。\n\n"
                "AI 和 Notion 仅在私聊使用。长文与超限媒体会在结果内提示，不逐个刷屏。\n"
                "处理时可以点击「取消任务」；Notion 写入前会明确要求确认。"
            )
        else:
            ai = "已开启" if self.settings.llm_enabled and self.pending.preferences(user_id).get("llm") else "已关闭"
            notion = "可连接" if self.oauth else "管理员尚未配置"
            text = ("阅读助手\n\n发送一个文章或视频链接，开始解析。\n"
                    "结果中可直接阅读、查看媒体、生成摘要或导出全文。\n\n"
                    f"AI：{ai} · Notion：{notion}\n"
                    f"文章与按钮有效期约 {self.settings.reader_pending_ttl // 60} 分钟。")
        if not private(message):
            text += "\n\nAI、设置和 Notion 操作请私聊机器人。"
        await reply(message, text, reply_markup=menu(user_id) if private(message) else None)

    async def articles(self, user_id: int, message: Any, offset: int = 0) -> None:
        entries = self.pending.recent(user_id, message.chat.id)
        page = entries[offset:offset + 8]
        rows = [[(truncate(item["title"] or "无标题", 42), make_callback(item["id"], "open"))] for item in page]
        nav = []
        if offset:
            nav.append(("上一页", f"ui:{user_id}:articles:{max(0, offset - 8)}"))
        if offset + 8 < len(entries):
            nav.append(("下一页", f"ui:{user_id}:articles:{offset + 8}"))
        if nav:
            rows.append(nav)
        rows.append([("返回首页", f"ui:{user_id}:home")])
        text = (f"最近文章 · {len(entries)} 篇\n点击标题重新打开文章。" if entries else
                "还没有可用文章。\n请直接发送链接，解析后会出现在这里。")
        await reply(message, text, reply_markup=buttons(rows))

    async def platforms(self, user_id: int, message: Any) -> None:
        snapshot = await self.worker.capabilities()
        await reply(message, "当前支持的平台\n\n" + snapshot.names +
                    "\n\n直接发送原文链接即可。部分平台需要管理员配置 Cookie，登录受限内容可能无法解析。",
                    reply_markup=menu(user_id))

    async def approval(self, message: Any, user_id: int, payload: dict[str, Any], description: str) -> None:
        key = self.pending.approve(user_id, message.chat.id, payload)
        await reply(
            message,
            description,
            reply_markup=buttons([[("确认执行", f"confirm:{key}"), ("取消", f"dismiss:{key}")]]),
        )

    def selected(self, user_id: int, message: Any, key: str | None = None) -> tuple[str, Article, tuple[str, ...]]:
        key = key or self.pending.latest(user_id, message.chat.id)
        result = self.pending.get(user_id, message.chat.id, key) if key else None
        if not key or not result:
            raise ValueError("内容已过期，请重新解析。")
        return key, result[0], result[1]

    async def read(self, user_id: int, message: Any, urls: list[str]) -> None:
        progress = Progress(message, user_id)
        succeeded = 0
        failures: list[str] = []
        for index, url in enumerate(urls, 1):
            prepared = None
            key = None
            try:
                await progress.update(f"正在解析 {index}/{len(urls)}\n{url}\n页面和媒体准备可能需要一些时间。")
                if self.pending.count(user_id) >= self.settings.reader_max_pending_per_user:
                    failures.append("待处理文章已达上限，请等待过期或使用 /cancel 清理最近文章。")
                    break
                async with self.capacity:
                    prepared = await self.worker.prepare(url)
                    await progress.update(
                        f"正在整理 {index}/{len(urls)} · {prepared.article.title or '文章'}\n正文和媒体会合并展示。")
                    key = self.pending.put(user_id, message.chat.id, prepared.article,
                                           prepared.lease_ids, prepared.raw_result)
                    await send_preview(message, prepared.article, self.worker,
                                       self.keyboard(key, user_id, private_chat=private(message)),
                                       self.settings.reader_pending_ttl // 60)
                succeeded += 1
            except DeliveryUncertain:
                failures.append(f"第 {index} 篇发送结果未确认，请先查看聊天；可从 /articles 找回原文。")
            except WorkerError as error:
                failures.append(f"第 {index} 篇：" + MESSAGES.get(error.code, "解析未成功，请稍后重试。"))
            except asyncio.CancelledError:
                if key:
                    self.pending.delete(key)
                    key = None
                await progress.update("解析已取消。已发送的其他文章仍可使用。", done=True)
                return
            except Exception:
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
            await progress.update(f"处理完成：成功 {succeeded}/{len(urls)}\n" + "\n".join(failures), done=True)
        else:
            await progress.clear()

    async def file(self, message: Any, article: Article, action: str) -> None:
        content = article.markdown if action == "markdown" else article.html
        stream = io.BytesIO(content.encode("utf-8"))
        stream.name = "article.md" if action == "markdown" else "article.html"
        await message.reply_document(stream, file_name=stream.name)

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
            reasoning_effort=self.settings.llm_reasoning_effort,
            base_url=self.settings.openai_base_url,
        )

    async def enhance(self, user_id: int, message: Any, key: str, operation: str, language: str | None = None) -> None:
        _, article, _ = self.selected(user_id, message, key)
        client = self.llm_client(user_id, sanitize(article))
        labels = {"summary": ("文章摘要", "summary"), "translate": ("全文翻译", "translated_markdown"),
                  "tags": ("文章标签", "tags"), "title": ("标题建议", "suggested_title"),
                  "normalize_markdown": ("整理后的文章", "normalized_markdown")}
        title, field = labels[operation]
        progress = Progress(message, user_id)
        try:
            await progress.update(f"正在生成{title}…\n{article.title}\n复杂内容可能需要更长时间。")
            result = await client.enhance(article, operation, user_id,
                                          language or self.pending.preferences(user_id)["language"])
            value = result.model_dump()[field]
            text = " · ".join(value) if isinstance(value, list) else str(value)
            if not text.strip():
                raise LLMError("llm_failed")
            self.selected(user_id, message, key)
            self.pending.derived(user_id, key, {field: value})
            await message.reply_rich(ai_rich(title, text), reply_markup=buttons([
                [("导出 AI 结果", make_callback(key, "ai_file")), ("回到原文", make_callback(key, "open"))],
            ]))
            await progress.clear()
        except asyncio.CancelledError:
            await progress.update("AI 任务已取消，原文保留。", done=True)
            return
        except LLMError as error:
            await progress.update(MESSAGES.get(str(error), "AI 暂时未完成，原文保留，可稍后重试。"), done=True)
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
                                  "请检查 Notion 后再决定是否重试。", done=True)
            return
        except NotionError as error:
            await progress.update(MESSAGES.get(error.code, "Notion 保存未完成，原文和媒体保留，请检查授权后重试。"),
                                  done=True)
            return
        await progress.clear()
        await reply(message, "已保存到 Notion。" + ("\n部分媒体以外链或说明保留。" if result["warnings"] else ""),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("打开 Notion 页面", url=result["url"])]]))
        for lease in leases:
            try:
                await self.worker.release(lease)
            except WorkerError:
                pass
        self.pending.clear_leases(key)

    async def request_save(self, user_id: int, message: Any, key: str) -> None:
        self.selected(user_id, message, key)
        if not self.notion:
            raise NotionError("authorization_required")
        if self.secrets and not self.secrets.credential(user_id):
            await self.notion_command(user_id, message, ["connect"])
            return
        try:
            target = self.notion.target(user_id)
        except NotionError:
            await self.notion_targets(user_id, message)
            return
        missing = await self.notion.schema(user_id, target["id"])
        description = f"将当前文章保存到：{target['title'] or target['id']}"
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
            await reply(message, "已移除本地授权与目标；可在 Notion 中撤销 Integration 权限。")
        elif command == "targets":
            await self.notion_targets(user_id, message)
        elif command == "target" and len(args) == 2:
            self.secrets.select(user_id, notion_id(args[1]))
            await reply(message, "已选择目标。")
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
                                              [("返回首页", f"ui:{user_id}:home")]]))

    async def settings_command(self, user_id: int, message: Any, args: list[str]) -> None:
        update: dict[str, Any] = {}
        if len(args) == 2 and args[0] == "llm" and args[1] in {"on", "off"}:
            update["llm"] = args[1] == "on"
        elif len(args) == 2 and args[0] == "language" and re.fullmatch(r"[\w-]{1,40}", args[1]):
            update["language"] = args[1]
        elif len(args) == 2 and args[0] == "model":
            if not re.fullmatch(r"[\w.:-]{1,100}", args[1]):
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
            f"思考强度：{self.settings.llm_reasoning_effort or '模型默认'}\n"
            "/settings llm on|off\n/settings language zh-CN\n/settings model <模型>\n"
            "默认无需个人 Key。修改模型：/settings model <模型名>",
            reply_markup=buttons([
                [("关闭 AI" if prefs["llm"] else "开启 AI", f"ui:{user_id}:llm:" + ("off" if prefs["llm"] else "on"))],
                [("翻译为中文", f"ui:{user_id}:language:zh-CN"), ("翻译为英文", f"ui:{user_id}:language:en")],
                [("恢复默认模型", f"ui:{user_id}:default_model"), ("返回首页", f"ui:{user_id}:home")],
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
            try:
                key, _, leases = self.selected(user_id, message)
                for lease in leases:
                    await self.worker.release(lease)
                self.pending.delete(key)
            except ValueError:
                pass
            await reply(message, "已清理最近的待处理内容。")
            return
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await reply(message, "正在处理上一个请求，可用 /cancel 取消。")
            return
        async with lock:
            task = asyncio.current_task()
            if task:
                self.active[user_id] = task
                self.active_chat[user_id] = message.chat.id
            try:
                if command in {"/start", "/help"}:
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
                        await reply(message, "此操作仅支持私聊。")
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
                        await reply(message, "请发送支持平台的 URL。")
            except asyncio.CancelledError:
                await reply(message, "任务已取消。")
            except (WorkerError, NotionError, LLMError) as error:
                code = error.code if isinstance(error, (WorkerError, NotionError)) else str(error)
                await reply(message, MESSAGES.get(code, "操作失败，原文已保留；请检查配置后重试。"))
            except ValueError:
                await reply(message, "没有找到可用文章，或命令参数不正确。\n"
                            "请先发送链接；已有文章可点击「最近文章」重新打开。", reply_markup=menu(user_id))
            except Exception:
                await reply(message, "操作暂时失败，请稍后重试。")
            finally:
                self.active.pop(user_id, None)
                self.active_chat.pop(user_id, None)

    async def notion_targets(self, user_id: int, message: Any, offset: int = 0) -> None:
        if not self.notion or not self.secrets:
            await self.notion_command(user_id, message, [])
            return
        if not self.secrets.credential(user_id):
            await self.notion_command(user_id, message, ['connect'])
            return
        values = await self.notion.targets(user_id)
        rows = [[(truncate(t['title'] or '未命名页面', 40), f"ui:{user_id}:target:{t['id']}")]
                for t in values[offset:offset + 8]]
        nav = []
        if offset:
            nav.append(('上一页', f'ui:{user_id}:targets:{max(0, offset - 8)}'))
        if offset + 8 < len(values):
            nav.append(('下一页', f'ui:{user_id}:targets:{offset + 8}'))
        if nav:
            rows.append(nav)
        rows.append([('返回首页', f'ui:{user_id}:home')])
        await reply(message, '选择 Notion 保存位置\n点击一个页面或数据源。' if values else
                    '暂无可保存的位置。\n请在 Notion 中把目标页面授权给此 Integration，再重新加载。',
                    reply_markup=buttons(rows))

    async def menu_action(self, user_id: int, message: Any, data: str) -> None:
        parts = data.split(':')
        if len(parts) not in {3, 4} or parts[1] != str(user_id) or not private(message):
            raise PermissionError
        action = parts[2]
        arg = parts[3] if len(parts) == 4 else ''
        panel = Panel(message)
        if action in {'home', 'help'}:
            await self.home(user_id, panel, help_page=action == 'help')
        elif action == 'articles':
            await self.articles(user_id, panel, max(0, int(arg or 0)))
        elif action == 'platforms':
            await self.platforms(user_id, panel)
        elif action == 'settings':
            await self.settings_command(user_id, panel, [])
        elif action == 'llm' and arg in {'on', 'off'}:
            await self.settings_command(user_id, panel, ['llm', arg])
        elif action == 'language' and arg in {'zh-CN', 'en'}:
            await self.settings_command(user_id, panel, ['language', arg])
        elif action == 'default_model':
            self.pending.preferences(user_id, {'model': ''})
            await self.settings_command(user_id, panel, [])
        elif action in {'notion', 'connect'}:
            await self.notion_command(user_id, panel, ['connect'] if action == 'connect' else [])
        elif action == 'targets':
            await self.notion_targets(user_id, panel, max(0, int(arg or 0)))
        elif action == 'target' and self.secrets:
            self.secrets.select(user_id, notion_id(arg))
            await reply(panel, '已选择保存位置。\n返回文章后点击「保存到 Notion」，核对并确认即可。',
                        reply_markup=buttons([[('最近文章', f'ui:{user_id}:articles')]]))
        else:
            raise ValueError('unknown_menu')

    async def callback(self, client: Any, query: Any) -> None:
        uid = int(query.from_user.id)
        message = query.message
        if uid not in self.settings.allowed_users or message is None:
            await query.answer("无权限", show_alert=True)
            return
        data = str(query.data or "")
        if data == f"ui:{uid}:cancel":
            task = self.active.get(uid)
            if task and not task.done() and self.active_chat.get(uid) == message.chat.id:
                task.cancel()
                await query.answer("正在取消")
            else:
                await query.answer("当前聊天没有正在执行的任务。")
            return
        lock = self.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            await query.answer("正在处理，请稍候。")
            return
        async with lock:
            task = asyncio.current_task()
            if task:
                self.active[uid] = task
                self.active_chat[uid] = message.chat.id
            try:
                data = str(query.data or "")
                if data.startswith("ui:"):
                    if data.split(":")[1] != str(uid):
                        await query.answer("这是其他用户的操作面板，请发送 /start 打开自己的面板。", show_alert=True)
                        return
                    if not private(message):
                        raise PermissionError
                    await query.answer()
                    await self.menu_action(uid, message, data)
                    return
                if data.startswith("dismiss:"):
                    payload = self.pending.consume(uid, message.chat.id, data.split(":", 1)[1])
                    if not payload:
                        raise ValueError("expired")
                    await query.answer("已取消")
                    await message.edit_text("已取消，本次不会写入或执行计划。", reply_markup=None)
                    return
                if data.startswith("confirm:"):
                    if not private(message):
                        raise PermissionError
                    payload = self.pending.consume(uid, message.chat.id, data.split(":", 1)[1])
                    if not payload:
                        raise ValueError("expired")
                    self.selected(uid, message, payload["key"])
                    await query.answer("正在执行")
                    if payload.get("add_schema"):
                        if not self.notion:
                            raise NotionError("authorization_required")
                        # Re-read schema, and require a new confirmation if missing fields changed.
                        missing = await self.notion.schema(uid, payload["target"])
                        if missing != payload["add_schema"]:
                            raise NotionError("schema_confirmation_required")
                        await self.notion.schema(uid, payload["target"], add=True)
                    if payload["kind"] == "save":
                        await self.save(uid, message, payload["key"], payload["target"])
                    elif payload["kind"] == "recover":
                        if not self.notion:
                            raise NotionError("authorization_required")
                        _, article, _ = self.selected(uid, message, payload["key"])
                        await self.notion.reconcile(uid, article, payload["target"], payload["page"])
                        await reply(message, "已恢复进度，可以重试保存。")
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
                await query.answer("正在处理")
                if action in {"more", "back"}:
                    markup = self.keyboard(key, uid, private_chat=private(message), expanded=action == "more")
                    if action == "more" and private(message) and self.pending.derived(uid, key):
                        markup.inline_keyboard.append([
                            InlineKeyboardButton("导出 AI 结果", callback_data=make_callback(key, "ai_file"))])
                    await message.edit_reply_markup(markup)
                elif action == "export":
                    await message.edit_reply_markup(buttons([
                        [("Markdown", make_callback(key, "markdown")), ("HTML", make_callback(key, "html"))],
                        [("返回操作", make_callback(key, "back"))],
                    ]))
                elif action == "open":
                    await send_preview(message, article, self.worker,
                                       self.keyboard(key, uid, private_chat=private(message)),
                                       self.settings.reader_pending_ttl // 60)
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
                    stream.name = "article-ai.md"
                    await message.reply_document(stream, file_name=stream.name)
                elif action in {"markdown", "html"}:
                    await self.file(message, article, action)
                elif action == "save":
                    await self.request_save(uid, message, key)
                else:
                    await self.enhance(uid, message, key, action)
            except asyncio.CancelledError:
                await reply(message, "任务已取消。")
            except PermissionError:
                await query.answer("此操作仅支持私聊。", show_alert=True)
            except ValueError:
                await query.answer("此按钮已过期或不属于你，请发送 /articles 打开自己的文章。", show_alert=True)
            except (NotionError, WorkerError, LLMError) as error:
                code = error.code if isinstance(error, (NotionError, WorkerError)) else str(error)
                await reply(message, MESSAGES.get(code, "操作失败，原文已保留，可以稍后重试。"))
            except Exception:
                await reply(message, "操作暂时失败，原文已保留。")
            finally:
                self.active.pop(uid, None)
                self.active_chat.pop(uid, None)

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
                await self.bot.send_message(uid, "Notion 授权完成；请 /notion targets 选择目标。")

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
