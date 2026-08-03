"""server.py の MCP プロトコル経由 E2E テスト.

tests/unit/test_server.py は「モジュール属性の存在確認」のみで FastMCP 層
(tool スキーマ, lifespan, JSON-RPC ハンドリング) を一切検証していない.
このファイルは `mcp.shared.memory.create_connected_server_and_client_session()`
を使い, `server.mcp` (FastMCP インスタンス) に in-memory ストリームで
ClientSession を接続, initialize ハンドシェイクを含む実 JSON-RPC でツールを
呼び出すことで, tool スキーマ破壊や lifespan 不具合の回帰を検出する.
"""

from __future__ import annotations

import importlib
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from qa_radar import server
from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig

# ---------------- fixtures (tests/unit/test_tools.py のパターンを流用) ----------------


def _src(slug: str = "s1") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name=f"Source {slug}",
        feed_url=f"https://{slug}.example.com/feed",
        site_url=f"https://{slug}.example.com",
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
        license_note="",
    )


def _article(
    sid: int,
    guid: str,
    *,
    title: str = "Test article",
    body: str = "body text",
    tags: list[str] | None = None,
    published_at: int | None = None,
) -> ArticleRow:
    return ArticleRow(
        source_id=sid,
        guid=guid,
        url=f"https://e.com/{guid}",
        title=title,
        snippet=f"snip-{guid}",
        body_hash=guid,
        body=body,
        author="Alice",
        published_at=published_at if published_at is not None else int(time.time()) - 3600,
        tags=tags or ["e2e"],
    )


@dataclass
class SeededDb:
    """テスト用に投入した記事1件の DB パスと id."""

    path: Path
    article_id: int


def _seed_db(db_path: Path) -> int:
    """DB を作成し記事1件を投入して閉じる. 記事 id を返す."""
    conn = init_db(db_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "g1", title="Playwright tutorial", tags=["e2e"]))
        row = conn.execute("SELECT id FROM articles WHERE guid = 'g1'").fetchone()
        return int(row["id"])
    finally:
        conn.close()


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SeededDb:
    """サンプル記事入りの一時 DB を作り QA_RADAR_DB_PATH をそこへ向ける."""
    db_path = tmp_path / "articles.db"
    article_id = _seed_db(db_path)
    monkeypatch.setenv("QA_RADAR_DB_PATH", str(db_path))
    return SeededDb(path=db_path, article_id=article_id)


# ==================== 1. list_tools: スキーマ検証 ====================


@pytest.mark.asyncio
async def test_list_tools_exposes_five_tools_with_expected_schema(seeded_db: SeededDb) -> None:
    """5 tool が公開され, 引数名・型が期待通り."""
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.list_tools()

    tools = {t.name: t for t in result.tools}
    assert set(tools) == {
        "search_articles",
        "list_recent",
        "get_article",
        "list_sources",
        "list_tags",
    }

    search_schema = tools["search_articles"].inputSchema
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["query"]["type"] == "string"
    assert search_schema["properties"]["limit"]["type"] == "integer"
    assert search_schema["properties"]["offset"]["type"] == "integer"
    tags_types = {opt.get("type") for opt in search_schema["properties"]["tags"]["anyOf"]}
    assert tags_types == {"array", "null"}

    recent_schema = tools["list_recent"].inputSchema
    assert recent_schema["properties"]["days"]["type"] == "integer"
    assert recent_schema["properties"]["limit"]["type"] == "integer"
    source_types = {opt.get("type") for opt in recent_schema["properties"]["source"]["anyOf"]}
    assert source_types == {"string", "null"}

    get_schema = tools["get_article"].inputSchema
    assert get_schema["required"] == ["article_id"]
    assert get_schema["properties"]["article_id"]["type"] == "integer"
    assert get_schema["properties"]["include_body"]["type"] == "boolean"

    assert tools["list_sources"].inputSchema["properties"] == {}

    tags_schema = tools["list_tags"].inputSchema
    assert tags_schema["properties"]["min_count"]["type"] == "integer"
    assert tags_schema["properties"]["limit"]["type"] == "integer"


# ==================== 2. 各ツールの実呼び出し (5ツール全部) ====================


@pytest.mark.asyncio
async def test_call_search_articles(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("search_articles", {"query": "playwright"})

    assert result.isError is False
    assert result.structuredContent is not None
    items = result.structuredContent["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Playwright tutorial"
    assert "body" not in items[0]  # 47条の5境界


@pytest.mark.asyncio
async def test_call_list_recent(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("list_recent", {"days": 1})

    assert result.isError is False
    items = result.structuredContent["result"]
    assert len(items) == 1
    assert items[0]["url"] == "https://e.com/g1"


@pytest.mark.asyncio
async def test_call_get_article(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool(
            "get_article", {"article_id": seeded_db.article_id, "include_body": True}
        )

    assert result.isError is False
    assert result.structuredContent["body"] == "body text"
    assert result.structuredContent["title"] == "Playwright tutorial"


@pytest.mark.asyncio
async def test_call_list_sources(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("list_sources", {})

    assert result.isError is False
    sources = result.structuredContent["result"]
    assert len(sources) == 1
    assert sources[0]["slug"] == "s1"
    assert sources[0]["article_count"] == 1


@pytest.mark.asyncio
async def test_call_list_tags(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("list_tags", {"min_count": 1})

    assert result.isError is False
    tags = result.structuredContent["result"]
    assert {t["tag"] for t in tags} == {"e2e"}


# ==================== 3. lifespan ====================


@pytest.mark.asyncio
async def test_lifespan_raises_when_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA_RADAR_DB_PATH が指すファイルが存在しない場合, 起動 (lifespan) が
    RuntimeError で失敗し, セッション確立自体が失敗する."""
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("QA_RADAR_DB_PATH", str(missing))

    with pytest.raises(ExceptionGroup) as excinfo:
        async with create_connected_server_and_client_session(server.mcp):
            pass

    causes = [e for e in excinfo.value.exceptions if isinstance(e, RuntimeError)]
    assert causes, f"RuntimeError が ExceptionGroup 内に見つかりません: {excinfo.value.exceptions}"
    assert "DB ファイルが存在しません" in str(causes[0])
    assert str(missing) in str(causes[0])


@pytest.mark.asyncio
async def test_lifespan_starts_via_env_path_resolution(seeded_db: SeededDb) -> None:
    """QA_RADAR_DB_PATH 経由で解決したパスに DB が存在すれば起動に成功する."""
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.list_tools()
    assert len(result.tools) >= 5


# ==================== 4. summarize_article の条件付き登録 ====================


@pytest.mark.asyncio
async def test_summarize_article_conditional_registration(
    seeded_db: SeededDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTHROPIC_API_KEY (+ anthropic パッケージ) が揃う時のみ summarize_article
    が tool として登録される. server.py の登録は import 時評価 (server.py:219)
    のため importlib.reload で再評価させる. テスト独立性を壊さないよう
    teardown で必ず元の状態へ reload し直す."""
    original_had_tool = hasattr(server, "summarize_article")
    try:
        # ---- ANTHROPIC_API_KEY 未設定 → 登録されない ----
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(server)
        async with create_connected_server_and_client_session(server.mcp) as session:
            names = {t.name for t in (await session.list_tools()).tools}
        assert "summarize_article" not in names

        # ---- ANTHROPIC_API_KEY 設定 + anthropic import 可能 → 登録される ----
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake-key")
        monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
        importlib.reload(server)

        # 実 API を呼ばないよう summarize 本体をスタブに差し替える
        def _fake_summarize(text: str, *, model: str, max_tokens: int) -> str:
            assert text  # 記事本文が渡ってくること
            return "スタブ要約"

        monkeypatch.setattr(server, "_summarize_text", _fake_summarize)

        async with create_connected_server_and_client_session(server.mcp) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert "summarize_article" in names
            result = await session.call_tool(
                "summarize_article", {"article_id": seeded_db.article_id}
            )

        assert result.isError is False
        assert result.structuredContent["summary"] == "スタブ要約"
        assert result.structuredContent["article_id"] == seeded_db.article_id
    finally:
        monkeypatch.undo()
        importlib.reload(server)
        # importlib.reload() は「以前の実行で定義され, 今回の実行では
        # 定義されない」名前を module 名前空間から自動削除しない (if 分岐が
        # False になっても古い summarize_article 属性が残留する). 元の状態
        # (未登録) に戻すには明示的に消す必要がある.
        if not original_had_tool and hasattr(server, "summarize_article"):
            del server.summarize_article
        assert hasattr(server, "summarize_article") == original_had_tool


# ==================== 5. 異常系 ====================


@pytest.mark.asyncio
async def test_get_article_unknown_id_returns_error(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("get_article", {"article_id": 9999})

    assert result.isError is True
    assert "9999" in result.content[0].text


@pytest.mark.asyncio
async def test_search_articles_empty_query_returns_empty(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("search_articles", {"query": ""})

    assert result.isError is False
    assert result.structuredContent == {"items": [], "has_more": False, "next_offset": None}


@pytest.mark.asyncio
async def test_search_articles_rejects_invalid_limit(seeded_db: SeededDb) -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("search_articles", {"query": "test", "limit": 0})

    assert result.isError is True
