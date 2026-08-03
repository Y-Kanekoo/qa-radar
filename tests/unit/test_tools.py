"""tools.py のユニットテスト. impl 関数を直接呼ぶ (MCP プロトコル経由しない)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig
from qa_radar.tools import (
    _escape_like_term,
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


def test_escape_like_term_treats_backslash_as_literal(tmp_path: Path) -> None:
    """バックスラッシュを ESCAPE 文字ではなくリテラルとして照合する."""
    conn = sqlite3.connect(tmp_path / "like.db")
    try:
        term = r"C:\path"
        pattern = f"%{_escape_like_term(term)}%"
        literal_match = conn.execute(
            "SELECT ? LIKE ? ESCAPE '\\'", (r"prefix C:\path suffix", pattern)
        ).fetchone()[0]
        different_match = conn.execute(
            "SELECT ? LIKE ? ESCAPE '\\'", (r"prefix C:Xpath suffix", pattern)
        ).fetchone()[0]

        assert literal_match == 1
        assert different_match == 0
    finally:
        conn.close()


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


@pytest.mark.parametrize("query", ["テスト", "自動化"])
def test_search_trigram_matches_japanese_body(query: str, tmp_path: Path) -> None:
    """3文字以上の日本語を和文 body の途中から検索できる."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(
            conn,
            _article(
                sid,
                "ja",
                title="日本語の記事",
                body="継続的なソフトウェアテストと自動化を紹介します",
            ),
        )

        result = search_articles_impl(conn, query)

        assert [item["url"] for item in result["items"]] == ["https://e.com/ja"]
    finally:
        conn.close()


def test_search_short_japanese_term_uses_like_and_orders_by_published_at(
    tmp_path: Path,
) -> None:
    """2文字語は LIKE で検索し、公開日時の新しい順に返す."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(
            conn,
            _article(sid, "old", body="品質を高める", published_at=1700000000),
        )
        insert_article(
            conn,
            _article(sid, "new", body="品質保証の実践", published_at=1700000100),
        )

        result = search_articles_impl(conn, "品質")

        assert [item["url"] for item in result["items"]] == [
            "https://e.com/new",
            "https://e.com/old",
        ]
    finally:
        conn.close()


def test_search_like_requires_every_term_across_searchable_columns(tmp_path: Path) -> None:
    """LIKE 経路でも3カラム間は OR、語間は AND として扱う."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(
            conn,
            _article(sid, "both", title="品質戦略", body="継続的な改善を進めます"),
        )
        insert_article(conn, _article(sid, "one", title="品質戦略", body="現状を解説します"))

        result = search_articles_impl(conn, "品質 改善")

        assert [item["url"] for item in result["items"]] == ["https://e.com/both"]
    finally:
        conn.close()


def test_search_hybrid_filters_with_long_and_short_terms_and_keeps_bm25_order(
    tmp_path: Path,
) -> None:
    """混在時は長語を FTS、短語を LIKE の AND 条件にし、BM25 順を維持する."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(
            conn,
            _article(sid, "title-ranked", title="AI テスト設計", body="実践を紹介します"),
        )
        insert_article(
            conn,
            _article(sid, "body-ranked", title="AI の記事", body="テスト設計を紹介します"),
        )
        insert_article(
            conn,
            _article(sid, "long-only", title="テスト設計", body="実践を紹介します"),
        )
        insert_article(
            conn,
            _article(sid, "short-only", title="AI の記事", body="実践を紹介します"),
        )

        result = search_articles_impl(conn, "AI テスト")

        assert [item["url"] for item in result["items"]] == [
            "https://e.com/title-ranked",
            "https://e.com/body-ranked",
        ]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("query", "literal_title", "wildcard_title"),
    [
        ("%", "カバレッジ 100%", "カバレッジ 1000"),
        ("_", "under_score", "underXscore"),
    ],
)
def test_search_like_escapes_wildcards(
    query: str,
    literal_title: str,
    wildcard_title: str,
    tmp_path: Path,
) -> None:
    """LIKE の % と _ を文字として扱い、ワイルドカードを暴発させない."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "literal", title=literal_title))
        insert_article(conn, _article(sid, "wildcard", title=wildcard_title))

        result = search_articles_impl(conn, query)

        assert [item["url"] for item in result["items"]] == ["https://e.com/literal"]
    finally:
        conn.close()


def test_search_route_boundary_is_observable_in_sql(tmp_path: Path) -> None:
    """長語ありなら FTS、全語が短語なら LIKE のみを使う."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(
            conn,
            _article(sid, "ja", body="ソフトウェアテストの自動化と品質改善"),
        )
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        fts_result = search_articles_impl(conn, "テスト 自動化")

        assert len(fts_result["items"]) == 1
        assert any("FROM articles_fts JOIN articles" in sql for sql in statements)

        statements.clear()
        hybrid_result = search_articles_impl(conn, "テスト 品質")

        assert len(hybrid_result["items"]) == 1
        assert any("FROM articles_fts JOIN articles" in sql for sql in statements)
        assert any("LIKE" in sql for sql in statements)

        statements.clear()
        like_result = search_articles_impl(conn, "品質 改善")

        assert len(like_result["items"]) == 1
        assert any("FROM articles a JOIN sources" in sql for sql in statements)
        assert not any("FROM articles_fts JOIN articles" in sql for sql in statements)
    finally:
        conn.set_trace_callback(None)
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


def test_search_like_filters_by_escaped_tag(tmp_path: Path) -> None:
    """LIKE 検索経路のタグ絞り込みで _ をワイルドカード化しない."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "literal", title="AI 記事", tags=["load_test"]))
        insert_article(conn, _article(sid, "wildcard", title="AI 記事", tags=["loadXtest"]))

        result = search_articles_impl(conn, "AI", tags=["load_test"])

        assert [item["url"] for item in result["items"]] == ["https://e.com/literal"]
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


def test_search_like_filters_by_date_range(tmp_path: Path) -> None:
    """LIKE 検索経路でも date_from/date_to を適用する."""
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "early", title="AI early", published_at=1705320000))
        insert_article(conn, _article(sid, "target", title="AI target", published_at=1707998400))
        insert_article(conn, _article(sid, "late", title="AI late", published_at=1710504000))

        result = search_articles_impl(
            conn,
            "AI",
            date_from="2024-02-01",
            date_to="2024-03-01",
        )

        assert [item["url"] for item in result["items"]] == ["https://e.com/target"]
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


def test_search_like_paginates_deterministically_and_includes_duplicate(
    tmp_path: Path,
) -> None:
    """LIKE 経路は同時刻を id で安定化し、転載重複を含めてページングする."""
    conn = _setup_db(tmp_path)
    try:
        sid1 = upsert_source(conn, _src("origin"))
        sid2 = upsert_source(conn, _src("repost"))
        published_at = 1700000000
        insert_article(
            conn,
            _article(sid1, "origin", title="AI origin", published_at=published_at),
        )
        origin_id = int(
            conn.execute("SELECT id FROM articles WHERE guid = 'origin'").fetchone()["id"]
        )
        insert_article(
            conn,
            _article(sid1, "regular", title="AI regular", published_at=published_at),
        )
        duplicate = _article(sid2, "duplicate", title="AI duplicate", published_at=published_at)
        duplicate.duplicate_of = origin_id
        insert_article(conn, duplicate)

        page1 = search_articles_impl(conn, "AI", limit=2)
        page2 = search_articles_impl(conn, "AI", limit=2, offset=2)

        assert [item["url"] for item in page1["items"]] == [
            "https://e.com/duplicate",
            "https://e.com/regular",
        ]
        assert page1["has_more"] is True
        assert page1["next_offset"] == 2
        assert [item["url"] for item in page2["items"]] == ["https://e.com/origin"]
        assert page2["has_more"] is False
        assert page2["next_offset"] is None
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


def test_search_accepts_fifty_short_terms(tmp_path: Path) -> None:
    """短語数上限ちょうどの50語は検索できる."""
    conn = _setup_db(tmp_path)
    try:
        result = search_articles_impl(conn, " ".join(["AI"] * 50))

        assert result == {"items": [], "has_more": False, "next_offset": None}
    finally:
        conn.close()


def test_search_accepts_more_than_fifty_long_terms(tmp_path: Path) -> None:
    """長語だけなら51語以上でも FTS5 の単一 MATCH パラメータで検索できる."""
    conn = _setup_db(tmp_path)
    try:
        result = search_articles_impl(conn, " ".join(["playwright"] * 51))

        assert result == {"items": [], "has_more": False, "next_offset": None}
    finally:
        conn.close()


def test_search_rejects_more_than_fifty_short_terms(tmp_path: Path) -> None:
    """短語51語以上は SQLite に渡す前に日本語の ValueError で拒否する."""
    conn = _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match=r"^短い語\(3文字未満\)が多すぎます\(上限50語\)$"):
            search_articles_impl(conn, " ".join(["AI"] * 51))
    finally:
        conn.close()


def test_search_empty_db(tmp_path: Path) -> None:
    conn = _setup_db(tmp_path)
    try:
        result = search_articles_impl(conn, "anything")
        assert result == {"items": [], "has_more": False, "next_offset": None}
    finally:
        conn.close()


def test_search_includes_cross_source_duplicates(tmp_path: Path) -> None:
    """MCP 全文検索は duplicate_of がある記事も検索対象に含める."""
    conn = _setup_db(tmp_path)
    try:
        sid1 = upsert_source(conn, _src("origin"))
        sid2 = upsert_source(conn, _src("repost"))
        insert_article(conn, _article(sid1, "origin", title="Shared Playwright news"))
        origin_id = int(conn.execute("SELECT id FROM articles").fetchone()["id"])
        duplicate = _article(sid2, "duplicate", title="Shared Playwright news")
        duplicate.duplicate_of = origin_id
        insert_article(conn, duplicate)

        result = search_articles_impl(conn, "playwright")

        assert len(result["items"]) == 2
        assert {item["url"] for item in result["items"]} == {
            "https://e.com/origin",
            "https://e.com/duplicate",
        }
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


def test_list_recent_filters_by_escaped_tag(tmp_path: Path) -> None:
    """新着一覧のタグ絞り込みで _ をワイルドカード化しない."""
    import time

    now = int(time.time())
    conn = _setup_db(tmp_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(
            conn,
            _article(sid, "literal", tags=["load_test"], published_at=now - 100),
        )
        insert_article(
            conn,
            _article(sid, "wildcard", tags=["loadXtest"], published_at=now - 100),
        )

        result = list_recent_impl(conn, days=1, tag="load_test")

        assert [item["url"] for item in result] == ["https://e.com/literal"]
    finally:
        conn.close()


def test_list_recent_excludes_cross_source_duplicates(tmp_path: Path) -> None:
    """一覧・集計は Pages / RSS と件数を揃えるため転載重複を除外する."""
    import time

    now = int(time.time())
    conn = _setup_db(tmp_path)
    try:
        sid1 = upsert_source(conn, _src("origin"))
        sid2 = upsert_source(conn, _src("repost"))
        insert_article(conn, _article(sid1, "origin", published_at=now - 100))
        origin_id = int(conn.execute("SELECT id FROM articles").fetchone()["id"])
        duplicate = _article(sid2, "repost", published_at=now - 50)
        duplicate.duplicate_of = origin_id
        insert_article(conn, duplicate)

        recent = list_recent_impl(conn, days=1)
        sources = {s["slug"]: s["article_count"] for s in list_sources_impl(conn)}
        tags = {t["tag"]: t["count"] for t in list_tags_impl(conn, min_count=1)}

        assert [item["url"] for item in recent] == ["https://e.com/origin"]
        assert sources == {"origin": 1, "repost": 0}
        assert tags == {"e2e": 1}
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
