"""dedup.py のユニットテスト."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qa_radar.crawler.dedup import CrossSourceDecision, is_known, resolve_cross_source_original
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


def _article_id(conn: sqlite3.Connection, guid: str) -> int:
    return int(conn.execute("SELECT id FROM articles WHERE guid = ?", (guid,)).fetchone()["id"])


def test_resolve_returns_no_decision_without_other_sources(tmp_path: Path) -> None:
    """別ソースに同一本文が無ければ元記事として扱う (何もマークしない)."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("s1"))
        sid2 = upsert_source(conn, _make_source("s2"))
        insert_article(conn, _make_article(sid1, "g1", body_hash="same_hash"))

        # 同一ソース内は guid 重複で守られるため判定対象外
        assert resolve_cross_source_original(conn, "same_hash", sid1, 1700000000) == (
            CrossSourceDecision()
        )
        # 別 body_hash も対象外
        assert resolve_cross_source_original(conn, "other_hash", sid2, 1700000000) == (
            CrossSourceDecision()
        )
    finally:
        conn.close()


def test_resolve_marks_new_article_when_existing_original_is_older(tmp_path: Path) -> None:
    """既存の元記事の方が古ければ、挿入記事を重複としてマークする."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("s1"))
        sid2 = upsert_source(conn, _make_source("s2"))
        sid3 = upsert_source(conn, _make_source("s3"))
        newer = _make_article(sid1, "newer", body_hash="same_hash")
        newer.published_at = 1800000000
        older = _make_article(sid2, "older", body_hash="same_hash")
        older.published_at = 1700000000
        insert_article(conn, newer)
        insert_article(conn, older)

        decision = resolve_cross_source_original(conn, "same_hash", sid3, 1900000000)

        assert decision.original_id == _article_id(conn, "older")
        assert decision.regrouped_ids == ()
    finally:
        conn.close()


def test_resolve_prefers_existing_article_on_same_published_at(tmp_path: Path) -> None:
    """公開日時が同値なら先に保存された既存記事を元記事として優先する."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("s1"))
        sid2 = upsert_source(conn, _make_source("s2"))
        insert_article(conn, _make_article(sid1, "existing", body_hash="same_hash"))

        decision = resolve_cross_source_original(conn, "same_hash", sid2, 1700000000)

        assert decision.original_id == _article_id(conn, "existing")
        assert decision.regrouped_ids == ()
    finally:
        conn.close()


def test_resolve_regroups_existing_group_when_new_article_is_older(tmp_path: Path) -> None:
    """挿入記事が最古なら、既存グループ全体を付け替える指示を返す."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("s1"))
        sid2 = upsert_source(conn, _make_source("s2"))
        sid3 = upsert_source(conn, _make_source("s3"))
        repost = _make_article(sid1, "repost", body_hash="same_hash")
        repost.published_at = 1700009999
        insert_article(conn, repost)
        repost_id = _article_id(conn, "repost")
        second_repost = _make_article(sid2, "second-repost", body_hash="same_hash")
        second_repost.published_at = 1700019999
        second_repost.duplicate_of = repost_id
        insert_article(conn, second_repost)

        decision = resolve_cross_source_original(conn, "same_hash", sid3, 1700000000)

        assert decision.original_id is None
        assert set(decision.regrouped_ids) == {repost_id, _article_id(conn, "second-repost")}
    finally:
        conn.close()


def test_resolve_ignores_marked_duplicates(tmp_path: Path) -> None:
    """duplicate_of が設定済みの記事だけの場合は元記事候補にしない."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("s1"))
        sid2 = upsert_source(conn, _make_source("s2"))
        sid3 = upsert_source(conn, _make_source("s3"))
        insert_article(conn, _make_article(sid1, "origin", body_hash="same_hash"))
        origin_id = _article_id(conn, "origin")
        duplicate = _make_article(sid2, "duplicate", body_hash="same_hash")
        duplicate.duplicate_of = origin_id
        insert_article(conn, duplicate)

        # sid1 から見ると別ソース側は重複マーク済みの1件のみ → 何もしない
        assert resolve_cross_source_original(conn, "same_hash", sid1, 1700000000) == (
            CrossSourceDecision()
        )
        # sid3 から見ると元記事 origin が候補になる
        assert (
            resolve_cross_source_original(conn, "same_hash", sid3, 1700000000).original_id
            == origin_id
        )
    finally:
        conn.close()
