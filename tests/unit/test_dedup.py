"""dedup.py のユニットテスト."""

from __future__ import annotations

from pathlib import Path

from qa_radar.crawler.dedup import is_cross_source_duplicate, is_known
from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig


def _make_source(slug: str = "test") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name="Test",
        feed_url="https://example.com/feed",
        site_url=None,
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=3600, max_items_per_fetch=30),
        license_note="",
    )


def _make_article(source_id: int, guid: str = "g1", body_hash: str = "h1") -> ArticleRow:
    return ArticleRow(
        source_id=source_id,
        guid=guid,
        url="https://example.com/a",
        title="t",
        snippet="s",
        body_hash=body_hash,
        body="b",
        author=None,
        published_at=1700000000,
    )


def test_is_known_false_for_new(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        assert is_known(conn, sid, "g1") is False
    finally:
        conn.close()


def test_is_known_true_after_insert(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        insert_article(conn, _make_article(sid, "g1"))
        assert is_known(conn, sid, "g1") is True
    finally:
        conn.close()


def test_double_insert_returns_false(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        assert insert_article(conn, _make_article(sid, "g1")) is True
        assert insert_article(conn, _make_article(sid, "g1")) is False
    finally:
        conn.close()


def test_cross_source_duplicate_detection(tmp_path: Path) -> None:
    """同じ body_hash の記事が異なるソースに存在することを検出."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("s1"))
        sid2 = upsert_source(conn, _make_source("s2"))
        insert_article(conn, _make_article(sid1, "g1", body_hash="same_hash"))
        # s2 から見て、別ソース s1 に同じ body_hash があれば True
        assert is_cross_source_duplicate(conn, "same_hash", sid2) is True
        # s1 から見て、自分以外には存在しない → False
        assert is_cross_source_duplicate(conn, "same_hash", sid1) is False
    finally:
        conn.close()
