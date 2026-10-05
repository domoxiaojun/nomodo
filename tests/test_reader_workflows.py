import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from test_bot_oauth import message, settings

from simpread.domain import normalize_worker_result
from simpread.storage import PendingStore
from simpread.telegram.app import App, reply_all
from simpread.telegram.callbacks import message_chunks
from simpread.telegram.input import message_urls
from simpread.worker import PreparedArticle


def article(title: str = "first") -> Any:
    return normalize_worker_result({"sourceUrl": "https://example.com", "title": title, "content": "body"})


def test_selection_survives_restart_and_is_scoped(tmp_path: Path) -> None:
    store = PendingStore(tmp_path / "p.db")
    first = store.put(1, 1, article(), (), {})
    second = store.put(1, 1, article("second"), (), {})
    foreign = store.put(2, 1, article("foreign"), (), {})
    group = store.put(1, -10, article("group"), (), {})
    store.select(1, 1, first)
    assert store.current(1, -10) == group
    assert store.current(1, 1) == first
    for key in (foreign, group):
        with pytest.raises(ValueError):
            store.select(1, 1, key)
    assert {k for k, _ in store.articles(1, 1)} == {first, second}
    store.close()
    store = PendingStore(tmp_path / "p.db")
    assert store.current(1, 1) == first
    with store.db:
        store.db.execute("UPDATE articles SET expires=0 WHERE id=?", (first,))
    store.delete(first)
    assert store.current(1, 1) == first and store.get(1, 1, first) is None
    store.select(1, 1, second)
    store.close()


def test_lossless_unicode_lists_are_delivered_in_full() -> None:
    text = "".join(f"目标{i} 😀 {'长标题' * 30}\nID:{i}\n" for i in range(100))
    chunks = message_chunks(text)
    assert len(chunks) > 1 and "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3800 for chunk in chunks)

    async def run() -> None:
        msg = message()
        await reply_all(msg, text)
        assert "".join(c.args[0] for c in msg.reply_text.call_args_list) == text

    asyncio.run(run())


def test_hidden_caption_and_explicit_reply_links() -> None:
    msg = message(text="阅读这里")
    msg.entities = [SimpleNamespace(url="https://example.com/a")]
    msg.reply_to_message = message(text="https://example.com/quoted")
    assert message_urls(msg) == ["https://example.com/a"]
    msg.text, msg.entities = "/read", []
    assert message_urls(msg) == []
    assert message_urls(msg, include_reply=True) == ["https://example.com/quoted"]
    msg.caption_entities = [SimpleNamespace(url="javascript:alert(1)")]
    assert message_urls(msg) == []


def test_select_command_controls_later_summary(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        first = app.pending.put(1, 1, article(), (), {})
        app.pending.put(1, 1, article("second"), (), {})
        cast(Any, app).enhance = AsyncMock()
        await app.dispatch(None, message(text=f"/select {first}"))
        msg = message(text="/summary")
        await app.dispatch(None, msg)
        cast(Any, app.enhance).assert_awaited_once_with(1, msg, first, "summary")
        await app.close()

    asyncio.run(run())


def test_reply_read_and_multilink_progress(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        cast(Any, app.worker).prepare = AsyncMock(return_value=PreparedArticle(article(), "job", (), {}))
        msg = message(text="/read")
        msg.reply_to_message = message(text="https://example.com/a https://example.com/b")
        await app.dispatch(None, msg)
        assert "第 1/2" in msg.reply_text.call_args.args[0]
        assert "第 2/2" in msg.reply_text.return_value.edit_text.call_args.args[0]
        assert msg.reply_rich.await_count == 2
        await app.close()

    asyncio.run(run())


def test_shutdown_waits_for_active_cleanup_and_stops_new_work(tmp_path: Path) -> None:
    async def run() -> None:
        app = App(settings(tmp_path))
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def prepare(_: str) -> Any:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        cast(Any, app.worker).prepare = AsyncMock(side_effect=prepare)
        worker_close = AsyncMock(
            side_effect=lambda: pytest.fail("closed before cleanup") if not cleaned.is_set() else None
        )
        original_close = app.worker.close
        cast(Any, app.worker).close = worker_close
        running = asyncio.create_task(app.dispatch(None, message(text="https://example.com")))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(app.close(), 1)
        await running
        assert cleaned.is_set() and not app.active and not app.requests
        later = message(text="https://example.com/new")
        await app.dispatch(None, later)
        later.reply_text.assert_not_awaited()
        worker_close.assert_awaited_once()
        await original_close()

    asyncio.run(run())
