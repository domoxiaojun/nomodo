from nomodo.domain import normalize_worker_result
from nomodo.telegram.callbacks import make_callback, parse_callback


def test_article_normalization_and_rendering():
    article = normalize_worker_result(
        {
            "sourceUrl": "https://example.com/a",
            "canonicalUrl": "https://example.com/a",
            "platform": "example",
            "title": "Title",
            "content": "hello",
            "media": [],
        }
    )
    assert article.source.platform == "example"
    assert "来源" in article.markdown
    assert "<article>" in article.html


def test_callback_contains_only_opaque_id():
    value = make_callback("opaque", "summary")
    assert value == "article:opaque:summary"
    assert parse_callback(value) == ("opaque", "summary")
