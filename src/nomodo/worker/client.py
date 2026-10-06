"""Versioned Worker HTTP adapter. Never imports the parse_hub_bot repository."""

from __future__ import annotations

import asyncio
import importlib.metadata
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from parsehub import ParseHub

from nomodo.domain import Article, normalize_worker_result
from nomodo.domain.normalize import public_result
from nomodo.domain.urls import normalize_share_url, platform_host, safe_url


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
    urls = list(
        dict.fromkeys(
            normalize_share_url(u.rstrip(".,;!?:）)]}>"))
            for u in re.findall(r"https?://[^\s<>，。；！、（）【】\"“”‘’]+", text)
        )
    )
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
        self._cache = tempfile.TemporaryDirectory(prefix="reader-lease-cache-")
        self._cache_mimes: dict[tuple[str, str], str] = {}

    async def close(self) -> None:
        await self.client.aclose()
        self._cache.cleanup()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
            if response.status_code >= 300:
                raise WorkerError("worker_http_error")
            body = response.json()
            if not isinstance(body, dict):
                raise WorkerError("worker_contract_error")
            return body
        except (httpx.HTTPError, ValueError, TypeError):
            raise WorkerError("worker_unavailable") from None

    async def capabilities(self, refresh: bool = False) -> WorkerCapabilities:
        if self.snapshot and not refresh and time.time() - self.snapshot.updated_at < 300:
            return self.snapshot
        body = await self.request("GET", "/api/v1/capabilities")
        if body.get("protocolVersion") != 3 or body.get("version") != self.version:
            raise WorkerError("worker_version_mismatch")
        platforms = body.get("platforms")
        if not isinstance(platforms, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
            for item in platforms
        ):
            raise WorkerError("worker_contract_error")
        self.snapshot = WorkerCapabilities(3, self.version, tuple(platforms), time.time())
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
        for entry in snapshot.platforms:
            patterns = entry.get("urlPatterns")
            domains = entry.get("domains")
            if isinstance(patterns, list) and isinstance(domains, list) and domains:
                if platform_host(url, entry["id"], domains) and any(
                    isinstance(pattern, str) and pattern and re.match(pattern, url) for pattern in patterns
                ):
                    return True
        platform = self.matcher.get_platform(url)
        if platform and any(e["id"] == platform.id and e.get("domains") and e.get("urlPatterns")
                            for e in snapshot.platforms):
            return False
        return bool(platform and platform.id in snapshot.platform_ids and platform_host(url, platform.id))

    async def prepare(self, url: str, timeout_seconds: float = 2700) -> PreparedArticle:
        url = normalize_share_url(url)
        if not safe_url(url):
            raise WorkerError("unsupported_url")
        parsed_url = urlsplit(url)
        if (
            parsed_url.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
            and parsed_url.path.startswith("/i/article/")
        ):
            raise WorkerError("article_post_link_required")
        if extract_urls(url) != [url] or not await self.supported(url):
            raise WorkerError("unsupported_url")
        job: dict[str, Any] = {}
        job_id: str | None = None
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
                if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                    raise WorkerError("worker_contract_error")
                if job.get("status") != "ready" or len(results) != 1 or results[0].get("error"):
                    error = results[0].get("error") if len(results) == 1 else None
                    error = error or job.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    known = {
                        "credentials_required", "credentials_invalid", "content_unavailable",
                        "upstream_timeout", "upstream_http", "upstream_contract", "media_failed", "unsupported_url",
                    }
                    raise WorkerError(code if isinstance(code, str) and code in known else "parse_failed")
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
                if job_id:
                    await asyncio.shield(self.cancel(job_id))
                results = job.get("results", [])
                if isinstance(results, list):
                    for result in results:
                        if isinstance(result, dict) and result.get("leaseId"):
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
        finally:
            shutil.rmtree(Path(self._cache.name) / opaque(lease_id), ignore_errors=True)
            self._cache_mimes = {key: value for key, value in self._cache_mimes.items() if key[0] != lease_id}

    async def renew(self, lease_id: str) -> None:
        await self.request("PUT", f"/api/v1/leases/{opaque(lease_id)}")

    async def download_media(
        self, lease_id: str, media_id: str, limit: int = 20_000_000, mime: str | None = None
    ) -> bytes:
        cache_key = (opaque(lease_id), opaque(media_id))
        cache_path = Path(self._cache.name) / cache_key[0] / cache_key[1]
        if cache_key in self._cache_mimes and cache_path.is_file():
            if mime and self._cache_mimes[cache_key] != mime:
                raise WorkerError("media_unavailable")
            if cache_path.stat().st_size > limit:
                raise WorkerError("media_too_large")
            return cache_path.read_bytes()
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
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                staging = cache_path.with_name(cache_path.name + "." + uuid.uuid4().hex)
                try:
                    staging.write_bytes(data)
                    staging.replace(cache_path)
                    self._cache_mimes[cache_key] = actual
                finally:
                    staging.unlink(missing_ok=True)
                return bytes(data)
        except (httpx.HTTPError, ValueError):
            raise WorkerError("media_unavailable") from None

    @asynccontextmanager
    async def media_file(self, lease_id: str, media_id: str, mime: str | None = None) -> AsyncIterator[Path]:
        # Telegram regular upload boundary; memory usage is independent of file size.
        limit = 2_000_000_000
        with tempfile.TemporaryDirectory(prefix="reader-media-") as directory:
            path = Path(directory) / "media"
            try:
                endpoint = f"/api/v1/leases/{opaque(lease_id)}/media/{opaque(media_id)}"
                async with self.client.stream("GET", endpoint) as response:
                    actual = response.headers.get("content-type", "").split(";")[0]
                    if response.status_code != 200 or (mime and actual != mime):
                        raise WorkerError("media_unavailable")
                    if int(response.headers.get("content-length", 0)) > limit:
                        raise WorkerError("media_too_large")
                    size = 0
                    with path.open("wb") as output:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise WorkerError("media_too_large")
                            output.write(chunk)
                    if not size:
                        raise WorkerError("media_unavailable")
                yield path
            except (httpx.HTTPError, ValueError):
                raise WorkerError("media_unavailable") from None
