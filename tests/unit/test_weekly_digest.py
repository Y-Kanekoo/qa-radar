"""scripts/weekly_digest.py の終了コードと永続化テスト."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.publisher.queries import fetch_latest_digest
from qa_radar.sources import FetchPolicy, SourceConfig

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import weekly_digest  # noqa: E402


def _create_db_with_recent_article(path: Path) -> None:
    conn = init_db(path)
    try:
        source_id = upsert_source(
            conn,
            SourceConfig(
                slug="example",
                name="Example",
                feed_url="https://example.com/feed",
                site_url="https://example.com",
                language="ja",
                category="blog",
                enabled=True,
                fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
                license_note="",
            ),
        )
        insert_article(
            conn,
            ArticleRow(
                source_id=source_id,
                guid="recent",
                url="https://example.com/recent",
                title="新着記事",
                snippet="公開済みスニペット",
                body_hash="hash",
                body="LLM に渡してはいけない本文",
                author=None,
                published_at=int(time.time()),
                tags=["e2e"],
            ),
        )
    finally:
        conn.close()


def test_main_skips_with_exit_zero_when_api_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(weekly_digest, "is_available", lambda: False)
    with caplog.at_level("WARNING"):
        exit_code = weekly_digest.main(["--db-path", str(tmp_path / "missing.db")])
    assert exit_code == 0
    assert "スキップ" in caplog.text


def test_main_returns_nonzero_when_llm_generation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = tmp_path / "articles.db"
    _create_db_with_recent_article(db_path)
    monkeypatch.setattr(weekly_digest, "is_available", lambda: True)

    def fail_generation(_items: object) -> str:
        raise RuntimeError("sk-secret-value")

    monkeypatch.setattr(weekly_digest, "generate_weekly_digest", fail_generation)
    with caplog.at_level("ERROR"):
        exit_code = weekly_digest.main(["--db-path", str(db_path)])
    assert exit_code == 1
    assert "sk-secret-value" not in caplog.text

    conn = init_db(db_path)
    try:
        assert fetch_latest_digest(conn) is None
    finally:
        conn.close()


def test_main_returns_nonzero_on_missing_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(weekly_digest, "is_available", lambda: True)
    assert weekly_digest.main(["--db-path", str(tmp_path / "missing.db")]) == 1


def test_main_saves_digest_before_discord_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "articles.db"
    _create_db_with_recent_article(db_path)
    monkeypatch.setattr(weekly_digest, "is_available", lambda: True)
    monkeypatch.setattr(weekly_digest, "generate_weekly_digest", lambda _items: "# 週報")
    monkeypatch.setattr(weekly_digest, "send_to_discord", lambda _chunks, _url: False)
    monkeypatch.setenv(weekly_digest.ENV_WEBHOOK, "https://discord.example/webhook/secret")

    exit_code = weekly_digest.main(["--db-path", str(db_path)])

    assert exit_code == 1
    assert "# 週報" in capsys.readouterr().out
    conn = init_db(db_path)
    try:
        digest = fetch_latest_digest(conn)
        assert digest is not None
        assert digest.content_md == "# 週報"
    finally:
        conn.close()


def test_main_uses_fixed_digest_when_no_articles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    conn.close()
    monkeypatch.setattr(weekly_digest, "is_available", lambda: True)
    monkeypatch.setattr(
        weekly_digest,
        "generate_weekly_digest",
        lambda _items: pytest.fail("0件では LLM を呼ばない"),
    )
    monkeypatch.delenv(weekly_digest.ENV_WEBHOOK, raising=False)

    exit_code = weekly_digest.main(["--db-path", str(db_path)])

    assert exit_code == 0
    assert "今週は新着なし" in capsys.readouterr().out
    conn = init_db(db_path)
    try:
        digest = fetch_latest_digest(conn)
        assert digest is not None
        assert digest.content_md == weekly_digest.NO_ARTICLES_DIGEST
    finally:
        conn.close()
