"""Owner/chat scoped durable articles, approvals, settings and optional budgets."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nomodo.domain import Article

from .ai import AIStore


class PendingStore:
    def __init__(self, path: Path, ttl: int = 1800) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.ttl = ttl
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, user_id INTEGER, chat_id INTEGER, article TEXT, leases TEXT,
                raw TEXT, derived TEXT, expires REAL, created REAL);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, user_id INTEGER, chat_id INTEGER, payload TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS budgets (user_id INTEGER, day TEXT, reserved REAL, PRIMARY KEY(user_id,day));
            CREATE TABLE IF NOT EXISTS selected_articles (
                user_id INTEGER, chat_id INTEGER, article_id TEXT, PRIMARY KEY(user_id,chat_id));
            CREATE TABLE IF NOT EXISTS ai_results (
                user_id INTEGER, article_id TEXT, field TEXT, signature TEXT, value TEXT,
                PRIMARY KEY(user_id, article_id, field));
        """)
        self.db.commit()
        self.ai = AIStore(self.db)

    def close(self) -> None:
        self.db.close()

    def put(self, user_id: int, chat_id: int, article: Article, leases: tuple[str, ...], raw: dict[str, Any]) -> str:
        key = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute(
                "INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    user_id,
                    chat_id,
                    article.model_dump_json(),
                    json.dumps(leases),
                    json.dumps(raw),
                    "{}",
                    time.time() + self.ttl,
                    time.time(),
                ),
            )
            self.db.execute("INSERT OR REPLACE INTO selected_articles VALUES (?,?,?)", (user_id, chat_id, key))
        return key

    def get(self, user_id: int, chat_id: int, key: str) -> tuple[Article, tuple[str, ...]] | None:
        row = self.db.execute(
            "SELECT article,leases FROM articles WHERE id=? AND user_id=? AND chat_id=? AND expires>?",
            (key, user_id, chat_id, time.time()),
        ).fetchone()
        return (Article.model_validate_json(row[0]), tuple(json.loads(row[1]))) if row else None

    def latest(self, user_id: int, chat_id: int) -> str | None:
        row = self.db.execute(
            "SELECT id FROM articles WHERE user_id=? AND chat_id=? AND expires>? ORDER BY created DESC LIMIT 1",
            (user_id, chat_id, time.time()),
        ).fetchone()
        return str(row[0]) if row else None

    def current(self, user_id: int, chat_id: int) -> str | None:
        row = self.db.execute(
            "SELECT article_id FROM selected_articles WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        ).fetchone()
        # An expired explicit selection must not silently operate on a different article.
        return str(row[0]) if row else self.latest(user_id, chat_id)

    def select(self, user_id: int, chat_id: int, key: str) -> None:
        if self.get(user_id, chat_id, key) is None:
            raise ValueError("article_unavailable")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO selected_articles VALUES (?,?,?)", (user_id, chat_id, key))

    def articles(self, user_id: int, chat_id: int) -> list[tuple[str, str]]:
        return [
            (row[0], Article.model_validate_json(row[1]).title or "无标题")
            for row in self.db.execute(
                "SELECT id,article FROM articles WHERE user_id=? AND chat_id=? AND expires>? ORDER BY created DESC",
                (user_id, chat_id, time.time()),
            )
        ]

    def recent(self, user_id: int, chat_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,article,expires FROM articles WHERE user_id=? AND chat_id=? AND expires>? ORDER BY created DESC",
            (user_id, chat_id, time.time()),
        )
        return [
            {"id": row["id"], "title": json.loads(row["article"]).get("title", ""), "expires": row["expires"]}
            for row in rows
        ]

    def find_url(self, user_id: int, chat_id: int, url: str) -> str | None:
        rows = self.db.execute(
            "SELECT id,article FROM articles WHERE user_id=? AND chat_id=? AND expires>? ORDER BY created DESC",
            (user_id, chat_id, time.time()),
        )
        return next(
            (str(row["id"]) for row in rows if json.loads(row["article"])["source"]["original_url"] == url),
            None,
        )

    def minutes_left(self, user_id: int, chat_id: int, key: str) -> int:
        row = self.db.execute(
            "SELECT expires FROM articles WHERE id=? AND user_id=? AND chat_id=?", (key, user_id, chat_id)
        ).fetchone()
        return max(1, int((row[0] - time.time()) / 60)) if row else 0

    def entries(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT id,user_id,chat_id,leases,expires FROM articles")]

    def count(self, user_id: int) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM articles WHERE user_id=? AND expires>?", (user_id, time.time())
            ).fetchone()[0]
        )

    def clear_leases(self, key: str) -> None:
        with self.db:
            self.db.execute("UPDATE articles SET leases='[]' WHERE id=?", (key,))

    def delete(self, key: str, *, forget_selection: bool = False) -> None:
        with self.db:
            self.ai.delete(key)
            self.db.execute("DELETE FROM articles WHERE id=?", (key,))
            self.db.execute("DELETE FROM ai_results WHERE article_id=?", (key,))
            if forget_selection:
                self.db.execute("DELETE FROM selected_articles WHERE article_id=?", (key,))

    def cached_ai(self, user_id: int, key: str, field: str, signature: str) -> Any:
        row = self.db.execute(
            "SELECT value FROM ai_results WHERE user_id=? AND article_id=? AND field=? AND signature=?",
            (user_id, key, field, signature),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_ai(self, user_id: int, key: str, field: str, signature: str, value: Any) -> None:
        with self.db:
            row = self.db.execute("SELECT derived FROM articles WHERE id=? AND user_id=?", (key, user_id)).fetchone()
            if not row:
                raise ValueError("article_expired")
            derived = json.loads(row[0])
            derived[field] = value
            self.db.execute(
                "UPDATE articles SET derived=? WHERE id=? AND user_id=?", (json.dumps(derived), key, user_id)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO ai_results VALUES (?,?,?,?,?)",
                (user_id, key, field, signature, json.dumps(value)),
            )

    def derived(self, user_id: int, key: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self.db.execute("SELECT derived FROM articles WHERE id=? AND user_id=?", (key, user_id)).fetchone()
        output = dict(json.loads(row[0])) if row else {}
        if row and value is not None:
            output.update({k: v for k, v in value.items() if v})
            with self.db:
                self.db.execute(
                    "UPDATE articles SET derived=? WHERE id=? AND user_id=?", (json.dumps(output), key, user_id)
                )
        return output

    def approve(self, user_id: int, chat_id: int, payload: dict[str, Any]) -> str:
        key = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute("DELETE FROM approvals WHERE expires<?", (time.time(),))
            self.db.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?)",
                (key, user_id, chat_id, json.dumps(payload), time.time() + 600),
            )
        return key

    def consume(self, user_id: int, chat_id: int, key: str) -> dict[str, Any] | None:
        with self.db:
            row = self.db.execute(
                "DELETE FROM approvals WHERE id=? AND user_id=? AND chat_id=? AND expires>? RETURNING payload",
                (key, user_id, chat_id, time.time()),
            ).fetchone()
        return dict(json.loads(row[0])) if row else None

    def preferences(self, user_id: int, update: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self.db.execute("SELECT payload FROM settings WHERE user_id=?", (user_id,)).fetchone()
        output = {"llm": True, "language": "zh-CN", "model": ""}
        if row:
            output.update(json.loads(row[0]))
        if update:
            output.update(update)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (user_id, json.dumps(output)))
        return output

    def reserve(self, user_id: int, cost: float, limit: float) -> bool:
        day = datetime.now(UTC).date().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO budgets VALUES (?,?,0)", (user_id, day))
            return (
                self.db.execute(
                    "UPDATE budgets SET reserved=reserved+? WHERE user_id=? AND day=? AND reserved+?<=?",
                    (cost, user_id, day, cost, limit),
                ).rowcount
                == 1
            )
