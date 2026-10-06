import asyncio
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from simpread.domain import normalize_worker_result
from simpread.integrations.notion import WORKSPACE, NotionClient, NotionError, NotionService, NotionStore
from simpread.integrations.notion.blocks import FIELDS, article_blocks, batches
from simpread.integrations.notion.media import upload_media

TARGET = "11111111-1111-4111-8111-111111111111"
PAGE = "22222222-2222-4222-8222-222222222222"


class Worker:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release(self, lease: str) -> None:
        self.released.append(lease)


def article() -> Any:
    return normalize_worker_result(
        {
            "sourceUrl": "https://example.com",
            "platform": "youtube",
            "title": "Long",
            "content": "\n\n".join("段落" + str(i) for i in range(140)),
            "media": [],
            "leaseId": "l",
        }
    )


def test_long_export_resume_no_duplicate_and_isolation(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[httpx.Request] = []
        rate_limit = True

        async def handler(req: httpx.Request) -> httpx.Response:
            nonlocal rate_limit
            calls.append(req)
            if req.method == "GET":
                return httpx.Response(200, json={"id": TARGET})
            if req.method == "POST":
                return httpx.Response(200, json={"id": PAGE, "url": "https://notion.so/page"})
            # second batch fails, next user retry must reuse the same page.
            if len([c for c in calls if c.method == "PATCH"]) == 2 and rate_limit:
                rate_limit = False
                return httpx.Response(429, headers={"Retry-After": "120"}, json={})
            return httpx.Response(200, json={})

        def factory(token: str) -> NotionClient:
            return NotionClient(token, httpx.MockTransport(handler))

        store = NotionStore(tmp_path / "n.db", "x" * 32)
        store.put_credential(1, "secret")
        store.save_target(1, {"id": TARGET, "kind": "page", "default": True})
        worker: Any = Worker()
        service = NotionService(store, worker, factory)
        with pytest.raises(NotionError, match="rate_limited"):
            await service.export_page(1, article(), TARGET, leases=("l",))
        record = store.export_status(1, article().content_hash, TARGET) or {}
        assert record["page_id"] == PAGE and record["next_batch"] == 1
        job = await service.export_page(1, article(), TARGET, leases=("l",))
        assert job["status"] == "sent"
        await service.export_page(1, article(), TARGET)
        assert sum(c.method == "POST" for c in calls) == 1
        assert worker.released == []  # Pending articles retain media for retry or another destination.
        assert store.export_status(2, article().content_hash, TARGET) is None
        store.close()

    asyncio.run(run())


NEW = "33333333-3333-4333-8333-333333333333"


def note() -> Any:
    return normalize_worker_result(
        {"sourceUrl": "https://example.com/note", "platform": "x", "title": "Note", "content": "hello", "leaseId": "l"}
    )


def notion_service(tmp_path: Path, handler: Any) -> tuple[NotionService, NotionStore]:
    store = NotionStore(tmp_path / "n.db", "x" * 32)
    store.put_credential(1, "secret")
    store.save_target(1, {"id": TARGET, "kind": "page", "default": True, "title": "父页面"})

    async def sleep(_: float) -> None:
        return None

    service = NotionService(
        store,
        Worker(),  # type: ignore[arg-type]
        lambda token: NotionClient(token, httpx.MockTransport(handler), sleep),
    )
    return service, store


@pytest.mark.parametrize("gone", ["trash", "archived", "missing"])
def test_deleted_sent_page_is_recreated_once(tmp_path: Path, gone: str) -> None:
    creates: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.rstrip("/")
        if req.method == "GET" and path.endswith(PAGE):
            if gone == "missing":
                return httpx.Response(404, json={"object": "error", "status": 404})
            body = {"id": PAGE, "url": "https://www.notion.so/old", "in_trash" if gone == "trash" else "archived": True}
            return httpx.Response(200, json=body)
        if req.method == "GET" and path.endswith(NEW):
            return httpx.Response(200, json={"id": NEW, "url": "https://www.notion.so/new"})
        if req.method == "GET":
            return httpx.Response(200, json={"id": TARGET})
        if req.method == "POST" and path.endswith("/pages"):
            page_id = PAGE if not creates else NEW
            creates.append(page_id)
            url = "https://www.notion.so/old" if page_id == PAGE else "https://www.notion.so/new"
            return httpx.Response(200, json={"id": page_id, "url": url})
        return httpx.Response(200, json={})

    async def run() -> None:
        service, store = notion_service(tmp_path, handler)
        value = note()
        try:
            first = await service.export_page(1, value, TARGET, leases=("l",))
            assert first["page_id"] == PAGE and "recreated" not in first
            second = await service.export_page(1, value, TARGET)
            stored = store.export_status(1, value.content_hash, TARGET) or {}
            assert second["recreated"] is True and second["page_id"] == NEW
            assert stored["status"] == "sent" and stored["page_id"] == NEW and "recreated" not in stored
            third = await service.export_page(1, value, TARGET)
            assert third["reused"] is True and third["url"] == "https://www.notion.so/new" and creates == [PAGE, NEW]
            assert "reused" not in (store.export_status(1, value.content_hash, TARGET) or {})
        finally:
            store.close()

    asyncio.run(run())


def test_force_new_creates_another_page_beside_the_live_one(tmp_path: Path) -> None:
    creates: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.rstrip("/")
        if req.method == "GET" and path.endswith(PAGE):
            return httpx.Response(200, json={"id": PAGE, "url": "https://www.notion.so/old"})
        if req.method == "GET" and path.endswith(NEW):
            return httpx.Response(200, json={"id": NEW, "url": "https://www.notion.so/new"})
        if req.method == "GET":
            return httpx.Response(200, json={"id": TARGET})
        if req.method == "POST" and path.endswith("/pages"):
            page_id = PAGE if not creates else NEW
            creates.append(page_id)
            url = "https://www.notion.so/old" if page_id == PAGE else "https://www.notion.so/new"
            return httpx.Response(200, json={"id": page_id, "url": url})
        return httpx.Response(200, json={})

    async def run() -> None:
        service, store = notion_service(tmp_path, handler)
        value = note()
        try:
            await service.export_page(1, value, TARGET)
            created = await service.export_page(1, value, TARGET, force_new=True)
            stored = store.export_status(1, value.content_hash, TARGET) or {}
            assert created["fresh"] is True and created["page_id"] == NEW and created["url"].endswith("/new")
            assert stored["page_id"] == NEW and "fresh" not in stored
            again = await service.export_page(1, value, TARGET)
            assert again["reused"] is True and creates == [PAGE, NEW]
        finally:
            store.close()

    asyncio.run(run())


def test_workspace_page_is_created_without_a_parent(tmp_path: Path) -> None:
    parents: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.rstrip("/")
        if req.method == "POST" and path.endswith("/pages"):
            body = json.loads(req.content)
            parents.append(body["parent"])
            assert body["properties"]["title"]["title"][0]["text"]["content"] == "Note"
            return httpx.Response(200, json={"id": PAGE, "url": "https://www.notion.so/standalone"})
        if req.method == "GET" and path.endswith("/pages/" + PAGE):
            return httpx.Response(200, json={"id": PAGE, "url": "https://www.notion.so/standalone"})
        if req.method == "PATCH":
            return httpx.Response(200, json={})
        raise AssertionError(f"{req.method} {path}")

    async def run() -> None:
        service, store = notion_service(tmp_path, handler)
        value = note()
        try:
            created = await service.export_page(1, value, WORKSPACE)
            assert parents == [{"type": "workspace", "workspace": True}]
            assert created["page_id"] == PAGE and created["url"].endswith("/standalone")
            stored = store.export_status(1, value.content_hash, WORKSPACE) or {}
            assert stored["status"] == "sent" and stored["page_id"] == PAGE
            again = await service.export_page(1, value, WORKSPACE)
            assert again["reused"] is True and len(parents) == 1
        finally:
            store.close()

    asyncio.run(run())


def test_workspace_permission_does_not_mark_the_page_sent(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.rstrip("/").endswith("/pages"):
            return httpx.Response(403, json={"object": "error", "status": 403})
        raise AssertionError(req.url.path)

    async def run() -> None:
        service, store = notion_service(tmp_path, handler)
        value = note()
        try:
            with pytest.raises(NotionError, match="permission_denied"):
                await service.export_page(1, value, WORKSPACE)
            stored = store.export_status(1, value.content_hash, WORKSPACE) or {}
            assert stored["status"] == "failed" and stored["page_id"] == "" and not stored["in_flight"]
        finally:
            store.close()

    asyncio.run(run())


def test_force_new_does_not_bypass_an_unknown_write(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(req.url.path)

    async def run() -> None:
        service, store = notion_service(tmp_path, handler)
        value = note()
        store.save_export(1, value.content_hash, TARGET, {"status": "unknown", "in_flight": True, "page_id": PAGE})
        try:
            with pytest.raises(NotionError, match="write_outcome_unknown"):
                await service.export_page(1, value, TARGET, force_new=True)
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["status", "network"])
def test_page_check_failure_does_not_create_another_page(tmp_path: Path, failure: str) -> None:
    creates = 0
    sent = False

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal creates
        path = req.url.path.rstrip("/")
        if req.method == "GET" and path.endswith(PAGE) and sent:
            if failure == "network":
                raise httpx.ConnectError("down")
            return httpx.Response(500, json={})
        if req.method == "GET":
            return httpx.Response(200, json={"id": TARGET})
        if req.method == "POST" and path.endswith("/pages"):
            creates += 1
            return httpx.Response(200, json={"id": PAGE, "url": "https://www.notion.so/old"})
        return httpx.Response(200, json={})

    async def run() -> None:
        nonlocal sent
        service, store = notion_service(tmp_path, handler)
        value = note()
        try:
            await service.export_page(1, value, TARGET)
            sent = True
            expected = "notion_unavailable" if failure == "network" else "notion_request_failed"
            with pytest.raises(NotionError, match=expected):
                await service.export_page(1, value, TARGET)
            stored = store.export_status(1, value.content_hash, TARGET) or {}
            assert creates == 1 and stored["status"] == "sent" and stored["page_id"] == PAGE
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "cancel", "500"])
def test_unknown_write_is_not_replayed(tmp_path: Path, failure: str) -> None:
    async def run() -> None:
        writes = 0

        async def handler(req: httpx.Request) -> httpx.Response:
            nonlocal writes
            if req.method == "GET":
                return httpx.Response(200, json={})
            writes += 1
            if failure == "timeout":
                raise httpx.ReadTimeout("private internal details")
            if failure == "cancel":
                raise asyncio.CancelledError
            return httpx.Response(500, json={})

        def factory(token: str) -> NotionClient:
            return NotionClient(token, httpx.MockTransport(handler))

        store = NotionStore(tmp_path / "n.db", "x" * 32)
        store.put_credential(1, "secret")
        store.save_target(1, {"id": TARGET, "kind": "page"})
        worker: Any = Worker()
        service = NotionService(store, worker, factory)
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else NotionError):
            await service.export_page(1, article(), TARGET, leases=("l",))
        with pytest.raises(NotionError, match="write_outcome_unknown"):
            await service.export_page(1, article(), TARGET)
        assert writes == 1 and worker.released == []
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize("status,code", [(401, "authorization_expired"), (403, "permission_denied")])
def test_auth_expiry_is_distinct_from_resource_permissions(tmp_path: Path, status: int, code: str) -> None:
    async def run() -> None:
        def factory(token: str) -> NotionClient:
            return NotionClient(token, httpx.MockTransport(lambda req: httpx.Response(status, json={})))

        store = NotionStore(tmp_path / "n.db", "x" * 32)
        store.put_credential(1, "secret")
        service = NotionService(store, Worker(), factory)  # type: ignore[arg-type]
        with pytest.raises(NotionError, match=code):
            await service.targets(1)
        assert (store.credential(1) is None) == (status == 401)
        store.close()

    asyncio.run(run())


def test_schema_confirmation_and_mapping(tmp_path: Path) -> None:
    async def run() -> None:
        schema = {"Name": {"type": "title"}}
        mutations = []

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "PATCH":
                mutations.append(req)
                for name, prop in json.loads(req.content)["properties"].items():
                    schema[name] = {"type": next(iter(prop))}
            return httpx.Response(200, json={"properties": schema})

        def factory(token: str) -> NotionClient:
            return NotionClient(token, httpx.MockTransport(handler))

        store = NotionStore(tmp_path / "n.db", "x" * 32)
        store.put_credential(1, "secret")
        store.save_target(1, {"id": TARGET, "kind": "data_source", "mapping": {}})
        service = NotionService(store, Worker(), factory)  # type: ignore[arg-type]
        assert len(await service.schema(1, TARGET)) == 5
        assert not mutations
        assert await service.schema(1, TARGET, add=True) == {}
        assert len(mutations) == 1 and len(schema) == len(FIELDS)
        assert store.targets(1)[0]["mapping"]["Title"] == "Name"

    asyncio.run(run())


def test_rich_blocks_no_truncation() -> None:
    value = normalize_worker_result(
        {
            "sourceUrl": "https://example.com",
            "platform": "x",
            "markdownContent": "# h\n\n```weirdlang\n" + "x" * 9000 + "\n```\n\n|a|b|\n|-|-|\n|1|2|\n\n> quote",
            "media": [],
        }
    )
    blocks = article_blocks(value, {})
    assert sum(len(t["text"]["content"]) for b in blocks if b["type"] == "code" for t in b["code"]["rich_text"]) == 9000
    assert any(b["type"] == "table" for b in blocks)
    assert len(batches(blocks)) >= 1


def test_media_upload_and_fallback() -> None:
    async def run() -> None:
        picture = io.BytesIO()
        Image.new("RGB", (2, 2)).save(picture, "PNG")

        class MediaWorker(Worker):
            async def download_media(self, *args: Any, **kwargs: Any) -> bytes:
                return picture.getvalue()

        class Upload:
            async def upload(self, *args: Any) -> str:
                return PAGE

        value = normalize_worker_result(
            {
                "sourceUrl": "https://example.com",
                "platform": "x",
                "leaseId": "l",
                "media": [{"mediaId": "m", "type": "photo", "mimeType": "image/png"}],
            }
        )
        output, warnings = await upload_media(value, Upload(), MediaWorker())  # type: ignore[arg-type]
        assert output["m"]["image"]["file_upload"]["id"] == PAGE and not warnings
        value.media[0].mime_type = "application/octet-stream"
        output, warnings = await upload_media(value, Upload(), MediaWorker())  # type: ignore[arg-type]
        assert output["m"]["type"] == "paragraph" and warnings

    asyncio.run(run())
