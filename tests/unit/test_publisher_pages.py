"""publisher/pages.py のユニットテスト."""

from __future__ import annotations

from pathlib import Path

from qa_radar.publisher.pages import (
    FeedItem,
    SourceSummary,
    TagSummary,
    render_index,
    render_sources_page,
    render_tags_page,
    write_html,
)


def _item(**kw: object) -> FeedItem:
    defaults: dict[str, object] = {
        "id": "https://example.com/1",
        "url": "https://example.com/1",
        "title": "Test",
        "snippet": "サマリ",
        "source_name": "Source",
        "author": "Alice",
        "published_at": 1700000000,
        "tags": ("e2e",),
    }
    defaults.update(kw)
    return FeedItem(**defaults)  # type: ignore[arg-type]


# ---------------- render_index ----------------


def test_index_contains_all_articles() -> None:
    items = [_item(url=f"https://e.com/{i}", title=f"記事{i}") for i in range(3)]
    html = render_index(items)
    assert "記事0" in html
    assert "記事1" in html
    assert "記事2" in html
    assert "qa-radar" in html


def test_index_escapes_html_in_title() -> None:
    """XSS 対策: タイトルに含まれる HTML は escape される."""
    item = _item(title="<script>alert(1)</script>")
    html = render_index([item])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_index_escapes_url() -> None:
    item = _item(url="https://example.com/?q=<script>")
    html = render_index([item])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_index_renders_tags_as_links() -> None:
    item = _item(tags=("e2e", "tooling"))
    html = render_index([item])
    assert 'href="tags/e2e.xml"' in html
    assert 'href="tags/tooling.xml"' in html
    assert "#e2e" in html


def test_index_includes_feed_links() -> None:
    """alternate link で Atom/RSS フィードを示している."""
    html = render_index([])
    assert 'rel="alternate"' in html
    assert 'href="./feed.atom"' in html
    assert 'href="./feed.xml"' in html


def test_index_legal_disclaimer_present() -> None:
    """47条の5/著作権表記がフッターに含まれている."""
    html = render_index([])
    assert "47条の5" in html
    assert "削除依頼" in html


# ---------------- render_sources_page ----------------


def test_sources_page_renders_table() -> None:
    sources = [
        SourceSummary(
            slug="s1",
            name="Source 1",
            site_url="https://s1.com",
            language="ja",
            category="blog",
            article_count=42,
            latest_published_at=1700000000,
        )
    ]
    html = render_sources_page(sources)
    assert "Source 1" in html
    assert "https://s1.com" in html
    assert "42" in html
    assert "blog" in html


def test_sources_page_handles_no_site_url() -> None:
    """site_url=None でも動作する."""
    sources = [
        SourceSummary(
            slug="s",
            name="Source",
            site_url=None,
            language="en",
            category="paper",
            article_count=1,
            latest_published_at=None,
        )
    ]
    html = render_sources_page(sources)
    assert "Source" in html


def test_sources_page_no_articles_shows_dash() -> None:
    sources = [
        SourceSummary(
            slug="s",
            name="Source",
            site_url=None,
            language="en",
            category="blog",
            article_count=0,
            latest_published_at=None,
        )
    ]
    html = render_sources_page(sources)
    assert "<td>-</td>" in html


# ---------------- render_tags_page ----------------


def test_tags_page_lists_tags() -> None:
    tags = [TagSummary(tag="e2e", article_count=42), TagSummary(tag="unit", article_count=10)]
    html = render_tags_page(tags)
    assert "#e2e" in html
    assert "#unit" in html
    assert "(42)" in html
    assert 'href="tags/e2e.xml"' in html


def test_tags_page_empty_renders() -> None:
    html = render_tags_page([])
    assert "qa-radar" in html


# ---------------- write_html ----------------


def test_write_html_creates_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "path" / "index.html"
    write_html("<html></html>", out)
    assert out.exists()
    assert out.read_text(encoding="utf-8") == "<html></html>"
