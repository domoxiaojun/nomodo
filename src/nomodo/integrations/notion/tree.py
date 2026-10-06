"""Checkpointed nested block writes; every request has its own durable recovery marker."""

import copy
from collections.abc import Callable
from typing import Any

from .blocks import batches, text_blocks
from .client import NotionClient, NotionError, notion_id
from .fingerprint import fingerprint

Json = dict[str, Any]


def nested(blocks: list[Json]) -> bool:
    return any(b["type"] != "table" and b.get(b["type"], {}).get("children") for b in blocks)


def operations(blocks: list[Json]) -> list[Json]:
    result: list[Json] = []

    def visit(values: list[Json], parent: str) -> None:
        shallow = []
        children = []
        for index, block in enumerate(values):
            path = f"{parent}/{index}"
            node = copy.deepcopy(block)
            if node["type"] != "table":
                descendants = node[node["type"]].pop("children", [])
                if descendants:
                    children.append((descendants, path))
            shallow.append(node)
        offset = 0
        for batch in batches(shallow):
            result.append({"parent": parent, "paths": [f"{parent}/{i}" for i in range(offset, offset + len(batch))],
                           "blocks": batch})
            offset += len(batch)
        for descendants, path in children:
            visit(descendants, path)
    visit(blocks, "")
    return result


def accept(job: Json, op: Json, results: Any) -> None:
    if not isinstance(results, list) or len(results) != len(op["paths"]):
        raise NotionError("write_outcome_unknown")
    ids = {}
    for path, expected, actual in zip(op["paths"], op["blocks"], results, strict=True):
        if not isinstance(actual, dict) or actual.get("type") != expected["type"] or not actual.get("id"):
            raise NotionError("write_outcome_unknown")
        try:
            ids[path] = notion_id(actual["id"])
        except (NotionError, TypeError):
            raise NotionError("write_outcome_unknown") from None
    job["tree_ids"].update(ids)
    job["tree_next"] += 1
    job["in_flight"] = False


async def write_tree(job: Json, client: NotionClient, save: Callable[[], None]) -> None:
    if "tree_ops" not in job:
        job.update(tree_ops=operations(job["blocks"]), tree_ids={"": job["page_id"]}, tree_next=0)
        save()
    while job["tree_next"] < len(job["tree_ops"]):
        index = job["tree_next"]
        op = job["tree_ops"][index]
        marker = f"{job['marker']} tree {index}"
        job["in_flight"] = True
        save()
        response = await client.append(job["tree_ids"][op["parent"]], [*op["blocks"], *text_blocks(marker)])
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list) or len(results) != len(op["blocks"]) + 1:
            raise NotionError("write_outcome_unknown")
        accept(job, op, results[:-1])
        save()


async def verify_content(expected: Json, actual: Json, client: NotionClient, *, children: bool = True) -> None:
    actual = copy.deepcopy(actual)
    kind = expected["type"]
    if actual.get("type") != kind:
        raise NotionError("write_outcome_unknown")
    if children and (actual.get("has_children") or expected.get(kind, {}).get("children")):
        descendants = await client.children(actual["id"])
        actual.setdefault(kind, {})["children"] = descendants
    try:
        if fingerprint(expected) != fingerprint(actual):
            raise NotionError("write_outcome_unknown")
    except (KeyError, TypeError, AttributeError, ValueError):
        raise NotionError("write_outcome_unknown") from None


async def verify_parents(job: Json, op: Json, client: NotionClient) -> None:
    expected_by_path = {path: block for planned in job["tree_ops"]
                        for path, block in zip(planned["paths"], planned["blocks"], strict=True)}
    paths = []
    path = op["parent"]
    while path:
        paths.append(path)
        path = path.rpartition("/")[0]
    for path in reversed(paths):
        container = path.rpartition("/")[0]
        siblings = await client.children(job["tree_ids"][container])
        matches = [block for block in siblings if block.get("id") == job["tree_ids"][path]]
        if len(matches) != 1:
            raise NotionError("write_outcome_unknown")
        await verify_content(expected_by_path[path], matches[0], client, children=False)


async def recover_tree(job: Json, client: NotionClient) -> None:
    index = job["tree_next"]
    if index >= len(job["tree_ops"]):
        raise NotionError("write_outcome_unknown")
    op = job["tree_ops"][index]
    await verify_parents(job, op, client)
    blocks = await client.children(job["tree_ids"][op["parent"]])
    marker = f"{job['marker']} tree {index}"
    matches = [i for i, b in enumerate(blocks) if "".join(
        r.get("plain_text") or r.get("text", {}).get("content", "")
        for r in b.get("paragraph", {}).get("rich_text", [])) == marker]
    count = len(op["blocks"])
    if len(matches) != 1 or matches[0] < count:
        raise NotionError("write_outcome_unknown")
    candidates = blocks[matches[0]-count:matches[0]]
    for expected, actual in zip(op["blocks"], candidates, strict=True):
        await verify_content(expected, actual, client)
    accept(job, op, candidates)
