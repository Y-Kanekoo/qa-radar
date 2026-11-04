"""scripts/run_crawl.py の main() のユニットテスト.

外部ネットワークや実 YAML 設定ファイルに依存しないよう、`load_sources` /
`load_blocked` / `run_crawl` (orchestrator) を monkeypatch して検証する.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from qa_radar.crawler.orchestrator import CrawlResult
from qa_radar.crawler.store import update_source_fetch_state, upsert_source
from qa_radar.sources import BlockedConfig, FetchPolicy, SourceConfig

# scripts/ を import path に追加 (notify_discord のテストと同じ手法)
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import run_crawl  # noqa: E402


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


def _patch_sources_and_blocked(
    monkeypatch: pytest.MonkeyPatch, sources: list[SourceConfig]
) -> None:
    """load_sources / load_blocked を実ファイルに触れない形へ差し替える."""
    monkeypatch.setattr(run_crawl, "load_sources", lambda path: sources)
    monkeypatch.setattr(
        run_crawl, "load_blocked", lambda path: BlockedConfig(blocked_domains=frozenset())
    )


def test_main_returns_1_when_all_sources_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """処理ソース数 > 0 かつ エラー数 == 処理ソース数 のとき exit 1."""
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("a"), _src("b")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        return CrawlResult(
            sources_processed=2,
            articles_added=0,
            errors=[
                {"slug": "a", "reason": "fetch_error", "detail": "boom"},
                {"slug": "b", "reason": "fetch_error", "detail": "boom"},
            ],
        )

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)

    exit_code = run_crawl.main(["--db-path", str(db_path)])
    assert exit_code == 1


def test_main_all_failure_emits_error_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("a")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        return CrawlResult(
            sources_processed=1,
            articles_added=0,
            errors=[{"slug": "a", "reason": "fetch_error", "detail": "boom"}],
        )

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)

    exit_code = run_crawl.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "::error title=Crawl total failure::" in captured.out
    # 全滅ケースでは部分失敗の warning annotation は出さない
    assert "::warning title=Crawl partial failure::" not in captured.out


def test_main_partial_failure_emits_warning_annotation_and_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """一部失敗のときは exit 0 のまま GitHub Actions warning annotation を出す."""
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("a"), _src("b"), _src("c")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        return CrawlResult(
            sources_processed=3,
            articles_added=5,
            errors=[{"slug": "b", "reason": "fetch_error", "detail": "timeout"}],
        )

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)

    exit_code = run_crawl.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "::warning title=Crawl partial failure::1/3 sources failed: b" in captured.out


def test_main_full_success_emits_no_failure_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("a")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        return CrawlResult(sources_processed=1, articles_added=2, errors=[])

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)

    exit_code = run_crawl.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "::warning title=Crawl partial failure::" not in captured.out
    assert "::error title=Crawl total failure::" not in captured.out


def test_main_logs_duplicate_mark_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """クロール完了時に重複マーク件数を独立した1行で出力する."""
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("a")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        return CrawlResult(
            sources_processed=1,
            articles_added=3,
            errors=[],
            duplicates_marked=2,
        )

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)
    caplog.set_level(logging.INFO, logger="qa_radar.run_crawl")

    assert run_crawl.main(["--db-path", str(db_path)]) == 0
    assert "重複マーク: 2 件" in caplog.messages


def test_main_emits_repeatedly_failing_source_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """consecutive_errors が閾値(9)以上のソースは警告 annotation を出す."""
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("flaky")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        # run_crawl() 内で DB に連続失敗状態を仕込む (実クロールを模した副作用)
        source_id = upsert_source(conn, _src("flaky"))
        for _ in range(9):
            update_source_fetch_state(conn, source_id, etag=None, last_modified=None, success=False)
        return CrawlResult(sources_processed=1, articles_added=0, errors=[])

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)

    exit_code = run_crawl.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        "::warning title=Source failing repeatedly::flaky が 9 回連続で失敗しています"
        in captured.out
    )


def test_main_no_repeatedly_failing_source_annotation_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "articles.db"
    _patch_sources_and_blocked(monkeypatch, [_src("a")])

    async def fake_run_crawl(conn, sources, blocked, *, concurrency=5):
        source_id = upsert_source(conn, _src("a"))
        for _ in range(3):
            update_source_fetch_state(conn, source_id, etag=None, last_modified=None, success=False)
        return CrawlResult(sources_processed=1, articles_added=0, errors=[])

    monkeypatch.setattr(run_crawl, "run_crawl", fake_run_crawl)

    exit_code = run_crawl.main(["--db-path", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "::warning title=Source failing repeatedly::" not in captured.out
