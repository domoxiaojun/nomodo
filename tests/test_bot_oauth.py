import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from nomodo.config import Settings
from nomodo.domain import normalize_worker_result
from nomodo.integrations.notion import NotionError, NotionStore
from nomodo.integrations.openai import ActionPlan, Executor
from nomodo.integrations.openai.schemas import Action
from nomodo.oauth.server import OAuthServer
from nomodo.telegram.app import App
from nomodo.worker import PreparedArticle, WorkerError


def settings(tmp_path: Path, **kwargs: Any) -> Settings:
    kwargs.setdefault("llm_enabled", False)
    return Settings(
        reader_bot_token=SecretStr("123:fixture"),
        reader_api_id=1,
        reader_api_hash=SecretStr("fixture"),
        reader_allowed_user_ids="1,2",
        parsehub_worker_secret=SecretStr("x" * 32),
        reader_data_path=tmp_path / "reader",
        reader_database_path=tmp_path / "reader.db",
        notion_database_path=tmp_path / "notion.db",
        **kwargs,
    )


def message(uid: int = 1, chat_id: int = 1, text: str = "", kind: str = "private") -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        chat=SimpleNamespace(id=chat_id, type=kind),
        text=text,
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
        reply_rich=AsyncMock(),
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
        reply_photo=AsyncMock(),
        reply_video=AsyncMock(),
        delete=AsyncMock(),
    )


def test_oauth_state_browser_owner_expiry(tmp_path: Path) -> None:
    store = NotionStore(tmp_path / "n.db", "x" * 32)
    state = store.oauth_begin(1)
    assert store.oauth_consume(state, "wrong") is None
    assert store.oauth_bind(state, "browser") == (1, None)
    assert store.oauth_bind(state, "second") is None
    assert store.oauth_consume(state, "wrong") is None
    assert store.oauth_consume(state, "browser") == (1, None)
    assert store.oauth_consume(state, "browser") is None
    expired = store.oauth_begin(2, ttl=-1)
    assert store.oauth_bind(expired, "browser") is None
    pkce = store.oauth_begin(1, pkce=True)
    assert (store.oauth_bind(pkce, "browser") or (0, None))[1]
    store.close()


def test_oauth_callback_exchanges_once_and_notifies(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = settings(
            tmp_path,
            notion_client_id="id",
            notion_client_secret=SecretStr("secret"),
            notion_credentials_key=SecretStr("x" * 32),
            notion_oauth_redirect_uri="https://reader.example/notion/oauth/callback",
        )
        store = NotionStore(tmp_path / "n.db", "x" * 32)
        client = SimpleNamespace(
            oauth_token=AsyncMock(return_value={"access_token": "ntn_token", "workspace_name": "W"}), close=AsyncMock()
        )
        notified = AsyncMock()
        server = OAuthServer(cfg, store, lambda _: cast(Any, client), notified)
        from aiohttp import web

        app = web.Application()
        server.routes(app)
        state = parse_qs(urlsplit(server.begin(1)).query)["state"][0]
        async with TestClient(TestServer(app)) as http:
            started = await http.get("/notion/oauth/start", params={"state": state}, allow_redirects=False)
            assert started.status == 302
            cookie = started.cookies["nomodo_oauth"]
            assert cookie["secure"] and cookie["httponly"]
            assert "code_challenge" not in started.headers["Location"]
            response = await http.get(
                "/notion/oauth/callback",
                params={"state": state, "code": "fixture"},
                headers={"Cookie": "nomodo_oauth=" + cookie.value},
            )
            assert response.status == 200
            replay = await http.get(
                "/notion/oauth/callback",
                params={"state": state, "code": "fixture"},
                headers={"Cookie": "nomodo_oauth=" + cookie.value},
            )
            assert replay.status == 400
        assert store.credential(1) == ("ntn_token", "W")
        assert store.credential(2) is None
        client.oauth_token.assert_awaited_once()
        notified.assert_awaited_once_with(1)
        store.close()

    asyncio.run(run())


def test_whitelist_mixed_urls_and_no_implicit_writes(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = normalize_worker_result(
            {
                "sourceUrl": "https://youtube.com/watch?v=a",
                "platform": "youtube",
                "title": "title",
                "content": "body",
                "media": [],
            }
        )
        cast(Any, app.worker).prepare = AsyncMock(
            side_effect=[WorkerError("unsupported_url"), PreparedArticle(value, "j", (), {})]
        )
        cast(Any, app.worker).capabilities = AsyncMock(return_value=SimpleNamespace(names="YouTube"))
        msg = message(text="https://unsupported.example/a https://youtube.com/watch?v=a")
        await app.dispatch(None, msg)
        assert app.pending.count(1) == 1
        assert any("成功 1/2" in c.args[0] for c in msg.reply_text.return_value.edit_text.call_args_list)
        stranger = message(uid=99, text="https://youtube.com/watch?v=a")
        await app.dispatch(None, stranger)
        assert cast(Any, app.worker).prepare.await_count == 2
        stranger.reply_text.assert_awaited_once()
        await app.close()

    asyncio.run(run())


def test_group_callbacks_and_foreign_owners_never_export(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = normalize_worker_result(
            {"sourceUrl": "https://example.com", "platform": "x", "content": "body", "media": []}
        )
        key = app.pending.put(1, -10, value, (), {})
        request_save = AsyncMock()
        app.request_save = request_save  # type: ignore[method-assign]
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=message(chat_id=-10, kind="group"),
            data=f"article:{key}:save",
            answer=AsyncMock(),
        )
        await app.callback(None, query)
        request_save.assert_not_awaited()
        query.from_user.id = 2
        await app.callback(None, query)
        request_save.assert_not_awaited()
        await app.close()

    asyncio.run(run())


def test_confirmation_is_bound_one_use_and_keeps_selected_target(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = normalize_worker_result(
            {"sourceUrl": "https://example.com", "platform": "x", "content": "body", "media": []}
        )
        key = app.pending.put(1, 1, value, (), {})
        approval = app.pending.approve(1, 1, {"kind": "save", "key": key, "target": "fixed-target", "add_schema": {}})
        cast(Any, app).save = AsyncMock()
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1), message=message(), data="confirm:" + approval, answer=AsyncMock()
        )
        await app.callback(None, query)
        await app.callback(None, query)
        cast(Any, app.save).assert_awaited_once_with(1, query.message, key, "fixed-target", force_new=False)
        await app.close()

    asyncio.run(run())


def test_executor_preflights_before_any_action() -> None:
    async def run() -> None:
        render = AsyncMock()
        write = AsyncMock()
        executor = Executor({"render_markdown": render, "export_notion": write})
        plan = ActionPlan(actions=[Action(name="render_markdown"), Action(name="export_notion")])
        with pytest.raises(PermissionError):
            await executor.run(plan)
        render.assert_not_awaited()
        write.assert_not_awaited()
        await executor.run(plan, confirmed=True)
        assert render.await_count == write.await_count == 1
        with pytest.raises(ValueError, match="action_not_registered"):
            await executor.run(ActionPlan(actions=[Action(name="list_notion_targets")]))
        with pytest.raises(ValueError, match="tool_call_limit"):
            await Executor({"render_markdown": render}, 1).run(ActionPlan(actions=[Action(name="render_markdown")] * 2))

    asyncio.run(run())


def test_http_health_uses_reader_state(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        async with TestClient(TestServer(app.http_app())) as client:
            assert (await client.get("/health")).status == 503
            app.bot = SimpleNamespace(is_initialized=True, is_connected=True)
            assert (await client.get("/health")).status == 503
            app.maintenance_task = asyncio.create_task(app.maintain())
            response = await client.get("/health")
            assert await response.json() == {"service": "nomodo", "ready": True, "maintenance": True}
            app.maintenance_task.cancel()
            await asyncio.gather(app.maintenance_task, return_exceptions=True)
            assert (await client.get("/health")).status == 503
        await app.close()

    asyncio.run(run())


def test_media_caption_is_parsed_and_list_preview_is_not_empty(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = normalize_worker_result(
            {"sourceUrl": "https://example.com", "platform": "x", "markdownContent": "- first\n- second"}
        )
        cast(Any, app.worker).prepare = AsyncMock(return_value=PreparedArticle(value, "job", (), {}))
        msg = message()
        msg.caption = "https://example.com"
        await app.dispatch(None, msg)
        cast(Any, app.worker).prepare.assert_awaited_once_with("https://example.com")
        rendered = str(msg.reply_rich.call_args.args[0])
        assert "first" in rendered and "second" in rendered
        await app.close()

    asyncio.run(run())


def test_cancel_cannot_interrupt_another_chat(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        running = asyncio.create_task(asyncio.Event().wait())
        app.active[1], app.active_chat[1] = running, 1
        msg = message(text="/cancel", chat_id=-10, kind="group")
        await app.dispatch(None, msg)
        assert not running.cancelling()
        assert "发起任务的聊天" in msg.reply_text.call_args.args[0]
        await app.dispatch(None, message(text="/cancel"))
        assert running.cancelling()
        await asyncio.gather(running, return_exceptions=True)
        await app.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_notion_save_keeps_article_media_until_cancel(tmp_path: Path, failure: bool) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = normalize_worker_result({"sourceUrl": "https://example.com", "platform": "x", "content": "body"})
        key = app.pending.put(1, 1, value, ("lease",), {})
        cast(Any, app.worker).release = AsyncMock()
        app.notion = cast(Any, SimpleNamespace(export_page=AsyncMock(
            side_effect=NotionError("rate_limited") if failure else None,
            return_value={"url": "https://notion.so/page", "warnings": []},
        )))
        await app.save(1, message(), key, "target")
        cast(Any, app.worker).release.assert_not_awaited()
        stored = app.pending.get(1, 1, key)
        assert stored and stored[1] == ("lease",)
        await app.dispatch(None, message(text="/cancel"))
        cast(Any, app.worker).release.assert_not_awaited()
        assert app.pending.get(1, 1, key)
        await app.close()

    asyncio.run(run())


def test_maintenance_renews_busy_expired_article_then_releases_when_idle(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        value = normalize_worker_result({"sourceUrl": "https://example.com", "platform": "x", "content": "body"})
        key = app.pending.put(1, 1, value, ("lease",), {})
        with app.pending.db:
            app.pending.db.execute("UPDATE articles SET expires=0 WHERE id=?", (key,))
        app.locks[1] = asyncio.Lock()
        await app.locks[1].acquire()
        observed = asyncio.Event()

        async def observe(_: str) -> None:
            observed.set()

        cast(Any, app.worker).renew = AsyncMock(side_effect=observe)
        cast(Any, app.worker).release = AsyncMock(side_effect=observe)
        task = asyncio.create_task(app.maintain())
        await asyncio.wait_for(observed.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert app.pending.entries()
        cast(Any, app.worker).release.assert_not_awaited()
        app.locks[1].release()
        observed.clear()
        task = asyncio.create_task(app.maintain())
        await asyncio.wait_for(observed.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not app.pending.entries()
        cast(Any, app.worker).release.assert_awaited_once_with("lease")
        await app.close()

    asyncio.run(run())


def test_private_onboarding_preserves_whitelist(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        cast(Any, app.worker).prepare = AsyncMock()
        try:
            for command in ('/start', '/help', '/id', '/start@reader_bot'):
                msg = message(uid=99, text=command)
                await app.dispatch(None, msg)
                assert '99' in msg.reply_text.call_args.args[0]
            group = message(uid=99, text='/start', kind='group')
            await app.dispatch(None, group)
            group.reply_text.assert_not_awaited()
            stranger = message(uid=99, text='/read https://youtube.com/watch?v=a')
            await app.dispatch(None, stranger)
            stranger.reply_text.assert_awaited_once()
            cast(Any, app.worker).prepare.assert_not_awaited()
            assert app.pending.count(99) == 0
        finally:
            await app.close()

    asyncio.run(run())


def test_start_reply_through_real_telegram_message(tmp_path: Path) -> None:
    from pyrogram import enums
    from pyrogram.types import Chat, Message, User

    async def run() -> None:
        app = App(settings(tmp_path))
        client = SimpleNamespace(send_message=AsyncMock())
        msg = Message(
            id=123,
            client=cast(Any, client),
            chat=Chat(id=99, type=enums.ChatType.PRIVATE),
            from_user=User(id=99, first_name='Fixture'),
            text=cast(Any, '/start'),
        )
        try:
            await app.dispatch(client, msg)
            client.send_message.assert_awaited_once()
            sent = client.send_message.call_args.kwargs
            assert sent['chat_id'] == 99
            assert '99' in sent['text']
            assert sent['link_preview_options'].is_disabled is True
        finally:
            await app.close()

    asyncio.run(run())
