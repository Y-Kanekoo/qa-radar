"""週刊 LLM ダイジェストの DB・生成・Pages 表示テスト."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qa_radar.db import SCHEMA_VERSION, init_db
from qa_radar.publisher.pages import Digest, render_digest_page
from qa_radar.publisher.queries import fetch_latest_digest, insert_digest
from qa_radar.summarizer.anthropic_client import DEFAULT_MODEL
from qa_radar.summarizer.digest import (
    DEFAULT_DIGEST_MAX_TOKENS,
    build_digest_prompt,
    generate_weekly_digest,
)


def _create_v4_db(path: Path) -> None:
    """最新版から v5 オブジェクトを除き、実スキーマ相当の v4 DB を作る."""
    conn = init_db(path)
    try:
        conn.execute("DROP TABLE digests")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()
    finally:
        conn.close()


def test_v4_db_migrates_to_v5_with_digests_table(tmp_path: Path) -> None:
    db_path = tmp_path / "articles.db"
    _create_v4_db(db_path)

    conn = init_db(db_path)
    try:
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(digests)")}
        assert version == SCHEMA_VERSION
        assert columns == {"id", "created_at", "period_start", "period_end", "content_md"}
    finally:
        conn.close()


def test_insert_and_fetch_latest_digest(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "articles.db")
    try:
        first_id = insert_digest(
            conn,
            created_at=100,
            period_start=10,
            period_end=90,
            content_md="# 古い",
        )
        latest_id = insert_digest(
            conn,
            created_at=200,
            period_start=100,
            period_end=200,
            content_md="# 新しい",
        )

        digest = fetch_latest_digest(conn)

        assert first_id != latest_id
        assert digest == Digest(latest_id, 200, 100, 200, "# 新しい")
    finally:
        conn.close()


def test_fetch_latest_digest_returns_none_when_empty(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "articles.db")
    try:
        assert fetch_latest_digest(conn) is None
    finally:
        conn.close()


def test_build_digest_prompt_whitelists_fields_and_limits_snippet() -> None:
    secret_body = "公開してはいけない記事本文"
    prompt = build_digest_prompt(
        [
            {
                "title": "記事タイトル",
                "snippet": "あ" * 120,
                "tags": ["e2e", "tooling"],
                "source_name": "Example",
                "url": "https://example.com/article",
                "body": secret_body,
                "author": "入力対象外",
            }
        ]
    )

    assert "記事タイトル" in prompt
    assert "あ" * 100 in prompt
    assert "あ" * 101 not in prompt
    assert secret_body not in prompt
    assert "入力対象外" not in prompt


def test_generate_weekly_digest_calls_anthropic_with_weekly_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_block = MagicMock(text="# 今週のハイライト\nまとめ")
    fake_message = MagicMock(content=[fake_block])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_message
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    monkeypatch.setattr("qa_radar.summarizer.digest.is_available", lambda: True)

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = generate_weekly_digest(
            [
                {
                    "title": "記事",
                    "snippet": "概要",
                    "tags": ["e2e"],
                    "source_name": "Source",
                    "url": "https://example.com",
                }
            ]
        )

    call = fake_client.messages.create.call_args
    assert result == "# 今週のハイライト\nまとめ"
    assert call.kwargs["model"] == DEFAULT_MODEL
    assert call.kwargs["max_tokens"] == DEFAULT_DIGEST_MAX_TOKENS
    assert "本文の内容を推測しない" in call.kwargs["system"]
    assert "タイトル: 記事" in call.kwargs["messages"][0]["content"]


def test_generate_weekly_digest_hides_sdk_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("sk-secret-value")
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client
    monkeypatch.setattr("qa_radar.summarizer.digest.is_available", lambda: True)

    with (
        patch.dict("sys.modules", {"anthropic": fake_anthropic}),
        pytest.raises(RuntimeError, match="LLM 呼び出しに失敗") as error,
    ):
        generate_weekly_digest([])

    assert "sk-secret-value" not in str(error.value)
    assert error.value.__cause__ is None


def test_render_digest_page_converts_minimal_markdown_and_escapes_html() -> None:
    digest = Digest(
        id=1,
        created_at=200,
        period_start=0,
        period_end=86400,
        content_md=(
            "# <script>見出し</script>\n"
            "通常 & 行\n"
            "## テーマ\n"
            "- <img src=x onerror=alert(1)>\n"
            "- 安全な項目"
        ),
    )

    html = render_digest_page(digest, source_count=44)

    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "<h2>&lt;script&gt;見出し&lt;/script&gt;</h2>" in html
    assert "<p>通常 &amp; 行</p>" in html
    assert "<h3>テーマ</h3>" in html
    assert "<ul>" in html and "<li>安全な項目</li>" in html
    assert "44ソース" in html


def test_render_digest_page_shows_placeholder() -> None:
    html = render_digest_page(None)
    assert "ダイジェストはまだ生成されていません" in html
    assert 'href="digest.html"' in html
