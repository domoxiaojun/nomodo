"""One-time state with Telegram owner and initiating browser binding."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from simpread.config import Settings
from simpread.integrations.notion import NotionClient, NotionStore


class OAuthServer:
    def __init__(
        self,
        settings: Settings,
        store: NotionStore,
        factory: Callable[[str], NotionClient] = NotionClient,
        notify: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self.settings, self.store, self.factory, self.notify = settings, store, factory, notify

    def begin(self, user_id: int) -> str:
        state = self.store.oauth_begin(user_id, pkce=self.settings.notion_pkce_enabled)
        return self.settings.notion_oauth_redirect_uri.rsplit("/", 1)[0] + "/start?" + urlencode({"state": state})

    def routes(self, app: web.Application) -> None:
        async def start(request: web.Request) -> web.StreamResponse:
            state, browser = request.query.get("state", ""), secrets.token_urlsafe(32)
            record = self.store.oauth_bind(state, browser)
            if record is None or record[0] not in self.settings.allowed_users:
                return web.Response(status=400, text="授权链接无效或已过期。")
            params = {
                "client_id": self.settings.notion_client_id,
                "response_type": "code",
                "owner": "user",
                "redirect_uri": self.settings.notion_oauth_redirect_uri,
                "state": state,
            }
            if record[1]:
                params.update(
                    code_challenge_method="S256",
                    code_challenge=base64.urlsafe_b64encode(hashlib.sha256(record[1].encode()).digest())
                    .rstrip(b"=")
                    .decode(),
                )
            response = web.HTTPFound("https://api.notion.com/v1/oauth/authorize?" + urlencode(params))
            response.set_cookie("simpread_oauth", browser, max_age=600, httponly=True, secure=True, samesite="Lax")
            response.headers["Referrer-Policy"] = "no-referrer"
            raise response

        async def callback(request: web.Request) -> web.Response:
            state, code = request.query.get("state", ""), request.query.get("code", "")
            binding = self.store.oauth_consume(state, request.cookies.get("simpread_oauth", "")) if code else None
            if binding is None or binding[0] not in self.settings.allowed_users:
                return web.Response(status=400, text="授权无效或已过期。")
            user_id, verifier = binding
            client = self.factory("")
            try:
                body = await client.oauth_token(
                    self.settings.notion_client_id,
                    self.settings.notion_client_secret.get_secret_value(),
                    self.settings.notion_oauth_redirect_uri,
                    code,
                    verifier,
                )
                token = body.get("access_token")
                if not isinstance(token, str) or not token:
                    raise ValueError("invalid_token")
                self.store.put_credential(user_id, token, str(body.get("workspace_name") or "Notion"))
            except Exception:
                return web.Response(status=502, text="授权交换失败，请重新发起授权。")
            finally:
                await client.close()
            if self.notify:
                try:
                    await self.notify(user_id)
                except Exception:
                    pass  # A notification failure does not undo a completed authorization.
            response = web.Response(
                text="授权完成，请回到 Telegram 使用 /notion targets。",
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
            response.del_cookie("simpread_oauth")
            return response

        path = urlsplit(self.settings.notion_oauth_redirect_uri).path
        app.router.add_get(path.rsplit("/", 1)[0] + "/start", start)
        app.router.add_get(path, callback)
