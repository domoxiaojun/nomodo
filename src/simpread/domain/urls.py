"""Public web URLs only; no network requests are performed here."""

import ipaddress
import re
from urllib.parse import urlsplit


def is_xhs_url(value: str) -> bool:
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return False
    return host in {"xhslink.com", "xhslink.cn", "xiaohongshu.com"} or host.endswith(".xiaohongshu.com")


def normalize_share_url(value: str) -> str:
    # Keep the complete query verbatim: XHS access can depend on xsec_token.
    if value.startswith("http://") and is_xhs_url(value):
        return "https://" + value[len("http://") :]
    return value


def safe_url(value: str | None) -> str | None:
    if not value or len(value) > 2000 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return None
        if (
            parts.port not in {None, 80, 443}
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*", host):
                return None
        return value
    except ValueError:
        return None


# Compatibility with Workers predating advertised domains.
DOMAINS = {
    "weixin": ("mp.weixin.qq.com",), "twitter": ("x.com", "twitter.com", "fixupx.com", "t.co"),
    "youtube": ("youtube.com", "youtu.be"), "bilibili": ("bilibili.com", "b23.tv", "bili2233.cn"),
    "douyin": ("douyin.com",), "tiktok": ("tiktok.com",), "instagram": ("instagram.com",),
    "threads": ("threads.com", "threads.net"), "weibo": ("weibo.com", "weibo.cn"),
    "zhihu": ("zhihu.com",), "xhs": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
    "kuaishou": ("kuaishou.com", "chenzhongtech.com"), "facebook": ("facebook.com",),
    "snapchat": ("snapchat.com",), "coolapk": ("coolapk.com",), "tieba": ("tieba.baidu.com",),
    "douban": ("douban.com", "douc.cc"), "xiaoheihe": ("xiaoheihe.cn",),
    "pipix": ("pipix.com",), "zuiyou": ("xiaochuankeji.cn",),
}


def platform_host(value: str, platform: str, domains: tuple[str, ...] | list[str] | None = None) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in (domains or DOMAINS.get(platform, ())))
