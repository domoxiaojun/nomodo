import asyncio
import copy
import io
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from test_bot_oauth import message

from nomodo.domain import normalize_worker_result
from nomodo.domain.models import Article
from nomodo.integrations.notion.blocks import article_blocks
from nomodo.integrations.notion.client import NotionError
from nomodo.integrations.notion.media import upload_media
from nomodo.integrations.notion.tree import recover_tree, write_tree
from nomodo.telegram.media import send_preview
from nomodo.telegram.ui import buttons
from nomodo.worker import WorkerClient


def test_rich_document_roundtrip_and_media_provenance() -> None:
    raw: dict[str, Any] = {"sourceUrl": "https://example.com/article", "platform": "weixin", "leaseId": "first",
           "markdownContent": "**bold** and [inside](/relative)\n\n- parent\n  - child `code`\n\n"
                              "> quote *emphasis*\n\n![alt](https://example.com/image.jpg)",
           "media": [{"mediaId": "photo", "type": "photo", "mimeType": "image/jpeg",
                      "originalUrl": "https://example.com/image.jpg"}]}
    article = normalize_worker_result(raw)
    assert "**bold**" in article.markdown
    assert article.markdown.count("inside") == 1
    assert "https://example.com/relative" in article.markdown
    assert "<strong>bold</strong>" in article.html
    assert "<ul><li><p>child" in article.html
    assert "<em>emphasis</em>" in article.html
    assert article.markdown.count("image.jpg") == 1 and "附件" not in article.markdown
    images = [b for b in article.blocks if b.type == "image"]
    assert len(images) == 1 and images[0].media_id == "photo"
    assert Article.model_validate_json(article.model_dump_json()) == article
    raw["leaseId"] = "second"
    raw["media"][0]["mediaId"] = "newphoto"
    assert normalize_worker_result(raw).content_hash == article.content_hash
    blocks = article_blocks(article, {})
    assert blocks[0]["paragraph"]["rich_text"][0]["annotations"]["bold"]
    parent = next(b for b in blocks if b["type"] == "bulleted_list_item")
    assert parent["bulleted_list_item"]["children"][0]["type"] == "bulleted_list_item"


def test_legacy_document_and_unsafe_inline_link() -> None:
    article = normalize_worker_result({"sourceUrl": "https://example.com", "content": "old"})
    legacy = article.model_dump()
    legacy.pop("schema_version")
    for b in legacy["blocks"]:
        for field in ("inlines", "children", "cells", "start", "header"):
            b.pop(field)
    assert Article.model_validate(legacy).content_hash == article.content_hash
    rendered = normalize_worker_result({"sourceUrl": "https://example.com", "htmlContent":
                                       '<p>a<br>b<a href="javascript:alert(1)">bad</a></p>'})
    assert "javascript:" not in rendered.html and "a<br/>b" in rendered.html


def test_notions_gif_pdf_audio_are_not_video() -> None:
    async def run() -> None:
        data = io.BytesIO()
        Image.new("RGB", (1, 1)).save(data, format="GIF")
        binaries = {"gif": data.getvalue(), "pdf": b"%PDF-1.7\nfixture", "audio": b"ID3fixture"}
        raw = {"sourceUrl": "https://example.com", "leaseId": "lease", "media": [
            {"mediaId": key, "type": "animation" if key == "gif" else "document", "mimeType": mime}
            for key, mime in [("gif", "image/gif"), ("pdf", "application/pdf"), ("audio", "audio/mpeg")]]}
        article = normalize_worker_result(raw)
        assert [b.type for b in article.blocks] == ["image", "file", "audio"]
        worker = AsyncMock()
        worker.download_media.side_effect = lambda lease, mid, **kw: binaries[mid]
        client = AsyncMock()
        client.upload.return_value = "id"
        uploaded, warnings = await upload_media(article, client, worker)
        assert not warnings
        assert [uploaded[key]["type"] for key in binaries] == ["image", "file", "audio"]
    asyncio.run(run())


def test_nested_notion_timeout_recovery_does_not_duplicate() -> None:
    async def run() -> None:
        article = normalize_worker_result({"sourceUrl": "https://example.com", "markdownContent":
                                          "- first\n  - second\n    - third **bold**\n\nend"})
        page = str(uuid4())
        job: dict[str, Any] = {"blocks": article_blocks(article, {}), "page_id": page,
                               "marker": "fixture", "in_flight": False}
        remote: dict[str, list[Any]] = {}
        calls = 0

        async def append(parent: str, blocks: list[Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            assert not any(b.get(b["type"], {}).get("children") for b in blocks)
            values = [{**copy.deepcopy(b), "id": str(uuid4())} for b in blocks]
            remote.setdefault(parent, []).extend(values)
            if calls == 2:
                raise NotionError("write_outcome_unknown")
            return {"results": values}

        client = AsyncMock()
        client.append.side_effect = append
        client.children.side_effect = lambda parent: remote[parent]
        snapshots = []
        with pytest.raises(NotionError, match="write_outcome_unknown"):
            await write_tree(job, client, lambda: snapshots.append(copy.deepcopy(job)))
        assert job["in_flight"] and job["tree_next"] == 1
        await recover_tree(job, client)
        await write_tree(job, client, lambda: snapshots.append(copy.deepcopy(job)))
        assert calls == len(job["tree_ops"])
        assert job["tree_next"] == len(job["tree_ops"])
        assert not job["in_flight"]
        assert any(b.get("bulleted_list_item", {}).get("rich_text", [{}])[0].get("text", {}).get("content") == "third "
                   for values in remote.values() for b in values if b["type"] == "bulleted_list_item")
    asyncio.run(run())


def test_missing_recovery_marker_keeps_unknown() -> None:
    async def run() -> None:
        job = {"tree_next": 0, "tree_ops": [{"parent": "", "blocks": [{}], "paths": ["/0"]}],
               "tree_ids": {"": str(uuid4())}, "marker": "fixture", "in_flight": True}
        client = AsyncMock()
        client.children.return_value = []
        with pytest.raises(NotionError, match="write_outcome_unknown"):
            await recover_tree(job, client)
        assert job["in_flight"] and job["tree_next"] == 0
    asyncio.run(run())


def test_oversize_preview_stays_inside_one_rich_message() -> None:
    async def run() -> None:
        downloaded = False

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal downloaded
            downloaded = True
            return httpx.Response(200, content=b"x" * 1024, headers={"Content-Type": "video/mp4"})

        worker = WorkerClient("http://worker", "fixture", "1", transport=httpx.MockTransport(handler))
        msg = message()
        article = normalize_worker_result({"sourceUrl": "https://example.com", "leaseId": "lease", "media": [
            {"mediaId": "large", "type": "video", "mimeType": "video/mp4", "sizeBytes": 21_000_000}]})
        try:
            await send_preview(msg, article, worker, buttons([]))
            assert not downloaded
            msg.reply_video.assert_not_awaited()
            msg.reply_photo.assert_not_awaited()
            assert msg.reply_rich.await_count == 1
            assert "未嵌入" in str(msg.reply_rich.call_args.args[0])
        finally:
            await worker.close()
    asyncio.run(run())


def test_small_media_cache_is_lease_scoped_and_removed_on_release() -> None:
    async def run() -> None:
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req.method)
            return httpx.Response(200, content=b"image", headers={"Content-Type": "image/png"})

        worker = WorkerClient("http://worker", "fixture", "1", transport=httpx.MockTransport(handler))
        try:
            assert await worker.download_media("lease", "img", mime="image/png") == b"image"
            assert await worker.download_media("lease", "img", mime="image/png") == b"image"
            assert calls == ["GET"]
            await worker.release("lease")
            await worker.download_media("lease", "img", mime="image/png")
            assert calls == ["GET", "DELETE", "GET"]
            await worker.download_media("other", "img", mime="image/png")
            assert calls[-1] == "GET" and len(calls) == 4
        finally:
            await worker.close()
    asyncio.run(run())


def test_reader_uses_worker_link_rules_and_validates_host() -> None:
    async def run() -> None:
        body = {"protocolVersion": 3, "version": "2.2.4", "platforms": [
            {"id": "weixin", "domains": ["mp.weixin.qq.com"],
             "urlPatterns": [r"https?://mp\.weixin\.qq\.com/s(?:/|\?).+"]},
            {"id": "youtube", "domains": ["youtube.com"], "urlPatterns": [r"https?://[^/]+/watch\?.+"]}]}
        worker = WorkerClient("http://worker", "fixture", "1", transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=body)))
        try:
            assert await worker.supported("https://mp.weixin.qq.com/s?__biz=fixture&mid=1")
            assert await worker.supported("https://www.youtube.com/watch?v=fixture")
            assert not await worker.supported("https://evilyoutube.com/watch?v=fixture")
            assert not await worker.supported("https://www.youtube.com/shorts/fixture")
        finally:
            await worker.close()
    asyncio.run(run())


def test_stream_file_cleanup_on_cancelled_send() -> None:
    async def run() -> None:
        worker = WorkerClient("http://worker", "fixture", "1", transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"fixture", headers={"Content-Type": "video/mp4"})))
        path = None
        try:
            with pytest.raises(asyncio.CancelledError):
                async with worker.media_file("lease", "media", mime="video/mp4") as path:
                    assert path.exists()
                    raise asyncio.CancelledError
            assert path is not None and not path.exists()
        finally:
            await worker.close()
    asyncio.run(run())
