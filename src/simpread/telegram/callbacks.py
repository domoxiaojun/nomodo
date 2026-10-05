import re

from simpread.domain import Article
from simpread.domain.render import markdown_block

ACTIONS = {"save", "summary", "title", "tags", "translate", "normalize_markdown", "markdown", "html"}


def make_callback(article_id: str, action: str) -> str:
    if action not in ACTIONS or not re.fullmatch(r"[A-Za-z0-9_-]{1,22}", article_id):
        raise ValueError("invalid_callback")
    return f"article:{article_id}:{action}"


def parse_callback(value: str) -> tuple[str, str] | None:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "article":
        return None
    try:
        make_callback(parts[1], parts[2])
    except ValueError:
        return None
    return parts[1], parts[2]


def truncate(text: str, limit: int) -> str:
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    return text.encode("utf-16-le")[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def message_chunks(text: str, limit: int = 3800) -> list[str]:
    """Lossless chunks bounded in Telegram UTF-16 units, including astral emoji."""
    if limit < 2:
        raise ValueError("message_limit_too_small")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    break_at = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        while size + width > limit:
            cut = break_at or len(current)
            chunks.append("".join(current[:cut]))
            current = current[cut:]
            size = sum(2 if ord(c) > 0xFFFF else 1 for c in current)
            break_at = 0
        current.append(char)
        size += width
        if char == "\n":
            break_at = len(current)
    if current:
        chunks.append("".join(current))
    return chunks


def preview(article: Article) -> str:
    body = "\n\n".join(markdown_block(b) for b in article.blocks if b.text or b.items or b.rows or b.children)
    return (
        f"{truncate(article.title or '无标题', 250)}\n平台：{article.source.platform}\n"
        + f"来源：{article.source.original_url}\n\n{truncate(body, 1300)}"
    )
