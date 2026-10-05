import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_bot_oauth import message, settings

from simpread.domain import normalize_worker_result
from simpread.integrations.openai import LLMError, ResponsesClient
from simpread.integrations.openai.client import sanitize
from simpread.telegram.app import App


@pytest.mark.parametrize("value", ["", "none", "minimal", "low", "medium", "high", "xhigh", "max", "invalid"])
def test_reasoning_environment_reaches_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", value)
    if value == "invalid":
        with pytest.raises(ValueError, match="llm_reasoning_effort"):
            settings(tmp_path, _env_file=None)
        return

    async def run() -> None:
        app = App(settings(
            tmp_path,
            _env_file=None,
            llm_enabled=True,
            openai_api_key=SecretStr("fixture"),
            openai_model="fixture",
            llm_input_usd_per_million=1,
            llm_output_usd_per_million=1,
            llm_reasoning_efforts=value,
        ))
        app.pending.preferences(1, {"llm": True})
        try:
            assert app.reader.profile(1)[0]["reasoning_effort"] == (value or None)
        finally:
            await app.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "invalid"])
def test_llm_failures_are_safe_and_leave_original_unchanged(mode: str) -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("secret private diagnostic")
        return httpx.Response(429 if mode == "rate_limit" else 200, json={"output": []})

    async def run() -> None:
        article = normalize_worker_result(
            {
                "sourceUrl": "https://example.com",
                "platform": "x",
                "content": "original",
                "cookie": "sensitive",
                "_files": ["/private/file"],
                "media": [],
            }
        )
        original = article.model_dump_json()
        client = ResponsesClient("key", "model", "identity", transport=httpx.MockTransport(handler))
        expected = {"timeout": "llm_timeout", "rate_limit": "llm_rate_limited", "invalid": "invalid_output"}[mode]
        with pytest.raises(LLMError, match=expected) as error:
            await client.enhance(article, "summary", 1)
        assert "secret" not in str(error.value) and article.model_dump_json() == original
        await client.close()

    asyncio.run(run())


def test_sanitized_input_excludes_worker_and_secret_fields() -> None:
    article = normalize_worker_result(
        {
            "sourceUrl": "https://example.com?token=secret",
            "platform": "x",
            "content": "cookie=abc-secret\nFile /Users/test/private\nhello",
            "leaseId": "secret-lease",
            "nativeError": "internal",
            "media": [
                {"mediaId": "media-secret", "type": "photo", "filename": "/private/path", "mimeType": "image/png"}
            ],
        }
    )
    prompt = sanitize(article)
    assert all(s not in prompt for s in ["abc-secret", "/Users/test", "secret-lease", "media-secret", "nativeError"])
    data = json.loads(prompt)
    assert "hello" in data["blocks"][0]["text"]


def test_redaction_preserves_other_fields_and_structured_content() -> None:
    article = normalize_worker_result(
        {
            "sourceUrl": "https://example.com",
            "platform": "x",
            "title": "token=hidden-value",
            "description": "keep description",
            "markdownContent": "- api_key=private-value\n- keep item\n\n|name|value|\n|---|---|\n|row|keep cell|",
        }
    )
    prompt = sanitize(article)
    data = json.loads(prompt)
    assert data["title"] == "[redacted]" and data["description"] == "keep description"
    assert "keep item" in prompt and "keep cell" in prompt and "private-value" not in prompt
    assert "token=hidden-value" == article.title


def test_budget_or_disabled_stops_before_network(tmp_path: Path) -> None:
    from pydantic import SecretStr

    async def run() -> None:
        app = App(
            settings(
                tmp_path,
                llm_enabled=True,
                openai_api_key=SecretStr("fixture"),
                openai_model="fixture",
                llm_daily_budget=0,
                llm_input_usd_per_million=1,
                llm_output_usd_per_million=1,
            )
        )
        with pytest.raises(LLMError, match="llm_disabled"):
            app.reader.profile(1)
        app.pending.preferences(1, {"llm": True})
        article = normalize_worker_result({"sourceUrl": "https://example.com", "content": "body"})
        key = app.pending.put(1, 1, article, (), {})
        with pytest.raises(LLMError, match="budget_exhausted"):
            await app.reader.run(1, 1, key, "summary")
        assert app.pending.preferences(2)["llm"] is False
        await app.close()

    asyncio.run(run())


def test_failed_media_send_releases_pending_lease(tmp_path: Path) -> None:
    from typing import cast

    from simpread.worker import PreparedArticle

    async def run() -> None:
        app = App(settings(tmp_path))
        article = normalize_worker_result(
            {"sourceUrl": "https://example.com", "platform": "x", "content": "original", "media": []}
        )
        worker = cast(Any, app.worker)
        worker.prepare = AsyncMock(return_value=PreparedArticle(article, "job", ("l",), {}))
        worker.release = AsyncMock()
        msg = message(text="https://example.com")
        msg.reply_text = AsyncMock(side_effect=RuntimeError("fixture"))
        with pytest.raises(RuntimeError):
            await app.read(1, msg, ["https://example.com"])
        worker.release.assert_awaited_once_with("l")
        assert app.pending.count(1) == 0
        await app.close()

    asyncio.run(run())
