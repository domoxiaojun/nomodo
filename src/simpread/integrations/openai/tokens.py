"""Model-aware token estimates with a conservative fallback for unknown providers."""

import json
from typing import Any

import tiktoken


class Tokens:
    def __init__(self, model: str) -> None:
        self.encoding: tiktoken.Encoding | None
        try:
            self.encoding = tiktoken.encoding_for_model(model)
        except (KeyError, ValueError):
            self.encoding = None

    def count(self, text: str) -> int:
        if self.encoding is None:
            return len(text.encode("utf-8"))
        return len(self.encoding.encode(text, disallowed_special=()))

    def json(self, value: Any) -> int:
        return self.count(json.dumps(value, ensure_ascii=False))
