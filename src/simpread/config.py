from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_ids(value: str) -> frozenset[int]:
    values = frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    if any(item <= 0 for item in values):
        raise ValueError("user ids must be positive")
    return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")
    reader_bot_token: SecretStr
    reader_api_id: int
    reader_api_hash: SecretStr
    reader_allowed_user_ids: str = ""
    reader_admin_user_ids: str = ""
    reader_host: str = "127.0.0.1"
    reader_port: int = Field(default=8090, ge=1, le=65535)
    reader_data_path: Path = Path("data/reader")
    reader_database_path: Path = Path("data/reader/reader.sqlite3")
    reader_pending_ttl: int = Field(default=1800, ge=60, le=86400)
    reader_max_pending_per_user: int = Field(default=20, ge=1, le=100)
    parsehub_worker_url: str = "http://127.0.0.1:8080"
    parsehub_worker_secret: SecretStr
    parsehub_worker_timeout_seconds: float = Field(default=30, gt=0, le=180)
    notion_client_id: str = ""
    notion_client_secret: SecretStr = SecretStr("")
    notion_oauth_redirect_uri: str = ""
    notion_credentials_key: SecretStr = SecretStr("")
    notion_database_path: Path = Path("data/notion/notion.sqlite3")
    notion_pkce_enabled: bool = False
    llm_enabled: bool = False
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""
    openai_timeout_seconds: float = Field(default=60, gt=0, le=180)
    llm_max_input_chars: int = Field(default=120000, gt=0, le=120000)
    llm_max_output_tokens: int = Field(default=4096, ge=100, le=16384)
    llm_max_tool_calls: int = Field(default=4, ge=1, le=4)
    llm_daily_budget: float = Field(default=1, ge=0)
    llm_input_usd_per_million: float = Field(default=0, ge=0)
    llm_output_usd_per_million: float = Field(default=0, ge=0)

    @property
    def allowed_users(self) -> frozenset[int]:
        return parse_ids(self.reader_allowed_user_ids)

    @property
    def admin_users(self) -> frozenset[int]:
        return parse_ids(self.reader_admin_user_ids) & self.allowed_users

    @model_validator(mode="after")
    def validate_configuration(self) -> Settings:
        _ = self.allowed_users, self.admin_users
        bot_id = self.reader_bot_token.get_secret_value().split(":", 1)[0]
        if not bot_id.isdigit():
            raise ValueError("invalid bot token")
        if len(self.parsehub_worker_secret.get_secret_value()) < 32:
            raise ValueError("worker secret requires at least 32 characters")
        worker_url = urlsplit(self.parsehub_worker_url)
        if (
            worker_url.scheme not in {"http", "https"}
            or not worker_url.hostname
            or worker_url.username
            or worker_url.password
            or worker_url.query
            or worker_url.fragment
        ):
            raise ValueError("invalid worker URL")
        if self.notion_client_id:
            if (
                not self.notion_client_secret.get_secret_value()
                or len(self.notion_credentials_key.get_secret_value()) < 32
            ):
                raise ValueError("incomplete Notion configuration")
            if urlsplit(self.notion_oauth_redirect_uri).scheme != "https":
                raise ValueError("Notion OAuth redirect must use HTTPS")
        if self.llm_enabled and (not self.openai_api_key.get_secret_value() or not self.openai_model):
            raise ValueError("LLM configuration is incomplete")
        if self.llm_enabled and (self.llm_input_usd_per_million <= 0 or self.llm_output_usd_per_million <= 0):
            raise ValueError("LLM price rates are required")
        return self
