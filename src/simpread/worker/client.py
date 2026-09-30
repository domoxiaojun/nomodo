"""Versioned Worker HTTP adapter. Never imports the parse_hub_bot repository."""

from __future__ import annotations

import asyncio
import importlib.metadata
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from parsehub import ParseHub

from simpread.domain import Article, normalize_worker_result
from simpread.domain.normalize import public_result
from simpread.domain.urls import safe_url


class WorkerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorkerCapabilities:
    protocol_version: int
    parsehub_version: str
    platforms: tuple[dict[str, Any], ...]
    updated_at: float

    @property
    def platform_ids(self) -> frozenset[str]:
        return frozenset(str(item["id"]) for item in self.platforms)

    @property
    def names(self) -> str:
        return "、".join(str(item.get("name") or item["id"]) for item in self.platforms)


@dataclass(frozen=True)
class PreparedArticle:
    article: Article
    job_id: str
    lease_ids: tuple[str, ...]
    raw_result: dict[str, Any]


def extract_urls(text: str) -> list[str]:
    urls = list(dict.fromkeys(u.rstrip(".,;!?:，。；！、）)]}>") for u in re.findall(r"https?://[^\s<>]+", text)))
    if len(urls) > 10:
        raise WorkerError("too_many_links")
    return urls


def opaque(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise WorkerError("invalid_worker_id")
    return value


class WorkerClient:
    def __init__(
        self,
        base_url: str,
        service_key: str,
        account_id: str,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval: float = 1,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_key}"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self.account_id, self.poll_interval = account_id, poll_interval
        self.snapshot: WorkerCapabilities | None = None
        self.matcher = ParseHub()
        self.version = importlib.metadata.version("parsehub")

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
            if response.status_code >= 300:
                raise WorkerError("worker_http_error")
            return dict(response.json())
        except (httpx.HTTPError, ValueError, TypeError):
            raise WorkerError("worker_unavailable") from None

    async def capabilities(self, refresh: bool = False) -> WorkerCapabilities:
        if self.snapshot and not refresh and time.time() - self.snapshot.updated_at < 300:
            return self.snapshot
        body = await self.request("GET", "/api/v1/capabilities")
        if body.get("protocolVersion") != 3 or body.get("version") != self.version:
            raise WorkerError("worker_version_mismatch")
        self.snapshot = WorkerCapabilities(3, self.version, tuple(body["platforms"]), time.time())
        return self.snapshot

    async def startup(self) -> None:
        health = await self.request("GET", "/api/v1/health")
        if str(health.get("botId")) != self.account_id:
            raise WorkerError("bot_identity_mismatch")
        await self.capabilities(refresh=True)

    async def supported(self, url: str) -> bool:
        if not safe_url(url):
            return False
        snapshot = await self.capabilities()
        platform = self.matcher.get_platform(url)  # Pure local match; no parse or get_raw_url.
        return platform is not None and platform.id in snapshot.platform_ids

    async def prepare(self, url: str, timeout_seconds: float = 2700) -> PreparedArticle:
        if extract_urls(url) != [url] or not await self.supported(url):
            raise WorkerError("unsupported_url")
        job: dict[str, Any] = {}
        success = False
        try:
            async with asyncio.timeout(timeout_seconds):
                job = await self.request(
                    "POST",
                    "/api/v1/jobs",
                    json={
                        "text": url,
                        "accountId": self.account_id,
                        "mode": "auto",
                        "outputMode": "preview",
                        "requestId": uuid.uuid4().hex,
                        "idempotencyKey": uuid.uuid4().hex,
                    },
                )
                job_id = opaque(str(job["id"]))
                while job.get("status") not in {"ready", "failed", "interrupted", "cancelled"}:
                    await asyncio.sleep(self.poll_interval)
                    job = await self.request("GET", f"/api/v1/jobs/{job_id}")
                results = job.get("results", [])
                if job.get("status") != "ready" or len(results) != 1 or results[0].get("error"):
                    raise WorkerError("parse_failed")
                item = public_result(results[0])
                if item.get("platform") not in (await self.capabilities()).platform_ids:
                    raise WorkerError("unsupported_url")
                article = normalize_worker_result(item)
                prepared = PreparedArticle(
                    article, job_id, (opaque(item["leaseId"]),) if item.get("leaseId") else (), item
                )
                success = True
                return prepared
        except TimeoutError:
            raise WorkerError("worker_timeout") from None
        except (ValueError, KeyError, TypeError):
            raise WorkerError("worker_contract_error") from None
        finally:
            if not success:
                if job.get("id"):
                    await asyncio.shield(self.cancel(str(job["id"])))
                for result in job.get("results", []):
                    if result.get("leaseId"):
                        await asyncio.shield(self.release(str(result["leaseId"])))

    async def cancel(self, job_id: str) -> None:
        try:
            await self.request("DELETE", f"/api/v1/jobs/{opaque(job_id)}")
        except WorkerError:
            pass

    async def release(self, lease_id: str) -> None:
        try:
            await self.request("DELETE", f"/api/v1/leases/{opaque(lease_id)}")
        except WorkerError:
            pass  # Worker TTL remains the final bound when unreachable.

    async def renew(self, lease_id: str) -> None:
        await self.request("PUT", f"/api/v1/leases/{opaque(lease_id)}")

    async def download_media(
        self, lease_id: str, media_id: str, limit: int = 20_000_000, mime: str | None = None
    ) -> bytes:
        try:
            async with self.client.stream(
                "GET", f"/api/v1/leases/{opaque(lease_id)}/media/{opaque(media_id)}"
            ) as response:
                actual = response.headers.get("content-type", "").split(";")[0]
                if response.status_code != 200 or (mime and actual != mime):
                    raise WorkerError("media_unavailable")
                if int(response.headers.get("content-length", 0)) > limit:
                    raise WorkerError("media_too_large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise WorkerError("media_too_large")
                if not data:
                    raise WorkerError("media_unavailable")
                return bytes(data)
        except (httpx.HTTPError, ValueError):
            raise WorkerError("media_unavailable") from None
