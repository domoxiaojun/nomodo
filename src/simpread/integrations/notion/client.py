"""Official Notion REST API, fixed origin, no blind replay of uncertain writes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx


class NotionError(RuntimeError):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def notion_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise NotionError("invalid_target") from None


class NotionClient:
    def __init__(
        self,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url="https://api.notion.com/v1/",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2025-09-03"},
            timeout=30,
            transport=transport,
            follow_redirects=False,
        )
        self.sleep = sleep

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, *, read_only: bool = False, **kwargs: Any) -> dict[str, Any]:
        mutation = method != "GET" and not read_only
        for attempt in range(4):
            try:
                response = await self.client.request(method, path, **kwargs)
            except httpx.HTTPError:
                if mutation:
                    raise NotionError("write_outcome_unknown") from None
                if attempt == 3:
                    raise NotionError("notion_unavailable") from None
                await self.sleep(2**attempt)
                continue
            status = response.status_code
            if status == 401:
                raise NotionError("authorization_expired", status)
            if status == 403:
                raise NotionError("permission_denied", status)
            if status == 409:
                raise NotionError("conflict", status)
            if status == 429:
                try:
                    delay = max(0, float(response.headers.get("Retry-After", "1")))
                except ValueError:
                    delay = 1
                if delay > 60 or attempt == 3:
                    raise NotionError("rate_limited", status)
                await self.sleep(delay)
                continue
            if status >= 500:
                if mutation:
                    raise NotionError("write_outcome_unknown", status)
                if attempt < 3:
                    await self.sleep(2**attempt)
                    continue
            if status >= 300:
                raise NotionError("notion_request_failed", status)
            try:
                return dict(response.json())
            except (ValueError, TypeError):
                raise NotionError("write_outcome_unknown" if mutation else "notion_invalid_response") from None
        raise NotionError("notion_unavailable")

    async def search(self) -> list[dict[str, Any]]:
        output = []
        cursor = None
        seen: set[str] = set()
        while True:
            body = await self.request(
                "POST",
                "search",
                read_only=True,
                json={"page_size": 100, **({"start_cursor": cursor} if cursor else {})},
            )
            results, cursor = self.page(body, seen)
            output.extend(results)
            if cursor is None:
                return output

    @staticmethod
    def page(body: dict[str, Any], seen: set[str]) -> tuple[list[dict[str, Any]], str | None]:
        results = body.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise NotionError("notion_invalid_response")
        if not body.get("has_more"):
            return results, None
        cursor = body.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise NotionError("notion_invalid_response")
        seen.add(cursor)
        return results, cursor

    async def target(self, target_id: str, kind: str) -> dict[str, Any]:
        return await self.request("GET", f"{'pages' if kind == 'page' else 'data_sources'}/{notion_id(target_id)}")

    async def children(self, page_id: str) -> list[dict[str, Any]]:
        output = []
        cursor = None
        seen: set[str] = set()
        while True:
            body = await self.request(
                "GET",
                f"blocks/{notion_id(page_id)}/children",
                params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})},
            )
            results, cursor = self.page(body, seen)
            output.extend(results)
            if cursor is None:
                return output

    async def create_page(
        self, parent: dict[str, Any], properties: dict[str, Any], children: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self.request(
            "POST", "pages", json={"parent": parent, "properties": properties, "children": children}
        )

    async def append(self, page_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.request("PATCH", f"blocks/{notion_id(page_id)}/children", json={"children": children})

    async def add_properties(self, target_id: str, properties: dict[str, Any]) -> None:
        await self.request("PATCH", f"data_sources/{notion_id(target_id)}", json={"properties": properties})

    async def upload(self, data: bytes, mime: str, filename: str) -> str:
        body = await self.request(
            "POST", "file_uploads", json={"mode": "single_part", "filename": filename, "content_type": mime}
        )
        upload_id = notion_id(body["id"])
        response = await self.request("POST", f"file_uploads/{upload_id}/send", files={"file": (filename, data, mime)})
        if response.get("status") != "uploaded":
            raise NotionError("media_failed")
        return upload_id

    async def oauth_token(
        self, client_id: str, secret: str, redirect_uri: str, code: str, verifier: str | None
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "oauth/token",
            auth=(client_id, secret),
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                **({"code_verifier": verifier} if verifier else {}),
            },
        )
