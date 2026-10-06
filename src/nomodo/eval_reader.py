"""Opt-in, budgeted reading evaluations. Without --live this only describes local fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from nomodo.config import Settings
from nomodo.domain import normalize_worker_result
from nomodo.integrations.openai.client import LLMError
from nomodo.integrations.openai.reader import ReaderService
from nomodo.storage import PendingStore


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text())
    if not isinstance(cases, list) or not all(
        isinstance(case, dict) and isinstance(case.get("id"), str) and isinstance(case.get("markdown"), str)
        and isinstance(case.get("question"), str) and isinstance(case.get("answerable"), bool) for case in cases
    ):
        raise ValueError("invalid evaluation fixtures")
    return cases


def usage_totals(pending: PendingStore) -> tuple[int, float, int]:
    row = pending.db.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual,reserved)),0), "
        "COALESCE(SUM(uncertain),0) FROM ai_usage"
    ).fetchone()
    return int(row[0]), float(row[1]), int(row[2])


async def evaluate(cases: list[dict[str, Any]], config: Settings, operations: list[str]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "live": True, "model": config.openai_model, "endpoint": config.openai_base_url,
        "protocol": config.llm_api_mode, "budget_usd": config.llm_daily_budget,
        "automatic_checks_are_not_quality_scores": True, "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="nomodo-eval-") as directory:
        pending = PendingStore(Path(directory) / "reader.db", ttl=3600)
        reader = ReaderService(pending, config, None)
        pending.preferences(1, {"llm": True})
        try:
            for case in cases:
                article = normalize_worker_result({"sourceUrl": "https://example.com/eval", "title": case["id"],
                                                   "markdownContent": case["markdown"]})
                key = pending.put(1, 1, article, (), {})
                for operation in operations:
                    start = time.perf_counter()
                    record: dict[str, Any] = {"case": case["id"], "operation": operation,
                                              "human_review": {"facts": None, "completeness": None,
                                                               "citation_support": None, "notes": ""}}
                    # This isolated runner is sequential: deltas belong to this invocation only.
                    before_usage = usage_totals(pending)
                    before_jobs = {job["id"] for job in pending.ai.jobs(1, 1, key)}
                    job = None
                    try:
                        job = await reader.run(1, 1, key, operation, question=case["question"])
                        result = job["result"]
                        record.update(status=job["status"], result=result)
                        if job.get("error"):
                            record["error"] = job["error"]
                        if result:
                            record["required_fact_hits"] = {
                                fact: fact in result["markdown"] for fact in case.get("required_facts", [])
                            }
                            if operation == "ask":
                                record["expected_answerability"] = case["answerable"]
                                record["answerability_match"] = result.get("answer_found") == case["answerable"]
                    except LLMError as error:
                        record.update(status="failed", error=str(error))
                        created = [dict(row) for row in pending.db.execute(
                            "SELECT id,status FROM ai_jobs WHERE user_id=? AND chat_id=? AND article_id=?",
                            (1, 1, key),
                        ) if row["id"] not in before_jobs]
                        if len(created) == 1:
                            job = created[0]
                    cost, uncertain = pending.ai.cost(job["id"]) if job else (0.0, 0)
                    after_usage = usage_totals(pending)
                    record.update(
                        job_id=job["id"] if job else None, estimated_cost=cost, uncertain_requests=uncertain,
                        request_count=after_usage[0] - before_usage[0],
                        incurred_cost=after_usage[1] - before_usage[1],
                        new_uncertain_requests=after_usage[2] - before_usage[2],
                    )
                    record["elapsed_seconds"] = time.perf_counter() - start
                    output["results"].append(record)
            output["estimated_total_usd"] = sum(r[0] for r in pending.db.execute("SELECT reserved FROM budgets"))
            output["request_count"] = pending.db.execute("SELECT count(*) FROM ai_usage").fetchone()[0]
        finally:
            pending.close()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/llm-reading.json"))
    parser.add_argument("--operations", default="summary,ask,translate")
    parser.add_argument("--model")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live", action="store_true", help="Send paid requests using the configured endpoint")
    parser.add_argument("--max-usd", type=float, help="Required estimated total budget for --live")
    args = parser.parse_args()
    cases = load_cases(args.fixtures)
    operations = args.operations.split(",")
    if any(op not in {"summary", "ask", "translate"} for op in operations):
        parser.error("operations must be summary,ask,translate")
    if args.live:
        if args.max_usd is None or not 0 < args.max_usd < float("inf"):
            parser.error("--live requires a positive finite --max-usd")
        overrides: dict[str, Any] = {
            "reader_bot_token": SecretStr("1:isolated-eval"), "reader_api_id": 1,
            "reader_api_hash": SecretStr("isolated-eval"), "parsehub_worker_secret": SecretStr("x" * 32),
            "llm_enabled": True, "llm_daily_budget": args.max_usd,
        }
        if args.model:
            overrides["openai_model"] = args.model
        try:
            config = Settings(**overrides)
        except ValidationError:
            parser.error("invalid model configuration; check endpoint, model capabilities and price rates")
        report = asyncio.run(evaluate(cases, config, operations))
    else:
        report = {"live": False, "network_requests": 0, "operations": operations,
                  "cases": [{"id": c["id"], "characters": len(c["markdown"]), "answerable": c["answerable"]}
                            for c in cases],
                  "next_step": "Use --live --max-usd only with authorization for paid calls."}
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(serialized + "\n")
    else:
        print(serialized)


if __name__ == "__main__":
    main()
