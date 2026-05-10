"""publisher/rss.py のユニットテスト."""

from __future__ import annotations

from pathlib import Path

import feedparser

from qa_radar.publisher.rss import (
    FeedItem,
    main_feed_url,
    tag_feed_url,
    write_feed,
)


def _item(
    *,
    url: str = "https://example.com/article-1",
    title: str = "Test article",
    snippet: str = "サマリー",
    source_name: str = "Example Blog",
    author: str | None = "Alice",
    published_at: int = 1700000000,
    tags: tuple[str, ...] = ("e2e", "tooling"),
) -> FeedItem:
    return FeedItem(
        id=url,
        url=url,
        title=title,
        snippet=snippet,
        source_name=source_name,
        author=author,
        published_at=published_at,
        tags=tags,
    )


def test_write_atom_feed_parses_back(tmp_path: Path) -> None:
    """生成した Atom が feedparser で bozo=False でパースできる."""
    out = tmp_path / "feed.atom"
    write_feed(
        [_item(url="https://example.com/1"), _item(url="https://example.com/2", title="記事2")],
        out,
        feed_url="https://example.com/feed.atom",
    )
    parsed = feedparser.parse(out.read_bytes())
    assert not parsed.bozo
    assert len(parsed.entries) == 2
    titles = {e.title for e in parsed.entries}
    assert titles == {"Test article", "記事2"}


def test_write_rss_feed_parses_back(tmp_path: Path) -> None:
    """生成した RSS 2.0 が feedparser で bozo=False でパースできる."""
    out = tmp_path / "feed.xml"
    write_feed(
        [_item(url="https://example.com/1")],
        out,
        feed_url="https://example.com/feed.xml",
        fmt="rss",
    )
    parsed = feedparser.parse(out.read_bytes())
    assert not parsed.bozo
    assert len(parsed.entries) == 1


def test_feed_summary_contains_snippet_and_source(tmp_path: Path) -> None:
    """summary に snippet + 出所明示が含まれる (47条の5境界)."""
    out = tmp_path / "feed.atom"
    write_feed(
        [_item(snippet="本文の要約100字以内", source_name="m3 Tech Blog")],
        out,
        feed_url="https://example.com/feed.atom",
    )
    parsed = feedparser.parse(out.read_bytes())
    summary = parsed.entries[0].summary
    assert "本文の要約100字以内" in summary
    assert "m3 Tech Blog" in summary


def test_feed_does_not_include_body(tmp_path: Path) -> None:
    """FeedItem には body フィールドが無い (公開境界)."""
    item = _item()
    assert not hasattr(item, "body")


def test_feed_includes_categories(tmp_path: Path) -> None:
    """タグが category として出力される."""
    out = tmp_path / "feed.atom"
    write_feed(
        [_item(tags=("e2e", "tooling", "process"))],
        out,
        feed_url="https://example.com/feed.atom",
    )
    parsed = feedparser.parse(out.read_bytes())
    cats = {tag.term for tag in parsed.entries[0].tags}
    assert cats == {"e2e", "tooling", "process"}


def test_empty_feed(tmp_path: Path) -> None:
    """0 件のフィードでもエラーにならない."""
    out = tmp_path / "feed.atom"
    write_feed([], out, feed_url="https://example.com/feed.atom")
    parsed = feedparser.parse(out.read_bytes())
    assert not parsed.bozo
    assert parsed.entries == []


def test_write_feed_creates_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "tags" / "subdir" / "e2e.xml"
    write_feed([_item()], out, feed_url="https://example.com/tags/e2e.xml")
    assert out.exists()


def test_main_feed_url() -> None:
    """メインフィード URL は site_base_url + 拡張子."""
    assert main_feed_url("atom").endswith("/feed.atom")
    assert main_feed_url("rss").endswith("/feed.xml")


def test_tag_feed_url() -> None:
    assert tag_feed_url("e2e").endswith("/tags/e2e.xml")


def test_feed_anonymous_author_uses_source_name(tmp_path: Path) -> None:
    """author=None の場合、source_name が author として出力される."""
    out = tmp_path / "feed.atom"
    write_feed(
        [_item(author=None, source_name="Anonymous Blog")],
        out,
        feed_url="https://example.com/feed.atom",
    )
    parsed = feedparser.parse(out.read_bytes())
    assert parsed.entries[0].author == "Anonymous Blog"
