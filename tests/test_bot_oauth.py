import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from simpread.config import Settings
from simpread.domain import normalize_worker_result
from simpread.integrations.notion import NotionStore
from simpread.integrations.openai import ActionPlan, Executor
from simpread.integrations.openai.schemas import Action
from simpread.oauth.server import OAuthServer
from simpread.telegram.app import App
from simpread.worker import PreparedArticle, WorkerError


def settings(tmp_path: Path, **kwargs: Any) -> Settings:
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
            cookie = started.cookies["simpread_oauth"]
            assert cookie["secure"] and cookie["httponly"]
            assert "code_challenge" not in started.headers["Location"]
            response = await http.get(
                "/notion/oauth/callback",
                params={"state": state, "code": "fixture"},
                headers={"Cookie": "simpread_oauth=" + cookie.value},
            )
            assert response.status == 200
            replay = await http.get(
                "/notion/oauth/callback",
                params={"state": state, "code": "fixture"},
                headers={"Cookie": "simpread_oauth=" + cookie.value},
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
        assert any("成功 1 / 2" in c.args[0] for c in msg.reply_text.call_args_list)
        stranger = message(uid=99, text="https://youtube.com/watch?v=a")
        await app.dispatch(None, stranger)
        assert cast(Any, app.worker).prepare.await_count == 2
        stranger.reply_text.assert_not_awaited()
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
        cast(Any, app.save).assert_awaited_once_with(1, query.message, key, "fixed-target")
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
            response = await client.get("/health")
            assert await response.json() == {"service": "simpread", "ready": True}
        await app.close()

    asyncio.run(run())
