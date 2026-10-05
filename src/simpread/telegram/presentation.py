"""Bounded native Telegram Rich Message rendering, without interpreting article text as markup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from lxml import html
from markdown_it import MarkdownIt
from pyrogram import types

from simpread.domain import Article
from simpread.domain.urls import safe_url

MAX_TEXT_BYTES = 27_000
MAX_BLOCKS = 350


def rich_text(value: Any) -> types.RichText:
    return cast(types.RichText, value)


def utf8_prefix(value: str, limit: int) -> str:
    return value.encode()[:max(0, limit)].decode('utf-8', errors='ignore')


@dataclass
class Budget:
    remaining: int = MAX_TEXT_BYTES
    blocks: int = 0
    shortened: bool = False

    def text(self, value: str) -> types.RichText:
        kept = utf8_prefix(value, self.remaining)
        self.remaining -= len(kept.encode())
        self.shortened |= kept != value
        return rich_text(kept)


def plain_block(block: Any) -> str:
    parts: list[str] = []
    if block.text:
        parts.append(block.text)
    elif block.inlines:
        parts.append("".join(item.text for item in block.inlines))
    parts.extend(block.items)
    parts.extend(plain_block(child) for child in block.children)
    return "\n".join(part for part in parts if part)


def article_rich(article: Article, media: list[Any], warnings: list[str],
                 lifetime_minutes: int) -> types.InputRichMessage:
    budget = Budget()
    blocks: list[Any] = [types.InputRichBlockSectionHeading(budget.text(article.title or '无标题'), size=1)]
    if article.description:
        blocks.append(types.InputRichBlockParagraph(budget.text(article.description)))
    for block in article.blocks:
        nested = len(block.children) or len(block.items)
        cost = 1 + (2 * nested if block.type in {"ordered_list", "unordered_list"} else len(block.rows))
        if budget.blocks + cost > MAX_BLOCKS or budget.remaining <= 0:
            budget.shortened = True
            break
        budget.blocks += cost
        if block.type == 'heading':
            blocks.append(types.InputRichBlockSectionHeading(budget.text(block.text), size=block.level or 2))
        elif block.type == 'code':
            blocks.append(types.InputRichBlockPreformatted(budget.text(block.text), language=block.language or ''))
        elif block.type == 'quote':
            blocks.append(types.InputRichBlockBlockQuotation([
                types.InputRichBlockParagraph(budget.text(plain_block(block) or block.text))
            ]))
        elif block.type in {'ordered_list', 'unordered_list'}:
            labels = [plain_block(child) for child in block.children] if block.children else list(block.items)
            labels = [label for label in labels if label.strip()]
            if not labels:
                continue
            blocks.append(types.InputRichBlockList([
                types.InputRichBlockListItem([types.InputRichBlockParagraph(budget.text(item))],
                                            value=i if block.type == 'ordered_list' else None)
                for i, item in enumerate(labels, block.start)
            ]))
        elif block.type == 'table' and block.rows:
            if max(map(len, block.rows), default=0) > 20:
                blocks.append(types.InputRichBlockPreformatted(
                    budget.text('\n'.join(' | '.join(row) for row in block.rows))))
            else:
                width = max(map(len, block.rows))
                blocks.append(types.InputRichBlockTable([
                    [types.RichBlockTableCell(text=budget.text(cell), is_header=i == 0)
                     for cell in row + [''] * (width - len(row))]
                    for i, row in enumerate(block.rows)
                ], is_bordered=True))
        elif block.type == 'divider':
            blocks.append(types.InputRichBlockDivider())
        elif block.type in {'link', 'image', 'video'}:
            represented = block.media_id and any(asset.media_id == block.media_id for asset in article.media)
            if not represented and (url := safe_url(block.url)):
                blocks.append(types.InputRichBlockParagraph(types.RichTextUrl(
                    budget.text(block.text or block.alt or ('查看媒体' if block.type != 'link' else '打开链接')), url)))
        elif block.text:
            blocks.append(types.InputRichBlockParagraph(budget.text(block.text)))
    # Group adjacent photos in a single slideshow inside the Rich Message.
    photos: list[Any] = []
    for item in [*media, None]:
        if isinstance(item, types.InputRichBlockPhoto):
            photos.append(item)
            continue
        if photos:
            blocks.append(types.InputRichBlockSlideshow(photos) if len(photos) > 1 else photos[0])
            photos = []
        if item is not None:
            blocks.append(item)
    notices = list(warnings)
    if budget.shortened:
        notices.append('正文超出单条展示范围；点击「导出」获取完整 Markdown 或 HTML。')
    if notices:
        blocks.append(types.InputRichBlockParagraph(rich_text('\n'.join(notices)[:1600])))
    footer: list[Any] = [article.source.platform + ' · ']
    if source := safe_url(article.source.original_url):
        footer.append(types.RichTextUrl(rich_text('查看原文'), source))
    footer.append(f' · 操作有效期约 {lifetime_minutes} 分钟')
    blocks.append(types.InputRichBlockFooter(rich_text(footer)))
    return types.InputRichMessage(blocks=blocks, skip_entity_detection=True)


def ai_rich(title: str, text: str) -> types.InputRichMessage:
    """Render model Markdown with HTML disabled and no automatic remote image fetches."""
    rendered = MarkdownIt('commonmark', {'html': False}).enable('table').render(utf8_prefix(text, 24_000))
    tree = html.fragment_fromstring(rendered or '<p>未生成内容。</p>', create_parent='div')
    for image in tree.xpath('.//img'):
        image.tag = 'span'
        image.text = image.get('alt') or '[图片]'
        image.attrib.clear()
    for link in tree.xpath('.//a'):
        if not safe_url(link.get('href')):
            link.drop_tag()
    from html import escape
    complex_layout = (len(list(tree.iter())) > 300
                      or any(len(list(node.iterancestors())) > 12 for node in tree.iter())
                      or any(len(row) > 20 for row in tree.xpath(".//tr")))
    if complex_layout:
        return types.InputRichMessage(blocks=[
            types.InputRichBlockSectionHeading(rich_text(title), size=2),
            types.InputRichBlockPreformatted(rich_text(utf8_prefix(text, 24_000))),
            types.InputRichBlockFooter(rich_text('内容结构较复杂，完整结果可通过按钮导出。')),
        ], skip_entity_detection=True)
    content = f'<h2>{escape(title)}</h2>' + ''.join(html.tostring(c, encoding='unicode') for c in tree)
    if len(text.encode()) > 24_000:
        content += '<p>内容较长，完整结果请点击「导出 AI 结果」。</p>'
    return types.InputRichMessage(html=content, skip_entity_detection=True)
