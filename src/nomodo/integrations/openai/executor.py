"""Closed action registry, preflight the entire plan and execute serially."""

from collections.abc import Awaitable, Callable
from typing import Any

from .schemas import ActionPlan

ALLOWED = frozenset(
    {
        "prepare_parse",
        "render_markdown",
        "render_html",
        "summarize_article",
        "translate_article",
        "generate_tags",
        "list_notion_targets",
        "export_notion",
    }
)


class Executor:
    def __init__(self, actions: dict[str, Callable[..., Awaitable[Any]]], max_calls: int = 4) -> None:
        self.actions = {k: v for k, v in actions.items() if k in ALLOWED}
        self.max_calls = min(4, max_calls)

    async def run(self, plan: ActionPlan, confirmed: bool = False) -> list[Any]:
        if len(plan.actions) > self.max_calls:
            raise ValueError("tool_call_limit")
        if any(action.name not in self.actions for action in plan.actions):
            raise ValueError("action_not_registered")
        if plan.requires_confirmation and not confirmed:
            raise PermissionError("confirmation_required")
        return [await self.actions[action.name](language=action.language) for action in plan.actions]
