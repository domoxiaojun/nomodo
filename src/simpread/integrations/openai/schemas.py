from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Enhancement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    suggested_title: str = ""
    tags: list[str] = Field(default_factory=list, max_length=20)
    translated_markdown: str = ""
    normalized_markdown: str = ""


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal[
        "prepare_parse",
        "render_markdown",
        "render_html",
        "summarize_article",
        "translate_article",
        "generate_tags",
        "list_notion_targets",
        "export_notion",
    ]
    language: str = Field(default="zh-CN", max_length=40)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[Action] = Field(min_length=1, max_length=4)
    explanation: str = Field(default="", max_length=1000)

    @property
    def requires_confirmation(self) -> bool:
        return any(a.name == "export_notion" for a in self.actions)
