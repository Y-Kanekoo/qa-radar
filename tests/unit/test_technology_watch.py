"""Offline checks for discovery, classification, delivery ledger and preview."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pytest

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import SCHEMA_VERSION, init_db
from qa_radar.publisher.notification_state import fetch_unnotified, mark_notified
from qa_radar.publisher.technology_watch import (
    WATCH_CHANNEL,
    build_payload,
    classify_release,
    load_profiles,
    record_delivery,
    select_candidates,
    send_payload,
)
from qa_radar.sources import FetchPolicy, SourceConfig, load_sources

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import notify_discord
import notify_technology_watch


def _source(slug: str = "browser-use-releases") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name="Browser Use releases",
        feed_url="https://github.com/browser-use/browser-use/releases.atom",
        site_url="https://github.com/browser-use/browser-use",
        language="en",
        category="tool",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=20),
        license_note="",
    )


def _article(
    sid: int, guid: str, *, url: str | None = None, snippet: str = "Added browser recording"
) -> ArticleRow:
    return ArticleRow(
        source_id=sid,
        guid=guid,
        url=url or f"https://github.com/browser-use/browser-use/releases/tag/{guid}",
        title=f"{guid} release",
        snippet=snippet,
        body_hash=guid,
        body="PRIVATE FULL BODY MUST NEVER APPEAR",
        author=None,
        published_at=int(time.time()),
    )


def _setup(path: Path) -> tuple[sqlite3.Connection, int]:
    conn = init_db(path)
    return conn, upsert_source(conn, _source())


def test_profiles_are_official_existing_sources() -> None:
    profiles = load_profiles()
    sources = {source.slug: source for source in load_sources()}
    assert set(profiles) <= set(sources)
    assert len(profiles) >= 8
    assert all(sources[slug].feed_url.endswith("/releases.atom") for slug in profiles)


@pytest.mark.parametrize(
    ("title", "snippet", "expected"),
    [
        ("v1.0.0", "", "重要リリース"),
        ("initial release", "", "新規公開"),
        ("v2.3.1", "Breaking API change", "重要リリース"),
        ("v2.3.2", "Added browser recording", "機能追加"),
        ("v2.3.2", "This release includes Browser Use toolsets", "機能追加"),
        ("v2.3.2", "Bug Fixes browser_find filename", "QA関連修正"),
        ("v2.3.2", "Added docs", None),
        ("stagehand-python@4.2.0a0.dev1579", "Added browser feature", None),
        ("v2.3.3", "Dependency bump only", None),
    ],
)
def test_release_classification(title: str, snippet: str, expected: str | None) -> None:
    result = classify_release(title, snippet)
    assert (result[0] if result else None) == expected


def test_selection_dedup_suppression_and_retry(tmp_path: Path) -> None:
    conn, sid = _setup(tmp_path / "articles.db")
    try:
        insert_article(conn, _article(sid, "v2.1.0"))
        insert_article(
            conn,
            _article(
                sid,
                "copy",
                url="https://github.com/browser-use/browser-use/releases/tag/v2.1.0",
            ),
        )
        insert_article(conn, _article(sid, "patch", snippet="Dependency bump only"))
        insert_article(
            conn,
            _article(sid, "impostor", url="https://example.com/releases/tag/v3.0.0"),
        )
        selected, deferred = select_candidates(conn, load_profiles())
        assert len(selected) == 1
        assert len(selected[0].article_ids) == 2
        assert deferred == 0
        payload = build_payload(selected)
        assert payload["allowed_mentions"] == {"parse": []}
        encoded = json.dumps(payload, ensure_ascii=False)
        for expected in (
            "何ができるか",
            "QA用途",
            "すぐ試せるか",
            "制限・成熟度",
            "ライセンス",
            "料金",
            "一次情報",
            "今回の差分",
            "動作未検証",
        ):
            assert expected in encoded
        assert "PRIVATE FULL BODY" not in encoded

        record_delivery(conn, selected, status="http_503", success=False, now=100)
        assert len(select_candidates(conn, load_profiles())[0]) == 1
        conn.execute("UPDATE articles SET fetched_at = 1")
        conn.commit()
        assert len(select_candidates(conn, load_profiles())[0]) == 1  # failed backlog persists
        record_delivery(conn, selected, status="http_204", success=True, now=200)
        assert select_candidates(conn, load_profiles())[0] == []
        attempts = conn.execute(
            "SELECT attempts, last_status FROM technology_delivery_attempts ORDER BY article_id"
        ).fetchall()
        assert [(row["attempts"], row["last_status"]) for row in attempts] == [
            (2, "http_204"),
            (2, "http_204"),
        ]
        sent = conn.execute(
            "SELECT COUNT(*) FROM article_notifications WHERE channel = ?", (WATCH_CHANNEL,)
        ).fetchone()[0]
        assert sent == 2
    finally:
        conn.close()


def test_existing_discord_mark_prevents_replay_and_generic_exclusion(tmp_path: Path) -> None:
    conn, sid = _setup(tmp_path / "articles.db")
    try:
        insert_article(conn, _article(sid, "v2.1.0"))
        article_id = conn.execute("SELECT id FROM articles").fetchone()[0]
        mark_notified(conn, article_id)
        assert select_candidates(conn, load_profiles())[0] == []
        assert (
            fetch_unnotified(conn, exclude_source_slugs=frozenset({"browser-use-releases"})) == []
        )
    finally:
        conn.close()


def test_new_sources_never_leak_to_ordinary_discord_before_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    insert_article(conn, _article(sid, "v1.0.0"))
    conn.close()
    monkeypatch.delenv("DISCORD_TECH_WATCH_WEBHOOK_URL", raising=False)
    with caplog.at_level("INFO"):
        assert notify_discord.main(["--db-path", str(path), "--dry-run"]) == 0
    assert "未通知記事なし" in caplog.text


def test_http_failure_and_bounded_retry_without_real_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter([429, 204])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(statuses), headers={"Retry-After": "0"})

    monkeypatch.setattr("qa_radar.publisher.technology_watch.time.sleep", lambda _s: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert send_payload(
            "https://discord.example/webhook", {"content": "synthetic"}, client=client
        ) == (
            True,
            "http_204",
        )
    assert len(seen) == 2


def test_dry_run_uses_copy_and_never_sends_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    insert_article(conn, _article(sid, "v1.0.0"))
    before = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    monkeypatch.setattr(
        notify_technology_watch,
        "send_payload",
        lambda *_args, **_kwargs: pytest.fail("dry-run attempted network send"),
    )
    monkeypatch.setenv("DISCORD_TECH_WATCH_WEBHOOK_URL", "https://secret.example/webhook")
    assert notify_technology_watch.main(["--db-path", str(path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "QA新技術ウォッチ" in output
    assert "secret.example" not in output
    conn = init_db(path)
    try:
        assert (
            conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == before
            == SCHEMA_VERSION
        )
        assert conn.execute("SELECT COUNT(*) FROM technology_delivery_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM article_notifications").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_failed_digest_retries_once_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    insert_article(conn, _article(sid, "v2.0.0"))
    conn.close()
    monkeypatch.setenv("DISCORD_TECH_WATCH_WEBHOOK_URL", "https://secret.example/webhook")
    calls: list[dict[str, object]] = []
    outcomes = iter([(False, "http_503"), (True, "http_204")])

    def stub_send(_webhook: str, payload: dict[str, object]) -> tuple[bool, str]:
        calls.append(payload)
        return next(outcomes)

    monkeypatch.setattr(notify_technology_watch, "send_payload", stub_send)
    argv = ["--db-path", str(path)]
    assert notify_technology_watch.main(argv) == 2
    assert notify_technology_watch.main(argv) == 0
    assert notify_technology_watch.main(argv) == 0
    assert len(calls) == 2
    conn = init_db(path)
    try:
        attempt = conn.execute(
            "SELECT attempts, last_status FROM technology_delivery_attempts"
        ).fetchone()
        assert (attempt["attempts"], attempt["last_status"]) == (2, "http_204")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM article_notifications WHERE channel = ?", (WATCH_CHANNEL,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
