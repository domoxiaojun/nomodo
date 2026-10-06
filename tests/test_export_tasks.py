import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_bot_oauth import message, settings
from test_notion import PAGE, TARGET, Worker, article

from simpread.integrations.notion import NotionClient, NotionError, NotionService, NotionStore
from simpread.telegram.app import App


def test_unknown_export_recovery_uses_durable_snapshot_and_original_target(tmp_path: Path) -> None:
    async def run() -> None:
        writes: list[str] = []
        marker = ""

        async def handler(req: httpx.Request) -> httpx.Response:
            nonlocal marker
            if req.method == "POST" and req.url.path.endswith("/pages"):
                body = json.loads(req.content)
                marker = body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
                writes.append("create")
                raise httpx.ReadTimeout("accepted but response lost")
            if req.method == "PATCH":
                writes.append("append")
                return httpx.Response(200, json={})
            if req.url.path.endswith("/children"):
                return httpx.Response(200, json={
                    "results": [{"paragraph": {"rich_text": [{"plain_text": marker}]}}], "has_more": False,
                })
            return httpx.Response(200, json={
                "id": PAGE, "parent": {"page_id": TARGET}, "url": "https://notion.so/recovered",
            })

        cfg = settings(tmp_path, notion_credentials_key=SecretStr("x" * 32))
        app = App(cfg)
        assert app.secrets and app.notion
        app.secrets.put_credential(1, "fixture")
        app.secrets.save_target(1, {"id": TARGET, "kind": "page", "default": True, "title": "original target"})
        app.notion.factory = lambda token: NotionClient(token, httpx.MockTransport(handler))
        value = article()
        key = app.pending.put(1, 1, value, (), {})
        await app.save(1, message(), key, TARGET)
        task = app.secrets.export_tasks(1)[0]
        assert task["job"]["status"] == "unknown"
        task_id = task["id"]
        app.pending.delete(key)
        await app.close()

        app = App(cfg)
        assert app.secrets and app.notion
        app.notion.factory = lambda token: NotionClient(token, httpx.MockTransport(handler))
        other = "33333333-3333-4333-8333-333333333333"
        app.secrets.save_target(1, {"id": other, "kind": "page", "title": "other"})
        app.secrets.select(1, other)
        # Recovery is independent of pending article TTL and current target.
        msg = message()
        await app.notion_command(1, msg, ["recover", task_id, PAGE])
        approval = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        query = SimpleNamespace(from_user=SimpleNamespace(id=2), message=message(), data=approval, answer=AsyncMock())
        await app.callback(None, query)
        assert app.secrets.export_tasks(1)[0]["job"]["status"] == "unknown"
        query.from_user.id = 1
        await app.callback(None, query)
        assert app.secrets.export_tasks(1)[0]["job"]["status"] == "partial"
        await app.notion_command(1, msg, ["retry", task_id])
        query.data = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        await app.callback(None, query)
        job = app.secrets.export_tasks(1)[0]
        assert job["job"]["status"] == "sent" and job["target"] == TARGET
        assert writes.count("create") == 1 and writes.count("append") > 0
        await app.callback(None, query)
        assert writes.count("create") == 1
        assert app.secrets.export_tasks(2) == []
        await app.close()

    asyncio.run(run())


def test_legacy_checkpoint_is_preserved_and_snapshot_is_not_guessed(tmp_path: Path) -> None:
    old = {"status": "unknown", "in_flight": True, "marker": "unchanged", "page_id": PAGE, "next_batch": 2}
    with sqlite3.connect(tmp_path / "n.db") as db:
        db.execute(
            "CREATE TABLE exports_v1 (user_id INTEGER,article_hash TEXT,target_id TEXT,payload TEXT,"
            "PRIMARY KEY(user_id,article_hash,target_id))"
        )
        db.execute("INSERT INTO exports_v1 VALUES (?,?,?,?)", (1, "legacy-digest", TARGET, json.dumps(old)))
    store = NotionStore(tmp_path / "n.db", "x" * 32)
    task_id = store.export_tasks(1)[0]["id"]
    store.close()
    store = NotionStore(tmp_path / "n.db", "x" * 32)
    assert store.export_tasks(1)[0]["id"] == task_id
    assert store.export_status(1, "legacy-digest", TARGET) == old
    service = NotionService(store, Worker())  # type: ignore[arg-type]
    with pytest.raises(NotionError, match="legacy_export_snapshot_missing"):
        service.task(1, task_id)
    with pytest.raises(NotionError, match="export_task_not_found"):
        service.task(2, task_id)
    store.close()


@pytest.mark.parametrize("operation", ["search", "children"])
def test_pagination_repeated_cursor_fails_without_loop(operation: str) -> None:
    async def run() -> None:
        calls = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"results": [], "has_more": True, "next_cursor": "same"})

        client = NotionClient("fixture", httpx.MockTransport(handler))
        with pytest.raises(NotionError, match="notion_invalid_response"):
            await (client.search() if operation == "search" else client.children(PAGE))
        assert calls == 2
        await client.close()

    asyncio.run(run())


def test_target_refresh_removes_stale_only_after_complete_success(tmp_path: Path) -> None:
    async def run() -> None:
        store = NotionStore(tmp_path / "n.db", "x" * 32)
        store.put_credential(1, "fixture")
        store.save_target(1, {"id": TARGET, "kind": "page", "default": True})
        store.save_target(1, {"id": PAGE, "kind": "page"})
        failing = True

        def handler(req: httpx.Request) -> httpx.Response:
            if failing:
                return httpx.Response(200, json={"results": [], "has_more": True, "next_cursor": None})
            titled = {"object": "page", "id": TARGET, "properties": {
                "title": {"type": "title", "title": [{"plain_text": "收件箱"}]},
            }}
            return httpx.Response(200, json={"results": [titled], "has_more": False})

        service = NotionService(store, Worker(), lambda token: NotionClient(token, httpx.MockTransport(handler)))  # type: ignore[arg-type]
        with pytest.raises(NotionError):
            await service.targets(1)
        assert len(store.targets(1)) == 2
        failing = False
        await service.targets(1)
        assert len(store.targets(1)) == 1 and store.targets(1)[0]["default"]
        store.close()

    asyncio.run(run())


def test_targets_command_delivers_last_id_in_large_list(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path, notion_credentials_key=SecretStr("x" * 32)))
        assert app.notion
        values = [{"id": f"id-{i}", "title": "😀" * 60, "kind": "page"} for i in range(100)]
        app.notion.targets = AsyncMock(return_value=values)  # type: ignore[method-assign]
        msg = message()
        await app.notion_command(1, msg, ["targets"])
        output = "".join(c.args[0] for c in msg.reply_text.call_args_list)
        assert "id-99" in output and all(v["id"] in output for v in values)
        assert all(any(v["id"] in call.args[0] for call in msg.reply_text.call_args_list) for v in values)
        assert msg.reply_text.await_count > 1
        await app.close()

    asyncio.run(run())
