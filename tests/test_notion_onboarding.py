import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr
from test_bot_oauth import message, settings

from nomodo.telegram.app import App


def test_home_has_direct_owner_bound_authorization_and_next_step(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_client_id='client', notion_client_secret=SecretStr('secret'),
                           notion_credentials_key=SecretStr('x' * 32),
                           notion_oauth_redirect_uri='https://example.com/notion/oauth/callback'))
        assert app.secrets is not None
        try:
            msg = message()
            await app.home(1, msg)
            text = msg.reply_text.call_args.args[0]
            keyboard = msg.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
            assert '尚未授权' in text and '三步' in text and '等待连接' not in text
            assert keyboard[0][0].text == '① 连接我的 Notion'
            url = urlsplit(keyboard[0][0].url)
            assert url.path == '/notion/oauth/start'
            state = parse_qs(url.query)['state'][0]
            assert app.secrets.oauth_bind(state, 'browser') == (1, None)
            app.secrets.put_credential(1, 'fixture', '工作空间')
            connected = message()
            await app.home(1, connected)
            first = connected.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
            assert first.text == '② 选择 Notion 保存位置' and first.callback_data == 'ui:1:targets'
            group = message(kind='group')
            await app.home(1, group)
            assert group.reply_text.call_args.kwargs['reply_markup'] is None
            assert '工作空间' not in group.reply_text.call_args.args[0]
        finally:
            await app.close()
    asyncio.run(run())


def test_unconnected_notion_menu_opens_guide_without_extra_click(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_client_id='client', notion_client_secret=SecretStr('secret'),
                           notion_credentials_key=SecretStr('x' * 32),
                           notion_oauth_redirect_uri='https://example.com/notion/oauth/callback'))
        msg = message()
        try:
            await app.notion_command(1, msg, [])
            text = msg.reply_text.call_args.args[0]
            rows = msg.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
            assert '登录' in text and '允许机器人访问' in text
            assert rows[0][0].url.startswith('https://example.com/notion/oauth/start?state=')
            assert rows[-1][0].callback_data == 'ui:1:home'
        finally:
            await app.close()
    asyncio.run(run())
