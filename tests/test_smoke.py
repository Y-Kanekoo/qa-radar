"""Phase 0/5: CI緑化のためのスモークテスト."""

from __future__ import annotations

import qa_radar


def test_version() -> None:
    """パッケージのバージョン文字列が期待通り定義されている."""
    assert qa_radar.__version__ == "0.1.0"


def test_main_callable() -> None:
    """`main()` が callable (Phase 5 で MCP サーバー起動に切替)."""
    assert callable(qa_radar.main)


def test_server_module_imports() -> None:
    """MCP サーバーモジュールがインポートエラーなくロードできる."""
    from qa_radar import server

    assert hasattr(server, "mcp")
    assert hasattr(server, "run_stdio")
    assert server.APP_NAME == "qa-radar"


def test_tools_module_imports() -> None:
    """tools 5本が全てインポート可能."""
    from qa_radar.tools import (
        get_article_impl,
        list_recent_impl,
        list_sources_impl,
        list_tags_impl,
        search_articles_impl,
    )

    assert callable(search_articles_impl)
    assert callable(list_recent_impl)
    assert callable(get_article_impl)
    assert callable(list_sources_impl)
    assert callable(list_tags_impl)
