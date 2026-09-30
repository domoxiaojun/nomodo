"""Independent encrypted SQLite credentials and durable export checkpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class NotionStore:
    def __init__(self, path: Path, key: str) -> None:
        raw = key.encode()
        if len(raw) != 32:
            raw = base64.urlsafe_b64decode(raw)
        if len(raw) != 32:
            raise ValueError("credential_key_invalid")
        self.aes = AESGCM(raw)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS secrets_v1 (user_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(user_id,kind));
            CREATE TABLE IF NOT EXISTS targets_v1 (
                user_id INTEGER,target_id TEXT,payload TEXT,PRIMARY KEY(user_id,target_id));
            CREATE TABLE IF NOT EXISTS exports_v1 (user_id INTEGER,article_hash TEXT,target_id TEXT,payload TEXT,
                                                  PRIMARY KEY(user_id,article_hash,target_id));
            CREATE TABLE IF NOT EXISTS oauth_v1 (
                state TEXT PRIMARY KEY,user_id INTEGER,browser TEXT,verifier TEXT,expires REAL);
        """)
        self.db.commit()
        for uid, digest, target, payload in self.db.execute("SELECT * FROM exports_v1").fetchall():
            value = json.loads(payload)
            if value.get("in_flight"):
                value["status"] = "unknown"
                self.save_export(uid, digest, target, value)

    def close(self) -> None:
        self.db.close()

    def secret(self, user_id: int, kind: str, value: str | None = None) -> str:
        binding = f"{user_id}:{kind}".encode()
        if value is not None:
            nonce = secrets.token_bytes(12)
            encrypted = base64.urlsafe_b64encode(nonce + self.aes.encrypt(nonce, value.encode(), binding)).decode()
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO secrets_v1 VALUES (?,?,?)", (user_id, kind, encrypted))
        row = self.db.execute("SELECT payload FROM secrets_v1 WHERE user_id=? AND kind=?", (user_id, kind)).fetchone()
        if not row:
            return ""
        raw = base64.urlsafe_b64decode(row[0])
        return self.aes.decrypt(raw[:12], raw[12:], binding).decode()

    def put_credential(self, user_id: int, token: str, workspace: str = "") -> None:
        self.secret(user_id, "notion", json.dumps({"token": token, "workspace": workspace}))

    def credential(self, user_id: int) -> tuple[str, str] | None:
        value = self.secret(user_id, "notion")
        if not value:
            return None
        body = json.loads(value)
        return str(body["token"]), str(body["workspace"])

    def disconnect(self, user_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM secrets_v1 WHERE user_id=? AND kind='notion'", (user_id,))
            self.db.execute("DELETE FROM targets_v1 WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM oauth_v1 WHERE user_id=?", (user_id,))

    def targets(self, user_id: int) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM targets_v1 WHERE user_id=?", (user_id,))]

    def save_target(self, user_id: int, target: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO targets_v1 VALUES (?,?,?)", (user_id, target["id"], json.dumps(target))
            )

    def select(self, user_id: int, target_id: str) -> None:
        targets = self.targets(user_id)
        if not any(t["id"] == target_id for t in targets):
            raise ValueError("target_not_found")
        for target in targets:
            target["default"] = target["id"] == target_id
            self.save_target(user_id, target)

    def export_status(self, user_id: int, article_hash: str, target_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM exports_v1 WHERE user_id=? AND article_hash=? AND target_id=?",
            (user_id, article_hash, target_id),
        ).fetchone()
        return dict(json.loads(row[0])) if row else None

    def save_export(self, user_id: int, article_hash: str, target_id: str, value: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO exports_v1 VALUES (?,?,?,?)",
                (user_id, article_hash, target_id, json.dumps(value)),
            )

    def oauth_begin(self, user_id: int, pkce: bool = False, ttl: int = 600) -> str:
        state = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM oauth_v1 WHERE user_id=? OR expires<?", (user_id, time.time()))
            self.db.execute(
                "INSERT INTO oauth_v1 VALUES (?,?,?,?,?)",
                (
                    hashlib.sha256(state.encode()).hexdigest(),
                    user_id,
                    None,
                    secrets.token_urlsafe(48) if pkce else None,
                    time.time() + ttl,
                ),
            )
        return state

    def oauth_bind(self, state: str, browser: str) -> tuple[int, str | None] | None:
        with self.db:
            row = self.db.execute(
                "UPDATE oauth_v1 SET browser=? WHERE state=? AND browser IS NULL AND expires>? "
                "RETURNING user_id,verifier",
                (hashlib.sha256(browser.encode()).hexdigest(), hashlib.sha256(state.encode()).hexdigest(), time.time()),
            ).fetchone()
        return (int(row[0]), row[1]) if row else None

    def oauth_consume(self, state: str, browser: str) -> tuple[int, str | None] | None:
        with self.db:
            row = self.db.execute(
                "DELETE FROM oauth_v1 WHERE state=? AND browser=? AND expires>? RETURNING user_id,verifier",
                (hashlib.sha256(state.encode()).hexdigest(), hashlib.sha256(browser.encode()).hexdigest(), time.time()),
            ).fetchone()
        return (int(row[0]), row[1]) if row else None
