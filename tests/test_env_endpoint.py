import asyncio
from pathlib import Path

import pytest

from nomodo.config import Settings
from nomodo.integrations.openai import ResponsesClient


def test_endpoint_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / '.env'
    env.write_text('READER_BOT_TOKEN=123:fixture\nREADER_API_ID=123\n'
                   'READER_API_HASH=fixture\nOPENAI_API_KEY=fixture\nPARSEHUB_WORKER_SECRET=' + 'x' * 32
                   + '\nOPENAI_BASE_URL=https://proxy.example/v1\n')
    monkeypatch.chdir(tmp_path)
    settings = Settings()  # type: ignore[call-arg]  # Required fields come from .env.
    assert settings.openai_base_url == 'https://proxy.example/v1'
    assert settings.llm_enabled is True
    assert settings.openai_model == 'gpt-6.1-sol'
    assert settings.llm_reasoning_effort == 'high'

    async def check() -> None:
        client = ResponsesClient('fixture', 'model', 'identity', base_url=settings.openai_base_url)
        try:
            assert str(client.client.base_url) == 'https://proxy.example/v1/'
        finally:
            await client.close()

    asyncio.run(check())
    with pytest.raises(ValueError, match='invalid OpenAI base URL'):
        Settings(openai_base_url='proxy.example')  # type: ignore[call-arg]
