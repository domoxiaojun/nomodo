import asyncio
import copy
from html import escape
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from nomodo.domain import normalize_worker_result
from nomodo.integrations.notion.blocks import article_blocks, text_blocks
from nomodo.integrations.notion.client import NotionError
from nomodo.integrations.notion.fingerprint import fingerprint
from nomodo.integrations.notion.tree import operations, recover_tree


def article(html: str) -> Any:
    return normalize_worker_result({"sourceUrl": "https://example.com", "htmlContent": html})


@pytest.mark.parametrize("text", ["a|b", r"a\|b", r"a\\|b", "`a|b`", " a|b ", "a|b|c"])
def test_table_inline_code_roundtrip(text: str) -> None:
    original = article(
        f"<table><tbody><tr><th>代码</th></tr><tr><td><code>{escape(text)}</code></td></tr></tbody></table>"
    )
    parsed = normalize_worker_result({"sourceUrl": "https://example.com", "markdownContent": original.markdown})
    table = next(b for b in parsed.blocks if b.type == "table")
    assert table.header and table.rows == [["代码"], [text]]
    assert table.cells[1][0][0].code
    outside = article(f"<p><code>{escape(text)}</code></p>")
    assert "<code>" in outside.html


@pytest.mark.parametrize("rows,header", [
    ("<tbody><tr><th>H</th></tr><tr><td>D</td></tr></tbody>", True),
    ("<thead><tr><td>H</td></tr></thead><tbody><tr><td>D</td></tr></tbody>", True),
    ("<tr><th>H</th></tr><tr><td>D</td></tr>", True),
    ("<tbody><tr><td>D</td></tr><tr><th>row label</th></tr></tbody>", False),
])
def test_table_first_content_row_controls_header(rows: str, header: bool) -> None:
    value = article(f"<table>{rows}</table>")
    assert value.blocks[0].header is header
    notion = article_blocks(value, {})[0]
    assert notion["table"]["has_column_header"] is header


@pytest.mark.parametrize("start", [0, 5, -2])
def test_notion_explicit_numbers_and_nested_styles(start: int) -> None:
    value = article(f'<ol start="{start}"><li><strong>first</strong><ul><li>child</li></ul></li><li>next</li></ol>')
    blocks = article_blocks(value, {})
    assert blocks[0]["type"] == blocks[1]["type"] == "paragraph"
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == f"{start}. "
    assert blocks[1]["paragraph"]["rich_text"][0]["text"]["content"] == f"{start+1}. "
    assert blocks[0]["paragraph"]["rich_text"][1]["annotations"]["bold"]
    assert blocks[0]["paragraph"]["children"][0]["type"] == "bulleted_list_item"
    assert value.blocks[0].start == start


def test_long_list_item_does_not_create_extra_numbered_items() -> None:
    value = article(f"<ol><li>{'a' * 2000}</li><li>second</li></ol>")
    blocks = article_blocks(value, {})
    assert [b["type"] for b in blocks[:2]] == ["numbered_list_item", "numbered_list_item"]
    first = blocks[0]["numbered_list_item"]
    continuation = first["children"][0]
    assert continuation["type"] == "paragraph"
    content = "".join(r["text"]["content"] for r in first["rich_text"] + continuation["paragraph"]["rich_text"])
    assert content == "a" * 2000


def paragraph(text: str = "original") -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": text, "link": {"url": "https://example.com/original"}},
         "annotations": {"bold": True}}
    ]}}


def job_for(block: dict[str, Any]) -> dict[str, Any]:
    return {"tree_next": 0, "tree_ops": operations([block]), "tree_ids": {"": str(uuid4())},
            "marker": "fixture", "in_flight": True, "status": "unknown"}


@pytest.mark.parametrize("change", ["text", "link", "style", "trashed"])
def test_recovery_rejects_changed_content_and_keeps_checkpoint(change: str) -> None:
    async def run() -> None:
        expected = paragraph()
        job = job_for(expected)
        before = copy.deepcopy(job)
        actual = {**copy.deepcopy(expected), "id": str(uuid4())}
        if change == "text":
            actual["paragraph"]["rich_text"][0]["text"]["content"] = "different"
        elif change == "link":
            actual["paragraph"]["rich_text"][0]["text"]["link"]["url"] = "https://example.com/changed"
        elif change == "style":
            actual["paragraph"]["rich_text"][0]["annotations"]["bold"] = False
        else:
            actual["in_trash"] = True
        client = AsyncMock()
        client.children.return_value = [actual, *text_blocks("fixture tree 0")]
        with pytest.raises(NotionError, match="write_outcome_unknown"):
            await recover_tree(job, client)
        assert job == before
        client.append.assert_not_awaited()
    asyncio.run(run())


def test_response_defaults_and_split_rich_text_do_not_change_fingerprint() -> None:
    expected = paragraph()
    actual = copy.deepcopy(expected)
    run = actual["paragraph"]["rich_text"][0]
    run["annotations"].update(italic=False, underline=False, strikethrough=False, code=False, color="default")
    run["plain_text"] = "original"
    left, right = copy.deepcopy(run), copy.deepcopy(run)
    left["text"]["content"], right["text"]["content"] = "orig", "inal"
    actual["paragraph"].update(rich_text=[left, right], color="default")
    actual.update(id=str(uuid4()), created_time="metadata", has_children=False)
    assert fingerprint(expected) == fingerprint(actual)

    async def check() -> None:
        job = job_for(expected)
        client = AsyncMock()
        client.children.return_value = [actual, *text_blocks("fixture tree 0")]
        await recover_tree(job, client)
        assert job["tree_next"] == 1 and not job["in_flight"]
    asyncio.run(check())


def test_recovery_rejects_edited_ancestor() -> None:
    async def run() -> None:
        expected = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [], "children": [paragraph()]}}
        job = job_for(expected)
        job.update(tree_next=1, tree_ids={"": str(uuid4()), "/0": str(uuid4())})
        parent = {"id": job["tree_ids"]["/0"], "type": "bulleted_list_item",
                  "bulleted_list_item": {"rich_text": paragraph("edited")["paragraph"]["rich_text"]}}
        client = AsyncMock()
        client.children.return_value = [parent]
        before = copy.deepcopy(job)
        with pytest.raises(NotionError, match="write_outcome_unknown"):
            await recover_tree(job, client)
        assert job == before
    asyncio.run(run())


def test_recovery_checks_table_row_contents() -> None:
    async def run() -> None:
        table = article_blocks(article("<table><tr><th>H</th></tr><tr><td>D</td></tr></table>"), {})[0]
        job = job_for(table)
        actual = copy.deepcopy(table)
        rows = actual["table"].pop("children")
        actual.update(id=str(uuid4()), has_children=True)
        rows[1]["table_row"]["cells"][0][0]["text"]["content"] = "changed"
        client = AsyncMock()
        client.children.side_effect = [[actual, *text_blocks("fixture tree 0")], rows]
        with pytest.raises(NotionError, match="write_outcome_unknown"):
            await recover_tree(job, client)
        assert job["tree_next"] == 0 and job["in_flight"]
    asyncio.run(run())
