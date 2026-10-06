"""Durable export state; unknown outcomes are quarantined until explicit reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

from nomodo.domain import Article
from nomodo.worker import WorkerClient

from .blocks import article_blocks, batches, mapping, properties, text_blocks
from .client import NotionClient, NotionError, notion_id
from .media import upload_media
from .store import NotionStore
from .tree import nested, recover_tree, write_tree

WORKSPACE = "workspace"


async def current_page(client: NotionClient, page_id: str) -> dict[str, Any] | None:
    """Return a page that can still be opened, or None when it was deleted or trashed."""
    try:
        page = await client.target(page_id, "page")
    except NotionError as error:
        if error.status == 404:
            return None
        raise
    if page.get("archived") or page.get("in_trash"):
        return None
    return page


def forget_page(job: dict[str, Any]) -> None:
    for field in ("tree_ops", "tree_ids", "tree_next"):
        job.pop(field, None)
    job.update(page_id="", url="", status="pending", in_flight=False, next_batch=0, error=None)


def page_parent(target: dict[str, Any]) -> dict[str, Any]:
    """Parent object for one new page. A workspace page is not nested under another page."""
    if target["kind"] == "workspace":
        return {"type": "workspace", "workspace": True}
    key = "page_id" if target["kind"] == "page" else "data_source_id"
    return {"type": key, key: target["id"]}


def rich_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("results", [])
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("plain_text")
        if not isinstance(text, str):
            nested = item.get("text")
            content = nested.get("content") if isinstance(nested, dict) else ""
            text = content if isinstance(content, str) else ""
        parts.append(text)
    return "".join(parts)


def location_title(item: dict[str, Any]) -> str:
    if item.get("object") == "page":
        properties = item.get("properties")
        if isinstance(properties, dict):
            for prop in properties.values():
                if isinstance(prop, dict) and prop.get("type") == "title":
                    return rich_text(prop.get("title")).strip()
        return ""
    return rich_text(item.get("title")).strip()


def paragraph_text(block: dict[str, Any]) -> str:
    rich = block.get("paragraph", {})
    runs = rich.get("rich_text", []) if isinstance(rich, dict) else []
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        text = item.get("plain_text")
        if not isinstance(text, str):
            nested = item.get("text")
            content = nested.get("content") if isinstance(nested, dict) else ""
            text = content if isinstance(content, str) else ""
        parts.append(text)
    return "".join(parts)


def is_checkpoint(text: str, marker: str) -> bool:
    """Only the exact progress lines written by an export, not a paragraph that mentions them."""
    if not marker or text == marker:
        return text == marker and bool(marker)
    for kind in ("batch", "tree"):
        prefix = f"{marker} {kind} "
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            return True
    return False


def derived_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(part for part in (str(item).strip() for item in value) if part)
    return str(value).strip() if value else ""


DERIVED_LABELS = {
    "summary": "摘要",
    "translated_markdown": "翻译",
    "tags": "标签",
    "suggested_title": "标题建议",
    "normalized_markdown": "整理排版",
}


async def strip_checkpoints(client: NotionClient, page_id: str, marker: str) -> None:
    """Remove progress lines after the article is stored. A cleanup failure must not duplicate the page."""
    if not marker:
        return
    try:
        await _strip_blocks(client, await client.children(page_id), marker, set())
    except NotionError:
        return


async def _strip_blocks(
    client: NotionClient, blocks: list[dict[str, Any]], marker: str, seen: set[str]
) -> None:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id or block_id in seen:
            continue
        seen.add(block_id)
        if is_checkpoint(paragraph_text(block), marker):
            await client.archive(block_id)
            continue
        if block.get("has_children"):
            await _strip_blocks(client, await client.children(block_id), marker, seen)


def listed_location(item: dict[str, Any]) -> bool:
    """Save locations are titled pages and databases, not rows inside a database."""
    if item.get("object") not in {"page", "data_source"}:
        return False
    if item.get("in_trash") or item.get("archived") or item.get("is_archived"):
        return False
    parent = item.get("parent")
    if not isinstance(parent, dict):
        parent = {}
    if item.get("object") == "page" and parent.get("type") in {"data_source_id", "database_id"}:
        return False
    return item.get("object") == "data_source" or bool(location_title(item))


class NotionService:
    def __init__(
        self, store: NotionStore, worker: WorkerClient, factory: Callable[[str], NotionClient] = NotionClient
    ) -> None:
        self.store, self.worker, self.factory = store, worker, factory
        self.locks: dict[int, asyncio.Lock] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        return self.locks.setdefault(user_id, asyncio.Lock())

    def client(self, user_id: int) -> NotionClient:
        credential = self.store.credential(user_id)
        if not credential:
            raise NotionError("authorization_required")
        return self.factory(credential[0])

    def target(self, user_id: int, target_id: str | None = None) -> dict[str, Any]:
        if target_id == WORKSPACE:
            return {"id": WORKSPACE, "title": "新页面", "kind": "workspace", "default": False, "mapping": {}}
        target = next(
            (t for t in self.store.targets(user_id) if (t["id"] == target_id if target_id else t.get("default"))), None
        )
        if not target:
            raise NotionError("target_required")
        return target

    async def targets(self, user_id: int) -> list[dict[str, Any]]:
        client = self.client(user_id)
        try:
            old = {t["id"]: t for t in self.store.targets(user_id)}
            output = []
            for item in await client.search():
                if not listed_location(item):
                    continue
                target_id = notion_id(str(item.get("id") or ""))
                value = {
                    "id": target_id,
                    "title": location_title(item),
                    "kind": item["object"],
                    "default": old.get(target_id, {}).get("default", False),
                    "mapping": old.get(target_id, {}).get("mapping", {}),
                }
                output.append(value)
            self.store.replace_targets(user_id, output)
            return output
        except NotionError as error:
            if error.code == "authorization_expired":
                self.store.disconnect(user_id)
            raise
        finally:
            await client.close()

    async def schema(self, user_id: int, target_id: str, *, add: bool = False) -> dict[str, Any]:
        if target_id == WORKSPACE:
            return {}
        async with self.lock(user_id):
            target = self.target(user_id, target_id)
            client = self.client(user_id)
            try:
                info = await client.target(target_id, target["kind"])
                if target["kind"] == "page":
                    return {}
                names, missing = mapping(info["properties"], target["mapping"])
                if missing and add:
                    await client.add_properties(target_id, missing)
                    info = await client.target(target_id, target["kind"])
                    names, missing = mapping(info["properties"], target["mapping"])
                if not missing:
                    target["mapping"] = names
                    self.store.save_target(user_id, target)
                return missing
            finally:
                await client.close()

    async def export_page(
        self,
        user_id: int,
        article: Article,
        target_id: str,
        *,
        leases: tuple[str, ...] = (),
        derived: dict[str, Any] | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        async with self.lock(user_id):
            client = self.client(user_id)
            job = None
            try:
                target = self.target(user_id, target_id)
                job = self.store.export_status(user_id, article.content_hash, target_id)
                snapshot = article.model_copy(deep=True)
                for asset in snapshot.media:
                    asset.lease_id = None
                if job and "article" not in job:
                    job.update(article=snapshot.model_dump(mode="json"), derived=derived or {})
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                recreated = False
                fresh = False
                if job and (job["status"] == "unknown" or job.get("in_flight")):
                    raise NotionError("write_outcome_unknown")
                page_id = str(job.get("page_id") or "") if job else ""
                if force_new and job and page_id:
                    kept = True
                    if job.get("status") == "sent":
                        try:
                            kept = await current_page(client, page_id) is not None
                        except NotionError:
                            kept = True
                    forget_page(job)
                    fresh, recreated = kept, not kept
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                elif job and job["status"] == "sent":
                    alive = await current_page(client, page_id) if page_id else None
                    if alive is not None:
                        url = alive.get("url")
                        if isinstance(url, str) and url.startswith("https://") and url != job.get("url"):
                            job["url"] = url
                            self.store.save_export(user_id, article.content_hash, target_id, job)
                        if not str(job.get("url") or "").startswith("https://"):
                            raise NotionError("page_url_unavailable")
                        await strip_checkpoints(client, page_id, str(job.get("marker") or ""))
                        return {**job, "reused": True}
                    forget_page(job)
                    recreated = True
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                if job:
                    derived = job.get("derived", derived or {})
                if target["kind"] == "workspace":
                    info: dict[str, Any] = {}
                else:
                    info = await client.target(target_id, target["kind"])
                props = properties(article, target, info, derived or {})
                if not job:
                    digest = hashlib.sha256(f"{user_id}:{article.content_hash}:{target_id}".encode()).hexdigest()
                    job = {
                        "status": "pending",
                        "page_id": "",
                        "url": "",
                        "in_flight": False,
                        "next_batch": 0,
                        "marker": f"checkpoint:{digest}",
                        "blocks": [],
                        "warnings": [],
                        "article": snapshot.model_dump(mode="json"),
                        "derived": derived or {},
                        "target_title": target.get("title", target_id),
                    }
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                if not job["blocks"]:
                    media, warnings = await upload_media(article, client, self.worker)
                    job["blocks"], job["warnings"] = article_blocks(article, media), warnings
                    if derived:
                        job["blocks"].extend(text_blocks("AI 派生内容（原文保留）", "heading_2"))
                        for name, value in derived.items():
                            shown = derived_text(value)
                            if shown:
                                label = DERIVED_LABELS.get(str(name), str(name))
                                job["blocks"].extend(text_blocks(f"{label}：{shown}"))
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                if not job["page_id"]:
                    job["in_flight"] = True
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                    for attempt in range(2):
                        try:
                            page = await client.create_page(
                                page_parent(target), props, text_blocks(job["marker"])
                            )
                            break
                        except NotionError as error:
                            if error.code != "conflict" or attempt:
                                raise
                            if target["kind"] != "workspace":
                                info = await client.target(target_id, target["kind"])
                                props = properties(article, target, info, derived or {})
                    job.update(
                        page_id=notion_id(page["id"]), url=page.get("url", ""), in_flight=False, status="partial"
                    )
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                if nested(job["blocks"]) or "tree_ops" in job:
                    def save_tree() -> None:
                        self.store.save_export(user_id, article.content_hash, target_id, job)
                    await write_tree(job, client, save_tree)
                else:
                    for index, batch in enumerate(batches(job["blocks"])):
                        if index < job["next_batch"]:
                            continue
                        job["in_flight"] = True
                        self.store.save_export(user_id, article.content_hash, target_id, job)
                        for attempt in range(2):
                            try:
                                await client.append(
                                    job["page_id"], [*batch, *text_blocks(f"{job['marker']} batch {index}")]
                                )
                                break
                            except NotionError as error:
                                if error.code != "conflict" or attempt:
                                    raise
                                if target["kind"] != "workspace":
                                    await client.target(target_id, target["kind"])
                        job.update(next_batch=index + 1, in_flight=False)
                        self.store.save_export(user_id, article.content_hash, target_id, job)
                if not job["url"]:
                    job["url"] = (await client.target(job["page_id"], "page")).get("url", "")
                if not str(job["url"]).startswith("https://"):
                    raise NotionError("page_url_unavailable")
                await strip_checkpoints(client, str(job["page_id"]), str(job.get("marker") or ""))
                job.update(status="sent", error=None)
                self.store.save_export(user_id, article.content_hash, target_id, job)
                if fresh:
                    return {**job, "fresh": True}
                return {**job, "recreated": True} if recreated else job
            except BaseException as error:
                if job and job["status"] != "sent":
                    uncertain = not isinstance(error, NotionError) or error.code == "write_outcome_unknown"
                    unknown = job.get("in_flight") and uncertain
                    job["status"] = "unknown" if unknown else "partial" if job.get("page_id") else "failed"
                    job["in_flight"] = bool(unknown)
                    job["error"] = error.code if isinstance(error, NotionError) else "interrupted"
                    self.store.save_export(user_id, article.content_hash, target_id, job)
                if isinstance(error, NotionError) and error.code == "authorization_expired":
                    self.store.disconnect(user_id)
                raise
            finally:
                await client.close()
                # The pending Article owns leases, including across failed exports and retries.

    async def drop_checkpoints(self, user_id: int, content_hash: str, target_id: str) -> None:
        """Best-effort removal when an already saved page is opened again."""
        async with self.lock(user_id):
            job = self.store.export_status(user_id, content_hash, target_id)
            if not job or job.get("status") != "sent":
                return
            page_id = str(job.get("page_id") or "")
            marker = str(job.get("marker") or "")
            if not page_id or not marker:
                return
            client = self.client(user_id)
            try:
                await strip_checkpoints(client, page_id, marker)
            finally:
                await client.close()

    def task(self, user_id: int, task_id: str) -> tuple[Article, dict[str, Any]]:
        tasks = self.store.export_tasks(user_id, task_id)
        if not tasks:
            raise NotionError("export_task_not_found")
        task = tasks[0]
        if not task["job"].get("article"):
            raise NotionError("legacy_export_snapshot_missing")
        return Article.model_validate(task["job"]["article"]), task

    async def reconcile(self, user_id: int, article: Article, target_id: str, page_id: str) -> None:
        async with self.lock(user_id):
            target = self.target(user_id, target_id)
            job = self.store.export_status(user_id, article.content_hash, target_id)
            if not job or job["status"] != "unknown":
                raise NotionError("recovery_not_needed")
            client = self.client(user_id)
            try:
                page_id = notion_id(page_id)
                info = await client.target(page_id, "page")
                parent = info.get("parent", {})
                if target["kind"] == "workspace":
                    standalone = parent.get("type") == "workspace" or parent.get("workspace") is True
                    if not standalone:
                        raise NotionError("recovery_target_mismatch")
                else:
                    key = "page_id" if target["kind"] == "page" else "data_source_id"
                    if parent.get(key) != target_id:
                        raise NotionError("recovery_target_mismatch")
                blocks = await client.children(page_id)
                text = {
                    "".join(
                        t.get("plain_text") or t.get("text", {}).get("content", "")
                        for t in b.get("paragraph", {}).get("rich_text", [])
                    )
                    for b in blocks
                }
                if job["marker"] not in text:
                    raise NotionError("recovery_marker_missing")
                if "tree_ops" in job:
                    await recover_tree(job, client)
                else:
                    if job["page_id"] and f"{job['marker']} batch {job['next_batch']}" not in text:
                        raise NotionError("write_outcome_unknown")
                    while f"{job['marker']} batch {job['next_batch']}" in text:
                        job["next_batch"] += 1
                job.update(page_id=page_id, url=info.get("url", ""), status="partial", in_flight=False)
                self.store.save_export(user_id, article.content_hash, target_id, job)
            finally:
                await client.close()
