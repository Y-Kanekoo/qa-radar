"""tagger/apply.py のユニットテスト."""

from __future__ import annotations

import json
from pathlib import Path

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig
from qa_radar.tagger.apply import retag_all, update_article_tags
from qa_radar.tagger.rules import TaggerConfig, TagRule, load_tagger_config


def _src() -> SourceConfig:
    return SourceConfig(
        slug="t",
        name="T",
        feed_url="https://example.com/feed",
        site_url=None,
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
        license_note="",
    )


def _article(source_id: int, guid: str, title: str, body: str = "") -> ArticleRow:
    return ArticleRow(
        source_id=source_id,
        guid=guid,
        url=f"https://example.com/{guid}",
        title=title,
        snippet=title[:50],
        body_hash=guid,  # ユニーク化
        body=body,
        author=None,
        published_at=1700000000,
    )


def test_update_article_tags(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", "Test"))
        # まず tags が空であることを確認
        row = conn.execute("SELECT id, tags_json FROM articles").fetchone()
        article_id = row["id"]
        assert json.loads(row["tags_json"]) == []
        # 更新
        update_article_tags(conn, article_id, ["e2e", "tooling"])
        conn.commit()
        row = conn.execute("SELECT tags_json FROM articles WHERE id = ?", (article_id,)).fetchone()
        assert json.loads(row["tags_json"]) == ["e2e", "tooling"]
    finally:
        conn.close()


def test_retag_all_processes_all_articles(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", "Playwright tutorial", ""))
        insert_article(conn, _article(sid, "g2", "Jest vs Vitest", ""))
        insert_article(conn, _article(sid, "g3", "ランチログ", "今日はラーメン"))

        config = load_tagger_config()
        stats = retag_all(conn, config)
        assert stats.total == 3
        # Playwright と Jest/Vitest はタグが付き、ランチログは付かない
        assert stats.tagged == 2
        assert stats.untagged == 1
        assert 0.5 < stats.coverage < 1.0
    finally:
        conn.close()


def test_retag_empty_db(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "test.db")
    try:
        config = load_tagger_config()
        stats = retag_all(conn, config)
        assert stats.total == 0
        assert stats.coverage == 0.0
    finally:
        conn.close()


def test_retag_with_custom_config(tmp_path: Path) -> None:
    """カスタム TaggerConfig での挙動."""
    conn = init_db(tmp_path / "test.db")
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", "Hello fooQuux world", ""))

        config = TaggerConfig(
            rules=(TagRule(tag="custom", keywords=("fooquux",), requires_co_tag=False),),
            co_occurrence=(),
            source_tags=(),
            max_tags=3,
            threshold=2,
            weight_title=2,
            weight_body=1,
        )
        stats = retag_all(conn, config)
        assert stats.tagged == 1
        row = conn.execute("SELECT tags_json FROM articles").fetchone()
        assert json.loads(row["tags_json"]) == ["custom"]
    finally:
        conn.close()
