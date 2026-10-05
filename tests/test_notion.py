import asyncio
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from simpread.domain import normalize_worker_result
from simpread.integrations.notion import NotionClient, NotionError, NotionService, NotionStore
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
