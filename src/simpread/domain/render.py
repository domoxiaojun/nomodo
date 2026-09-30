import re
from html import escape

from .models import Article, Block
from .urls import safe_url


def literal(text: str) -> str:
    for char in "\\`*_{}[]<>#!|":
        text = text.replace(char, "\\" + char)
    return text


def markdown_block(block: Block) -> str:
    text = literal(block.text)
    if block.type == "heading":
        return "#" * (block.level or 1) + " " + text
    if block.type == "quote":
        return "> " + text.replace("\n", "\n> ")
    if block.type in {"unordered_list", "ordered_list"}:
        return "\n".join(
            f"{str(i) + '.' if block.type == 'ordered_list' else '-'} {literal(t)}"
            for i, t in enumerate(block.items, 1)
        )
    if block.type == "code":
        fence = "`" * max(3, max(map(len, re.findall(r"`+", block.text)), default=0) + 1)
        language = re.sub(r"[^a-zA-Z0-9+#-]", "", block.language or "")
        return f"{fence}{language}\n{block.text}\n{fence}"
    if block.type == "table" and block.rows:
        width = max(map(len, block.rows))
        rows = [
            "| " + " | ".join(literal(c).replace("\n", " ") for c in row + [""] * (width - len(row))) + " |"
            for row in block.rows
        ]
        rows.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n".join(rows)
    if block.type == "divider":
        return "---"
    if block.type in {"image", "video", "link"}:
        url, label = safe_url(block.url), literal(block.alt or block.text or block.type)
        return f"{'!' if block.type == 'image' else ''}[{label}](<{url}>)" if url else f"[{label}：附件]"
    return text


def article_to_markdown(article: Article) -> str:
    parts = [f"# {literal(article.title)}"] if article.title else []
    if article.description:
        parts.append("> " + literal(article.description))
    parts.extend(markdown_block(b) for b in article.blocks)
    if source := safe_url(article.source.original_url):
        parts.append(f"来源：[{literal(article.source.platform)}](<{source}>)")
    return "\n\n".join(parts) + "\n"


def article_to_html(article: Article) -> str:
    parts = [f"<h1>{escape(article.title)}</h1>"] if article.title else []
    if article.description:
        parts.append(f"<blockquote>{escape(article.description)}</blockquote>")
    for b in article.blocks:
        text = escape(b.text)
        if b.type == "heading":
            tag = f"h{b.level or 1}"
            parts.append(f"<{tag}>{text}</{tag}>")
        elif b.type == "code":
            parts.append(f"<pre><code>{text}</code></pre>")
        elif b.type in {"unordered_list", "ordered_list"}:
            tag = "ul" if b.type == "unordered_list" else "ol"
            parts.append(f"<{tag}>" + "".join(f"<li>{escape(t)}</li>" for t in b.items) + f"</{tag}>")
        elif b.type == "table":
            parts.append(
                "<table>"
                + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>" for row in b.rows)
                + "</table>"
            )
        elif b.type == "divider":
            parts.append("<hr/>")
        elif b.type in {"image", "video", "link"}:
            url = safe_url(b.url)
            label = escape(b.alt or b.text or b.type)
            if url and b.type == "image":
                parts.append(f'<img src="{escape(url, quote=True)}" alt="{label}"/>')
            elif url:
                parts.append(f'<p><a href="{escape(url, quote=True)}">{label}</a></p>')
            else:
                parts.append(f"<p>{label}：附件</p>")
        else:
            tag = "blockquote" if b.type == "quote" else "p"
            parts.append(f"<{tag}>{text}</{tag}>")
    if source := safe_url(article.source.original_url):
        parts.append(f'<p>来源：<a href="{escape(source, quote=True)}">{escape(article.source.platform)}</a></p>')
    return (
        '<!doctype html><html><head><meta charset="utf-8"/></head><body><article>'
        + "\n".join(parts)
        + "</article></body></html>"
    )
