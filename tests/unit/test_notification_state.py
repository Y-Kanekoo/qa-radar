"""publisher/notification_state.py のユニットテスト."""

from __future__ import annotations

from pathlib import Path

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.publisher.notification_state import (
    DISCORD_CHANNEL,
    fetch_unnotified,
    mark_notified,
    mark_notified_bulk,
)
from qa_radar.sources import FetchPolicy, SourceConfig


def _src(slug: str = "s1") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name=f"Source {slug}",
        feed_url=f"https://{slug}.example.com/feed",
        site_url=None,
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
        license_note="",
    )


def _article(sid: int, guid: str, *, published_at: int = 1700000000) -> ArticleRow:
    return ArticleRow(
        source_id=sid,
        guid=guid,
        url=f"https://e.com/{guid}",
        title=f"title-{guid}",
        snippet=f"snip-{guid}",
        body_hash=guid,
        body="full body",
        author="Alice",
        published_at=published_at,
        tags=["e2e"],
    )


def _get_article_id(conn, guid: str) -> int:
    return int(conn.execute("SELECT id FROM articles WHERE guid = ?", (guid,)).fetchone()["id"])


# ---------------- fetch_unnotified ----------------


def test_fetch_unnotified_returns_all_when_no_notifications(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        insert_article(conn, _article(sid, "g2"))
        unnot = fetch_unnotified(conn)
        assert len(unnot) == 2
        assert {u.item.url for u in unnot} == {"https://e.com/g1", "https://e.com/g2"}
    finally:
        conn.close()


def test_fetch_unnotified_excludes_already_notified(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        insert_article(conn, _article(sid, "g2"))
        aid1 = _get_article_id(conn, "g1")
        mark_notified(conn, aid1)
        unnot = fetch_unnotified(conn)
        assert len(unnot) == 1
        assert unnot[0].item.url == "https://e.com/g2"
    finally:
        conn.close()


def test_fetch_unnotified_per_channel(tmp_path: Path) -> None:
    """別チャネルでマークされていても discord は未通知扱い."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        aid = _get_article_id(conn, "g1")
        mark_notified(conn, aid, channel="slack")
        unnot = fetch_unnotified(conn, channel=DISCORD_CHANNEL)
        assert len(unnot) == 1
    finally:
        conn.close()


def test_fetch_unnotified_descending_by_published_at(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "old", published_at=1700000000))
        insert_article(conn, _article(sid, "new", published_at=1800000000))
        unnot = fetch_unnotified(conn)
        assert unnot[0].item.url == "https://e.com/new"
        assert unnot[1].item.url == "https://e.com/old"
    finally:
        conn.close()


def test_fetch_unnotified_respects_limit(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        for i in range(5):
            insert_article(conn, _article(sid, f"g{i}", published_at=1700000000 + i))
        unnot = fetch_unnotified(conn, limit=2)
        assert len(unnot) == 2
    finally:
        conn.close()


def test_fetch_unnotified_carries_article_id(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        unnot = fetch_unnotified(conn)
        aid = _get_article_id(conn, "g1")
        assert unnot[0].article_id == aid
    finally:
        conn.close()


def test_fetch_unnotified_does_not_load_body(tmp_path: Path) -> None:
    """FeedItem には body フィールドが無い (47条の5境界)."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        unnot = fetch_unnotified(conn)
        assert not hasattr(unnot[0].item, "body")
    finally:
        conn.close()


# ---------------- mark_notified ----------------


def test_mark_notified_idempotent(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1"))
        aid = _get_article_id(conn, "g1")
        mark_notified(conn, aid)
        mark_notified(conn, aid)  # 再呼出しは無害
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM article_notifications WHERE article_id = ?",
            (aid,),
        ).fetchone()
        assert rows["c"] == 1
    finally:
        conn.close()


def test_mark_notified_bulk(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        for i in range(3):
            insert_article(conn, _article(sid, f"g{i}"))
        aids = [_get_article_id(conn, f"g{i}") for i in range(3)]
        inserted = mark_notified_bulk(conn, aids)
        assert inserted == 3
        # 再実行は0件挿入
        again = mark_notified_bulk(conn, aids)
        assert again == 0
    finally:
        conn.close()


def test_mark_notified_bulk_empty(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        assert mark_notified_bulk(conn, []) == 0
    finally:
        conn.close()


# ---------------- DBスキーマ v2 マイグレーション ----------------


def test_v1_to_v2_migration_adds_table(tmp_path: Path) -> None:
    """v1 DB を v2 のコードで開いた時、article_notifications テーブルが追加される."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    # v2 で初期化されているのでこれだけで通る
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='article_notifications'"
    ).fetchall()
    assert len(rows) == 1
    # version も2に更新されている
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == 2
    conn.close()
