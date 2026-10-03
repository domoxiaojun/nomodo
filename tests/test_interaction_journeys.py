import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pyrogram.errors import BadRequest
from test_bot_oauth import message, settings
from test_rich_ux import article

from simpread.integrations.notion import NotionError
from simpread.integrations.openai import LLMError
from simpread.integrations.openai.schemas import Enhancement
from simpread.telegram.app import App
from simpread.telegram.ui import Activity, Panel
from simpread.worker import PreparedArticle, WorkerError


def query(uid: int, data: str, chat_id: int = 1) -> Any:
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data,
                           message=message(uid=uid, chat_id=chat_id), answer=AsyncMock())


def test_busy_request_status_and_explicit_retry_journey(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        task = asyncio.create_task(asyncio.sleep(60))
        lock = asyncio.Lock()
        await lock.acquire()
        app.locks[1] = lock
        app.active[1] = task
        app.active_chat[1] = 1
        app.activities[1] = Activity(1, '正在生成私聊文章摘要')
        cast(Any, app.worker).prepare = AsyncMock(return_value=PreparedArticle(article(), 'j', (), {}))
        try:
            incoming = message(text='https://example.com/a')
            await app.dispatch(None, incoming)
            cast(Any, app.worker).prepare.assert_not_awaited()
            markup = incoming.reply_text.call_args.kwargs['reply_markup']
            retry = next(b.callback_data for row in markup.inline_keyboard for b in row
                         if b.callback_data.startswith('retry:'))
            status = query(1, 'ui:1:status')
            await app.callback(None, status)
            assert '正在生成私聊文章摘要' in status.message.edit_text.call_args.args[0]
            assert app.active[1] is task
            other_chat = message(chat_id=2, text='/status')
            await app.dispatch(None, other_chat)
            assert '私聊文章摘要' not in other_chat.reply_text.call_args.args[0]
            lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            foreign = query(2, retry)
            await app.callback(None, foreign)
            cast(Any, app.worker).prepare.assert_not_awaited()
            selected = query(1, retry)
            await app.callback(None, selected)
            selected.answer.assert_awaited_once()
            cast(Any, app.worker).prepare.assert_awaited_once()
            await app.callback(None, query(1, retry))
            cast(Any, app.worker).prepare.assert_awaited_once()
        finally:
            if lock.locked():
                lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.close()
    asyncio.run(run())


def test_read_failure_retry_and_cache_refresh_journey(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        worker = cast(Any, app.worker)
        worker.prepare = AsyncMock(side_effect=[WorkerError('parse_failed'), PreparedArticle(article(), 'j', (), {}),
                                               PreparedArticle(article(), 'j2', (), {})])
        msg = message(text='https://example.com/a')
        try:
            await app.read(1, msg, ['https://example.com/a'])
            markup = msg.reply_text.return_value.edit_text.call_args.kwargs['reply_markup']
            retry = markup.inline_keyboard[0][0].callback_data
            await app.callback(None, query(1, retry))
            assert worker.prepare.await_count == 2
            await app.read(1, message(), ['https://example.com/a'])
            assert worker.prepare.await_count == 2
            key = app.current_key(1, 1)
            await app.callback(None, query(1, f'article:{key}:refresh'))
            assert worker.prepare.await_count == 3
        finally:
            await app.close()
    asyncio.run(run())


def test_model_picker_select_reset_and_failure_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, openai_api_key=SecretStr('fixture')))
        provider = SimpleNamespace(client=SimpleNamespace(models=SimpleNamespace(list=AsyncMock(return_value=
            SimpleNamespace(data=[SimpleNamespace(id='gpt-6.1-sol'), SimpleNamespace(id='other-model')])))),
            close=AsyncMock())
        monkeypatch.setattr('simpread.telegram.app.ResponsesClient', lambda *a, **k: provider)
        try:
            msg = message()
            await app.model_choices(1, msg)
            rows = msg.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
            data = next(b.callback_data for row in rows for b in row if b.text == 'other-model')
            await app.callback(None, query(2, data))
            assert not app.pending.preferences(2)['model']
            await app.callback(None, query(1, data))
            assert app.pending.preferences(1)['model'] == 'other-model'
            await app.menu_action(1, message(), 'ui:1:reset_settings')
            assert app.pending.preferences(1)['model'] == ''
            provider.client.models.list.side_effect = RuntimeError('secret internal error')
            failed = message()
            await app.model_choices(1, failed, refresh=True)
            assert 'secret' not in failed.reply_text.call_args.args[0]
            labels = [b.text for row in failed.reply_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
            assert '手动输入' in labels and '返回设置' in labels
        finally:
            await app.close()
    asyncio.run(run())


def test_disconnect_requires_owner_confirmation_and_keeps_exports(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr('x' * 32)))
        assert app.secrets
        app.secrets.put_credential(1, 'fixture', 'workspace')
        app.secrets.save_export(1, 'article-hash', 'target', {'status': 'sent', 'page_id': 'existing'})
        msg = message()
        try:
            await app.menu_action(1, msg, 'ui:1:disconnect')
            data = msg.edit_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            assert app.secrets.credential(1)
            await app.callback(None, query(2, data))
            assert app.secrets.credential(1)
            await app.callback(None, query(1, data))
            assert app.secrets.credential(1) is None
            saved = app.secrets.export_status(1, 'article-hash', 'target')
            assert saved and saved['page_id'] == 'existing'
        finally:
            await app.close()
    asyncio.run(run())


def test_unknown_notion_write_recovery_never_blindly_retries(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr('x' * 32)))
        assert app.secrets and app.notion
        value = article()
        key = app.pending.put(1, 1, value, ('lease',), {})
        target = 'a' * 32
        app.secrets.save_export(1, value.content_hash, target, {'status': 'unknown'})
        cast(Any, app.notion).export_page = AsyncMock(side_effect=NotionError('write_outcome_unknown'))
        cast(Any, app.notion).reconcile = AsyncMock()
        msg = message()
        try:
            await app.save(1, msg, key, target)
            markup = msg.reply_text.return_value.edit_text.call_args.kwargs['reply_markup']
            data = markup.inline_keyboard[0][0].callback_data
            await app.callback(None, query(1, data))
            bad = message(text='https://evil.example/' + 'b' * 32)
            await app.dispatch(None, bad)
            assert 'Notion' in bad.reply_text.call_args.args[0]
            good = message(text='https://app.notion.com/Report-' + 'b' * 32)
            await app.dispatch(None, good)
            confirm = good.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            payload = app.pending.consume(1, 1, confirm.split(':')[1])
            assert payload and payload['target'] == target and payload['key'] == key
            cast(Any, app.notion).export_page.assert_awaited_once()
            cast(Any, app.notion).reconcile.assert_not_awaited()
        finally:
            await app.close()
    asyncio.run(run())


def test_export_filename_and_caption_are_useful_and_safe(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = article()
        value.title = '../危险/标题\n:测试'
        msg = message()
        try:
            await app.file(msg, value, 'markdown')
            sent = msg.reply_document.call_args.kwargs
            assert '/' not in sent['file_name'] and '\n' not in sent['file_name']
            assert sent['file_name'].endswith('.md') and not sent['file_name'].startswith('.')
            assert '完整原文' in sent['caption']
        finally:
            await app.close()
    asyncio.run(run())


def test_panel_edit_expiry_opens_exactly_one_replacement() -> None:
    class ExpiredEdit(BadRequest):
        ID = 'MESSAGE_EDIT_TIME_EXPIRED'
    async def run() -> None:
        msg = message()
        msg.edit_text.side_effect = ExpiredEdit()
        await Panel(msg).reply_text('新面板')
        msg.reply_text.assert_awaited_once_with('新面板')
    asyncio.run(run())


def test_ai_retry_button_and_unknown_command_have_next_steps(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        key = app.pending.put(1, 1, article(), (), {})
        llm = SimpleNamespace(enhance=AsyncMock(side_effect=[LLMError('llm_timeout'), Enhancement(summary='成功')]),
                              close=AsyncMock())
        cast(Any, app).llm_client = lambda *_: llm
        msg = message()
        try:
            await app.enhance(1, msg, key, 'summary')
            markup = msg.reply_text.return_value.edit_text.call_args.kwargs['reply_markup']
            retry = markup.inline_keyboard[0][0].callback_data
            await app.callback(None, query(1, retry))
            assert llm.enhance.await_count == 2 and app.pending.derived(1, key)['summary'] == '成功'
            invalid = message(text='/unknown')
            await app.dispatch(None, invalid)
            assert invalid.reply_text.call_args.kwargs['reply_markup']
        finally:
            await app.close()
    asyncio.run(run())


def test_expired_recovery_input_does_not_parse_private_page(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        app.pending.preferences(1, {'input': {'kind': 'notion_recover', 'chat_id': 1, 'expires': time.time() - 1}})
        cast(Any, app.worker).prepare = AsyncMock()
        msg = message(text='https://www.notion.so/' + 'a' * 32)
        try:
            await app.dispatch(None, msg)
            cast(Any, app.worker).prepare.assert_not_awaited()
            assert '过期' in msg.reply_text.call_args.args[0]
        finally:
            await app.close()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['home', 'help', 'guide:read', 'guide:ai', 'guide:notion', 'guide:controls',
                                   'read', 'articles', 'platforms', 'settings', 'reasoning', 'status', 'notion',
                                   'model_input', 'input_cancel', 'reset_settings'])
def test_primary_navigation_always_has_a_valid_exit(tmp_path: Path, action: str) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        cast(Any, app.worker).capabilities = AsyncMock(return_value=SimpleNamespace(names='YouTube、微信公众号'))
        msg = message()
        try:
            await app.menu_action(1, msg, 'ui:1:' + action)
            markup = msg.edit_text.call_args.kwargs['reply_markup']
            assert markup.inline_keyboard
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data:
                        assert len(button.callback_data.encode()) <= 64
        finally:
            await app.close()
    asyncio.run(run())


def test_callback_acknowledged_once_when_later_step_fails(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        incoming = query(1, 'ui:1:unknown_action')
        try:
            await app.callback(None, incoming)
            incoming.answer.assert_awaited_once()
            assert incoming.message.reply_text.call_args.kwargs['reply_markup']
        finally:
            await app.close()
    asyncio.run(run())


def test_expired_model_input_does_not_turn_into_ai_request(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        app.pending.put(1, 1, article(), (), {})
        app.pending.preferences(1, {'input': {'chat_id': 1, 'expires': time.time() - 1}})
        cast(Any, app).plan = AsyncMock()
        try:
            await app.dispatch(None, message(text='gpt-something'))
            cast(Any, app).plan.assert_not_awaited()
            assert app.pending.preferences(1)['model'] == ''
        finally:
            await app.close()
    asyncio.run(run())


def test_unsupported_file_and_group_private_feature_have_guidance(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        app.bot = SimpleNamespace(me=SimpleNamespace(username='domoparsebot'))
        try:
            media = message()
            await app.unsupported_input(None, media)
            assert '网页链接' in media.reply_text.call_args.args[0]
            group = message(text='/settings', kind='group')
            await app.dispatch(None, group)
            button = group.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
            assert button.url == 'https://t.me/domoparsebot'
        finally:
            await app.close()
    asyncio.run(run())
