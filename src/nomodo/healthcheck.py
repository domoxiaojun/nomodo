"""Check this receiver, not the external Worker's health."""

import json
import os
from urllib.request import ProxyHandler, build_opener


def check() -> int:
    try:
        port = int(os.environ.get("READER_PORT", "8090"))
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            body = json.loads(response.read(4096))
        return 0 if body.get("service") == "nomodo" and body.get("ready") is True else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(check())
