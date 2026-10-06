from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Enhancement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    suggested_title: str = ""
    tags: list[str] = Field(default_factory=list, max_length=20)
    translated_markdown: str = ""
    normalized_markdown: str = ""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(StrictModel):
    block_id: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=500)


class SummaryPoint(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    citations: list[Citation] = Field(min_length=1, max_length=5)


class Summary(StrictModel):
    conclusion: str = Field(min_length=1, max_length=1500)
    points: list[SummaryPoint] = Field(min_length=1, max_length=12)
    citations: list[Citation] = Field(min_length=1, max_length=5)


class Answer(StrictModel):
    found: bool
    answer: str = Field(max_length=6000)
    citations: list[Citation] = Field(max_length=12)
    needs_more_context: bool = False


class TranslationUnit(StrictModel):
    id: str
    text: str


class Transformation(StrictModel):
    units: list[TranslationUnit]


class Titles(StrictModel):
    titles: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=1, max_length=5)


class Tags(StrictModel):
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=20)


class ReadingContext(StrictModel):
    context: str = Field(max_length=2000)


class GlossaryTerm(StrictModel):
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=200)


class Glossary(StrictModel):
    terms: list[GlossaryTerm] = Field(max_length=30)


class EvidenceReview(StrictModel):
    supported: bool
    explanation: str = Field(max_length=2000)


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
