"""Extract visible and hidden links from Telegram text/captions without fetching."""

from typing import Any

from simpread.worker import WorkerError, extract_urls


def message_urls(message: Any) -> list[str]:
    text = message.text or getattr(message, 'caption', None) or ''
    urls = extract_urls(text)
    for entity in (getattr(message, 'entities', None) or getattr(message, 'caption_entities', None) or []):
        if url := getattr(entity, 'url', None):
            urls.extend(extract_urls(url))
    output = list(dict.fromkeys(urls))
    if len(output) > 10:
        raise WorkerError('too_many_links')
    return output
