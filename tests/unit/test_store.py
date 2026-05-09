"""store.py のユニットテスト."""

from __future__ import annotations

import json
from pathlib import Path

from qa_radar.crawler.store import (
    ArticleRow,
    finish_crawl_run,
    get_source_fetch_state,
    insert_article,
    start_crawl_run,
    update_source_fetch_state,
    upsert_source,
)
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig


def _make_source(slug: str = "t") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name="Test",
        feed_url="https://example.com/feed",
        site_url="https://example.com",
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=3600, max_items_per_fetch=30),
        license_note="ok",
    )


def test_upsert_source_returns_same_id_for_same_slug(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid1 = upsert_source(conn, _make_source("a"))
        sid2 = upsert_source(conn, _make_source("a"))
        assert sid1 == sid2
        sid3 = upsert_source(conn, _make_source("b"))
        assert sid3 != sid1
    finally:
        conn.close()


def test_upsert_source_updates_changed_fields(tmp_path: Path) -> None:
    """同じ slug で再 upsert すると name 等が更新される."""
    conn = init_db(tmp_path / "test.db")
    try:
        s1 = _make_source("a")
        upsert_source(conn, s1)
        s2 = SourceConfig(
            slug="a",
            name="Updated Name",
            feed_url=s1.feed_url,
            site_url=s1.site_url,
            language=s1.language,
            category=s1.category,
            enabled=False,
            fetch_policy=s1.fetch_policy,
            license_note=s1.license_note,
        )
        upsert_source(conn, s2)
        row = conn.execute("SELECT * FROM sources WHERE slug='a'").fetchone()
        assert row["name"] == "Updated Name"
        assert row["enabled"] == 0
    finally:
        conn.close()


def test_insert_article_returns_true_then_false(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        a = ArticleRow(
            source_id=sid,
            guid="g1",
            url="https://e.com/1",
            title="t",
            snippet="s",
            body_hash="h",
            body="b",
            author=None,
            published_at=1700000000,
        )
        assert insert_article(conn, a) is True
        assert insert_article(conn, a) is False
    finally:
        conn.close()


def test_fetch_state_lifecycle(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        # 初期状態
        etag, lm, fa = get_source_fetch_state(conn, sid)
        assert etag is None and lm is None and fa is None

        # 成功更新
        update_source_fetch_state(conn, sid, etag="W/abc", last_modified="2024", success=True)
        etag, lm, fa = get_source_fetch_state(conn, sid)
        assert etag == "W/abc"
        assert lm == "2024"
        assert fa is not None

        # 失敗時は consecutive_errors が増える
        update_source_fetch_state(conn, sid, etag=None, last_modified=None, success=False)
        row = conn.execute("SELECT consecutive_errors FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["consecutive_errors"] == 1

        # 成功で 0 にリセット
        update_source_fetch_state(conn, sid, etag="x", last_modified="y", success=True)
        row = conn.execute("SELECT consecutive_errors FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["consecutive_errors"] == 0
    finally:
        conn.close()


def test_get_source_fetch_state_unknown_id(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        assert get_source_fetch_state(conn, 9999) == (None, None, None)
    finally:
        conn.close()


def test_crawl_run_lifecycle(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        run_id = start_crawl_run(conn)
        finish_crawl_run(
            conn,
            run_id,
            sources_processed=5,
            articles_added=10,
            errors=[{"slug": "x", "reason": "y"}],
        )
        row = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["sources_processed"] == 5
        assert row["articles_added"] == 10
        assert row["finished_at"] is not None
        errors = json.loads(row["errors_json"])
        assert errors[0]["slug"] == "x"
    finally:
        conn.close()
