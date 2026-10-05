import re
from html import escape

from .models import Article, Block, Inline
from .urls import safe_url

MEDIA_TYPES = {"image", "video", "animation", "audio", "file", "link"}


def literal(text: str) -> str:
    for char in "\\`*_{}[]<>#!|":
        text = text.replace(char, "\\" + char)
    return text.replace("\n", "  \n")


def markdown_runs(runs: list[Inline], *, table: bool = False) -> str:
    out = []
    for run in runs:
        text = literal(run.text)
        if run.code:
            fence = "`" * (max(map(len, re.findall(r"`+", run.text)), default=0) + 1)
            code = run.text.replace("\n", " ")
            if table:
                code = code.replace("|", "\\|")
            text = fence + " " + code + " " + fence
        for enabled, mark in ((run.bold, "**"), (run.italic, "*"), (run.strike, "~~")):
            if enabled and text.strip():
                lead = text[:len(text) - len(text.lstrip())]
                tail = text[len(text.rstrip()):]
                text = lead + mark + text.strip() + mark + tail
        if url := safe_url(run.url):
            text = f"[{text}](<{url}>)"
        out.append(text)
    return "".join(out)


def html_runs(runs: list[Inline]) -> str:
    out = []
    for run in runs:
        text = escape(run.text).replace("\n", "<br/>")
        for enabled, tag in ((run.code, "code"), (run.bold, "strong"), (run.italic, "em"), (run.strike, "del")):
            if enabled:
                text = f"<{tag}>{text}</{tag}>"
        if url := safe_url(run.url):
            text = f'<a href="{escape(url, quote=True)}">{text}</a>'
        out.append(text)
    return "".join(out)


def markdown_block(block: Block) -> str:
    text = markdown_runs(block.inlines) if block.inlines else literal(block.text)
    if block.type == "heading":
        return "#" * (block.level or 1) + " " + text
    if block.type == "quote":
        body = "\n\n".join(map(markdown_block, block.children)) if block.children else text
        return "> " + body.replace("\n", "\n> ")
    if block.type in {"unordered_list", "ordered_list"}:
        values = [markdown_block(c) for c in block.children] if block.children else [literal(t) for t in block.items]
        lines = []
        for i, value in enumerate(values, block.start):
            prefix = f"{i}. " if block.type == "ordered_list" else "- "
            lines.append(prefix + value.replace("\n", "\n" + " " * len(prefix)))
        return "\n".join(lines)
    if block.type == "list_item":
        return "\n\n".join(map(markdown_block, block.children)) or text
    if block.type == "code":
        fence = "`" * max(3, max(map(len, re.findall(r"`+", block.text)), default=0) + 1)
        language = re.sub(r"[^a-zA-Z0-9+#-]", "", block.language or "")
        return f"{fence}{language}\n{block.text}\n{fence}"
    if block.type == "table" and block.rows:
        table_values = [[markdown_runs(c, table=True) for c in r] for r in block.cells] if block.cells else [
            [literal(c) for c in row] for row in block.rows]
        width = max(map(len, table_values))
        rows = ["| " + " | ".join(c.replace("\n", " ") for c in row + [""] * (width-len(row))) + " |"
                for row in table_values]
        if not block.header:
            rows.insert(0, "| " + " | ".join([""] * width) + " |")
        rows.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n".join(rows)
    if block.type == "divider":
        return "---"
    if block.type in MEDIA_TYPES:
        if block.type == "link" and block.inlines:
            return text
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


def html_block(b: Block) -> str:
    text = html_runs(b.inlines) if b.inlines else escape(b.text).replace("\n", "<br/>")
    if b.type == "heading":
        tag = f"h{b.level or 1}"
        return f"<{tag}>{text}</{tag}>"
    if b.type == "code":
        return f"<pre><code>{escape(b.text)}</code></pre>"
    if b.type in {"unordered_list", "ordered_list"}:
        tag = "ul" if b.type == "unordered_list" else "ol"
        start = f' start="{b.start}"' if tag == "ol" else ""
        items = ("".join(map(html_block, b.children)) if b.children
                 else "".join(f"<li>{escape(t)}</li>" for t in b.items))
        return f"<{tag}{start}>{items}</{tag}>"
    if b.type in {"list_item", "quote"}:
        tag = "li" if b.type == "list_item" else "blockquote"
        return f"<{tag}>" + ("".join(map(html_block, b.children)) if b.children else text) + f"</{tag}>"
    if b.type == "table":
        values = ([[html_runs(c) for c in row] for row in b.cells] if b.cells
                  else [[escape(c) for c in r] for r in b.rows])
        rows = []
        for index, row in enumerate(values):
            tag = "th" if index == 0 and b.header else "td"
            rows.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in row) + "</tr>")
        return "<table>" + "".join(rows) + "</table>"
    if b.type == "divider":
        return "<hr/>"
    if b.type in MEDIA_TYPES:
        if b.type == "link" and b.inlines:
            return f"<p>{text}</p>"
        url, label = safe_url(b.url), escape(b.alt or b.text or b.type)
        if url:
            escaped = escape(url, quote=True)
            if b.type == "image":
                return f'<img src="{escaped}" alt="{label}"/>'
            if b.type in {"video", "audio"}:
                return f'<{b.type} controls src="{escaped}"></{b.type}>'
            return f'<p><a href="{escaped}">{label}</a></p>'
        return f"<p>{label}：附件</p>"
    return f"<p>{text}</p>"


def article_to_html(article: Article) -> str:
    parts = [f"<h1>{escape(article.title)}</h1>"] if article.title else []
    if article.description:
        parts.append(f"<blockquote>{escape(article.description)}</blockquote>")
    parts.extend(map(html_block, article.blocks))
    if source := safe_url(article.source.original_url):
        parts.append(f'<p>来源：<a href="{escape(source, quote=True)}">{escape(article.source.platform)}</a></p>')
    return ('<!doctype html><html><head><meta charset="utf-8"/></head><body><article>'
            + "\n".join(parts) + "</article></body></html>")
