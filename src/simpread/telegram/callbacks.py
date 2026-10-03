import re

from simpread.domain import Article

ACTIONS = {
    "save", "summary", "title", "tags", "translate", "normalize_markdown",
    "markdown", "html", "open", "more", "back", "export", "ai_file", "remove",
    "regen_summary", "regen_translate", "regen_tags",
    "regen_title", "regen_normalize_markdown",
}


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


def preview(article: Article) -> str:
    body = "\n\n".join(b.text for b in article.blocks if b.text)
    return (
        f"{truncate(article.title or '无标题', 250)}\n平台：{article.source.platform}\n"
        + f"来源：{article.source.original_url}\n\n{truncate(body, 1300)}"
    )
