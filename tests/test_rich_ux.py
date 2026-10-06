import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pyrogram import enums, raw, types
from pyrogram.errors import BadRequest
from test_bot_oauth import message, settings

from nomodo.domain import normalize_worker_result
from nomodo.domain.models import Block, MediaAsset
from nomodo.integrations.openai.schemas import Enhancement
from nomodo.telegram.app import App
from nomodo.telegram.input import message_urls
from nomodo.telegram.media import DeliveryUncertain, prepare_media, send_preview
from nomodo.telegram.presentation import ai_rich, article_rich
from nomodo.telegram.ui import buttons
from nomodo.worker import PreparedArticle, WorkerError


def article() -> Any:
    return normalize_worker_result({'sourceUrl': 'https://example.com/a', 'platform': 'fixture',
                                    'title': '<unsafe> & 标题', 'content': '正文', 'media': []})


def test_rich_native_blocks_and_media_serialize_without_sending() -> None:
    async def run() -> None:
        value = article()
        value.blocks = [
            Block(type='heading', level=2, text='标题'), Block(type='paragraph', text='<b>文字</b>'),
            Block(type='code', text='print(1)', language='python'), Block(type='quote', text='引用'),
            Block(type='ordered_list', items=['甲', '乙']), Block(type='table', rows=[['列一', '列二'], ['A', 'B']]),
        ]
        stream = io.BytesIO(b'fake-upload')
        stream.name = 'photo.jpg'
        photo = types.InputRichBlockPhoto(types.InputMediaPhoto(stream))
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerUser(user_id=1, access_hash=2)),
            save_file=AsyncMock(return_value=raw.types.InputFile(id=1, parts=1, name='photo', md5_checksum='')),
            invoke=AsyncMock(return_value=SimpleNamespace(photo=SimpleNamespace(id=3, access_hash=4,
                                                                               file_reference=b'ref'))),
        )
        payload = article_rich(value, [photo], [], 30)
        wire = await payload.write(client=cast(Any, client), chat_id=1)
        assert isinstance(wire, raw.types.InputRichMessage)
        names = [type(b).__name__ for b in wire.blocks]
        assert {'PageBlockTable', 'PageBlockPreformatted', 'PageBlockOrderedList', 'PageBlockPhoto'} <= set(names)
        assert len(wire.photos or []) == 1
        paragraph = wire.blocks[2]
        assert isinstance(paragraph, raw.types.PageBlockParagraph)
        assert isinstance(paragraph.text, raw.types.TextPlain)
        assert paragraph.text.text == '<b>文字</b>'
        assert all(isinstance(call.args[0], raw.functions.messages.UploadMedia)
                   for call in client.invoke.call_args_list)
        assert wire.noautolink is True
    asyncio.run(run())


def test_real_message_rich_reply_keeps_thread_and_buttons() -> None:
    async def run() -> None:
        client = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(id=88)))
        msg = types.Message(id=4, client=cast(Any, client), chat=types.Chat(id=-10, type=enums.ChatType.SUPERGROUP),
                            message_thread_id=42)
        worker = SimpleNamespace(download_media=AsyncMock())
        keyboard = buttons([[('导出', 'article:key:export')]])
        await send_preview(msg, article(), cast(Any, worker), keyboard)
        args = client.send_rich_message.call_args.kwargs
        assert args['chat_id'] == -10 and args['message_thread_id'] == 42
        assert args['reply_parameters'].message_id == 4 and args['reply_markup'] is keyboard
    asyncio.run(run())


def test_one_result_for_multiple_media_and_cleanup_progress(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = article()
        value.media = [MediaAsset(media_id=f'm{i}', lease_id='lease', type='photo') for i in range(3)]
        cast(Any, app.worker).prepare = AsyncMock(return_value=PreparedArticle(value, 'j', ('lease',), {}))
        cast(Any, app.worker).download_media = AsyncMock(return_value=b'photo')
        msg = message(text='https://example.com/a')
        try:
            await app.read(1, msg, ['https://example.com/a'])
            msg.reply_rich.assert_awaited_once()
            msg.reply_photo.assert_not_awaited()
            msg.reply_video.assert_not_awaited()
            msg.reply_document.assert_not_awaited()
            msg.reply_text.assert_awaited_once()  # transient progress only
            msg.reply_text.return_value.delete.assert_awaited_once()
            blocks = msg.reply_rich.call_args.args[0].blocks
            assert len(next(b for b in blocks if isinstance(b, types.InputRichBlockSlideshow)).blocks) == 3
        finally:
            await app.close()
    asyncio.run(run())


def test_media_failures_stay_in_one_result() -> None:
    async def run() -> None:
        value = article()
        value.media = [MediaAsset(media_id='m', lease_id='l', type='photo')]
        worker = SimpleNamespace(download_media=AsyncMock(side_effect=WorkerError('media_unavailable')))
        msg = message()
        await send_preview(msg, value, cast(Any, worker), buttons([]))
        msg.reply_rich.assert_awaited_once()
        msg.reply_text.assert_not_awaited()
        payload = msg.reply_rich.call_args.args[0]
        assert any('未嵌入' in str(getattr(b, 'text', '')) for b in payload.blocks)
    asyncio.run(run())


def test_media_count_and_size_limits() -> None:
    async def run() -> None:
        value = article()
        value.media = [MediaAsset(media_id=f'm{i}', lease_id='l', type='photo') for i in range(25)]
        worker = SimpleNamespace(download_media=AsyncMock(return_value=b'p'))
        blocks, warnings = await prepare_media(value, cast(Any, worker))
        assert len(blocks) == 20 and warnings
        assert worker.download_media.await_count == 20
    asyncio.run(run())


def test_oversize_text_has_explicit_export_notice() -> None:
    async def run() -> None:
        value = article()
        value.blocks = [Block(type='paragraph', text='中文🙂' * 20_000)]
        wire = await article_rich(value, [], [], 30).write(client=cast(Any, SimpleNamespace()), chat_id=1)
        assert '超出单条展示范围' in str(wire)
        assert len(str(wire).encode()) < 50_000
    asyncio.run(run())


@pytest.mark.parametrize('failure', [TimeoutError(), OSError()])
def test_uncertain_send_never_resends(failure: Exception, tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        cast(Any, app.worker).prepare = AsyncMock(return_value=PreparedArticle(article(), 'j', ('lease',), {}))
        cast(Any, app.worker).release = AsyncMock()
        msg = message()
        msg.reply_rich.side_effect = failure
        try:
            await app.read(1, msg, ['https://example.com/a'])
            msg.reply_rich.assert_awaited_once()
            assert app.pending.count(1) == 1
            cast(Any, app.worker).release.assert_not_awaited()
        finally:
            await app.close()
    asyncio.run(run())


def test_definite_rich_rejection_falls_back_once() -> None:
    class RichRejected(BadRequest):
        ID = 'RICH_MESSAGE_INVALID'
    async def run() -> None:
        msg = message()
        msg.reply_rich.side_effect = RichRejected()
        await send_preview(msg, article(), cast(Any, SimpleNamespace()), buttons([]))
        msg.reply_text.assert_awaited_once()
        assert '富媒体' in msg.reply_text.call_args.args[0]
    asyncio.run(run())


def test_ai_markup_is_formatted_without_external_images() -> None:
    payload = ai_rich('摘要', '**加粗**\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n![secret](http://internal/x)\n<script>x</script>')
    assert payload.html is not None
    assert '<strong>加粗</strong>' in payload.html and '<table>' in payload.html
    assert '<img' not in payload.html and '<script>' not in payload.html
    assert not payload.media


def test_hidden_and_caption_urls() -> None:
    msg = SimpleNamespace(text=None, caption='分享 https://example.com/a', entities=None,
                          caption_entities=[SimpleNamespace(url='https://example.com/b')])
    assert message_urls(msg) == ['https://example.com/a', 'https://example.com/b']


def test_recent_articles_and_settings_enforce_owner_and_chat(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        app.pending.put(1, 1, article(), (), {})
        app.pending.put(1, 2, article(), (), {})
        app.pending.put(2, 1, article(), (), {})
        try:
            assert len(app.pending.recent(1, 1)) == 1
            await app.menu_action(1, message(), 'ui:1:llm:off')
            assert app.pending.preferences(1)['llm'] is False
            with pytest.raises(PermissionError):
                await app.menu_action(2, message(uid=2), 'ui:1:llm:on')
            assert app.pending.preferences(1)['llm'] is False
            msg = message()
            await app.notion_command(1, msg, ['connect'])
            assert '管理员配置' in msg.reply_text.call_args.args[0]
        finally:
            await app.close()
    asyncio.run(run())


def test_ai_shows_requested_field_and_preserves_source(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = article()
        key = app.pending.put(1, 1, value, (), {})
        client = SimpleNamespace(enhance=AsyncMock(return_value=Enhancement(
            summary='**摘要**', suggested_title='其他')),
                                 close=AsyncMock())
        cast(Any, app).llm_client = lambda *_: client
        msg = message()
        try:
            await app.enhance(1, msg, key, 'summary')
            payload = msg.reply_rich.call_args.args[0]
            assert '<strong>摘要</strong>' in payload.html and '其他' not in payload.html
            assert app.pending.derived(1, key) == {'summary': '**摘要**'}
            saved = app.pending.get(1, 1, key)
            assert saved is not None and saved[0].content_hash == value.content_hash
        finally:
            await app.close()
    asyncio.run(run())


def test_uncertain_preview_return_keeps_no_duplicate() -> None:
    async def run() -> None:
        msg = message()
        msg.reply_rich.return_value = None
        with pytest.raises(DeliveryUncertain):
            await send_preview(msg, article(), cast(Any, SimpleNamespace()), buttons([]))
        msg.reply_text.assert_not_awaited()
    asyncio.run(run())


def test_cancel_button_cannot_cancel_another_chat(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        task = asyncio.create_task(asyncio.sleep(60))
        app.active[1] = task
        app.active_chat[1] = 1
        try:
            query = SimpleNamespace(from_user=SimpleNamespace(id=1), message=message(chat_id=2),
                                    data='ui:1:cancel', answer=AsyncMock())
            await app.callback(None, query)
            assert not task.cancelling()
            query.message = message(chat_id=1)
            await app.callback(None, query)
            assert task.cancelling()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.close()
    asyncio.run(run())


@pytest.mark.parametrize(("flag", "expected"), [("reused", "原来的页面"), ("recreated", "重新创建")])
def test_save_notice_distinguishes_existing_and_recreated_pages(tmp_path: Path, flag: str, expected: str) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        key = app.pending.put(1, 1, article(), (), {})
        app.notion = cast(Any, SimpleNamespace(export_page=AsyncMock(return_value={
            "url": "https://www.notion.so/page", "warnings": [], flag: True,
        })))
        msg = message()
        try:
            await app.save(1, msg, key, "target")
            text = msg.reply_text.return_value.edit_text.call_args.args[0]
            keyboard = msg.reply_text.return_value.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
            labels = [button.text for row in keyboard for button in row]
            assert expected in text and keyboard[0][0].url == "https://www.notion.so/page"
            assert ("新建一篇" in labels) is (flag == "reused")
        finally:
            await app.close()

    asyncio.run(run())


def test_save_confirmation_says_a_new_child_page_will_be_created(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        key = app.pending.put(1, 1, article(), (), {})
        app.notion = cast(Any, SimpleNamespace(
            target=lambda *_args: {"id": "34f4f127-954e-80ec-a245-dd274f101fff", "title": "", "kind": "page"},
            schema=AsyncMock(return_value={}),
        ))
        msg = message()
        try:
            target = "34f4f127-954e-80ec-a245-dd274f101fff"
            await app.request_save(1, msg, key, target)
            text = msg.reply_text.call_args.args[0]
            assert "未命名页面" in text and "新建一篇页面" in text
            assert target in text
            assert msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "新建页面"
        finally:
            await app.close()

    asyncio.run(run())


def test_saved_article_can_open_the_old_page_or_create_another(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr("x" * 32)))
        assert app.secrets
        key = app.pending.put(1, 1, article(), (), {})
        stored = app.pending.get(1, 1, key)
        assert stored
        target = "34f4f127-954e-80ec-a245-dd274f101fff"
        app.secrets.put_credential(1, "fixture")
        app.secrets.save_export(1, stored[0].content_hash, target, {
            "status": "sent", "url": "https://www.notion.so/old", "page_id": "22222222-2222-4222-8222-222222222222",
        })
        drop = AsyncMock()
        app.notion = cast(Any, SimpleNamespace(
            target=lambda *_args: {"id": target, "title": "父页面", "kind": "page"},
            schema=AsyncMock(return_value={}),
            drop_checkpoints=drop,
        ))
        msg = message()
        try:
            await app.request_save(1, msg, key)
            text = msg.reply_text.call_args.args[0]
            keyboard = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
            assert "已经保存过" in text and "再新建一篇" in text
            assert keyboard[0][0].url == "https://www.notion.so/old"
            assert keyboard[1][0].text == "新建一篇"
            drop.assert_awaited_once_with(1, stored[0].content_hash, target)
        finally:
            await app.close()

    asyncio.run(run())


def test_first_save_can_choose_a_new_page(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr("x" * 32)))
        assert app.secrets and app.notion
        app.secrets.put_credential(1, "fixture")
        key = app.pending.put(1, 1, article(), (), {})
        export_page = AsyncMock()
        cast(Any, app.notion).export_page = export_page
        msg = message()
        try:
            await app.request_save(1, msg, key)
            text = msg.reply_text.call_args.args[0]
            labels = [button.text for row in msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
                      for button in row]
            assert "保存这篇新文章" in text and labels[:3] == ["新建页面", "放到已有页面下面", "暂不保存"]
            assert app.pending.preferences(1)["notion_resume"]["key"] == key
            await app.menu_action(1, msg, "ui:1:new_page")
            confirm = msg.edit_text.call_args.args[0]
            assert "独立页面" in confirm and "不放到已有页面下面" in confirm
            nonce = msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
            payload = app.pending.consume(1, 1, nonce)
            assert payload and payload["target"] == "workspace" and payload["key"] == key
            assert not payload.get("force_new")
            export_page.assert_not_awaited()
        finally:
            await app.close()

    asyncio.run(run())


def test_save_parent_list_offers_a_new_page_only_while_saving(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr("x" * 32)))
        assert app.secrets and app.notion
        app.secrets.put_credential(1, "fixture")
        parent = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        cast(Any, app.notion).targets = AsyncMock(return_value=[
            {"id": parent, "title": "", "kind": "page", "default": False},
        ])
        try:
            settings_list = message()
            await app.notion_targets(1, settings_list)
            keyboard = settings_list.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
            setting_labels = [button.text for row in keyboard for button in row]
            assert "新建页面" not in setting_labels and "未命名页面 · 页面" in setting_labels
            assert "选择 Notion 保存位置" in settings_list.reply_text.call_args.args[0]
            key = app.pending.put(1, 1, article(), (), {})
            app.pending.preferences(1, {"notion_resume": {"key": key, "chat_id": 1}})
            saving = message()
            await app.notion_targets(1, saving)
            rows = saving.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
            assert rows[0][0].text == "新建页面" and rows[0][0].callback_data == "ui:1:new_page"
            assert "放到已有页面下面" in saving.reply_text.call_args.args[0]
            assert "未命名页面 · 页面" in rows[1][0].text
        finally:
            await app.close()

    asyncio.run(run())


def test_workspace_permission_offers_an_existing_parent(tmp_path: Path) -> None:
    from nomodo.integrations.notion import NotionError
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr("x" * 32)))
        key = app.pending.put(1, 1, article(), (), {})
        app.notion = cast(Any, SimpleNamespace(export_page=AsyncMock(
            side_effect=NotionError("permission_denied", 403))))
        msg = message()
        try:
            await app.save(1, msg, key, "workspace")
            edited = msg.reply_text.return_value.edit_text.call_args
            assert "不能直接新建独立页面" in edited.args[0]
            assert edited.kwargs["reply_markup"].inline_keyboard[0][0].text == "放到已有页面下面"
            assert app.pending.preferences(1)["notion_resume"]["key"] == key
        finally:
            await app.close()

    asyncio.run(run())


def test_failed_notion_save_keeps_media_for_retry(tmp_path: Path) -> None:
    from nomodo.integrations.notion import NotionError
    async def run() -> None:
        app = App(settings(tmp_path))
        key = app.pending.put(1, 1, article(), ('lease',), {})
        app.notion = cast(Any, SimpleNamespace(export_page=AsyncMock(side_effect=NotionError('notion_unavailable'))))
        cast(Any, app.worker).release = AsyncMock()
        try:
            await app.save(1, message(), key, 'target')
            saved = app.pending.get(1, 1, key)
            assert saved is not None and saved[1] == ('lease',)
            cast(Any, app.worker).release.assert_not_awaited()
        finally:
            await app.close()
    asyncio.run(run())


def test_complex_ai_layout_uses_bounded_literal_fallback() -> None:
    text = '| ' + ' | '.join('column' for _ in range(25)) + ' |\n'
    text += '| ' + ' | '.join('---' for _ in range(25)) + ' |\n'
    text += '| ' + ' | '.join('value' for _ in range(25)) + ' |\n'
    payload = ai_rich('宽表格', text)
    assert payload.blocks and payload.html is None


def test_actual_sdk_send_path_issues_one_rich_rpc() -> None:
    from pyrogram import Client
    async def run() -> None:
        client = Client('offline', api_id=1, api_hash='fixture', in_memory=True)
        cast(Any, client).resolve_peer = AsyncMock(return_value=raw.types.InputPeerUser(user_id=1, access_hash=2))
        cast(Any, client).invoke = AsyncMock(return_value=raw.types.UpdateShortSentMessage(
            id=99, pts=1, pts_count=1, date=1, out=True))
        msg = types.Message(id=1, client=client, chat=types.Chat(id=1, type=enums.ChatType.PRIVATE))
        await send_preview(msg, article(), cast(Any, SimpleNamespace()), buttons([[('导出', 'article:key:export')]]))
        call = cast(Any, client).invoke.call_args.args[0]
        assert isinstance(call, raw.functions.messages.SendMessage)
        assert isinstance(call.rich_message, raw.types.InputRichMessage)
        assert call.message == '' and call.reply_markup is not None
        cast(Any, client).invoke.assert_awaited_once()
    asyncio.run(run())
