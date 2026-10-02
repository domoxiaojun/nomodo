import asyncio
from pathlib import Path

import pytest

from simpread.config import Settings
from simpread.integrations.openai import ResponsesClient


def test_endpoint_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / '.env'
    env.write_text('READER_BOT_TOKEN=123:fixture\nREADER_API_ID=123\n'
                   'READER_API_HASH=fixture\nPARSEHUB_WORKER_SECRET=' + 'x' * 32
                   + '\nOPENAI_BASE_URL=https://proxy.example/v1\n')
    settings = Settings(_env_file=env)
    assert settings.openai_base_url == 'https://proxy.example/v1'

    async def check() -> None:
        client = ResponsesClient('fixture', 'model', 'identity', base_url=settings.openai_base_url)
        try:
            assert str(client.client.base_url) == 'https://proxy.example/v1/'
        finally:
            await client.close()

    asyncio.run(check())
    with pytest.raises(ValueError, match='invalid OpenAI base URL'):
        Settings(_env_file=env, openai_base_url='proxy.example')
