from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_ids(value: str) -> frozenset[int]:
    values = frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    if any(item <= 0 for item in values):
        raise ValueError("user ids must be positive")
    return values


class ModelCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_tokens: int = Field(ge=4096)
    max_output_tokens: int = Field(ge=100)
    structured_mode: Literal["strict", "json"] = "strict"
    reasoning_efforts: tuple[str, ...] = ()
    chat_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"

    @model_validator(mode="after")
    def validate_capacity(self) -> ModelCapability:
        if self.context_tokens <= self.max_output_tokens + 2048:
            raise ValueError("model context must leave input space")
        if set(self.reasoning_efforts) - {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("invalid reasoning capabilities")
        return self


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
    openai_base_url: str = "https://api.openai.com/v1"
    llm_api_mode: Literal["responses", "chat"] = "responses"
    llm_structured_mode: Literal["strict", "json"] = "strict"
    llm_context_tokens: int = Field(default=32768, ge=4096)
    llm_reasoning_efforts: str = ""
    llm_model_capabilities: dict[str, ModelCapability] = Field(default_factory=dict)
    llm_chat_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    llm_concurrency: int = Field(default=2, ge=1, le=8)
    llm_retrieval_initial_groups: int = Field(default=2, ge=1)
    llm_verify_risks: bool = False
    llm_translation_glossaries: dict[str, dict[str, str]] = Field(default_factory=dict)
    llm_translation_expansion_ratios: dict[str, float] = Field(default_factory=dict)
    llm_reasoning_effort: ReasoningEffort = None
    openai_timeout_seconds: float = Field(default=60, gt=0, le=180)
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
        if any(not 1 <= ratio <= 10 for ratio in self.llm_translation_expansion_ratios.values()):
            raise ValueError("translation expansion ratios must be between 1 and 10")
        if any(not source.strip() or not target.strip() for glossary in self.llm_translation_glossaries.values()
               for source, target in glossary.items()):
            raise ValueError("glossary terms must be nonempty")
        endpoint = urlsplit(self.openai_base_url)
        if (
            endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        ):
            raise ValueError("invalid OpenAI base URL")
        if self.llm_context_tokens <= self.llm_max_output_tokens + 2048:
            raise ValueError("LLM context must leave room for input and schema")
        allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if set(self.reasoning_efforts) - allowed_efforts:
            raise ValueError("invalid reasoning capabilities")
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
        if self.llm_enabled and not self.openai_model:
            raise ValueError("LLM configuration is incomplete")
        if self.llm_enabled and (self.llm_input_usd_per_million <= 0 or self.llm_output_usd_per_million <= 0):
            raise ValueError("LLM price rates are required")
        return self

    @property
    def reasoning_efforts(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.llm_reasoning_efforts.split(",") if s.strip())
