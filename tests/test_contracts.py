from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from openai.types.shared import ReasoningEffort
from pydantic import SecretStr

from simpread.config import Settings
from simpread.domain import normalize_worker_result
from simpread.integrations.notion import NotionStore
from simpread.integrations.openai import ResponsesClient
from simpread.storage import PendingStore
from simpread.worker import WorkerClient, WorkerError


def result_payload() -> dict:
    return {
        "sourceUrl": "https://example.com/a",
        "canonicalUrl": "https://example.com/a",
        "platform": "example",
        "title": "Title",
        "content": "hello",
        "resultType": "post",
        "media": [],
    }


def test_pending_store_owner_isolation(tmp_path: Path) -> None:
    store = PendingStore(tmp_path / "reader.sqlite3")
    article = normalize_worker_result(result_payload())
    key = store.put(1, 10, article, ("lease",), result_payload())
    assert store.get(1, 10, key) is not None
    assert store.get(2, 10, key) is None
    store.close()


def test_notion_credential_encryption_and_export_state(tmp_path: Path) -> None:
    store = NotionStore(tmp_path / "notion.sqlite3", "x" * 32)
    store.put_credential(1, "ntn_secret", "workspace")
    assert store.credential(1) == ("ntn_secret", "workspace")
    assert b"ntn_secret" not in (tmp_path / "notion.sqlite3").read_bytes()
    store.save_export(1, "article", "target", {"status": "in_flight", "in_flight": True})
    store.close()
    recovered = NotionStore(tmp_path / "notion.sqlite3", "x" * 32)
    assert (recovered.export_status(1, "article", "target") or {})["status"] == "unknown"
    recovered.close()


@pytest.mark.parametrize("effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_responses_payload_uses_store_false(effort: ReasoningEffort) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "created_at": 1,
                "model": "model",
                "object": "response",
                "status": "completed",
                "parallel_tool_calls": False,
                "tool_choice": "none",
                "tools": [],
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "annotations": [],
                                "text": json.dumps(
                                    {
                                        "summary": "ok",
                                        "suggested_title": "",
                                        "tags": [],
                                        "translated_markdown": "",
                                        "normalized_markdown": "",
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    async def run() -> None:
        client = ResponsesClient(
            "secret", "model", "identity", transport=httpx.MockTransport(handler), reasoning_effort=effort
        )
        assert (await client.enhance(normalize_worker_result(result_payload()), "summary", 1)).summary == "ok"
        await client.close()

    asyncio.run(run())
    assert seen["store"] is False
    if effort is None:
        assert "reasoning" not in seen
    else:
        assert seen["reasoning"] == {"effort": effort}
    assert seen["safety_identifier"]
    assert "secret" not in json.dumps(seen)


def test_worker_rejects_unsupported_without_post() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"protocolVersion": 3, "version": "2.2.4", "platforms": [{"id": "youtube"}]})

    async def run() -> None:
        client = WorkerClient("http://worker", "secret", "123", transport=httpx.MockTransport(handler))
        client.matcher.get_platform = lambda url: None
        with pytest.raises(WorkerError, match="unsupported_url"):
            await client.prepare("https://unsupported.example/a")
        await client.close()

    asyncio.run(run())
    assert calls == ["GET"]


def test_settings_requires_secret_and_whitelist() -> None:
    settings = Settings(
        llm_enabled=False,
        reader_bot_token=SecretStr("123:abc"),
        reader_api_id=1,
        reader_api_hash=SecretStr("hash"),
        reader_allowed_user_ids="1,2",
        parsehub_worker_secret=SecretStr("x" * 32),
    )
    assert settings.allowed_users == {1, 2}
    with pytest.raises(ValueError):
        Settings(
            reader_bot_token=SecretStr("123:abc"),
            reader_api_id=1,
            reader_api_hash=SecretStr("hash"),
            reader_allowed_user_ids="1",
            parsehub_worker_secret=SecretStr("short"),
        )
