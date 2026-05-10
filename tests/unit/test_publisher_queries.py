"""publisher/queries.py のユニットテスト."""

from __future__ import annotations

import json
from pathlib import Path

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.publisher.queries import (
    fetch_recent_articles,
    fetch_source_summaries,
    fetch_tag_summaries,
)
from qa_radar.sources import FetchPolicy, SourceConfig


def _src(slug: str = "s1", category: str = "blog") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name=f"Source {slug}",
        feed_url=f"https://{slug}.example.com/feed",
        site_url=f"https://{slug}.example.com",
        language="en",
        category=category,
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
        license_note="",
    )


def _article(
    sid: int,
    guid: str,
    title: str = "T",
    *,
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
        body="full body",
        author="Alice",
        published_at=published_at,
        tags=tags or [],
    )


def test_fetch_recent_returns_descending_by_published_at(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", "old", published_at=1700000000))
        insert_article(conn, _article(sid, "g2", "new", published_at=1800000000))
        items = fetch_recent_articles(conn)
        assert [i.title for i in items] == ["new", "old"]
    finally:
        conn.close()


def test_fetch_recent_includes_source_name(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src("s1"))
        insert_article(conn, _article(sid, "g1"))
        items = fetch_recent_articles(conn)
        assert items[0].source_name == "Source s1"
    finally:
        conn.close()


def test_fetch_recent_does_not_load_body(tmp_path: Path) -> None:
    """FeedItem には body フィールドが無く、SQL も body を SELECT しない."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        items = fetch_recent_articles(conn)
        assert not hasattr(items[0], "body")
    finally:
        conn.close()


def test_fetch_recent_filters_by_tag(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=["e2e", "tooling"]))
        insert_article(conn, _article(sid, "g2", tags=["unit"]))
        e2e_items = fetch_recent_articles(conn, tag="e2e")
        assert len(e2e_items) == 1
        assert e2e_items[0].url == "https://e.com/g1"
    finally:
        conn.close()


def test_fetch_recent_respects_limit(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        for i in range(5):
            insert_article(conn, _article(sid, f"g{i}", published_at=1700000000 + i))
        items = fetch_recent_articles(conn, limit=3)
        assert len(items) == 3
    finally:
        conn.close()


def test_fetch_recent_parses_tags_json(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=["e2e", "tooling"]))
        items = fetch_recent_articles(conn)
        assert items[0].tags == ("e2e", "tooling")
    finally:
        conn.close()


# ---------------- source summaries ----------------


def test_fetch_source_summaries(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _src("s1"))
        sid2 = upsert_source(conn, _src("s2"))
        insert_article(conn, _article(sid1, "g1", published_at=1700000000))
        insert_article(conn, _article(sid1, "g2", published_at=1800000000))
        insert_article(conn, _article(sid2, "h1", published_at=1750000000))
        summaries = fetch_source_summaries(conn)
        # article_count DESC で並ぶ
        assert summaries[0].slug == "s1"
        assert summaries[0].article_count == 2
        assert summaries[0].latest_published_at == 1800000000
        assert summaries[1].slug == "s2"
        assert summaries[1].article_count == 1
    finally:
        conn.close()


def test_fetch_source_summaries_includes_zero_count_sources(tmp_path: Path) -> None:
    """記事0件のソースも含まれる (LEFT JOIN)."""
    conn = init_db(tmp_path / "test.db")
    try:
        upsert_source(conn, _src("empty"))
        summaries = fetch_source_summaries(conn)
        assert any(s.slug == "empty" and s.article_count == 0 for s in summaries)
    finally:
        conn.close()


# ---------------- tag summaries ----------------


def test_fetch_tag_summaries(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=["e2e", "tooling"]))
        insert_article(conn, _article(sid, "g2", tags=["e2e"]))
        insert_article(conn, _article(sid, "g3", tags=["unit"]))
        summaries = fetch_tag_summaries(conn)
        # e2e=2 が最頻
        assert summaries[0].tag == "e2e"
        assert summaries[0].article_count == 2
        tag_names = {t.tag for t in summaries}
        assert tag_names == {"e2e", "tooling", "unit"}
    finally:
        conn.close()


def test_fetch_tag_summaries_min_count(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=["e2e", "rare"]))
        insert_article(conn, _article(sid, "g2", tags=["e2e"]))
        summaries = fetch_tag_summaries(conn, min_count=2)
        # rare はカウント1 → min_count=2 で除外
        assert {t.tag for t in summaries} == {"e2e"}
    finally:
        conn.close()


def test_fetch_tag_summaries_handles_articles_with_no_tags(tmp_path: Path) -> None:
    """tags_json='[]' の記事は集計対象外."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", tags=[]))
        # body_hash UNIQUE は無いので tags=[] で挿入できる
        # SQL の json_each([]) は0行を返すため、結果は空
        summaries = fetch_tag_summaries(conn)
        assert summaries == []
        # tags_json が JSON 配列として有効であることを確認
        row = conn.execute("SELECT tags_json FROM articles").fetchone()
        assert json.loads(row["tags_json"]) == []
    finally:
        conn.close()
