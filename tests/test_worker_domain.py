import asyncio
import json
from typing import Any

import httpx
import pytest

from nomodo.domain import normalize_worker_result
from nomodo.domain.normalize import public_result
from nomodo.domain.urls import safe_url
from nomodo.worker import WorkerClient, WorkerError

URL = "https://www.youtube.com/watch?v=fixture"


def result() -> dict[str, Any]:
    return {
        "sourceUrl": URL,
        "canonicalUrl": URL,
        "platform": "youtube",
        "title": "T",
        "content": "body",
        "media": [
            {
                "mediaId": "m1",
                "type": "live_photo",
                "mimeType": "image/jpeg",
                "videoMediaId": "m2",
                "filename": "/private/work/image.jpg",
                "sizeBytes": 10,
                "durationSeconds": 1.5,
            }
        ],
        "leaseId": "lease",
        "_files": ["/private/x"],
        "_mediaFiles": {"m1": {"path": "/private/x"}},
        "nativeError": "cookie=private",
    }


def test_article_preserves_blocks_live_pair_and_stable_identity() -> None:
    payload = result()
    payload.update(
        markdownContent="# Header\n\n> quote\n\n- one\n- two\n\n```python\nprint(1)\n```\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n[link](https://example.com)"
    )
    article = normalize_worker_result(payload)
    assert {b.type for b in article.blocks} >= {
        "heading",
        "quote",
        "unordered_list",
        "code",
        "table",
        "link",
        "image",
        "video",
    }
    assert article.media[0].video_media_id == "m2"
    assert article.media[0].filename == "image.jpg"
    assert "<table>" in article.html and "print(1)" in article.markdown
    payload["leaseId"] = "another"
    payload["media"][0]["mediaId"] = "another_media"
    payload["media"][0]["videoMediaId"] = "another_video"
    assert normalize_worker_result(payload).content_hash == article.content_hash
    public = json.dumps(public_result(payload))
    assert "/private/" not in public and "_files" not in public and "cookie" not in public


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "http://user:pass@example.com",
        "http://0x7f000001/",
        "https://localhost",
        "file:///tmp/test",
    ],
)
def test_unsafe_urls_are_not_rendered(url: str) -> None:
    assert safe_url(url) is None


def test_poll_success_and_leased_download_contract() -> None:
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("capabilities"):
            return httpx.Response(
                200,
                json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "youtube", "name": "YouTube"}]},
            )
        if req.method == "POST":
            body = json.loads(req.content)
            assert "delivery" not in body and body["accountId"] == "123"
            return httpx.Response(202, json={"id": "job", "status": "queued", "results": []})
        if req.url.path.endswith("/jobs/job") and req.method == "GET":
            return httpx.Response(200, json={"id": "job", "status": "ready", "results": [result()]})
        if req.url.path.endswith("/media/m1"):
            return httpx.Response(200, content=b"bytes", headers={"Content-Type": "image/jpeg"})
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        worker = WorkerClient("http://worker", "secret", "123", transport=httpx.MockTransport(handler), poll_interval=0)
        article = await worker.prepare(URL)
        assert article.lease_ids == ("lease",) and "_files" not in article.raw_result
        assert await worker.download_media("lease", "m1", mime="image/jpeg") == b"bytes"
        await worker.renew("lease")
        await worker.release("lease")
        await worker.close()

    asyncio.run(run())
    assert ("PUT", "/api/v1/leases/lease") in calls
    assert ("DELETE", "/api/v1/leases/lease") in calls


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_cancel_job(cancel: bool) -> None:
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        if req.url.path.endswith("capabilities"):
            return httpx.Response(
                200, json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "youtube"}]}
            )
        return httpx.Response(200, json={"id": "job", "status": "queued", "results": []})

    async def run() -> None:
        worker = WorkerClient("http://worker", "secret", "123", transport=httpx.MockTransport(handler))
        if cancel:
            task = asyncio.create_task(worker.prepare(URL))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(WorkerError, match="worker_timeout"):
                await worker.prepare(URL, timeout_seconds=0.01)
        await worker.close()

    asyncio.run(run())
    assert "DELETE" in calls


def test_protocol_mismatch_and_media_limits() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"protocolVersion": 3, "version": "0", "platforms": []})
        return httpx.Response(200, content=b"large", headers={"Content-Type": "text/plain"})

    async def run() -> None:
        worker = WorkerClient("http://worker", "secret", "123", transport=httpx.MockTransport(handler))
        with pytest.raises(WorkerError, match="worker_version_mismatch"):
            await worker.capabilities()
        with pytest.raises(WorkerError, match="media_too_large"):
            await worker.download_media("lease", "m", limit=2)
        with pytest.raises(WorkerError, match="media_unavailable"):
            await worker.download_media("lease", "m", mime="image/jpeg")
        await worker.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "malformed", [{"status": "ready", "results": None}, {"status": "ready", "results": [None]}, []]
)
def test_malformed_poll_keeps_job_identity_for_cleanup(malformed: Any) -> None:
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("capabilities"):
            return httpx.Response(
                200, json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "youtube"}]}
            )
        if req.method == "POST":
            return httpx.Response(202, json={"id": "job", "status": "queued"})
        if req.method == "GET":
            return httpx.Response(200, json=malformed)
        return httpx.Response(200, json={})

    async def run() -> None:
        worker = WorkerClient("http://worker", "secret", "123", transport=httpx.MockTransport(handler), poll_interval=0)
        try:
            with pytest.raises(WorkerError, match="worker_contract_error"):
                await worker.prepare(URL)
        finally:
            await worker.close()

    asyncio.run(run())
    assert ("DELETE", "/api/v1/jobs/job") in calls


@pytest.mark.parametrize("media", [None, ["bad"]])
def test_invalid_media_keeps_contract_error_and_releases_lease(media: Any) -> None:
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("capabilities"):
            return httpx.Response(
                200, json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "youtube"}]}
            )
        if req.method == "POST":
            return httpx.Response(200, json={"id": "job", "status": "ready", "results": [{**result(), "media": media}]})
        return httpx.Response(200, json={})

    async def run() -> None:
        worker = WorkerClient("http://worker", "secret", "123", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(WorkerError, match="worker_contract_error"):
                await worker.prepare(URL)
        finally:
            await worker.close()

    asyncio.run(run())
    assert ("DELETE", "/api/v1/leases/lease") in calls


def test_html_structure_and_embedded_links_survive_plain_fallback() -> None:
    value = normalize_worker_result(
        {
            "sourceUrl": URL,
            "platform": "youtube",
            "plainContent": "heading item cell",
            "htmlContent": '<h2>heading</h2><ul><li>item <a href="https://example.com/list">link</a></li></ul>'
            '<table><tr><td>cell<img src="https://example.com/a.png"></td></tr></table>',
        }
    )
    assert {b.type for b in value.blocks} >= {"heading", "unordered_list", "table", "image"}
    nested = next(b for b in value.blocks if b.type == "unordered_list")
    assert nested.children[0].children[0].inlines[-1].url == "https://example.com/list"
    assert "https://example.com/list" in value.markdown and "<table>" in value.html
    alternate = normalize_worker_result(
        {"sourceUrl": URL, "platform": "youtube", "contentFormat": "html", "content": "<h2>heading</h2>"}
    )
    assert alternate.blocks[0].type == "heading"
