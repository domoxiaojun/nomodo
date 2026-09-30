"""Owner/chat scoped durable articles, approvals, settings and daily budget reservations."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simpread.domain import Article


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
        """)
        self.db.commit()

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

    def delete(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM articles WHERE id=?", (key,))

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
        output = dict(json.loads(row[0])) if row else {"llm": False, "language": "zh-CN", "model": ""}
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
