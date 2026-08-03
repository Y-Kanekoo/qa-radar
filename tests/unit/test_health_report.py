"""scripts/health_report.py のユニットテスト.

死活検知のために実フィードを叩き直すことはしない設計のため、このテストも
本番DBの信号 (consecutive_errors / published_at / fetched_at) を模したテスト用DBと
モックした webhook のみで完結させる。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from qa_radar.crawler.store import (
    OverallStats,
    SourceErrorStatus,
    SourceStaleness,
    get_overall_stats,
    get_source_staleness,
    get_sources_with_errors,
    update_source_fetch_state,
    upsert_source,
)
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig

# scripts/ を import path に追加 (他のスクリプトテストと同じ手法)
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import health_report  # noqa: E402

_RealClient = httpx.Client


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


def _client_class(handler) -> type[httpx.Client]:
    """モック webhook を積んだ httpx.Client の差し替えクラスを作る."""

    class _MockClient(_RealClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockClient


# ---------------- build_digest ----------------


def test_build_digest_no_issues() -> None:
    digest = health_report.build_digest(
        error_sources=[],
        staleness=[],
        stats=OverallStats(total_articles=100, recent_7d_count=5),
        db_size_bytes=11 * 1024 * 1024,
        now=2_000_000_000,
    )
    assert "総記事数: 100件" in digest
    assert "DBファイルサイズ: 11.00MB" in digest
    assert "直近7日の新規記事: 5件" in digest
    assert "エラー中のソースはありません" in digest
    assert "該当ソースはありません" in digest


def test_build_digest_danger_vs_warning_error_labels() -> None:
    digest = health_report.build_digest(
        error_sources=[
            SourceErrorStatus(slug="danger-src", consecutive_errors=9),
            SourceErrorStatus(slug="warn-src", consecutive_errors=8),
        ],
        staleness=[],
        stats=OverallStats(total_articles=0, recent_7d_count=0),
        db_size_bytes=0,
        now=2_000_000_000,
    )
    assert "[⚠️危険] danger-src — 9回連続失敗" in digest
    assert "[注意] warn-src — 8回連続失敗" in digest


def test_build_digest_staleness_labels_for_each_tier() -> None:
    now = 2_000_000_000
    day = 24 * 3600
    staleness = [
        SourceStaleness(slug="healthy", name="Healthy", latest_activity_at=now - 1 * day),
        SourceStaleness(slug="warn", name="Warn", latest_activity_at=now - 30 * day),
        SourceStaleness(slug="critical", name="Critical", latest_activity_at=now - 90 * day),
        SourceStaleness(slug="never", name="Never", latest_activity_at=None),
    ]
    digest = health_report.build_digest(
        error_sources=[],
        staleness=staleness,
        stats=OverallStats(total_articles=1, recent_7d_count=1),
        db_size_bytes=1,
        now=now,
    )
    # 健全 (1日前) は出力に含まれない
    assert "healthy" not in digest
    assert "[新着なし(注意)] warn — 最終更新 30日前" in digest
    assert "[長期停止疑い] critical — 最終更新 90日前" in digest
    assert "[長期停止疑い] never — 記事取得実績なし" in digest


def test_build_digest_includes_low_frequency_disclaimer() -> None:
    digest = health_report.build_digest(
        error_sources=[],
        staleness=[],
        stats=OverallStats(total_articles=0, recent_7d_count=0),
        db_size_bytes=0,
    )
    assert "論文誌" in digest
    assert "必ずしも障害を意味しません" in digest


# ---------------- split_for_discord ----------------


def test_split_for_discord_short_text_single_chunk() -> None:
    chunks = health_report.split_for_discord("line1\nline2")
    assert chunks == ["line1\nline2"]


def test_split_for_discord_splits_long_text_without_exceeding_limit() -> None:
    lines = [f"line-{i}" * 20 for i in range(200)]
    text = "\n".join(lines)
    chunks = health_report.split_for_discord(text, limit=500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500
    # 全行が失われていないことを確認 (結合すると元の行集合と一致)
    rejoined_lines = "\n".join(chunks).split("\n")
    assert rejoined_lines == lines


def test_split_for_discord_truncates_single_oversized_line() -> None:
    huge_line = "x" * 3000
    chunks = health_report.split_for_discord(huge_line, limit=2000)
    assert len(chunks) == 1
    assert len(chunks[0]) == 2000


# ---------------- send_to_discord ----------------


def test_send_to_discord_all_success_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req.read())
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "Client", _client_class(handler))
    ok = health_report.send_to_discord(["chunk1", "chunk2"], "https://discord/wh")
    assert ok is True
    assert len(received) == 2


def test_send_to_discord_partial_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(500)
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "Client", _client_class(handler))
    ok = health_report.send_to_discord(["a", "b", "c"], "https://discord/wh")
    assert ok is False


# ---------------- main ----------------


def test_main_missing_db_returns_1(tmp_path: Path) -> None:
    exit_code = health_report.main(["--db-path", str(tmp_path / "missing.db")])
    assert exit_code == 1


def test_main_prints_digest_and_skips_send_when_webhook_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # pytest はテストセッション中に root logger へ自前の LogCaptureHandler を差し込むため、
    # `logging.basicConfig()` (root に既存 handler があれば no-op) は実質何もせず、
    # warning は実際の stderr には出力されない。ログ内容の検証は capsys ではなく
    # caplog (pytest のログキャプチャ機構) で行う必要がある。
    monkeypatch.delenv(health_report.ENV_ALERT_WEBHOOK, raising=False)
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    try:
        upsert_source(conn, _src())
    finally:
        conn.close()

    with caplog.at_level("WARNING"):
        exit_code = health_report.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "qa-radar ソース健全性レポート" in captured.out
    assert f"環境変数 {health_report.ENV_ALERT_WEBHOOK}" in caplog.text


def test_main_sends_to_discord_when_webhook_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(health_report.ENV_ALERT_WEBHOOK, "https://discord/wh")
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    try:
        sid = upsert_source(conn, _src("flaky"))
        for _ in range(9):
            update_source_fetch_state(conn, sid, etag=None, last_modified=None, success=False)
    finally:
        conn.close()

    sent: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req.read())
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "Client", _client_class(handler))

    exit_code = health_report.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(sent) == 1
    assert "flaky" in captured.out


def test_main_returns_nonzero_when_discord_send_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(health_report.ENV_ALERT_WEBHOOK, "https://discord/wh")
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    try:
        upsert_source(conn, _src())
    finally:
        conn.close()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "Client", _client_class(handler))

    exit_code = health_report.main(["--db-path", str(db_path)])
    assert exit_code == 1


# ---------------- store関数との結合の健全性 (sanity) ----------------


def test_build_digest_wires_real_store_query_results(tmp_path: Path) -> None:
    """store.py の集計関数の戻り値をそのまま build_digest に渡せることを確認する."""
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    try:
        upsert_source(conn, _src())
        error_sources = get_sources_with_errors(conn)
        staleness = get_source_staleness(conn)
        stats = get_overall_stats(conn)
    finally:
        conn.close()

    digest = health_report.build_digest(
        error_sources=error_sources,
        staleness=staleness,
        stats=stats,
        db_size_bytes=db_path.stat().st_size,
    )
    assert "qa-radar ソース健全性レポート" in digest
