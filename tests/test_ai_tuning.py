import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_ai_reader import Generator, app_for, fixture_article, response

from nomodo import eval_reader
from nomodo.domain import normalize_worker_result
from nomodo.integrations.openai.client import LLMError, ResponsesClient
from nomodo.integrations.openai.content import pack, prepare
from nomodo.integrations.openai.reader import ReaderService
from nomodo.integrations.openai.schemas import Answer, Citation
from nomodo.integrations.openai.tokens import Tokens
from nomodo.integrations.openai.tuning import AdaptiveGate, evidence_risks, ordered_map


def test_reading_uses_input_window_instead_of_translation_cap(tmp_path: Path) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        generator = Generator()
        app.reader.factory = generator.factory
        value = fixture_article("Paris is a city. " * 700)
        old_units, _ = prepare(value, Tokens("fixture"), 4096 // 3)
        old_count = len(pack(old_units, Tokens("fixture"), 4096 // 3))
        key = app.pending.put(1, 1, value, (), {})
        await app.reader.run(1, 1, key, "summary")
        assert old_count > 5 and len(generator.calls) == 1
        await app.reader.run(1, 1, key, "summary", mode="detailed")
        assert len(generator.calls) == 2  # Verified extraction reused; only the detailed synthesis is new.
        await app.close()
    asyncio.run(run())


def test_local_layout_requires_no_model_or_credentials(tmp_path: Path) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        app.settings.llm_enabled = False
        app.settings.openai_api_key = type(app.settings.openai_api_key)("")
        app.reader.factory = lambda *a, **kw: pytest.fail("local formatting must not build a client")
        value = normalize_worker_result({"sourceUrl": "https://example.com", "markdownContent":
                                        "hello **bold** word\n\n```python\nx = '  keep  '\n```"})
        key = app.pending.put(1, 1, value, (), {})
        job = await app.reader.run(1, 1, key, "normalize_markdown")
        assert job["result"]["local"] and "x = '  keep  '" in job["result"]["markdown"]
        assert "<strong>bold</strong> word" in job["result"]["html"]
        assert app.pending.ai.cost(job["id"]) == (0, 0)
        await app.close()
    asyncio.run(run())


def test_translation_preserves_paragraph_context_and_glossary(tmp_path: Path) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        generator = Generator()
        app.reader.factory = generator.factory
        value = normalize_worker_result({"sourceUrl": "https://example.com", "markdownContent":
                                        "# Travel\n\nParis **Paris** `code` remains Paris."})
        key = app.pending.put(1, 1, value, (), {})
        result = await app.reader.run(1, 1, key, "translate")
        requests = [json.loads(c["input"][-1]["content"]) for c in generator.calls
                    if c["text"]["format"]["name"] == "Transformation"]
        assert requests[0]["glossary"]["Paris"] == "巴黎"
        assert "code" in requests[0]["context"]["paragraph"]
        assert "<strong>巴黎</strong>" in result["result"]["html"]
        await app.close()
    asyncio.run(run())


def test_history_compression_only_processes_new_turns(tmp_path: Path) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        generator = Generator()
        app.reader.factory = generator.factory
        key = app.pending.put(1, 1, fixture_article(), (), {})
        for question in ("Which city?", "What is said about it?", "Explain that again?"):
            await app.reader.run(1, 1, key, "ask", question=question)
        calls = [json.loads(c["input"][-1]["content"]) for c in generator.calls
                 if c["text"]["format"]["name"] == "ReadingContext"]
        assert len(calls) == 2
        assert "Which city?" in calls[0]["history_excerpt"]
        assert "Which city?" not in calls[1]["history_excerpt"]
        assert "What is said about it?" in calls[1]["history_excerpt"]
        await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("answerable", [False, True])
def test_retrieval_finds_tail_and_expands_before_no_answer(tmp_path: Path, answerable: bool) -> None:
    async def run() -> None:
        app = app_for(tmp_path, llm_context_tokens=8192, llm_max_output_tokens=2048)
        generator = Generator()
        app.reader.factory = generator.factory
        text = "\n\n".join(f"section {i} " + "unrelated background. " * 90 for i in range(30))
        if answerable:
            text += "\n\nParis is recorded at NeptuneSite."
        key = app.pending.put(1, 1, fixture_article(text), (), {})
        job = await app.reader.run(1, 1, key, "ask", question="What city is recorded at NeptuneSite?")
        coverage = job["result"]["coverage"]
        assert coverage["total"] > 5
        assert job["result"]["answer_found"] is answerable
        assert (coverage["scanned"] < coverage["total"]) if answerable else (coverage["scanned"] == coverage["total"])
        await app.close()
    asyncio.run(run())


def test_bounded_concurrency_and_failure_drain() -> None:
    async def run() -> None:
        gate = AdaptiveGate(3)
        peak = 0
        completed = []
        async def work(i: int) -> int:
            nonlocal peak
            async with gate.slot():
                peak = max(peak, gate.active)
                await asyncio.sleep((3 - i % 3) / 1000)
                completed.append(i)
                return i
        assert await ordered_map(list(range(7)), work, 3) == list(range(7))
        assert peak == 3 and len(completed) == 7 and gate.active == 0
        peak = 0
        gate.limited()
        assert gate.cooldown > time.monotonic()
        await ordered_map(list(range(3)), work, 3)
        assert peak == 1 and gate.active == 0
        gate.cooldown = 0
        cleaned = asyncio.Event()
        async def failing(i: int) -> int:
            async with gate.slot():
                if i == 0:
                    await asyncio.sleep(0)
                    raise LLMError("fixture_failure")
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return i
        with pytest.raises(LLMError, match="fixture_failure"):
            await ordered_map([0, 1], failing, 2)
        assert cleaned.is_set() and gate.active == 0
    asyncio.run(run())


def test_risk_checks_are_heuristics_not_automatic_truth() -> None:
    answer = Answer(found=True, answer="Take 20 kg. It is effective.",
                    citations=[Citation(block_id=1, quote="Take 12 mg. The trial cannot prove effectiveness.")])
    assert set(evidence_risks(answer)) == {"numbers_or_dates", "units", "negation"}


def test_opt_in_evidence_review_rejects_unsupported_claim(tmp_path: Path) -> None:
    async def run() -> None:
        app = app_for(tmp_path, llm_verify_risks=True)
        generator = Generator()
        kinds = []
        async def handler(req: httpx.Request) -> httpx.Response:
            kind = json.loads(req.content)["text"]["format"]["name"]
            kinds.append(kind)
            if kind == "EvidenceReview":
                return httpx.Response(200, json=response({"supported": False, "explanation": "Negation omitted."}))
            return await generator.handle(req)
        def factory(*args: Any, **kwargs: Any) -> ResponsesClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return ResponsesClient(*args, **kwargs)
        app.reader.factory = factory
        key = app.pending.put(1, 1, fixture_article("The trial cannot prove effectiveness."), (), {})
        with pytest.raises(LLMError, match="unsupported_claim"):
            await app.reader.run(1, 1, key, "summary")
        assert kinds == ["Summary", "EvidenceReview"]
        assert "summary" not in app.pending.derived(1, key)
        await app.close()
    asyncio.run(run())


def test_artifacts_expire_with_article(tmp_path: Path) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        generator = Generator()
        app.reader.factory = generator.factory
        key = app.pending.put(1, 1, fixture_article(), (), {})
        await app.reader.run(1, 1, key, "ask", question="Which city?")
        assert app.pending.db.execute("SELECT count(*) FROM ai_artifacts").fetchone()[0] > 0
        app.pending.delete(key)
        app.reader.prune_locks()
        assert app.pending.db.execute("SELECT count(*) FROM ai_artifacts").fetchone()[0] == 0
        assert not app.reader.locks
        await app.close()
    asyncio.run(run())


def test_eval_default_never_constructs_model(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr("sys.argv", ["eval_reader"])
    monkeypatch.setattr(eval_reader, "Settings", lambda **kw: pytest.fail("dry-run loaded live configuration"))
    eval_reader.main()
    report = json.loads(capsys.readouterr().out)
    assert report["live"] is False and report["network_requests"] == 0 and len(report["cases"]) >= 6


def test_eval_live_requires_explicit_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["eval_reader", "--live"])
    with pytest.raises(SystemExit):
        eval_reader.main()


@pytest.mark.parametrize("outcome", ["success", "timeout", "no_usage", "budget", "configuration"])
def test_eval_accounts_for_failed_and_cached_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        generator = Generator()
        if outcome == "timeout":
            generator.fail_at = 1
        if outcome == "no_usage":
            generator.no_usage = True
        if outcome == "budget":
            app.settings.llm_daily_budget = 0.000001
        if outcome == "configuration":
            app.settings.openai_api_key = type(app.settings.openai_api_key)("")
        original = eval_reader.ReaderService

        def factory(*args: Any, **kwargs: Any) -> Any:
            reader = original(*args, **kwargs)
            reader.factory = generator.factory
            return reader

        monkeypatch.setattr(eval_reader, "ReaderService", factory)
        try:
            report = await eval_reader.evaluate(
                [{"id": "case", "markdown": "Paris is the capital.", "question": "Which city?",
                  "answerable": True}], app.settings, ["summary", "summary"],
            )
        finally:
            await app.close()
        first, repeated = report["results"]
        assert first["job_id"] == repeated["job_id"]
        assert repeated["request_count"] == repeated["incurred_cost"] == repeated["new_uncertain_requests"] == 0
        assert first["estimated_cost"] == repeated["estimated_cost"]
        assert first["incurred_cost"] == pytest.approx(report["estimated_total_usd"])
        assert first["request_count"] == report["request_count"] == len(generator.calls)
        assert first["uncertain_requests"] == repeated["uncertain_requests"]
        if outcome in {"timeout", "no_usage"}:
            assert first["new_uncertain_requests"] == first["uncertain_requests"] == 1
            assert first["incurred_cost"] > 0
        if outcome in {"timeout", "budget", "configuration"}:
            assert first["status"] == repeated["status"] == "failed"
            assert first["error"] == repeated["error"]
        else:
            assert first["status"] == "completed"
        if outcome == "configuration":
            assert first["job_id"] is None
        else:
            assert first["job_id"]
    asyncio.run(run())


def test_eval_pre_job_failure_does_not_reuse_previous_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        app = app_for(tmp_path)
        generator = Generator()
        class Reader(ReaderService):
            async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                if generator.calls:
                    raise LLMError("llm_config_incomplete")
                return await super().run(*args, **kwargs)

        def factory(*args: Any, **kwargs: Any) -> Any:
            reader = Reader(*args, **kwargs)
            reader.factory = generator.factory
            return reader

        monkeypatch.setattr(eval_reader, "ReaderService", factory)
        try:
            report = await eval_reader.evaluate(
                [{"id": "case", "markdown": "Paris is the capital.", "question": "Which city?",
                  "answerable": True}], app.settings, ["summary", "ask"],
            )
        finally:
            await app.close()
        first, failed = report["results"]
        assert first["job_id"] and first["estimated_cost"] > 0
        assert failed["job_id"] is None
        assert failed["estimated_cost"] == failed["request_count"] == failed["uncertain_requests"] == 0
    asyncio.run(run())
