"""Network-free HTML/Markdown parsing; keep inline annotations and block order."""

from typing import Any, cast
from urllib.parse import urljoin

from lxml import html
from markdown_it import MarkdownIt

from .models import Block, Inline
from .urls import safe_url


def parse_markdown(text: str, base_url: str = "") -> list[Block]:
    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    return parse_html(parser.render(text), base_url)


def parse_html(text: str, base_url: str = "") -> list[Block]:
    root = html.fragment_fromstring(text or "<p></p>", create_parent="div")
    for bad in root.xpath(".//script|.//style|.//iframe|.//object|.//form"):
        bad.drop_tree()

    def url(value: str | None) -> str | None:
        return safe_url(urljoin(base_url, value)) if value else None

    def inline(node: Any, marks: dict[str, Any] | None = None) -> list[Inline | Block]:
        marks = dict(marks or {})
        tag = str(node.tag).lower()
        for tags, mark in (({"strong", "b"}, "bold"), ({"em", "i"}, "italic"),
                           ({"code"}, "code"), ({"s", "del", "strike"}, "strike")):
            if tag in tags:
                marks[mark] = True
        if tag == "a":
            marks["url"] = url(node.get("href"))
        if tag == "br":
            return [Inline(text="\n", **marks)]
        if tag in {"img", "video", "audio"}:
            source = node.get("src") or next(iter(node.xpath("./source/@src")), None)
            address = url(source)
            if address:
                return [Block(type=cast(Any, "image" if tag == "img" else tag), url=address, alt=node.get("alt"))]
            return []
        out: list[Inline | Block] = [Inline(text=node.text, **marks)] if node.text else []
        for child in node:
            out.extend(inline(child, marks))
            if child.tail:
                out.append(Inline(text=child.tail, **marks))
        return out

    def paragraphs(parts: list[Inline | Block], kind: Any = "paragraph", level: int | None = None) -> list[Block]:
        out: list[Block] = []
        runs: list[Inline] = []

        def flush() -> None:
            if runs and any(r.text.strip() for r in runs):
                # A standalone link stays a legacy link block; mixed prose stays inline.
                link = runs[0].url if len(runs) == 1 else None
                out.append(Block(type="link" if kind == "paragraph" and link else kind,
                                 text="".join(r.text for r in runs), inlines=list(runs), url=link, level=level))
            runs.clear()

        for part in parts:
            if isinstance(part, Inline):
                runs.append(part)
            else:
                flush()
                out.append(part)
        flush()
        return out

    def container(node: Any) -> list[Block]:
        out: list[Block] = []
        pending: list[Inline | Block] = [Inline(text=node.text)] if node.text else []
        block_tags = {"p", "div", "section", "article", "main", "figure", "figcaption", "ul", "ol", "li",
                      "blockquote", "pre", "table", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
        for child in node:
            if child.tag in block_tags:
                out.extend(paragraphs(pending))
                pending = []
                out.extend(visit(child))
            else:
                pending.extend(inline(child))
            if child.tail:
                pending.append(Inline(text=child.tail))
        out.extend(paragraphs(pending))
        return out

    def visit(node: Any) -> list[Block]:
        tag = str(node.tag).lower()
        if tag in {"ul", "ol"}:
            items = [Block(type="list_item", children=container(child)) for child in node if child.tag == "li"]
            try:
                start = int(node.get("start", "1"))
            except ValueError:
                start = 1
            return [Block(type="ordered_list" if tag == "ol" else "unordered_list", children=items, start=start)]
        if tag == "blockquote":
            return [Block(type="quote", children=container(node))]
        if tag == "pre":
            code = node.find("code")
            language = code.get("class", "").removeprefix("language-") if code is not None else ""
            return [Block(type="code", text="".join(node.itertext()).rstrip("\n"), language=language or None)]
        if tag == "table":
            cells: list[list[list[Inline]]] = []
            extras: list[Block] = []
            header = False
            for row in node.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr"):
                current = []
                for cell in row:
                    if cell.tag not in {"td", "th"}:
                        continue
                    parts = inline(cell)
                    current.append([p for p in parts if isinstance(p, Inline)])
                    extras.extend(p for p in parts if isinstance(p, Block))
                if current:
                    if not cells:
                        header = bool(row.xpath("./th")) or row.getparent().tag == "thead"
                    cells.append(current)
            rows = [["".join(r.text for r in cell) for cell in row] for row in cells]
            return [Block(type="table", rows=rows, cells=cells, header=header), *extras]
        if tag == "hr":
            return [Block(type="divider")]
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return paragraphs(inline(node), "heading", int(tag[1]))
        if tag == "p":
            return paragraphs(inline(node))
        return container(node)

    return container(root)
