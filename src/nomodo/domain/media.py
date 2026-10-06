"""Output semantics are determined by the actual MIME, with legacy type fallbacks."""


def media_kind(kind: str, mime: str | None) -> str:
    if mime:
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        return "file"
    if kind in {"photo", "live_photo"}:
        return "image"
    return "video" if kind == "video" else "file"
