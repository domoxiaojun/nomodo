"""Durable export state; unknown outcomes are quarantined until explicit reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

from simpread.domain import Article
from simpread.worker import WorkerClient

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
                if item.get("object") not in {"page", "data_source"}:
                    continue
                target_id = notion_id(item["id"])
                title = item.get("title", [])
                if item["object"] == "page":
                    title = next(
                        (p.get("title", []) for p in item.get("properties", {}).values() if p.get("type") == "title"),
                        [],
                    )
                value = {
                    "id": target_id,
                    "title": "".join(str(t.get("plain_text") or t.get("text", {}).get("content", "")) for t in title),
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
                    marker = hashlib.sha256(f"{user_id}:{article.content_hash}:{target_id}".encode()).hexdigest()
                    job = {
                        "status": "pending",
                        "page_id": "",
                        "url": "",
                        "in_flight": False,
                        "next_batch": 0,
                        "marker": f"SimpRead export {marker}",
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
                            if value:
                                job["blocks"].extend(text_blocks(f"{name}: {value}"))
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
