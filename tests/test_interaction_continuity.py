import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from test_bot_oauth import message, settings
from test_rich_ux import article

from nomodo.integrations.notion import NotionStore
from nomodo.integrations.openai.schemas import Enhancement
from nomodo.oauth.server import OAuthServer
from nomodo.telegram.app import App


def test_notions_target_selection_resumes_exact_article_with_confirmation(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr('x' * 32)))
        assert app.secrets is not None
        target_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        app.secrets.put_credential(1, 'fixture')
        app.secrets.save_target(1, {'id': target_id, 'title': '收件箱', 'kind': 'page', 'default': False})
        first = article()
        first.title = '原来的文章'
        key = app.pending.put(1, 1, first, (), {})
        app.pending.put(1, 1, article(), (), {})  # A later article must not replace the intended save.
        app.pending.preferences(1, {'notion_resume': {'key': key, 'chat_id': 1}})
        cast(Any, app.notion).schema = AsyncMock(return_value={})
        cast(Any, app.notion).export_page = AsyncMock()
        msg = message()
        try:
            await app.select_notion_target(1, msg, target_id)
            assert '原来的文章' in msg.reply_text.call_args.args[0]
            keyboard = msg.reply_text.call_args.kwargs['reply_markup']
            nonce = keyboard.inline_keyboard[0][0].callback_data.split(':')[1]
            payload = app.pending.consume(1, 1, nonce)
            assert payload and payload['key'] == key and payload['target'] == target_id
            cast(Any, app.notion).export_page.assert_not_awaited()
            assert app.pending.preferences(1)['notion_resume'] is None
        finally:
            await app.close()
    asyncio.run(run())


def test_idle_cancel_and_model_input_preserve_article(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        key = app.pending.put(1, 1, article(), (), {})
        try:
            await app.dispatch(None, message(text='/cancel'))
            assert app.pending.get(1, 1, key)
            await app.menu_action(1, message(), 'ui:1:model_input')
            await app.dispatch(None, message(text='custom-model'))
            assert app.pending.preferences(1)['model'] == 'custom-model'
            await app.menu_action(1, message(), 'ui:1:effort:medium')
            assert app.pending.preferences(1)['reasoning_effort'] == 'medium'
            await app.menu_action(1, message(), 'ui:1:model_input')
            await app.dispatch(None, message(text='/cancel'))
            assert app.pending.preferences(1)['model'] == 'custom-model'
            assert app.pending.get(1, 1, key)
        finally:
            await app.close()
    asyncio.run(run())


def test_ai_reuses_matching_result_and_regenerates_explicitly(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        key = app.pending.put(1, 1, article(), (), {})
        client = SimpleNamespace(enhance=AsyncMock(return_value=Enhancement(summary='摘要')), close=AsyncMock())
        cast(Any, app).llm_client = lambda *_: client
        try:
            await app.enhance(1, message(), key, 'summary')
            await app.enhance(1, message(), key, 'summary')
            assert client.enhance.await_count == 1
            await app.enhance(1, message(), key, 'summary', regenerate=True)
            assert client.enhance.await_count == 2
            app.pending.preferences(1, {'model': 'different-model'})
            await app.enhance(1, message(), key, 'summary')
            assert client.enhance.await_count == 3
            app.pending.delete(key)
            assert app.pending.db.execute('SELECT count(*) FROM ai_results').fetchone()[0] == 0
        finally:
            await app.close()
    asyncio.run(run())


def test_home_reports_connected_notion_and_actual_target(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr('x' * 32)))
        assert app.secrets is not None
        app.secrets.put_credential(1, 'fixture', '我的空间')
        app.secrets.save_target(1, {'id': 'a' * 32, 'title': '读书笔记', 'default': True})
        msg = message()
        try:
            await app.home(1, msg)
            assert '已连接 · 读书笔记' in msg.reply_text.call_args.args[0]
        finally:
            await app.close()
    asyncio.run(run())


def test_oauth_browser_success_and_error_have_safe_next_action(tmp_path: Path) -> None:
    async def run() -> None:
        store = NotionStore(tmp_path / 'notion.db', 'x' * 32)
        cfg = settings(tmp_path, notion_client_id='client', notion_client_secret=SecretStr('secret'),
                       notion_credentials_key=SecretStr('x' * 32),
                       notion_oauth_redirect_uri='https://example.com/notion/oauth/callback')
        client = SimpleNamespace(oauth_token=AsyncMock(return_value={'access_token': 'sensitive-token'}),
                                 close=AsyncMock())
        server = OAuthServer(cfg, store, lambda _: cast(Any, client))
        server.bot_username = 'domoparsebot'
        app = web.Application()
        server.routes(app)
        state = store.oauth_begin(1)
        store.oauth_bind(state, 'browser')
        try:
            async with TestClient(TestServer(app)) as http:
                response = await http.get('/notion/oauth/callback', params={'state': state, 'code': 'private-code'},
                                          headers={'Cookie': 'nomodo_oauth=browser'})
                html = await response.text()
                assert response.status == 200 and response.content_type == 'text/html'
                assert 'https://t.me/domoparsebot?start=notion' in html
                assert 'sensitive-token' not in html and 'private-code' not in html
                assert response.headers['Cache-Control'] == 'no-store'
                error = await http.get('/notion/oauth/callback?error=access_denied')
                assert error.status == 400 and '重新授权' in await error.text()
        finally:
            store.close()
    asyncio.run(run())


def test_opened_article_becomes_current_without_cross_chat_leak(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        old = app.pending.put(1, 1, article(), (), {})
        new = app.pending.put(1, 1, article(), (), {})
        other = app.pending.put(1, 2, article(), (), {})
        try:
            assert app.selected(1, message())[0] == new
            app.selected(1, message(), old)
            assert app.selected(1, message())[0] == old
            assert app.selected(1, message(chat_id=2))[0] == other
            app.pending.delete(old)
            assert app.pending.current(1, 1) == old
            with pytest.raises(ValueError, match="过期"):
                app.selected(1, message())
            assert app.selected(1, message(chat_id=2))[0] == other
            await app.menu_action(1, message(), 'ui:1:model_input')
            await app.menu_action(1, message(), 'ui:1:home')
            assert app.pending.preferences(1)['input'] is None
        finally:
            await app.close()
    asyncio.run(run())
