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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from simpread.config import Settings
from simpread.domain import Article
from simpread.integrations.notion import NotionError, NotionService, NotionStore
from simpread.integrations.notion.client import notion_id
from simpread.integrations.openai import ActionPlan, Executor, LLMError, ResponsesClient
from simpread.integrations.openai.client import redact, sanitize
from simpread.oauth.server import OAuthServer
from simpread.storage import PendingStore
from simpread.worker import WorkerClient, WorkerError, extract_urls

from .callbacks import make_callback, parse_callback, preview, truncate
from .media import send_preview

MESSAGES = {
    "unsupported_url": "暂不支持该平台。",
    "worker_version_mismatch": "Worker 与 Reader 的 ParseHub 版本不匹配。",
    "authorization_required": "请先 /notion connect 授权。",
    "authorization_expired": "Notion 授权已失效，请重新连接。",
    "target_required": "请先 /notion targets 并用 /notion target <id> 选择目标。",
    "schema_confirmation_required": "需要确认添加数据库属性，请重新点击保存按钮。",
    "write_outcome_unknown": "上次写入结果不明，已禁止重复创建。请在 Notion 核对页面，再用 /notion recover <page-id>。",
    "rate_limited": "Notion 暂时限流，请稍后重试。",
    "budget_exhausted": "已达到今日 LLM 预算，原文仍可导出。",
    "llm_disabled": "LLM 默认关闭；请在私聊使用 /settings llm on 启用。",
}


def private(message: Any) -> bool:
    return message is not None and message.chat.type in {"private", enums.ChatType.PRIVATE}


async def reply(message: Any, text: str, **kwargs: Any) -> None:
    await message.reply_text(
        truncate(text, 3800), parse_mode=enums.ParseMode.DISABLED, disable_web_page_preview=True, **kwargs
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
        self.bot: Any = None
        self.capacity = asyncio.Semaphore(2)

    async def close(self) -> None:
        await self.worker.close()
        self.pending.close()
        if self.secrets:
            self.secrets.close()

    def keyboard(self, key: str) -> InlineKeyboardMarkup:
        actions = [
            ("保存到 Notion", "save"),
            ("生成摘要", "summary"),
            ("生成标签", "tags"),
            ("翻译", "translate"),
            ("标题建议", "title"),
            ("整理排版", "normalize_markdown"),
            ("Markdown", "markdown"),
            ("HTML", "html"),
        ]
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(label, callback_data=make_callback(key, action))
                    for label, action in actions[i : i + 2]
                ]
                for i in range(0, len(actions), 2)
            ]
        )

    async def approval(self, message: Any, user_id: int, payload: dict[str, Any], description: str) -> None:
        key = self.pending.approve(user_id, message.chat.id, payload)
        await reply(
            message,
            description,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认执行", callback_data=f"confirm:{key}")]]),
        )

    def selected(self, user_id: int, message: Any, key: str | None = None) -> tuple[str, Article, tuple[str, ...]]:
        key = key or self.pending.latest(user_id, message.chat.id)
        result = self.pending.get(user_id, message.chat.id, key) if key else None
        if not key or not result:
            raise ValueError("内容已过期，请重新解析。")
        return key, result[0], result[1]

    async def read(self, user_id: int, message: Any, urls: list[str]) -> None:
        succeeded = 0
        for url in urls:
            prepared = None
            key = None
            try:
                if self.pending.count(user_id) >= self.settings.reader_max_pending_per_user:
                    await reply(message, "待处理内容过多，请等待过期或使用 /cancel 清理最近文章。")
                    break
                async with self.capacity:
                    prepared = await self.worker.prepare(url)
                key = self.pending.put(
                    user_id, message.chat.id, prepared.article, prepared.lease_ids, prepared.raw_result
                )
                await reply(message, preview(prepared.article), reply_markup=self.keyboard(key))
                missing = await send_preview(message, prepared.article, self.worker)
                if missing:
                    await reply(message, f"{missing} 个媒体未能预览；原文与格式导出仍可用。")
                succeeded += 1
            except WorkerError as error:
                text = MESSAGES.get(error.code, "解析失败，请稍后重试。")
                if error.code == "unsupported_url":
                    text += "\n当前支持：" + (await self.worker.capabilities()).names
                await reply(message, text)
            except BaseException:
                if key:
                    self.pending.delete(key)
                    key = None
                raise
            finally:
                if prepared and key is None:
                    for lease in prepared.lease_ids:
                        await self.worker.release(lease)
        if len(urls) > 1:
            await reply(
                message, f"处理完成：成功 {succeeded} / {len(urls)}" + ("；任务失败。" if not succeeded else "。")
            )

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
        input_price = float(prefs.get("input_price") or self.settings.llm_input_usd_per_million)
        output_price = float(prefs.get("output_price") or self.settings.llm_output_usd_per_million)
        # UTF-8 bytes conservatively bound text tokens. Reserve prompt overhead and full output cap.
        cost = (
            (len(text.encode()) + 8000) * input_price + self.settings.llm_max_output_tokens * output_price
        ) / 1_000_000
        if not self.pending.reserve(user_id, cost, self.settings.llm_daily_budget):
            raise LLMError("budget_exhausted")
        return ResponsesClient(
            key,
            model,
            self.settings.reader_bot_token.get_secret_value(),
            timeout=self.settings.openai_timeout_seconds,
            max_input_chars=self.settings.llm_max_input_chars,
            max_output_tokens=self.settings.llm_max_output_tokens,
        )

    async def enhance(self, user_id: int, message: Any, key: str, operation: str, language: str | None = None) -> None:
        _, article, _ = self.selected(user_id, message, key)
        client = self.llm_client(user_id, sanitize(article))
        try:
            result = await client.enhance(
                article, operation, user_id, language or self.pending.preferences(user_id)["language"]
            )
        finally:
            await client.close()
        derived = result.model_dump()
        self.pending.derived(user_id, key, derived)
        text = "\n\n".join(f"{k}: {v}" for k, v in derived.items() if v) or "模型未生成有效内容。"
        if len(text.encode("utf-16-le")) > 7000:
            stream = io.BytesIO(text.encode())
            stream.name = "ai-derived.md"
            await message.reply_document(stream, file_name=stream.name)
        else:
            await reply(message, "AI 派生内容（原文保留）：\n" + text)

    async def save(self, user_id: int, message: Any, key: str, target_id: str) -> None:
        key, article, leases = self.selected(user_id, message, key)
        if not self.notion:
            raise NotionError("authorization_required")
        try:
            result = await self.notion.export_page(
                user_id, article, target_id, leases=leases, derived=self.pending.derived(user_id, key)
            )
            await reply(
                message, "已保存：" + result["url"] + ("\n部分媒体已降级为说明或外链。" if result["warnings"] else "")
            )
        finally:
            for lease in leases:
                await self.worker.release(lease)
            self.pending.clear_leases(key)

    async def request_save(self, user_id: int, message: Any, key: str) -> None:
        self.selected(user_id, message, key)
        if not self.notion:
            raise NotionError("authorization_required")
        target = self.notion.target(user_id)
        missing = await self.notion.schema(user_id, target["id"])
        description = f"将当前文章保存到：{target['title'] or target['id']}"
        if missing:
            description += "\n同时添加缺少属性：" + "、".join(missing)
        await self.approval(
            message, user_id, {"kind": "save", "key": key, "target": target["id"], "add_schema": missing}, description
        )

    async def notion_command(self, user_id: int, message: Any, args: list[str]) -> None:
        if not self.notion or not self.secrets:
            raise NotionError("authorization_required")
        command = args[0] if args else "status"
        if command == "connect":
            if not self.oauth:
                await reply(message, "管理员尚未配置 Notion OAuth。")
            else:
                await reply(message, "打开授权链接：\n" + self.oauth.begin(user_id))
        elif command == "disconnect":
            self.secrets.disconnect(user_id)
            await reply(message, "已移除本地授权与目标；可在 Notion 中撤销 Integration 权限。")
        elif command == "targets":
            values = await self.notion.targets(user_id)
            await reply(
                message, "\n".join(f"{t['id']}  {t['title']} ({t['kind']})" for t in values) or "暂无可访问目标。"
            )
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
            await reply(message, "已连接：" + credential[1] if credential else "尚未连接，请 /notion connect。")

    async def settings_command(self, user_id: int, message: Any, args: list[str]) -> None:
        update: dict[str, Any] = {}
        if len(args) == 2 and args[0] == "llm" and args[1] in {"on", "off"}:
            update["llm"] = args[1] == "on"
        elif len(args) == 2 and args[0] == "language" and re.fullmatch(r"[\w-]{1,40}", args[1]):
            update["language"] = args[1]
        elif len(args) == 4 and args[0] == "model":
            a, b = float(args[2]), float(args[3])
            if not 0 < a <= 10000 or not 0 < b <= 10000 or not re.fullmatch(r"[\w.:-]{1,100}", args[1]):
                raise ValueError("模型/单价无效。")
            update = {"model": args[1], "input_price": a, "output_price": b}
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
        prefs = self.pending.preferences(user_id, update)
        await reply(
            message,
            f"LLM：{prefs['llm']}；语言：{prefs['language']}；模型：{prefs['model'] or '默认'}\n"
            "/settings llm on|off\n/settings language zh-CN\n/settings model <模型> <输入单价> <输出单价>\n"
            "/settings key <key|clear>（会尽力删除含 Key 的消息）",
        )

    async def plan(self, user_id: int, message: Any, intent: str) -> None:
        key, _, _ = self.selected(user_id, message)
        client = self.llm_client(user_id, redact(intent))
        try:
            plan = await client.plan(intent, user_id)
        finally:
            await client.close()
        target_id = None
        missing = {}
        if plan.requires_confirmation:
            if not self.notion:
                raise NotionError("authorization_required")
            target = self.notion.target(user_id)
            target_id = target["id"]
            missing = await self.notion.schema(user_id, target_id)
        description = "执行计划：\n" + "\n".join(a.name for a in plan.actions)
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
        if user_id not in self.settings.allowed_users:
            return
        text = message.text or ""
        tokens = text.split()
        command = tokens[0].split("@")[0] if tokens else ""
        if command == "/cancel":
            task = self.active.get(user_id)
            if task and not task.done():
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
            try:
                if command in {"/start", "/help"}:
                    await reply(
                        message,
                        "发送 URL 或 /read <url> 预览。\n/summary、/notion connect|status|targets|target|disconnect\n"
                        "/settings、/agent <目标>、/cancel。Notion/LLM 仅限私聊，默认不会自动执行。",
                    )
                elif command in {"/notion", "/settings", "/summary", "/agent"}:
                    if not private(message):
                        await reply(message, "此操作仅支持私聊。")
                    elif command == "/notion":
                        await self.notion_command(user_id, message, tokens[1:])
                    elif command == "/settings":
                        await self.settings_command(user_id, message, tokens[1:])
                    elif command == "/summary":
                        key, _, _ = self.selected(user_id, message)
                        await self.enhance(user_id, message, key, "summary")
                    else:
                        await self.plan(user_id, message, text.partition(" ")[2])
                else:
                    urls = extract_urls(text)
                    if urls:
                        await self.read(user_id, message, urls)
                    elif private(message) and not command.startswith("/"):
                        await self.plan(user_id, message, text)
                    else:
                        await reply(message, "请发送支持平台的 URL。")
            except asyncio.CancelledError:
                await reply(message, "任务已取消。")
            except (WorkerError, NotionError, LLMError) as error:
                code = error.code if isinstance(error, (WorkerError, NotionError)) else str(error)
                await reply(message, MESSAGES.get(code, "操作失败，原文已保留；请检查配置后重试。"))
            except ValueError:
                await reply(message, "参数无效或文章已过期，请检查命令或重新解析。")
            except Exception:
                await reply(message, "操作暂时失败，请稍后重试。")
            finally:
                self.active.pop(user_id, None)

    async def callback(self, client: Any, query: Any) -> None:
        uid = int(query.from_user.id)
        message = query.message
        if uid not in self.settings.allowed_users or message is None:
            await query.answer("无权限", show_alert=True)
            return
        lock = self.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            await query.answer("正在处理，请稍候。")
            return
        async with lock:
            task = asyncio.current_task()
            if task:
                self.active[uid] = task
            try:
                data = str(query.data or "")
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
                if action not in {"markdown", "html"} and not private(message):
                    raise PermissionError
                await query.answer("正在处理")
                if action in {"markdown", "html"}:
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
                await query.answer("内容/确认按钮已过期，请重新操作。", show_alert=True)
            except (NotionError, WorkerError, LLMError) as error:
                code = error.code if isinstance(error, (NotionError, WorkerError)) else str(error)
                await reply(message, MESSAGES.get(code, "操作失败，原文已保留，可以稍后重试。"))
            except Exception:
                await reply(message, "操作暂时失败，原文已保留。")
            finally:
                self.active.pop(uid, None)

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
        self.bot.add_handler(MessageHandler(self.dispatch, filters.text))
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
