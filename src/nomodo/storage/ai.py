"""Temporary, owner/chat scoped AI jobs and atomic budget accounting on the Reader DB."""

import json
import math
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from nomodo.integrations.openai.client import LLMError


class AIStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS ai_jobs (
                id TEXT PRIMARY KEY,user_id INTEGER,chat_id INTEGER,article_id TEXT,fingerprint TEXT,
                spec TEXT,status TEXT,result TEXT,error TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS ai_job_cache ON ai_jobs(user_id,chat_id,article_id,fingerprint);
            CREATE TABLE IF NOT EXISTS ai_steps (job_id TEXT,step TEXT,result TEXT,PRIMARY KEY(job_id,step));
            CREATE TABLE IF NOT EXISTS ai_turns (job_id TEXT PRIMARY KEY,article_id TEXT,question TEXT,answer TEXT);
            CREATE TABLE IF NOT EXISTS ai_usage (
                id TEXT PRIMARY KEY,job_id TEXT,user_id INTEGER,day TEXT,reserved REAL,actual REAL,
                input_rate REAL,output_rate REAL,uncertain INTEGER);
            CREATE TABLE IF NOT EXISTS ai_artifacts (
                user_id INTEGER,chat_id INTEGER,article_id TEXT,namespace TEXT,key TEXT,payload TEXT,
                PRIMARY KEY(user_id,chat_id,article_id,namespace,key));
        """)
        with self.db:
            self.db.execute("UPDATE ai_jobs SET status='interrupted',error='interrupted' WHERE status='running'")

    def article_valid(self, uid: int, chat: int, article: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM articles WHERE id=? AND user_id=? AND chat_id=? AND expires>?",
            (article, uid, chat, time.time()),
        ).fetchone() is not None

    def get(self, uid: int, chat: int, key: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT j.* FROM ai_jobs j JOIN articles a ON a.id=j.article_id "
            "WHERE j.id=? AND j.user_id=? AND j.chat_id=? AND a.user_id=? AND a.chat_id=? AND a.expires>?",
            (key, uid, chat, uid, chat, time.time()),
        ).fetchone()
        if not row:
            raise LLMError("ai_expired")
        value = dict(row)
        value["spec"] = json.loads(value["spec"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def jobs(self, uid: int, chat: int, article: str) -> list[dict[str, Any]]:
        keys = self.db.execute(
            "SELECT id FROM ai_jobs WHERE user_id=? AND chat_id=? AND article_id=? ORDER BY created DESC",
            (uid, chat, article),
        ).fetchall()
        return [self.get(uid, chat, row[0]) for row in keys] if self.article_valid(uid, chat, article) else []

    def begin(self, uid: int, chat: int, article: str, fingerprint: str, spec: dict[str, Any], refresh: bool) -> str:
        if not self.article_valid(uid, chat, article):
            raise LLMError("ai_expired")
        if not refresh:
            row = self.db.execute(
                "SELECT id FROM ai_jobs WHERE user_id=? AND chat_id=? AND article_id=? AND fingerprint=? "
                "ORDER BY created DESC LIMIT 1", (uid, chat, article, fingerprint),
            ).fetchone()
            if row:
                return str(row[0])
        key = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute(
                "INSERT INTO ai_jobs VALUES (?,?,?,?,?,?,'pending',NULL,NULL,?)",
                (key, uid, chat, article, fingerprint, json.dumps(spec, ensure_ascii=False), time.time()),
            )
        return key

    def state(self, job: dict[str, Any], status: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE ai_jobs SET status=?,error=? WHERE id=?", (status, error, job["id"]))

    def step(self, job: dict[str, Any], key: str, value: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.get(job["user_id"], job["chat_id"], job["id"])
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO ai_steps VALUES (?,?,?)", (job["id"], key, json.dumps(value)))
        row = self.db.execute("SELECT result FROM ai_steps WHERE job_id=? AND step=?", (job["id"], key)).fetchone()
        return json.loads(row[0]) if row else None

    def finish(self, job: dict[str, Any], result: dict[str, Any], derived: dict[str, Any]) -> None:
        with self.db:
            self.get(job["user_id"], job["chat_id"], job["id"])
            self.db.execute(
                "UPDATE ai_jobs SET status='completed',result=?,error=NULL WHERE id=?", (json.dumps(result), job["id"])
            )
            row = self.db.execute("SELECT derived FROM articles WHERE id=?", (job["article_id"],)).fetchone()
            previous = json.loads(row[0])
            previous.update(derived)
            self.db.execute("UPDATE articles SET derived=? WHERE id=?", (json.dumps(previous), job["article_id"]))
            if job["spec"]["operation"] == "ask":
                self.db.execute(
                    "INSERT OR REPLACE INTO ai_turns VALUES (?,?,?,?)",
                    (job["id"], job["article_id"], job["spec"]["question"], result["markdown"]),
                )

    def history(self, uid: int, chat: int, article: str) -> list[dict[str, str]]:
        return [dict(r) for r in self.db.execute(
            "SELECT t.question,t.answer FROM ai_turns t JOIN ai_jobs j ON t.job_id=j.id "
            "WHERE j.user_id=? AND j.chat_id=? AND t.article_id=? ORDER BY j.created", (uid, chat, article),
        )] if self.article_valid(uid, chat, article) else []

    def reserve(
        self, job: dict[str, Any], tokens: int, output: int, rate_in: float, rate_out: float, limit: float
    ) -> str:
        if not all(math.isfinite(v) and v >= 0 for v in (rate_in, rate_out, limit)):
            raise LLMError("llm_config_incomplete")
        cost = (tokens * rate_in + output * rate_out) / 1_000_000
        day = datetime.now(UTC).date().isoformat()
        key = secrets.token_urlsafe(12)
        with self.db:
            self.get(job["user_id"], job["chat_id"], job["id"])
            self.db.execute("INSERT OR IGNORE INTO budgets VALUES (?,?,0)", (job["user_id"], day))
            changed = self.db.execute(
                "UPDATE budgets SET reserved=reserved+? WHERE user_id=? AND day=? AND reserved+?<=?",
                (cost, job["user_id"], day, cost, limit),
            ).rowcount
            if not changed:
                raise LLMError("budget_exhausted")
            self.db.execute("INSERT INTO ai_usage VALUES (?,?,?,?,?,NULL,?,?,1)",
                            (key, job["id"], job["user_id"], day, cost, rate_in, rate_out))
        return key

    def settle(self, ticket: str, input_tokens: int | None, output_tokens: int | None, rejected: bool) -> None:
        with self.db:
            row = self.db.execute("SELECT * FROM ai_usage WHERE id=? AND actual IS NULL", (ticket,)).fetchone()
            if not row:
                return  # A deleted article must not resurrect private accounting rows.
            if rejected:
                actual = 0.0
            elif (isinstance(input_tokens, int) and isinstance(output_tokens, int)
                  and input_tokens >= 0 and output_tokens >= 0):
                actual = (input_tokens * row["input_rate"] + output_tokens * row["output_rate"]) / 1_000_000
            else:
                return
            self.db.execute("UPDATE budgets SET reserved=MAX(0,reserved+?) WHERE user_id=? AND day=?",
                            (actual - row["reserved"], row["user_id"], row["day"]))
            self.db.execute("UPDATE ai_usage SET actual=?,uncertain=0 WHERE id=?", (actual, ticket))

    def cost(self, job_id: str) -> tuple[float, int]:
        row = self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),COALESCE(SUM(uncertain),0) FROM ai_usage WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return float(row[0]), int(row[1])

    def progress(self, job_id: str) -> int:
        return int(self.db.execute("SELECT count(*) FROM ai_steps WHERE job_id=?", (job_id,)).fetchone()[0])

    def artifact(
        self, job: dict[str, Any], namespace: str, key: str, value: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.get(job["user_id"], job["chat_id"], job["id"])
        scope = (job["user_id"], job["chat_id"], job["article_id"], namespace, key)
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO ai_artifacts VALUES (?,?,?,?,?,?)",
                                (*scope, json.dumps(value, ensure_ascii=False)))
        row = self.db.execute(
            "SELECT payload FROM ai_artifacts WHERE user_id=? AND chat_id=? AND article_id=? AND namespace=? AND key=?",
            scope,
        ).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, article_id: str) -> None:
        with self.db:
            for table in ("ai_steps", "ai_turns", "ai_usage"):
                self.db.execute(f"DELETE FROM {table} WHERE job_id IN (SELECT id FROM ai_jobs WHERE article_id=?)",
                                (article_id,))
            self.db.execute("DELETE FROM ai_jobs WHERE article_id=?", (article_id,))
            self.db.execute("DELETE FROM ai_artifacts WHERE article_id=?", (article_id,))

    def purge_expired(self) -> None:
        for row in self.db.execute("SELECT id FROM articles WHERE expires<=?", (time.time(),)).fetchall():
            self.delete(row[0])
            with self.db:
                self.db.execute("UPDATE articles SET derived='{}' WHERE id=?", (row[0],))
