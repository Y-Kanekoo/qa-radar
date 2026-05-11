"""tools.py のユニットテスト. impl 関数を直接呼ぶ (MCP プロトコル経由しない)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig
from qa_radar.tools import (
    _fts5_safe_query,
    _iso_to_unix,
    _unix_to_iso,
    get_article_impl,
    list_recent_impl,
    list_sources_impl,
    list_tags_impl,
    search_articles_impl,
)


def _src(slug: str = "s1") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name=f"Source {slug}",
        feed_url=f"https://{slug}.example.com/feed",
        site_url=f"https://{slug}.example.com",
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
        license_note="",
    )


def _article(
    sid: int,
    guid: str,
    *,
    title: str = "Test article",
    body: str = "body text",
    tags: list[str] | None = None,
    published_at: int = 1700000000,
) -> ArticleRow:
    return ArticleRow(
        source_id=sid,
        guid=guid,
        url=f"https://e.com/{guid}",
        title=title,
        snippet=f"snip-{guid}",
        body_hash=guid,
        body=body,
        author="Alice",
        published_at=published_at,
        tags=tags or ["e2e"],
    )


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


# ---------------- helpers ----------------


class TestFts5SafeQuery:
    def test_single_term(self) -> None:
        assert _fts5_safe_query("playwright") == '"playwright"'

    def test_multiple_terms(self) -> None:
        assert _fts5_safe_query("playwright cypress") == '"playwright" "cypress"'

    def test_empty(self) -> None:
        assert _fts5_safe_query("") == '""'

    def test_escapes_double_quote(self) -> None:
        assert _fts5_safe_query('foo "bar"') == '"foo" """bar"""'

    def test_strips_extra_whitespace(self) -> None:
        assert _fts5_safe_query("  foo   bar  ") == '"foo" "bar"'


def test_iso_to_unix_roundtrip() -> None:
    unix = 1700000000
    iso = _unix_to_iso(unix)
    assert _iso_to_unix(iso) == unix


def test_iso_to_unix_handles_z_suffix() -> None:
    assert _iso_to_unix("2024-01-15T12:00:00Z") == 1705320000


# ---------------- search_articles ----------------


def test_search_returns_matching_articles(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", title="Playwright tutorial"))
        insert_article(conn, _article(sid, "g2", title="Jest guide"))
        result = search_articles_impl(conn, "playwright")
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "Playwright tutorial"
        assert result["items"][0]["url"] == "https://e.com/g1"
        assert "body" not in result["items"][0]  # 47条の5境界
    finally:
        conn.close()


def test_search_filters_by_tag(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", title="A test", tags=["e2e"]))
        insert_article(conn, _article(sid, "g2", title="A test", tags=["unit"]))
        result = search_articles_impl(conn, "test", tags=["e2e"])
        assert len(result["items"]) == 1
        assert result["items"][0]["tags"] == ["e2e"]
    finally:
        conn.close()


def test_search_filters_by_date_range(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        # 2024-01-15
        insert_article(conn, _article(sid, "g1", title="early test", published_at=1705320000))
        # 2024-02-15
        insert_article(conn, _article(sid, "g2", title="late test", published_at=1707998400))
        result = search_articles_impl(
            conn,
            "test",
            date_from="2024-02-01",
            date_to="2024-03-01",
        )
        assert len(result["items"]) == 1
        assert result["items"][0]["url"] == "https://e.com/g2"
    finally:
        conn.close()


def test_search_has_more_pagination(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        for i in range(5):
            insert_article(conn, _article(sid, f"g{i}", title=f"test {i}"))
        result = search_articles_impl(conn, "test", limit=3)
        assert len(result["items"]) == 3
        assert result["has_more"] is True
        assert result["next_offset"] == 3
    finally:
        conn.close()


def test_search_no_more_when_fewer_than_limit(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        for i in range(2):
            insert_article(conn, _article(sid, f"g{i}", title=f"test {i}"))
        result = search_articles_impl(conn, "test", limit=10)
        assert result["has_more"] is False
        assert result["next_offset"] is None
    finally:
        conn.close()


def test_search_offset_works(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        for i in range(5):
            insert_article(conn, _article(sid, f"g{i}", title=f"test {i}"))
        page1 = search_articles_impl(conn, "test", limit=2, offset=0)
        page2 = search_articles_impl(conn, "test", limit=2, offset=2)
        ids1 = {item["url"] for item in page1["items"]}
        ids2 = {item["url"] for item in page2["items"]}
        assert ids1 & ids2 == set()  # ページ間で重複なし
    finally:
        conn.close()


def test_search_rejects_invalid_limit(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="limit"):
            search_articles_impl(conn, "test", limit=0)
        with pytest.raises(ValueError, match="limit"):
            search_articles_impl(conn, "test", limit=101)
    finally:
        conn.close()


def test_search_rejects_negative_offset(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="offset"):
            search_articles_impl(conn, "test", offset=-1)
    finally:
        conn.close()


def test_search_empty_db(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        result = search_articles_impl(conn, "anything")
        assert result == {"items": [], "has_more": False, "next_offset": None}
    finally:
        conn.close()


# ---------------- list_recent ----------------


def test_list_recent_returns_recent_only(tmp_path: Path) -> None:
    import time

    now = int(time.time())
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "fresh", published_at=now - 3600))
        insert_article(conn, _article(sid, "old", published_at=now - 86400 * 10))
        result = list_recent_impl(conn, days=1)
        assert len(result) == 1
        assert result[0]["url"] == "https://e.com/fresh"
    finally:
        conn.close()


def test_list_recent_filters_by_source(tmp_path: Path) -> None:
    import time

    now = int(time.time())
    conn = _setup_db(tmp_path)
    try:
        sid1 = upsert_source(conn, _src("s1"))
        sid2 = upsert_source(conn, _src("s2"))
        insert_article(conn, _article(sid1, "a", published_at=now - 100))
        insert_article(conn, _article(sid2, "b", published_at=now - 100))
        result = list_recent_impl(conn, days=1, source="s1")
        assert len(result) == 1
        assert result[0]["url"] == "https://e.com/a"
    finally:
        conn.close()


def test_list_recent_filters_by_tag(tmp_path: Path) -> None:
    import time

    now = int(time.time())
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "e", tags=["e2e"], published_at=now - 100))
        insert_article(conn, _article(sid, "u", tags=["unit"], published_at=now - 100))
        result = list_recent_impl(conn, days=1, tag="e2e")
        assert len(result) == 1
        assert result[0]["tags"] == ["e2e"]
    finally:
        conn.close()


def test_list_recent_rejects_invalid_days(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="days"):
            list_recent_impl(conn, days=0)
        with pytest.raises(ValueError, match="days"):
            list_recent_impl(conn, days=1000)
    finally:
        conn.close()


def test_list_recent_rejects_invalid_limit(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="limit"):
            list_recent_impl(conn, limit=0)
    finally:
        conn.close()


# ---------------- get_article ----------------


def test_get_article_returns_metadata_only_by_default(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", title="Test", body="full body content"))
        article_id = conn.execute("SELECT id FROM articles").fetchone()["id"]
        result = get_article_impl(conn, article_id)
        assert "body" not in result
        assert result["title"] == "Test"
    finally:
        conn.close()


def test_get_article_with_include_body(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", body="full body content"))
        article_id = conn.execute("SELECT id FROM articles").fetchone()["id"]
        result = get_article_impl(conn, article_id, include_body=True)
        assert result["body"] == "full body content"
    finally:
        conn.close()


def test_get_article_unknown_id_raises(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="9999"):
            get_article_impl(conn, 9999)
    finally:
        conn.close()


# ---------------- list_sources ----------------


def test_list_sources_includes_counts(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid1 = upsert_source(conn, _src("s1"))
        upsert_source(conn, _src("s2"))
        insert_article(conn, _article(sid1, "a"))
        insert_article(conn, _article(sid1, "b"))
        result = list_sources_impl(conn)
        # 記事数 DESC で並ぶ
        assert result[0]["slug"] == "s1"
        assert result[0]["article_count"] == 2
        assert result[0]["latest_at"] is not None
        assert result[1]["slug"] == "s2"
        assert result[1]["article_count"] == 0
        assert result[1]["latest_at"] is None
    finally:
        conn.close()


# ---------------- list_tags ----------------


def test_list_tags_aggregates(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=["e2e", "tooling"]))
        insert_article(conn, _article(sid, "g2", tags=["e2e"]))
        insert_article(conn, _article(sid, "g3", tags=["unit"]))
        result = list_tags_impl(conn, min_count=1)
        tags = {t["tag"]: t["count"] for t in result}
        assert tags == {"e2e": 2, "tooling": 1, "unit": 1}
        # e2e が最頻なので先頭
        assert result[0]["tag"] == "e2e"
    finally:
        conn.close()


def test_list_tags_respects_min_count(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=["common", "rare"]))
        insert_article(conn, _article(sid, "g2", tags=["common"]))
        result = list_tags_impl(conn, min_count=2)
        assert {t["tag"] for t in result} == {"common"}
    finally:
        conn.close()


def test_list_tags_rejects_invalid_args(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="min_count"):
            list_tags_impl(conn, min_count=0)
        with pytest.raises(ValueError, match="limit"):
            list_tags_impl(conn, limit=0)
        with pytest.raises(ValueError, match="limit"):
            list_tags_impl(conn, limit=1000)
    finally:
        conn.close()
