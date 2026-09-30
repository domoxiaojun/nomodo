"""Public web URLs only; no network requests are performed here."""

import ipaddress
import re
from urllib.parse import urlsplit


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
