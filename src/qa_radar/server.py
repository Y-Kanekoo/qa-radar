"""qa-radar MCP サーバー (FastMCP / stdio).

Tool 5本を公開:
    search_articles, list_recent, get_article, list_sources, list_tags

DB パス解決優先度:
    1. 環境変数 QA_RADAR_DB_PATH
    2. リポジトリローカル data/articles.db (開発用)
    3. platformdirs.user_cache_dir/qa-radar/articles.db (本番、Phase 6で配布)

Claude Desktop / Claude Code への登録例 (.mcp.json):
    {
      "mcpServers": {
        "qa-radar": {
          "command": "uvx",
          "args": ["qa-radar"],
          "env": {"QA_RADAR_DB_PATH": "/path/to/articles.db"}
        }
      }
    }
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import platformdirs
from mcp.server.fastmcp import Context, FastMCP

from qa_radar.db import init_db
from qa_radar.tools import (
    get_article_impl,
    list_recent_impl,
    list_sources_impl,
    list_tags_impl,
    search_articles_impl,
)

logger = logging.getLogger("qa_radar.mcp")

ENV_DB_PATH = "QA_RADAR_DB_PATH"
APP_NAME = "qa-radar"


def get_db_path() -> Path:
    """DB ファイルのパスを優先度順に解決する."""
    env = os.environ.get(ENV_DB_PATH, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    repo_local = Path(__file__).resolve().parent.parent.parent / "data" / "articles.db"
    if repo_local.exists():
        return repo_local
    return Path(platformdirs.user_cache_dir(APP_NAME)) / "articles.db"


@dataclass
class AppContext:
    """MCP lifespan で共有される実行コンテキスト."""

    db: sqlite3.Connection


@asynccontextmanager
async def lifespan(_mcp: FastMCP) -> AsyncIterator[AppContext]:
    """サーバー起動時に DB を開き、終了時に閉じる."""
    db_path = get_db_path()
    if not db_path.exists():
        # ヒントを stderr に出力 (stdout は JSON-RPC 用なので汚さない)
        print(
            f"qa-radar: DB が見つかりません: {db_path}\n"
            "ヒント:\n"
            "  - QA_RADAR_DB_PATH 環境変数で DB ファイルパスを指定する\n"
            "  - もしくは uv run python scripts/run_crawl.py を実行して DB を生成する",
            file=sys.stderr,
        )
        raise RuntimeError(f"DB ファイルが存在しません: {db_path}")

    logger.info("MCP サーバー起動: db=%s", db_path)
    conn = init_db(db_path)
    try:
        yield AppContext(db=conn)
    finally:
        conn.close()
        logger.info("MCP サーバー停止")


mcp = FastMCP(APP_NAME, lifespan=lifespan)


@mcp.tool()
def search_articles(
    ctx: Context,
    query: str,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """QA/テスト自動化に関する記事を全文検索する.

    トピックに関する質問に使う. 「今週の記事」「最近の話題」のような時間軸の
    問合せには list_recent を優先すること.

    Args:
        query: 検索キーワード. スペース区切りで複数語を AND 検索.
        tags: タグでフィルタ. 例: ["e2e", "ai-testing"].
        date_from: ISO8601 文字列で from 日時 (例 "2024-01-15").
        date_to: ISO8601 文字列で to 日時.
        limit: 返却件数 (1〜100, 既定 20).
        offset: ページング用オフセット.

    Returns:
        {items: [{id, title, url, snippet, ...}], has_more, next_offset}.
        BM25 でランキング (title 重み5、tags 2、body 1).
    """
    db = ctx.request_context.lifespan_context.db
    return search_articles_impl(
        db,
        query,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def list_recent(
    ctx: Context,
    days: int = 1,
    source: str | None = None,
    tag: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """直近 N 日の新着記事を新しい順で返す.

    「今日の新着」「今週のQAニュース」のような時間軸の問合せに使う.

    Args:
        days: 何日前までを含めるか (1〜365, 既定 1).
        source: ソース slug でフィルタ. 例: "playwright-releases".
        tag: タグでフィルタ. 例: "e2e".
        limit: 返却件数 (1〜100, 既定 30).

    Returns:
        記事リスト (published_at DESC).
    """
    db = ctx.request_context.lifespan_context.db
    return list_recent_impl(db, days=days, source=source, tag=tag, limit=limit)


@mcp.tool()
def get_article(
    ctx: Context,
    article_id: int,
    include_body: bool = False,
) -> dict[str, Any]:
    """記事 ID から詳細を取得する.

    Args:
        article_id: search_articles / list_recent の戻り値の `id`.
        include_body: True で本文を含む. **既定 False** (47条の5境界).
            ローカル MCP からの自分用利用に限り True 推奨.

    Returns:
        記事1件の dict. include_body=True 時に "body" キーが追加される.
    """
    db = ctx.request_context.lifespan_context.db
    return get_article_impl(db, article_id, include_body=include_body)


@mcp.tool()
def list_sources(ctx: Context) -> list[dict[str, Any]]:
    """集約しているソース一覧を返す.

    各ソースの記事数・最終取得日・カテゴリを含む.

    Returns:
        ソースリスト (記事数の多い順).
    """
    db = ctx.request_context.lifespan_context.db
    return list_sources_impl(db)


@mcp.tool()
def list_tags(
    ctx: Context,
    min_count: int = 5,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """利用可能なタグの一覧と各タグの記事数を返す.

    Args:
        min_count: この数未満のロングテールを除外 (既定 5).
        limit: 返却件数 (1〜500, 既定 100).

    Returns:
        [{tag, count}, ...] のリスト (件数の多い順).
    """
    db = ctx.request_context.lifespan_context.db
    return list_tags_impl(db, min_count=min_count, limit=limit)


def run_stdio() -> None:
    """MCP サーバーを stdio トランスポートで起動する."""
    mcp.run(transport="stdio")
