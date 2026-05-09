"""統合テスト: 実フィードへの接続. `--integration` 指定時のみ実行される."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa_radar.crawler.orchestrator import run_crawl
from qa_radar.db import init_db
from qa_radar.sources import load_blocked, load_sources


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_arxiv_crawl(tmp_path: Path) -> None:
    """arxiv cs.SE の実フィードに正常接続できることを確認.

    arxiv は週末 (土日) は更新がないため、articles_added は0〜数十件と振れる.
    ここでは「エラーなく取得できる」ことのみ検証する.
    """
    sources = [s for s in load_sources() if s.slug == "arxiv-cs-se"]
    assert len(sources) == 1

    blocked = load_blocked()
    conn = init_db(tmp_path / "test.db")
    try:
        result = await run_crawl(conn, sources, blocked, concurrency=1)
    finally:
        conn.close()

    assert result.sources_processed == 1
    assert not result.errors


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_github_releases_crawl(tmp_path: Path) -> None:
    """GitHub releases.atom を取得 (Playwright)."""
    sources = [s for s in load_sources() if s.slug == "playwright-releases"]
    assert len(sources) == 1

    conn = init_db(tmp_path / "test.db")
    try:
        result = await run_crawl(
            conn,
            sources,
            load_blocked(),
            concurrency=1,
        )
    finally:
        conn.close()

    assert result.sources_processed == 1
    assert result.articles_added >= 1
    assert not result.errors
