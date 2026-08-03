"""store.py のユニットテスト."""

from __future__ import annotations

import json
from pathlib import Path

from qa_radar.crawler.store import (
    ArticleRow,
    finish_crawl_run,
    get_overall_stats,
    get_repeatedly_failing_sources,
    get_source_fetch_state,
    get_source_staleness,
    get_sources_with_errors,
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


def test_get_repeatedly_failing_sources_filters_by_threshold(tmp_path: Path) -> None:
    """既定閾値(9)未満は含まれず、以上は含まれる."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid_a = upsert_source(conn, _make_source("a"))
        sid_b = upsert_source(conn, _make_source("b"))
        sid_c = upsert_source(conn, _make_source("c"))

        for _ in range(8):  # 閾値未満
            update_source_fetch_state(conn, sid_a, etag=None, last_modified=None, success=False)
        for _ in range(9):  # 閾値ちょうど
            update_source_fetch_state(conn, sid_b, etag=None, last_modified=None, success=False)
        for _ in range(10):  # 閾値超過
            update_source_fetch_state(conn, sid_c, etag=None, last_modified=None, success=False)

        result = dict(get_repeatedly_failing_sources(conn))
        assert "a" not in result
        assert result["b"] == 9
        assert result["c"] == 10
    finally:
        conn.close()


def test_get_repeatedly_failing_sources_empty_when_none_failing(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        upsert_source(conn, _make_source("a"))
        assert get_repeatedly_failing_sources(conn) == []
    finally:
        conn.close()


def test_get_repeatedly_failing_sources_custom_threshold(tmp_path: Path) -> None:
    """threshold引数で閾値を変更できる."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source("a"))
        for _ in range(3):
            update_source_fetch_state(conn, sid, etag=None, last_modified=None, success=False)
        assert get_repeatedly_failing_sources(conn, threshold=3) == [("a", 3)]
        assert get_repeatedly_failing_sources(conn, threshold=4) == []
    finally:
        conn.close()


def test_get_repeatedly_failing_sources_success_resets_below_threshold(tmp_path: Path) -> None:
    """成功でconsecutive_errorsが0に戻ったソースは対象外になる."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source("a"))
        for _ in range(9):
            update_source_fetch_state(conn, sid, etag=None, last_modified=None, success=False)
        assert get_repeatedly_failing_sources(conn) == [("a", 9)]

        update_source_fetch_state(conn, sid, etag="x", last_modified="y", success=True)
        assert get_repeatedly_failing_sources(conn) == []
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


# ---------------- get_sources_with_errors (週次ヘルスレポート用) ----------------


def test_get_sources_with_errors_orders_by_count_desc(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid_a = upsert_source(conn, _make_source("a"))
        sid_b = upsert_source(conn, _make_source("b"))
        for _ in range(2):
            update_source_fetch_state(conn, sid_a, etag=None, last_modified=None, success=False)
        for _ in range(5):
            update_source_fetch_state(conn, sid_b, etag=None, last_modified=None, success=False)
        result = get_sources_with_errors(conn)
        assert [s.slug for s in result] == ["b", "a"]
        assert result[0].consecutive_errors == 5
        assert result[1].consecutive_errors == 2
    finally:
        conn.close()


def test_get_sources_with_errors_excludes_zero_and_disabled(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        upsert_source(conn, _make_source("healthy"))  # consecutive_errors=0
        disabled = SourceConfig(
            slug="disabled",
            name="Disabled",
            feed_url="https://example.com/feed2",
            site_url=None,
            language="en",
            category="blog",
            enabled=False,
            fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
            license_note="",
        )
        sid_disabled = upsert_source(conn, disabled)
        update_source_fetch_state(conn, sid_disabled, etag=None, last_modified=None, success=False)
        assert get_sources_with_errors(conn) == []
    finally:
        conn.close()


# ---------------- get_source_staleness (週次ヘルスレポート用) ----------------


def test_get_source_staleness_none_when_no_articles(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        upsert_source(conn, _make_source("empty"))
        result = get_source_staleness(conn)
        assert len(result) == 1
        assert result[0].slug == "empty"
        assert result[0].latest_activity_at is None
    finally:
        conn.close()


def test_get_source_staleness_picks_max_of_published_and_fetched(tmp_path: Path) -> None:
    """published_at と fetched_at のうち、記事間で最も新しい方が採用される."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source("s"))
        # published_at が新しい記事 (fetched_at は insert_article 内部で現在時刻になるため
        # 直接 UPDATE して past の値に揃え、published_at 側が勝つケースを作る)
        insert_article(
            conn,
            ArticleRow(
                source_id=sid,
                guid="g1",
                url="https://e.com/1",
                title="t1",
                snippet="s1",
                body_hash="h1",
                body=None,
                author=None,
                published_at=2_000_000_000,
            ),
        )
        conn.execute("UPDATE articles SET fetched_at = 1 WHERE guid = 'g1'")
        conn.commit()
        result = get_source_staleness(conn)
        assert result[0].latest_activity_at == 2_000_000_000
    finally:
        conn.close()


def test_get_source_staleness_excludes_disabled_sources(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        disabled = SourceConfig(
            slug="disabled",
            name="Disabled",
            feed_url="https://example.com/feed2",
            site_url=None,
            language="en",
            category="blog",
            enabled=False,
            fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
            license_note="",
        )
        upsert_source(conn, disabled)
        assert get_source_staleness(conn) == []
    finally:
        conn.close()


# ---------------- get_overall_stats (週次ヘルスレポート用) ----------------


def test_get_overall_stats_counts_total_and_recent(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        now = 2_000_000_000
        eight_days_ago = now - 8 * 24 * 3600
        three_days_ago = now - 3 * 24 * 3600
        insert_article(
            conn,
            ArticleRow(
                source_id=sid,
                guid="old",
                url="https://e.com/old",
                title="old",
                snippet="s",
                body_hash="old",
                body=None,
                author=None,
                published_at=eight_days_ago,
            ),
        )
        conn.execute("UPDATE articles SET fetched_at = ? WHERE guid = 'old'", (eight_days_ago,))
        insert_article(
            conn,
            ArticleRow(
                source_id=sid,
                guid="new",
                url="https://e.com/new",
                title="new",
                snippet="s",
                body_hash="new",
                body=None,
                author=None,
                published_at=three_days_ago,
            ),
        )
        conn.execute("UPDATE articles SET fetched_at = ? WHERE guid = 'new'", (three_days_ago,))
        conn.commit()

        stats = get_overall_stats(conn, now=now)
        assert stats.total_articles == 2
        assert stats.recent_7d_count == 1  # 8日前は対象外, 3日前のみ対象
    finally:
        conn.close()


def test_get_overall_stats_boundary_at_exactly_7_days(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _make_source())
        now = 2_000_000_000
        exactly_7_days_ago = now - 7 * 24 * 3600
        insert_article(
            conn,
            ArticleRow(
                source_id=sid,
                guid="edge",
                url="https://e.com/edge",
                title="edge",
                snippet="s",
                body_hash="edge",
                body=None,
                author=None,
                published_at=exactly_7_days_ago,
            ),
        )
        conn.execute(
            "UPDATE articles SET fetched_at = ? WHERE guid = 'edge'", (exactly_7_days_ago,)
        )
        conn.commit()

        stats = get_overall_stats(conn, now=now)
        assert stats.recent_7d_count == 1  # >= cutoff のため含まれる
    finally:
        conn.close()
