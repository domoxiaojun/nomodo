"""Network-free Markdown/HTML normalization into portable structured blocks."""

from typing import Any

from lxml import html
from markdown_it import MarkdownIt

from .models import Block
from .urls import safe_url


def parse_markdown(text: str) -> list[Block]:
    return parse_html(MarkdownIt("commonmark", {"html": False}).enable("table").render(text))


def parse_html(text: str) -> list[Block]:
    root = html.fragment_fromstring(text or "<p></p>", create_parent="div")
    for bad in root.xpath(".//script|.//style|.//iframe|.//object|.//form"):
        bad.drop_tree()
    out: list[Block] = []

    def visit(node: Any) -> None:
        tag = str(node.tag)
        content = "".join(node.itertext()).strip()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            out.append(Block(type="heading", level=int(tag[1]), text=content))
        elif tag == "pre":
            code = node.find("code")
            language = code.get("class", "").removeprefix("language-") if code is not None else ""
            out.append(Block(type="code", text="".join(node.itertext()).rstrip("\n"), language=language or None))
        elif tag in {"ul", "ol"}:
            out.append(
                Block(
                    type="unordered_list" if tag == "ul" else "ordered_list",
                    items=["".join(c.itertext()).strip() for c in node if c.tag == "li"],
                )
            )
        elif tag == "table":
            out.append(
                Block(
                    type="table",
                    rows=[["".join(c.itertext()).strip() for c in r if c.tag in {"td", "th"}] for r in node.iter("tr")],
                )
            )
        elif tag == "blockquote":
            out.append(Block(type="quote", text=content))
        elif tag == "hr":
            out.append(Block(type="divider"))
        elif tag in {"img", "video", "a"}:
            url = safe_url(node.get("href" if tag == "a" else "src"))
            if url:
                out.append(
                    Block(
                        type="image" if tag == "img" else "link" if tag == "a" else "video",
                        text=content,
                        url=url,
                        alt=node.get("alt"),
                    )
                )
        elif tag == "p":
            if content:
                out.append(Block(type="paragraph", text=content))
            for child in node.iterdescendants():
                if child.tag in {"img", "a", "video"}:
                    visit(child)
        else:
            if node.text and node.text.strip():
                out.append(Block(type="paragraph", text=node.text.strip()))
            for child in node:
                visit(child)
                if child.tail and child.tail.strip():
                    out.append(Block(type="paragraph", text=child.tail.strip()))

    visit(root)
    return out
