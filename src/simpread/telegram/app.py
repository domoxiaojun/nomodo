"""Single update receiver with explicit external-write confirmation and durable state."""

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import logging
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
from simpread.integrations.openai import ActionPlan, Executor, LLMError
from simpread.integrations.openai.reader import ReaderService, save_credential
from simpread.oauth.server import OAuthServer
from simpread.storage import PendingStore
from simpread.worker import WorkerClient, WorkerError

from .callbacks import make_callback, message_chunks, parse_callback, preview, truncate
from .input import message_urls
from .media import send_preview

MESSAGES = {
    "article_post_link_required": "这是 X 文章直链。请发送发布这篇文章的原始推文链接，以便获取正文和媒体。",
    "credentials_required": "该笔记需要平台登录凭据，请联系管理员检查 Worker 的平台配置。",
    "credentials_invalid": "Worker 的平台凭据未能通过验证，请联系管理员检查；无需重新连接 Notion。",
    "content_unavailable": "未能获取笔记内容。请确认原文仍可访问，并发送完整分享链接（保留链接参数）。",
    "upstream_timeout": "平台响应超时，请稍后重试。",
    "upstream_http": "平台请求失败，请稍后重试。",
    "upstream_contract": "平台返回的数据格式无法解析，需要检查 Worker 的解析器兼容性。",
    "media_failed": "平台媒体下载失败，请稍后重试或查看原文。",
    "unsupported_url": "暂不支持该平台。",
    "worker_version_mismatch": "Worker 与 Reader 的 ParseHub 版本不匹配。",
    "authorization_required": "请先 /notion connect 授权。",
    "authorization_expired": "Notion 授权已失效，请重新连接。",
    "permission_denied": "当前 Notion 资源无访问权限，请检查页面共享与 Integration 权限；授权仍保留。",
    "target_required": "请先 /notion targets 并用 /notion target <id> 选择目标。",
    "schema_confirmation_required": "需要确认添加数据库属性，请重新点击保存按钮。",
    "write_outcome_unknown": (
        "写入结果不明，已禁止重复创建。请用 /notion jobs 查任务，再用 /notion recover <任务ID> <页面ID>。"
    ),
    "export_task_not_found": "导出任务不存在或不属于你，请用 /notion jobs 查看。",
    "legacy_export_snapshot_missing": "旧任务缺少文章快照。请重新解析原文章并尝试保存，再按任务 ID 恢复。",
    "rate_limited": "Notion 暂时限流，请稍后重试。",
    "budget_exhausted": "已达到今日 LLM 预算，原文仍可导出。",
    "llm_disabled": "LLM 默认关闭；请在私聊使用 /settings llm on 启用。",
    "ai_expired": "文章或 AI 结果已过期，请重新解析；不会自动切换到另一篇文章。",
    "ai_configuration_changed": "模型、端点或文章已变化，请重新生成；不沿用旧任务的分块。",
    "personal_key_endpoint_changed": "个人 Key 绑定的端点已变化，请重新设置 Key；没有把旧 Key 发到新服务。",
    "reasoning_unsupported": "该模型配置未声明支持此思考强度，请选择默认强度或由管理员填写能力配置。",
    "llm_auth_failed": "LLM 鉴权失败，请检查当前端点对应的 Key。",
    "llm_rate_limited": "LLM 服务暂时限流，已保留成功分块，可用 /ai 查看并重试。",
    "llm_timeout": "LLM 请求超时，费用可能已产生；成功分块已保存，不会自动重放这次请求。",
    "llm_connection_error": "LLM 连接中断，结果和费用不确定；可用 /ai 显式重试。",
    "llm_unavailable": "LLM 服务异常，未自动切换服务或重放请求。",
    "llm_parameter_error": "当前端点不支持所配置的模型/参数，请检查协议与能力配置。",
    "refused": "模型拒绝处理该内容，原文保留。",
    "output_truncated": "模型输出未完成，未当作全文保存；成功分块仍可继续使用。",
    "invalid_output": "模型返回格式不符合要求，未覆盖原文。",
    "invalid_citation": "模型引用无法在原文核对，结果未发布，原文保留。",
    "incomplete_transformation": "转换结果缺少或打乱了原文分块，未发布不完整全文。",
    "protected_content_changed": "转换改变了代码/链接等保护标记，结果未发布。",
    "normalization_changed_content": "排版结果改变了正文内容，已拒绝并保留原文。",
    "llm_context_too_small": "配置的上下文/输出空间不足，请调整模型能力与输出预算。",
    "input_too_large": "当前步骤超出配置的上下文预算，请调整模型能力配置。",
    "question_required": "请用 /ask <问题> 提问。",
    "article_empty": "当前文章没有可供模型阅读的文本。",
    "invalid_glossary": "术语表包含无法核对的原文词条，翻译未继续。",
    "terminology_mismatch": "译文与已确定的术语表不一致，该分块未标记完成。",
    "unsupported_claim": "额外复核未能确认论断受到引用支持，结果未发布，原文保留。",
}
logger = logging.getLogger(__name__)


def private(message: Any) -> bool:
    return message is not None and message.chat.type in {"private", enums.ChatType.PRIVATE}


async def reply(message: Any, text: str, **kwargs: Any) -> Any:
    return await message.reply_text(
        truncate(text, 3800), parse_mode=enums.ParseMode.DISABLED, disable_web_page_preview=True, **kwargs
    )


async def reply_all(message: Any, text: str) -> None:
    for chunk in message_chunks(text):
        await reply(message, chunk)


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
        self.reader = ReaderService(self.pending, settings, self.secrets)
        self.locks: dict[int, asyncio.Lock] = {}
        self.active: dict[int, asyncio.Task[Any]] = {}
        self.active_chats: dict[int, int] = {}
        self.requests: set[asyncio.Task[Any]] = set()
        self.maintenance_task: asyncio.Task[Any] | None = None
        self.stopping = False
        self.bot: Any = None
        self.capacity = asyncio.Semaphore(2)

    async def close(self) -> None:
        await self.stop_requests()
        if self.maintenance_task:
            self.maintenance_task.cancel()
            await asyncio.gather(self.maintenance_task, return_exceptions=True)
        await self.worker.close()
        self.pending.close()
        if self.secrets:
            self.secrets.close()

    async def stop_requests(self) -> None:
        self.stopping = True
        tasks = (self.requests | set(self.active.values())) - {asyncio.current_task()}
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def dispatch(self, client: Any, message: Any) -> None:
        if self.stopping:
            return
        task = asyncio.create_task(self._dispatch(client, message))
        self.requests.add(task)
        try:
            await task
        finally:
            self.requests.discard(task)

    async def callback(self, client: Any, query: Any) -> None:
        if self.stopping:
            await query.answer("服务正在停止，请稍后再试。")
            return
        task = asyncio.create_task(self._callback(client, query))
        self.requests.add(task)
        try:
            await task
        finally:
            self.requests.discard(task)

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
        key = key or self.pending.current(user_id, message.chat.id)
        result = self.pending.get(user_id, message.chat.id, key) if key else None
        if not key or not result:
            raise ValueError("内容已过期，请重新解析。")
        return key, result[0], result[1]

    async def read(self, user_id: int, message: Any, urls: list[str]) -> None:
        succeeded = 0
        for index, url in enumerate(urls, 1):
            prepared = None
            key = None
            try:
                if self.pending.count(user_id) >= self.settings.reader_max_pending_per_user:
                    await reply(message, "待处理内容过多，请等待过期或使用 /cancel 清理最近文章。")
                    break
                if len(urls) > 1:
                    await reply(message, f"正在解析第 {index}/{len(urls)} 个链接：{url}")
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
                    self.pending.delete(key, forget_selection=True)
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

    async def enhance(
        self, user_id: int, message: Any, key: str, operation: str, language: str | None = None,
        *, mode: str = "brief", question: str = "", refresh: bool = False, retry: str | None = None,
    ) -> None:
        self.selected(user_id, message, key)
        language = language or self.pending.preferences(user_id)["language"]
        if not re.fullmatch(r"[\w-]{1,40}", language):
            raise ValueError("invalid_language")
        status = None
        last = 0.0

        async def progress(text: str) -> None:
            nonlocal status, last
            if time.monotonic() - last < 3:
                return
            last = time.monotonic()
            try:
                if status is None:
                    status = await reply(message, text)
                else:
                    await status.edit_text(text, parse_mode=enums.ParseMode.DISABLED)
            except Exception:
                pass  # Progress delivery must not repeat a paid generation.

        job = await self.reader.run(
            user_id, message.chat.id, key, operation, language=language, mode=mode, question=question,
            refresh=refresh, retry=retry, progress=progress,
        )
        await self.ai_result(user_id, message, job)

    async def ai_result(self, uid: int, message: Any, job: dict[str, Any]) -> None:
        self.pending.ai.get(uid, message.chat.id, job["id"])
        cost, uncertain = self.pending.ai.cost(job["id"])
        if job["status"] != "completed":
            await reply(message, f"任务 {job['id']} 未完成：{MESSAGES.get(job['error'], job['status'])}\n"
                        f"继续：/ai retry {job['id']}；已计入预算 ${cost:.6f}（不确定请求 {uncertain}）")
            return
        buttons = [("查看依据", "sources"), ("下载 Markdown", "md"), ("下载 HTML", "html"),
                   ("重新生成（会计费）", "regenerate")]
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"ai:{job['id']}:{action}")]
            for label, action in buttons
        ])
        text = f"AI 派生内容（原文保留）· {job['spec']['operation']}\n{job['result']['markdown']}"
        notice = ""
        if job["result"].get("quality", {}).get("risk_flags"):
            checked = job["result"]["quality"].get("reviewed")
            notice += "\n数字、单位或否定含义已额外复核。" if checked else "\n数字、单位或否定含义需结合原文核对。"
        if job["result"].get("local"):
            notice += "\n本地排版完成，未调用模型。"
        if len(text.encode("utf-16-le")) // 2 > 3500:
            stream = io.BytesIO(job["result"]["markdown"].encode())
            stream.name = f"ai-{job['spec']['operation']}-{job['id']}.md"
            await message.reply_document(stream, file_name=stream.name)
            text = "全文已作为文件发送。"
        cached = "\n复用已有结果，本次未调用模型。" if job.get("cache_hit") else ""
        await reply(message, text + notice + cached + f"\n任务 {job['id']} · 累计 ${cost:.6f} · 不确定请求 {uncertain}",
                    reply_markup=markup)

    async def ai_command(self, uid: int, message: Any, args: list[str]) -> None:
        if args and args[0] == "result" and len(args) == 2:
            await self.ai_result(uid, message, self.pending.ai.get(uid, message.chat.id, args[1]))
            return
        if args and args[0] == "retry" and len(args) == 2:
            job = self.pending.ai.get(uid, message.chat.id, args[1])
            await self.enhance(uid, message, job["article_id"], job["spec"]["operation"], retry=job["id"])
            return
        key, _, _ = self.selected(uid, message)
        rows = []
        for job in self.pending.ai.jobs(uid, message.chat.id, key):
            cost, uncertain = self.pending.ai.cost(job["id"])
            rows.append(f"{job['id']} · {job['spec']['operation']} · {job['status']}\n"
                        f"已完成 {self.pending.ai.progress(job['id'])} 步 · ${cost:.6f} · 不确定请求 {uncertain}\n"
                        + (f"查看：/ai result {job['id']}" if job["status"] == "completed"
                           else f"继续：/ai retry {job['id']}"))
        await reply_all(message, "\n\n".join(rows) or "当前文章暂无 AI 任务；旧派生内容仍可随原有保存流程使用。")

    async def ai_callback(self, uid: int, message: Any, data: str) -> None:
        _, job_id, action = data.split(":")
        job = self.pending.ai.get(uid, message.chat.id, job_id)
        if action == "regenerate":
            spec = job["spec"]
            await self.enhance(uid, message, job["article_id"], spec["operation"], spec["language"],
                               mode=spec["mode"], question=spec["question"], refresh=True)
        elif job["status"] != "completed":
            await self.ai_result(uid, message, job)
        elif action == "sources":
            text = "\n\n".join(
                f"{'标题/描述' if c['block_id'] == 0 else '原文第 ' + str(c['block_id']) + ' 段'}：\n{c['quote']}"
                for c in job["result"]["citations"]
            )
            await reply_all(message, text or "此结果没有原文引用；不表示经过引文验证。")
        elif action in {"md", "html"}:
            content = job["result"]["markdown" if action == "md" else "html"]
            stream = io.BytesIO(content.encode())
            stream.name = f"ai-{job['spec']['operation']}-{job_id}.{action}"
            await message.reply_document(stream, file_name=stream.name)
        else:
            raise ValueError("invalid_action")

    async def save(self, user_id: int, message: Any, key: str, target_id: str) -> None:
        key, article, _ = self.selected(user_id, message, key)
        if not self.notion:
            raise NotionError("authorization_required")
        result = await self.notion.export_page(
            user_id, article, target_id, derived=self.pending.derived(user_id, key)
        )
        await reply(
            message, "已保存：" + result["url"] + ("\n部分媒体已降级为说明或外链。" if result["warnings"] else "")
        )

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
            await reply_all(
                message, "\n".join(f"{t['id']}  {t['title']} ({t['kind']})" for t in values) or "暂无可访问目标。"
            )
        elif command == "target" and len(args) == 2:
            self.secrets.select(user_id, notion_id(args[1]))
            await reply(message, "已选择目标。")
        elif command == "jobs":
            tasks = self.secrets.export_tasks(user_id)
            states = {
                "sent": "已完成", "unknown": "结果待核对", "partial": "部分完成", "failed": "失败", "pending": "待执行"
            }
            await reply_all(
                message,
                "\n\n".join(
                    f"{task['id']}  {states.get(task['job']['status'], '待检查')}\n"
                    f"{task['job'].get('article', {}).get('title') or '历史文章'}\n"
                    f"目标：{task['job'].get('target_title') or task['target']}\n"
                    f"/notion retry {task['id']}"
                    for task in tasks
                ) or "暂无导出任务。",
            )
        elif command in {"retry", "recover"} and len(args) == (2 if command == "retry" else 3):
            article, task = self.notion.task(user_id, args[1])
            unknown = task["job"]["status"] == "unknown" or task["job"].get("in_flight")
            if command == "retry" and unknown:
                raise NotionError("write_outcome_unknown")
            missing = await self.notion.schema(user_id, task["target"]) if command == "retry" else {}
            await self.approval(
                message,
                user_id,
                {
                    "kind": "retry_task" if command == "retry" else "recover_task",
                    "task": task["id"],
                    "target": task["target"],
                    "page": notion_id(args[2]) if command == "recover" else None,
                    "add_schema": missing,
                },
                f"{'重试保存' if command == 'retry' else '核对并恢复'}：{truncate(article.title or '无标题', 250)}\n"
                f"原目标：{task['target']}\n" + ("将添加属性：" + "、".join(missing) if missing else ""),
            )
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
            save_credential(self.settings, self.secrets, user_id, "" if args[1] == "clear" else args[1])
            await self.bot.send_message(user_id, "个人 OpenAI Key 已更新。", parse_mode=enums.ParseMode.DISABLED)
            return
        elif len(args) == 2 and args[0] == "reasoning":
            model = self.pending.preferences(user_id).get("model") or self.settings.openai_model
            capability = self.settings.llm_model_capabilities.get(model)
            supported = (capability.reasoning_efforts if capability else
                         self.settings.reasoning_efforts if model == self.settings.openai_model else ())
            if args[1] != "default" and args[1] not in supported:
                raise LLMError("reasoning_unsupported")
            update["reasoning"] = None if args[1] == "default" else args[1]
        prefs = self.pending.preferences(user_id, update)
        await reply(
            message,
            f"LLM：{prefs['llm']}；语言：{prefs['language']}；模型：{prefs['model'] or '默认'}\n"
            "/settings llm on|off\n/settings language zh-CN\n/settings model <模型> <输入单价> <输出单价>\n"
            "/settings reasoning <default|强度>\n/settings key <key|clear>（会尽力删除含 Key 的消息）\n"
            f"端点：{self.settings.openai_base_url} · 协议：{self.settings.llm_api_mode}",
        )

    async def plan(self, user_id: int, message: Any, intent: str) -> None:
        key, _, _ = self.selected(user_id, message)
        job = await self.reader.run(user_id, message.chat.id, key, "plan", question=intent)
        if job["status"] != "completed":
            await self.ai_result(user_id, message, job)
            return
        plan = ActionPlan.model_validate(job["result"]["plan"])
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

    async def _dispatch(self, client: Any, message: Any) -> None:
        user_id = int(message.from_user.id) if message.from_user else 0
        if user_id not in self.settings.allowed_users:
            return
        text = message.text or getattr(message, "caption", None) or ""
        tokens = text.split()
        command = tokens[0].split("@")[0] if tokens else ""
        if command == "/cancel":
            task = self.active.get(user_id)
            if task and not task.done():
                if self.active_chats.get(user_id) != message.chat.id:
                    await reply(message, "请在发起任务的聊天中取消。")
                    return
                task.cancel()
                await reply(message, "已请求取消。")
                return
            try:
                key, _, leases = self.selected(user_id, message)
                for lease in leases:
                    await self.worker.release(lease)
                self.pending.delete(key, forget_selection=True)
            except ValueError:
                pass
            await reply(message, "已清理最近的待处理内容。")
            return
        if command == "/ai" and len(tokens) == 1 and private(message):
            try:
                await self.ai_command(user_id, message, [])
            except (LLMError, ValueError):
                await reply(message, "当前没有有效文章，请先发送链接。")
            return
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await reply(message, "正在处理上一个请求，可用 /cancel 取消。")
            return
        async with lock:
            task = asyncio.current_task()
            if task:
                self.active[user_id] = task
                self.active_chats[user_id] = message.chat.id
            try:
                if command in {"/start", "/help"}:
                    await reply(
                        message,
                        "发送链接，或回复一条消息后用 /read 解析。\n/articles 查看文章，/select <ID> 选择。\n"
                        "/summary [brief|detailed]、/translate [语言]、/ask <问题>、/ai [retry|result <任务ID>]\n"
                        "/notion connect|status|targets|target|jobs|retry|recover|disconnect\n"
                        "/settings、/agent <目标>、/cancel。Notion/LLM 仅限私聊，默认不会自动执行。",
                    )
                elif command == "/articles":
                    selected = self.pending.current(user_id, message.chat.id)
                    await reply_all(
                        message,
                        "\n\n".join(
                            f"{'当前 · ' if key == selected else ''}{title}\n/select {key}"
                            for key, title in self.pending.articles(user_id, message.chat.id)
                        ) or "暂无有效文章，请发送链接解析。",
                    )
                elif command == "/select" and len(tokens) == 2:
                    self.pending.select(user_id, message.chat.id, tokens[1])
                    key, article, _ = self.selected(user_id, message)
                    await reply(message, preview(article), reply_markup=self.keyboard(key))
                elif command in {"/notion", "/settings", "/summary", "/agent", "/translate", "/ask", "/ai"}:
                    if not private(message):
                        await reply(message, "此操作仅支持私聊。")
                    elif command == "/notion":
                        await self.notion_command(user_id, message, tokens[1:])
                    elif command == "/settings":
                        await self.settings_command(user_id, message, tokens[1:])
                    elif command == "/summary":
                        key, _, _ = self.selected(user_id, message)
                        if len(tokens) == 1:
                            await self.enhance(user_id, message, key, "summary")
                        else:
                            await self.enhance(user_id, message, key, "summary", mode=tokens[1])
                    elif command == "/translate":
                        key, _, _ = self.selected(user_id, message)
                        await self.enhance(user_id, message, key, "translate", tokens[1] if len(tokens) > 1 else None)
                    elif command == "/ask":
                        key, _, _ = self.selected(user_id, message)
                        await self.enhance(user_id, message, key, "ask", question=text.partition(" ")[2])
                    elif command == "/ai":
                        await self.ai_command(user_id, message, tokens[1:])
                    else:
                        await self.plan(user_id, message, text.partition(" ")[2])
                else:
                    urls = message_urls(message, include_reply=command == "/read")
                    if urls:
                        await self.read(user_id, message, urls)
                    elif private(message) and not command.startswith("/"):
                        key, _, _ = self.selected(user_id, message)
                        await self.enhance(user_id, message, key, "ask", question=text)
                    else:
                        await reply(message, "请发送支持平台的 URL。")
            except asyncio.CancelledError:
                if not self.stopping:
                    await reply(message, "任务已取消。")
            except (WorkerError, NotionError, LLMError) as error:
                code = error.code if isinstance(error, (WorkerError, NotionError)) else str(error)
                await reply(message, MESSAGES.get(code, "操作失败，原文已保留；请检查配置后重试。"))
            except ValueError:
                await reply(message, "参数无效或文章已过期，请检查命令或重新解析。")
            except Exception as error:
                logger.warning("event=request.failed kind=message error_type=%s", type(error).__name__)
                await reply(message, "操作暂时失败，请稍后重试。")
            finally:
                self.active.pop(user_id, None)
                self.active_chats.pop(user_id, None)

    async def _callback(self, client: Any, query: Any) -> None:
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
                self.active_chats[uid] = message.chat.id
            try:
                data = str(query.data or "")
                if data.startswith("ai:"):
                    if not private(message):
                        raise PermissionError
                    parts = data.split(":")
                    if len(parts) != 3:
                        raise ValueError("invalid_callback")
                    self.pending.ai.get(uid, message.chat.id, parts[1])
                    await query.answer("正在处理")
                    await self.ai_callback(uid, message, data)
                    return
                if data.startswith("confirm:"):
                    if not private(message):
                        raise PermissionError
                    payload = self.pending.consume(uid, message.chat.id, data.split(":", 1)[1])
                    if not payload:
                        raise ValueError("expired")
                    task_article = None
                    task_record = None
                    if payload["kind"] in {"retry_task", "recover_task"}:
                        if not self.notion:
                            raise NotionError("authorization_required")
                        task_article, task_record = self.notion.task(uid, payload["task"])
                        if task_record["target"] != payload["target"]:
                            raise ValueError("target_mismatch")
                        if payload["kind"] == "retry_task" and (
                            task_record["job"]["status"] == "unknown" or task_record["job"].get("in_flight")
                        ):
                            raise NotionError("write_outcome_unknown")
                    else:
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
                    if task_article is not None and task_record is not None and self.notion:
                        if payload["kind"] == "recover_task":
                            await self.notion.reconcile(uid, task_article, payload["target"], payload["page"])
                            await reply(message, f"已恢复进度，请用 /notion retry {payload['task']} 继续。")
                        else:
                            result = await self.notion.export_page(
                                uid, task_article, payload["target"], derived=task_record["job"].get("derived", {})
                            )
                            await reply(
                                message,
                                "已保存：" + result["url"]
                                + ("\n部分媒体已降级为说明或外链。" if result["warnings"] else ""),
                            )
                    elif payload["kind"] == "save":
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
                if not self.stopping:
                    await reply(message, "任务已取消。")
            except PermissionError:
                await query.answer("此操作仅支持私聊。", show_alert=True)
            except ValueError:
                await query.answer("内容/确认按钮已过期，请重新操作。", show_alert=True)
            except (NotionError, WorkerError, LLMError) as error:
                code = error.code if isinstance(error, (NotionError, WorkerError)) else str(error)
                await reply(message, MESSAGES.get(code, "操作失败，原文已保留，可以稍后重试。"))
            except Exception as error:
                logger.warning("event=request.failed kind=callback error_type=%s", type(error).__name__)
                await reply(message, "操作暂时失败，原文已保留。")
            finally:
                self.active.pop(uid, None)
                self.active_chats.pop(uid, None)

    async def maintain(self) -> None:
        while True:
            self.pending.ai.purge_expired()
            self.reader.prune_locks()
            for entry in self.pending.entries():
                lock = self.locks.get(entry["user_id"])
                if entry["user_id"] in self.settings.allowed_users and lock and lock.locked():
                    # A long export may cross the expiry time; keep its media alive until it finishes.
                    for lease in json.loads(entry["leases"]):
                        try:
                            await self.worker.renew(lease)
                        except WorkerError:
                            pass
                    continue
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
            maintaining = bool(self.maintenance_task and not self.maintenance_task.done())
            ready = bool(
                self.bot and self.bot.is_initialized and self.bot.is_connected and maintaining and not self.stopping
            )
            return web.json_response(
                {"service": "simpread", "ready": ready, "maintenance": maintaining}, status=200 if ready else 503
            )

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
        self.bot.add_handler(MessageHandler(self.dispatch, filters.text | filters.caption))
        self.bot.add_handler(CallbackQueryHandler(self.callback))
        runner = None
        try:
            await self.worker.startup()
            runner = web.AppRunner(self.http_app(), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, self.settings.reader_host, self.settings.reader_port).start()
            self.maintenance_task = asyncio.create_task(self.maintain())
            await self.bot.start()
            await idle()
        finally:
            await self.stop_requests()
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
