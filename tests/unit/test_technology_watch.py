"""Offline checks for discovery, classification, delivery ledger and preview."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pytest

import qa_radar.publisher.technology_watch as technology_watch
from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import SCHEMA_VERSION, init_db
from qa_radar.publisher.notification_state import fetch_unnotified, mark_notified
from qa_radar.publisher.technology_watch import (
    PENDING_TTL_SECONDS,
    WATCH_CHANNEL,
    DeliveryResult,
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
    repository = (
        "promptfoo/promptfoo" if slug == "promptfoo-releases" else "browser-use/browser-use"
    )
    return SourceConfig(
        slug=slug,
        name="Browser Use releases",
        feed_url=f"https://github.com/{repository}/releases.atom",
        site_url=f"https://github.com/{repository}",
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


def test_v7_delivery_history_migrates_without_losing_retry(tmp_path: Path) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    insert_article(conn, _article(sid, "v2.0.0"))
    article = conn.execute("SELECT id, fetched_at FROM articles").fetchone()
    conn.execute("DROP TABLE technology_delivery_attempts")
    conn.execute(
        """
        CREATE TABLE technology_delivery_attempts (
            article_id INTEGER PRIMARY KEY REFERENCES articles(id),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempted_at INTEGER NOT NULL,
            last_status TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO technology_delivery_attempts VALUES (?, 1, 123, 'http_429')",
        (article["id"],),
    )
    conn.execute("UPDATE schema_version SET version = 7")
    conn.commit()
    conn.close()
    migrated = init_db(path)
    try:
        row = migrated.execute("SELECT * FROM technology_delivery_attempts").fetchone()
        assert (
            migrated.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        )
        assert (row["attempts"], row["last_status"], row["last_attempted_at"]) == (
            1,
            "http_429",
            123,
        )
        assert row["expires_at"] == article["fetched_at"] + PENDING_TTL_SECONDS
        assert row["last_message_id"] is None
        assert row["excluded_reason"] is None
    finally:
        migrated.close()


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

        record_delivery(conn, selected, outcome=DeliveryResult("retryable", "http_429"), now=100)
        assert len(select_candidates(conn, load_profiles())[0]) == 1
        conn.execute("UPDATE articles SET fetched_at = 1")
        conn.commit()
        assert len(select_candidates(conn, load_profiles())[0]) == 1  # failed backlog persists
        record_delivery(
            conn,
            selected,
            outcome=DeliveryResult("confirmed", "http_200", "123456789"),
            now=200,
        )
        assert select_candidates(conn, load_profiles())[0] == []
        attempts = conn.execute(
            "SELECT attempts, last_status, last_message_id FROM technology_delivery_attempts ORDER BY article_id"
        ).fetchall()
        assert [
            (row["attempts"], row["last_status"], row["last_message_id"]) for row in attempts
        ] == [
            (2, "http_200", "123456789"),
            (2, "http_200", "123456789"),
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


def test_dedicated_delivery_stays_suppressed_after_webhook_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "articles.db"
    conn = init_db(path)
    sid = upsert_source(conn, _source("promptfoo-releases"))
    insert_article(
        conn,
        _article(
            sid,
            "v1.0.0",
            url="https://github.com/promptfoo/promptfoo/releases/tag/v1.0.0",
        ),
    )
    conn.close()
    monkeypatch.setenv("DISCORD_TECH_WATCH_WEBHOOK_URL", "https://discord.example/synthetic")
    monkeypatch.setattr(
        notify_technology_watch,
        "send_payload",
        lambda *_args, **_kwargs: DeliveryResult("confirmed", "http_200", "123456789"),
    )
    assert notify_technology_watch.main(["--db-path", str(path)]) == 0
    monkeypatch.delenv("DISCORD_TECH_WATCH_WEBHOOK_URL", raising=False)
    with caplog.at_level("INFO"):
        assert notify_discord.main(["--db-path", str(path), "--dry-run"]) == 0
    assert "未通知記事なし" in caplog.text


@pytest.mark.parametrize("status", ["network_uncertain", "unconfirmed_http_204"])
def test_uncertain_delivery_blocks_generic_until_explicit_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: str,
) -> None:
    path = tmp_path / "articles.db"
    conn = init_db(path)
    sid = upsert_source(conn, _source("promptfoo-releases"))
    insert_article(
        conn,
        _article(
            sid,
            "v1.0.0",
            url="https://github.com/promptfoo/promptfoo/releases/tag/v1.0.0",
        ),
    )
    article_id = conn.execute("SELECT id FROM articles").fetchone()[0]
    conn.close()
    monkeypatch.setenv("DISCORD_TECH_WATCH_WEBHOOK_URL", "https://discord.example/tech-synthetic")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/generic-synthetic")
    monkeypatch.setattr(
        notify_technology_watch,
        "send_payload",
        lambda *_args, **_kwargs: DeliveryResult("uncertain", status),
    )
    assert notify_technology_watch.main(["--db-path", str(path)]) == 2

    monkeypatch.delenv("DISCORD_TECH_WATCH_WEBHOOK_URL")

    async def unexpected_send(*_args: object, **_kwargs: object) -> None:
        pytest.fail("held article was sent to the ordinary Discord channel")

    monkeypatch.setattr(notify_discord, "send_batch", unexpected_send)
    with caplog.at_level("INFO"):
        assert notify_discord.main(["--db-path", str(path)]) == 0
    assert "未通知記事なし" in caplog.text

    conn = init_db(path)
    try:
        row = conn.execute(
            "SELECT excluded_reason FROM technology_delivery_attempts WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        assert row["excluded_reason"] == "manual_reconciliation"
        assert conn.execute("SELECT COUNT(*) FROM article_notifications").fetchone()[0] == 0

        # A human has checked Discord and explicitly decided that retry is safe.
        with conn:
            changed = conn.execute(
                """
                UPDATE technology_delivery_attempts
                SET excluded_reason = NULL, last_status = 'reconciled_retry'
                WHERE article_id = ? AND excluded_reason = 'manual_reconciliation'
                """,
                (article_id,),
            ).rowcount
        assert changed == 1
    finally:
        conn.close()

    caplog.clear()
    with caplog.at_level("INFO"):
        assert notify_discord.main(["--db-path", str(path), "--dry-run"]) == 0
    assert "[DRY]" in caplog.text
    assert "v1.0.0 release" in caplog.text


def test_deferred_candidate_survives_lookback_then_expires_visibly(tmp_path: Path) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    base = int(time.time())
    try:
        for index in range(6):
            insert_article(conn, _article(sid, f"v2.1.{index}"))
        first, deferred = select_candidates(conn, load_profiles(), now=base)
        assert (len(first), deferred) == (5, 1)
        pending_rows = conn.execute(
            "SELECT COUNT(*) FROM technology_delivery_attempts WHERE last_status = 'pending'"
        ).fetchone()[0]
        assert pending_rows == 6
        deferred_id = next(
            row[0]
            for row in conn.execute("SELECT id FROM articles")
            if row[0] not in {item.article_ids[0] for item in first}
        )
        record_delivery(
            conn,
            first,
            outcome=DeliveryResult("confirmed", "http_200", "123456789"),
            now=base,
        )
        conn.execute(
            "UPDATE articles SET fetched_at = ? WHERE id = ?", (base - 8 * 86400, deferred_id)
        )
        conn.commit()
        later, _ = select_candidates(conn, load_profiles(), now=base + 8 * 86400)
        assert [item.article_ids for item in later] == [(deferred_id,)]
        assert (
            conn.execute(
                "SELECT expires_at FROM technology_delivery_attempts WHERE article_id = ?",
                (deferred_id,),
            ).fetchone()[0]
            == base + PENDING_TTL_SECONDS
        )
        expired, _ = select_candidates(conn, load_profiles(), now=base + PENDING_TTL_SECONDS + 1)
        assert expired == []
        status = conn.execute(
            "SELECT last_status, excluded_reason FROM technology_delivery_attempts WHERE article_id = ?",
            (deferred_id,),
        ).fetchone()
        assert (status["last_status"], status["excluded_reason"]) == (
            "expired",
            "pending_ttl_30d",
        )
    finally:
        conn.close()


@pytest.mark.parametrize("status", [200, 204, 503])
def test_unconfirmed_response_is_held_without_automatic_retry(tmp_path: Path, status: int) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    try:
        insert_article(conn, _article(sid, "v2.1.0"))
        selected, _ = select_candidates(conn, load_profiles())
        with httpx.Client(
            transport=httpx.MockTransport(lambda _req: httpx.Response(status, json={}))
        ) as client:
            outcome = send_payload(
                "https://discord.example/api/webhooks/123/SYNTHETIC",
                {"content": "synthetic"},
                client=client,
            )
        assert outcome.state == "uncertain"
        record_delivery(conn, selected, outcome=outcome)
        assert select_candidates(conn, load_profiles())[0] == []
        row = conn.execute(
            "SELECT last_status, excluded_reason, last_message_id FROM technology_delivery_attempts"
        ).fetchone()
        assert row["excluded_reason"] == "manual_reconciliation"
        assert row["last_message_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM article_notifications").fetchone()[0] == 0
    finally:
        conn.close()


def test_timeout_is_uncertain_and_transport_log_hides_synthetic_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with (
        caplog.at_level(logging.INFO),
        caplog.at_level(logging.INFO, logger="httpx"),
        httpx.Client(transport=httpx.MockTransport(timeout)) as client,
    ):
        outcome = send_payload(
            "https://discord.example/api/webhooks/123/SECRET_SENTINEL",
            {"content": "synthetic"},
            client=client,
        )
    assert outcome == DeliveryResult("uncertain", "network_uncertain")
    assert "SECRET_SENTINEL" not in caplog.text


def test_invalid_webhook_url_is_classified_without_logging_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        outcome = send_payload("https://[SECRET_SENTINEL", {"content": "synthetic"})
    assert outcome == DeliveryResult("retryable", "invalid_webhook_url")
    assert "SECRET_SENTINEL" not in caplog.text


def test_cli_httpx_info_log_never_exposes_synthetic_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "articles.db"
    conn, sid = _setup(path)
    insert_article(conn, _article(sid, "v2.0.0"))
    conn.close()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "123456789"})

    real_client = httpx.Client
    monkeypatch.setattr(
        technology_watch.httpx,
        "Client",
        lambda *_args, **_kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv(
        "DISCORD_TECH_WATCH_WEBHOOK_URL",
        "https://discord.example/api/webhooks/123/SECRET_SENTINEL",
    )
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.INFO)
    with caplog.at_level(logging.INFO):
        assert notify_technology_watch.main(["--db-path", str(path)]) == 0
    assert len(seen) == 1
    assert seen[0].url.params["wait"] == "true"
    conn = init_db(path)
    try:
        assert (
            conn.execute("SELECT last_message_id FROM technology_delivery_attempts").fetchone()[0]
            == "123456789"
        )
    finally:
        conn.close()
    output = capsys.readouterr()
    assert "SECRET_SENTINEL" not in caplog.text + output.out + output.err


def test_http_failure_and_bounded_retry_without_real_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter([429, 200])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = next(statuses)
        if status == 200:
            return httpx.Response(status, json={"id": "123456789"})
        return httpx.Response(status, headers={"Retry-After": "0"})

    monkeypatch.setattr("qa_radar.publisher.technology_watch.time.sleep", lambda _s: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert send_payload(
            "https://discord.example/webhook?thread_id=42&wait=false",
            {"content": "synthetic"},
            client=client,
        ) == DeliveryResult("confirmed", "http_200", "123456789")
    assert len(seen) == 2
    assert all(request.url.params["wait"] == "true" for request in seen)
    assert all(request.url.params["thread_id"] == "42" for request in seen)


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
    outcomes = iter(
        [
            DeliveryResult("retryable", "http_429"),
            DeliveryResult("confirmed", "http_200", "123456789"),
        ]
    )

    def stub_send(_webhook: str, payload: dict[str, object]) -> DeliveryResult:
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
        assert (attempt["attempts"], attempt["last_status"]) == (2, "http_200")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM article_notifications WHERE channel = ?", (WATCH_CHANNEL,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
