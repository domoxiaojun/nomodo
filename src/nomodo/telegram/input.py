"""Extract visible and text-link URLs without following links or fetching quoted content."""

from typing import Any

from nomodo.domain.urls import normalize_share_url
from nomodo.worker import WorkerError, extract_urls


def message_urls(message: Any, *, include_reply: bool = False) -> list[str]:
    text = message.text or getattr(message, "caption", None) or ""
    urls = extract_urls(text)
    entities = (getattr(message, "entities", None) or []) + (getattr(message, "caption_entities", None) or [])
    for entity in entities:
        url = getattr(entity, "url", None)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.append(normalize_share_url(url))
    urls = list(dict.fromkeys(urls))
    if not urls and include_reply and (replied := getattr(message, "reply_to_message", None)):
        return message_urls(replied)
    if len(urls) > 10:
        raise WorkerError("too_many_links")
    return urls
