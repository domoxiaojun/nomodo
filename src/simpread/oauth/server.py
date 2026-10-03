"""One-time state with Telegram owner and initiating browser binding."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from html import escape
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
        self.bot_username = ""

    def page(self, title: str, detail: str, status: int = 200) -> web.Response:
        button = ""
        if self.bot_username:
            link = "https://t.me/" + self.bot_username + "?start=notion"
            button = f'<a href="{escape(link, quote=True)}">返回 Telegram，继续设置</a>'
        body = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<style>body{{margin:0;background:#f3f5f8;color:#172635;font:17px/1.7 system-ui,sans-serif}}
main{{max-width:480px;margin:12vh auto;padding:32px;background:white;border-radius:20px}}
small{{color:#5d6e7e}}h1{{font-size:26px;margin:8px 0}}a{{display:block;padding:12px 18px;
border-radius:10px;background:#176b58;color:white;text-align:center;text-decoration:none}}
@media(max-width:540px){{main{{margin:10vh 16px;padding:24px}}}}</style></head>
<body><main><small>nomodo · 阅读助手</small><h1>{escape(title)}</h1>
<p>{escape(detail)}</p>{button}<p><small>可以关闭此页，返回机器人继续操作。</small></p></main></body></html>'''
        return web.Response(text=body, status=status, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                        "base-uri 'none'; frame-ancestors 'none'"),
        })

    def begin(self, user_id: int) -> str:
        state = self.store.oauth_begin(user_id, pkce=self.settings.notion_pkce_enabled)
        return self.settings.notion_oauth_redirect_uri.rsplit("/", 1)[0] + "/start?" + urlencode({"state": state})

    def routes(self, app: web.Application) -> None:
        async def start(request: web.Request) -> web.StreamResponse:
            state, browser = request.query.get("state", ""), secrets.token_urlsafe(32)
            record = self.store.oauth_bind(state, browser)
            if record is None or record[0] not in self.settings.allowed_users:
                return self.page("授权链接已失效", "请返回机器人，点击 Notion 中的连接按钮，重新获取授权链接。", 400)
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
            if request.query.get("error"):
                return self.page("授权尚未完成", "没有连接到 Notion。你可以返回机器人重新授权，其他功能仍可使用。", 400)
            state, code = request.query.get("state", ""), request.query.get("code", "")
            binding = self.store.oauth_consume(state, request.cookies.get("simpread_oauth", "")) if code else None
            if binding is None or binding[0] not in self.settings.allowed_users:
                return self.page("授权无效或已过期",
                                 "请在同一浏览器完成授权；需要重试时，从机器人获取新的授权链接。", 400)
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
                return self.page("暂时无法完成连接",
                                 "请返回机器人重新授权。如果仍然失败，请联系管理员检查 Notion 配置。", 502)
            finally:
                await client.close()
            if self.notify:
                try:
                    await self.notify(user_id)
                except Exception:
                    pass  # A notification failure does not undo a completed authorization.
            response = self.page("Notion 已连接",
                                 "下一步：返回机器人选择保存位置。若你刚才在保存文章，可以继续完成保存确认。")
            response.del_cookie("simpread_oauth")
            return response

        path = urlsplit(self.settings.notion_oauth_redirect_uri).path
        app.router.add_get(path.rsplit("/", 1)[0] + "/start", start)
        app.router.add_get(path, callback)
