"""Small-red-book contract fixtures, with no real credentials or platform requests."""

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from test_bot_oauth import message

from simpread.domain import normalize_worker_result
from simpread.integrations.notion.blocks import article_blocks
from simpread.telegram.app import MESSAGES
from simpread.telegram.input import message_urls
from simpread.telegram.media import send_preview
from simpread.worker import WorkerClient, WorkerError, extract_urls

NOTE = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
SIGNED = NOTE + "?xsec_token=fixture%2B%2F%3D&xsec_source=pc_share"


def payload(count: int = 1) -> dict[str, Any]:
    return {
        "platform": "xhs", "sourceUrl": SIGNED, "canonicalUrl": NOTE,
        "title": "小红书笔记", "contentFormat": "plain", "plainContent": "第一步：准备食材\n第二步：烹饪\n#家常菜 😀",
        "leaseId": "lease", "media": [
            {"mediaId": f"p{i}", "type": "photo", "mimeType": "image/jpeg", "filename": f"{i}.jpg", "sizeBytes": 3}
            for i in range(count)
        ],
    }


def test_chinese_share_text_and_hidden_links_keep_access_query() -> None:
    assert extract_urls("分享笔记“http://xhslink.com/a/abc”，复制后打开小红书！") == ["https://xhslink.com/a/abc"]
    assert extract_urls(f"正文（{SIGNED}）更多说明") == [SIGNED]
    assert extract_urls("http://xhslink.cn/a/abc https://xhslink.cn/a/abc") == ["https://xhslink.cn/a/abc"]
    from types import SimpleNamespace

    msg = message(text="小红书笔记")
    msg.entities = [SimpleNamespace(url=SIGNED)]
    assert message_urls(msg) == [SIGNED]


def test_worker_receives_signed_url_and_rejects_false_xhs_hosts() -> None:
    sent: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "xhs"}]})
        sent.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"id": "job", "status": "ready", "results": [payload()]})

    async def run() -> None:
        worker = WorkerClient("http://worker", "fixture", "1", transport=httpx.MockTransport(handler))
        try:
            for url in (SIGNED, "http://xhslink.cn/a/abc", NOTE.replace("/explore/", "/discovery/item/")):
                await worker.prepare(url)
            assert not await worker.supported("https://evilxiaohongshu.com/explore/abc")
            assert not await worker.supported("https://www.xiaohongshu.com.evil.example/explore/abc")
        finally:
            await worker.close()

    asyncio.run(run())
    assert sent == [SIGNED, "https://xhslink.cn/a/abc", NOTE.replace("/explore/", "/discovery/item/")]


@pytest.mark.parametrize("code", ["credentials_required", "credentials_invalid", "content_unavailable",
                                 "upstream_timeout", "upstream_contract", "media_failed", "private_unknown"])
def test_known_worker_failure_survives_without_error_details(code: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "xhs"}]})
        return httpx.Response(200, json={"id": "job", "status": "failed", "results": [
            {"error": {"code": code, "message": "private diagnostic"}}
        ]})

    async def run() -> None:
        worker = WorkerClient("http://worker", "fixture", "1", transport=httpx.MockTransport(handler))
        try:
            expected = "parse_failed" if code == "private_unknown" else code
            with pytest.raises(WorkerError) as caught:
                await worker.prepare(SIGNED)
            assert str(caught.value) == expected
            if code != "private_unknown":
                assert code in MESSAGES
        finally:
            await worker.close()

    asyncio.run(run())


def test_note_newlines_tags_and_complete_live_photo_order_in_exports() -> None:
    value = payload(12)
    value["media"][3].update(type="live_photo", videoMediaId="motion", videoMimeType="video/mp4")
    article = normalize_worker_result(value)
    assert "准备食材  \n第二步" in article.markdown
    assert "准备食材<br/>第二步" in article.html
    assert "#家常菜 😀" in article.blocks[0].text
    expected = ["p0", "p1", "p2", "p3", "motion", *[f"p{i}" for i in range(4, 12)]]
    assert [b.media_id for b in article.blocks if b.media_id] == expected
    assert article.markdown.index("3.jpg") < article.markdown.index("video：附件") < article.markdown.index("4.jpg")
    media = {mid: {"type": "image" if mid != "motion" else "video", "fixture_id": mid} for mid in expected}
    notion = article_blocks(article, media)
    assert [b["fixture_id"] for b in notion if "fixture_id" in b] == expected
    assert notion[0]["paragraph"]["rich_text"][0]["text"]["content"] == value["plainContent"]


@pytest.mark.parametrize("fail_download,fail_album", [(False, False), (True, False), (False, True)])
def test_preview_all_images_in_order_and_no_blind_replay(fail_download: bool, fail_album: bool) -> None:
    async def run() -> None:
        sent: list[list[bytes]] = []
        worker = AsyncMock()

        async def download(_: str, mid: str, **kwargs: Any) -> bytes:
            if fail_download and mid == "p5":
                raise WorkerError("media_unavailable")
            return mid.encode()

        worker.download_media.side_effect = download
        msg = message()

        async def group(items: list[Any]) -> None:
            sent.append([item.media.getvalue() for item in items])
            if fail_album and len(sent) == 1:
                raise TimeoutError("delivery unknown")

        msg.reply_media_group = AsyncMock(side_effect=group)
        value = payload(23)
        value["media"][3].update(type="live_photo", videoMediaId="motion", videoMimeType="video/mp4")
        failed = await send_preview(msg, normalize_worker_result(value), cast(Any, worker))
        expected = [b"p0", b"p1", b"p2", b"p3", b"motion", *[f"p{i}".encode() for i in range(4, 23)]]
        if fail_download:
            expected.remove(b"p5")
        assert [item for batch in sent for item in batch] == expected
        assert all(2 <= len(batch) <= 10 for batch in sent)
        assert worker.download_media.await_count == 24
        assert failed == (10 if fail_album else 1 if fail_download else 0)
        msg.reply_photo.assert_not_awaited()
        msg.reply_video.assert_not_awaited()

    asyncio.run(run())


def test_preview_cancellation_propagates() -> None:
    async def run() -> None:
        worker = AsyncMock()
        worker.download_media.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await send_preview(message(), normalize_worker_result(payload()), cast(Any, worker))

    asyncio.run(run())
