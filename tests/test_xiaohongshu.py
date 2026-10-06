"""Small-red-book contract fixtures, with no real credentials or platform requests."""

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from test_bot_oauth import message

from nomodo.domain import normalize_worker_result
from nomodo.integrations.notion.blocks import article_blocks
from nomodo.telegram.app import MESSAGES
from nomodo.telegram.input import message_urls
from nomodo.telegram.media import DeliveryUncertain, prepare_media, send_preview
from nomodo.telegram.ui import buttons
from nomodo.worker import WorkerClient, WorkerError, extract_urls

NOTE = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
SIGNED = NOTE + "?xsec_token=fixture%2B%2F%3D&xsec_source=pc_share"


def payload(count: int = 1) -> dict[str, Any]:
    return {
        "platform": "xhs", "sourceUrl": SIGNED, "canonicalUrl": NOTE,
        "title": "小红书笔记", "contentFormat": "plain",
        "plainContent": (
            "小红书笔记\n第一步：准备食材\n第二步：烹饪 #家常菜 😀\n"
            "#下饭菜\n#家常菜[话题]#\n#家常菜 😀\n复制后打开【小红书】查看笔记！"
        ),
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
    assert [block.text for block in article.blocks if block.type == "paragraph"] == [
        "第一步：准备食材", "第二步：烹饪 #家常菜 😀",
    ]
    assert "#下饭菜" not in article.markdown and "复制后打开" not in article.markdown
    assert "[话题]" not in article.markdown
    expected = ["p0", "p1", "p2", "p3", "motion", *[f"p{i}" for i in range(4, 12)]]
    assert [b.media_id for b in article.blocks if b.media_id] == expected
    assert article.markdown.index("3.jpg") < article.markdown.index("video：附件") < article.markdown.index("4.jpg")
    media = {mid: {"type": "image" if mid != "motion" else "video", "fixture_id": mid} for mid in expected}
    notion = article_blocks(article, media)
    assert [b["fixture_id"] for b in notion if "fixture_id" in b] == expected
    assert notion[0]["paragraph"]["rich_text"][0]["text"]["content"] == "第一步：准备食材"


def test_other_platforms_keep_tags_and_single_newlines() -> None:
    raw = payload()
    raw["platform"] = "wechat"
    raw["plainContent"] = "第一行\n#标签\n复制后打开小红书"
    article = normalize_worker_result(raw)
    assert article.blocks[0].text == raw["plainContent"]


@pytest.mark.parametrize("fail_download", [False, True])
def test_preview_keeps_live_photo_order_inside_one_rich_message(fail_download: bool) -> None:
    async def run() -> None:
        downloaded: list[str] = []
        worker = AsyncMock()

        async def download(_: str, mid: str, **kwargs: Any) -> bytes:
            downloaded.append(mid)
            if fail_download and mid == "p5":
                raise WorkerError("media_unavailable")
            return mid.encode()

        worker.download_media.side_effect = download
        value = payload(23)
        value["media"][3].update(type="live_photo", videoMediaId="motion", videoMimeType="video/mp4")
        article = normalize_worker_result(value)
        blocks, warnings = await prepare_media(article, cast(Any, worker))
        assert downloaded[:6] == ["p0", "p1", "p2", "p3", "motion", "p4"]
        assert len(downloaded) == 20 and warnings and len(blocks) == (19 if fail_download else 20)
        msg = message()
        await send_preview(msg, article, cast(Any, worker), buttons([]))
        msg.reply_rich.assert_awaited_once()
        msg.reply_photo.assert_not_awaited()
        msg.reply_video.assert_not_awaited()

    asyncio.run(run())


def test_uncertain_rich_send_is_not_retried() -> None:
    async def run() -> None:
        worker = AsyncMock()
        worker.download_media = AsyncMock(return_value=b"p")
        msg = message()
        msg.reply_rich.side_effect = TimeoutError("delivery unknown")
        with pytest.raises(DeliveryUncertain):
            await send_preview(msg, normalize_worker_result(payload()), cast(Any, worker), buttons([]))
        msg.reply_rich.assert_awaited_once()

    asyncio.run(run())


def test_preview_cancellation_propagates() -> None:
    async def run() -> None:
        worker = AsyncMock()
        worker.download_media.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await send_preview(message(), normalize_worker_result(payload()), cast(Any, worker), buttons([]))

    asyncio.run(run())
