"""D1-fix のレビュー指摘に対する回帰テスト."""

from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import qa_radar.db as db_module
from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.publisher.discord_content import (
    DISCORD_CONTENT_LIMIT,
    send_to_discord,
    split_for_discord,
)
from qa_radar.publisher.pages import Digest, render_digest_page
from qa_radar.publisher.queries import DigestStats, fetch_digest_articles, fetch_digest_stats
from qa_radar.sources import FetchPolicy, SourceConfig
from qa_radar.summarizer.digest import (
    DEFAULT_DIGEST_MAX_TOKENS,
    DigestInput,
    build_digest_prompt,
    generate_weekly_digest,
)
from qa_radar.tools import list_recent_impl

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import weekly_digest  # noqa: E402


def _source(slug: str) -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name=f"Source {slug}",
        feed_url=f"https://{slug}.example.com/feed",
        site_url=None,
        language="ja",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=200),
        license_note="",
    )


def _article(source_id: int, guid: str, *, published_at: int) -> ArticleRow:
    return ArticleRow(
        source_id=source_id,
        guid=guid,
        url=f"https://example.com/{guid}",
        title=f"記事 {guid}",
        snippet="公開済みスニペット",
        body_hash=f"hash-{guid}",
        body="LLM に渡してはいけない本文",
        author=None,
        published_at=published_at,
        tags=["e2e"],
    )


def _prompt_item(index: int = 1) -> dict[str, object]:
    return {
        "title": f"記事 {index}",
        "snippet": "概要",
        "tags": ["e2e"],
        "source_name": "Source",
        "url": f"https://example.com/{index}",
    }


def test_prompt_states_week_total_and_selection_contract() -> None:
    items = DigestInput([_prompt_item(index) for index in range(120)], total_count=382)

    prompt = build_digest_prompt(items)

    assert "今週の全382件のうち最新120件" in prompt


def test_digest_default_token_limit_is_4000() -> None:
    assert DEFAULT_DIGEST_MAX_TOKENS == 4000


@pytest.mark.parametrize(
    ("stop_reason", "blocks", "message"),
    [
        ("max_tokens", [MagicMock(text="途中まで")], "最大トークン数"),
        ("end_turn", [MagicMock(text="   ")], "生成結果が空"),
        ("end_turn", [], "生成結果が空"),
    ],
)
def test_generate_rejects_truncation_and_empty_content(
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str,
    blocks: list[MagicMock],
    message: str,
) -> None:
    fake_client = MagicMock()
    fake_client.messages.create.return_value = MagicMock(
        content=blocks,
        stop_reason=stop_reason,
    )
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    monkeypatch.setattr("qa_radar.summarizer.digest.is_available", lambda: True)

    with (
        patch.dict("sys.modules", {"anthropic": fake_anthropic}),
        pytest.raises(RuntimeError, match=message),
    ):
        generate_weekly_digest([_prompt_item()])


def test_digest_queries_count_all_but_return_only_requested_latest_items(tmp_path: Path) -> None:
    now = int(time.time())
    conn = init_db(tmp_path / "articles.db")
    try:
        first_source = upsert_source(conn, _source("first"))
        second_source = upsert_source(conn, _source("second"))
        for index in range(121):
            source_id = first_source if index % 2 == 0 else second_source
            insert_article(conn, _article(source_id, f"recent-{index}", published_at=now - index))
        insert_article(conn, _article(first_source, "old", published_at=now - 8 * 86400))
        origin = _article(first_source, "origin", published_at=now - 500)
        insert_article(conn, origin)
        origin_id = int(
            conn.execute("SELECT id FROM articles WHERE guid = 'origin'").fetchone()["id"]
        )
        duplicate = _article(second_source, "duplicate", published_at=now - 100)
        duplicate.duplicate_of = origin_id
        insert_article(conn, duplicate)

        stats = fetch_digest_stats(conn, period_start=now - 7 * 86400, period_end=now)
        items = fetch_digest_articles(
            conn,
            period_start=now - 7 * 86400,
            period_end=now,
            limit=120,
        )

        assert stats == DigestStats(article_count=122, source_count=2)
        assert len(items) == 120
        assert "body" not in items[0]
        assert all(item["url"] != "https://example.com/duplicate" for item in items)
    finally:
        conn.close()


def test_list_recent_public_limit_is_100(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "articles.db")
    try:
        assert list_recent_impl(conn, limit=100) == []
        with pytest.raises(ValueError, match="1〜100"):
            list_recent_impl(conn, limit=101)
    finally:
        conn.close()


def test_render_digest_links_only_http_urls_and_keeps_attributes_escaped() -> None:
    digest = Digest(
        id=1,
        created_at=100,
        period_start=0,
        period_end=100,
        content_md=(
            "- 安全 https://example.com/path?a=1&b=2\n"
            "- 危険 javascript:alert(1)\n"
            '- 属性 https://example.com/" onclick="alert(1)'
        ),
    )

    html = render_digest_page(
        digest,
        article_count=3,
        digest_source_count=1,
    )

    assert '<a href="https://example.com/path?a=1&amp;b=2" rel="noopener">' in html
    assert '<a href="javascript:' not in html
    assert 'href="https://example.com/&quot;' in html
    assert '" onclick="alert(1)' not in html
    assert "全3件・1ソース" in html


def test_render_digest_stops_urls_at_japanese_punctuation_and_next_url() -> None:
    digest = Digest(
        id=1,
        created_at=100,
        period_start=0,
        period_end=100,
        content_md=(
            "日本語 URL https://example.com/記事。次の記事も参照\n"
            "連続 URL https://example.com/ahttps://example.com/b\n"
            "括弧内 URL (https://example.com/c)。"
        ),
    )

    html = render_digest_page(
        digest,
        article_count=5,
        digest_source_count=1,
    )

    assert (
        '<a href="https://example.com/記事" rel="noopener">https://example.com/記事</a>。' in html
    )
    assert '<a href="https://example.com/a" rel="noopener">https://example.com/a</a>' in html
    assert '<a href="https://example.com/b" rel="noopener">https://example.com/b</a>' in html
    assert '<a href="https://example.com/c" rel="noopener">https://example.com/c</a>)。' in html
    assert html.count('rel="noopener"') == 4
    assert "主要4件を紹介（他1件）" in html


def test_shared_discord_split_uses_2000_character_limit() -> None:
    chunks = split_for_discord("x" * (DISCORD_CONTENT_LIMIT + 1))
    assert DISCORD_CONTENT_LIMIT == 2000
    assert len(chunks) == 1
    assert len(chunks[0]) == 2000
    assert chunks[0].endswith("…")


def test_shared_discord_sender_handles_invalid_url_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_url = "https://discord.com:notaport/api/webhooks/1/TOKEN"
    with caplog.at_level("ERROR"):
        result = send_to_discord(["本文"], secret_url)
    assert result is False
    assert "TOKEN" not in caplog.text
    assert "ネットワークエラー" in caplog.text


def test_weekly_digest_uses_seven_days_and_latest_120(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    conn.close()
    captured: dict[str, int] = {}
    fixed_now = 2_000_000_000
    monkeypatch.setattr(weekly_digest, "is_available", lambda: True)
    monkeypatch.setattr(weekly_digest.time, "time", lambda: fixed_now)

    def fake_stats(_conn: sqlite3.Connection, *, period_start: int, period_end: int) -> DigestStats:
        captured["stats_start"] = period_start
        captured["stats_end"] = period_end
        return DigestStats(article_count=382, source_count=44)

    def fake_articles(
        _conn: sqlite3.Connection,
        *,
        period_start: int,
        period_end: int,
        limit: int,
    ) -> list[dict[str, object]]:
        captured["articles_start"] = period_start
        captured["articles_end"] = period_end
        captured["limit"] = limit
        return [_prompt_item()]

    monkeypatch.setattr(weekly_digest, "fetch_digest_stats", fake_stats)
    monkeypatch.setattr(weekly_digest, "fetch_digest_articles", fake_articles)
    monkeypatch.setattr(
        weekly_digest,
        "generate_weekly_digest",
        lambda items: f"# 今週のハイライト\n{items.total_count}件\n- https://example.com/1",
    )
    monkeypatch.delenv(weekly_digest.ENV_WEBHOOK, raising=False)

    assert weekly_digest.main(["--db-path", str(db_path)]) == 0
    assert captured == {
        "stats_start": fixed_now - 7 * 86400,
        "stats_end": fixed_now,
        "articles_start": fixed_now - 7 * 86400,
        "articles_end": fixed_now,
        "limit": 120,
    }


def test_weekly_digest_writes_generated_once_and_marks_discord_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "articles.db"
    conn = init_db(db_path)
    source_id = upsert_source(conn, _source("output"))
    insert_article(conn, _article(source_id, "output", published_at=int(time.time())))
    conn.close()
    output_path = tmp_path / "github-output"
    monkeypatch.setattr(weekly_digest, "is_available", lambda: True)
    monkeypatch.setattr(
        weekly_digest,
        "generate_weekly_digest",
        lambda _items: "# 今週のハイライト\n- https://example.com/output",
    )
    monkeypatch.setattr(weekly_digest, "send_to_discord", lambda _chunks, _url: False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv(weekly_digest.ENV_WEBHOOK, "https://discord.example/webhook")

    assert weekly_digest.main(["--db-path", str(db_path)]) == 1
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "generated=true",
        "discord_failed=true",
    ]


def test_weekly_entrypoint_suppresses_httpx_info_logs() -> None:
    httpx_logger = logging.getLogger("httpx")
    original_level = httpx_logger.level
    try:
        httpx_logger.setLevel(logging.INFO)
        importlib.reload(weekly_digest)
        assert httpx_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(original_level)


def test_v5_migration_is_idempotent_when_table_already_exists(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "articles.db")
    try:
        db_module._migrate_to_v5(conn)
    finally:
        conn.close()


def test_health_workflow_publishes_generated_digest_after_step_failure() -> None:
    workflow = (_SCRIPTS.parent / ".github" / "workflows" / "health.yml").read_text(
        encoding="utf-8"
    )
    assert "continue-on-error: true" in workflow
    assert "!cancelled() && steps.digest.outputs.generated == 'true'" in workflow
    assert "discord_failed: ${{ steps.digest.outputs.discord_failed }}" in workflow
