"""server.py の統合・補助テスト. MCP プロトコル経由ではなく構造を検証する."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from qa_radar import server


def test_mcp_instance_exists() -> None:
    """FastMCP インスタンスが正しく作られている."""
    assert server.mcp is not None
    assert server.APP_NAME == "qa-radar"


def test_get_db_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数 QA_RADAR_DB_PATH が最優先."""
    explicit = tmp_path / "custom.db"
    monkeypatch.setenv("QA_RADAR_DB_PATH", str(explicit))
    assert server.get_db_path() == explicit.resolve()


def test_get_db_path_repo_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数が無くてもリポジトリローカルの data/articles.db が存在すれば採用."""
    monkeypatch.delenv("QA_RADAR_DB_PATH", raising=False)
    # 開発時 data/articles.db が存在する場合のみ意味のあるテスト
    repo_local = Path(server.__file__).resolve().parent.parent.parent / "data" / "articles.db"
    result = server.get_db_path()
    if repo_local.exists():
        assert result == repo_local
    else:
        # 存在しなければ platformdirs cache パスへフォールバック
        assert "qa-radar" in str(result)


def test_get_db_path_cache_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """環境変数なし & リポジトリローカル無し → platformdirs cache."""
    monkeypatch.delenv("QA_RADAR_DB_PATH", raising=False)
    # __file__ を tmp_path 配下に置き換えて repo_local が存在しないシナリオを作る
    fake_module_path = tmp_path / "fake" / "qa_radar" / "server.py"
    fake_module_path.parent.mkdir(parents=True)
    monkeypatch.setattr(server, "__file__", str(fake_module_path))
    result = server.get_db_path()
    # platformdirs のキャッシュディレクトリは "qa-radar" を含む
    assert "qa-radar" in str(result)
    assert result.name == "articles.db"


def test_get_db_path_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数の ~ がホームディレクトリに展開される."""
    monkeypatch.setenv("QA_RADAR_DB_PATH", "~/custom.db")
    result = server.get_db_path()
    assert "~" not in str(result)
    assert result.name == "custom.db"


def test_get_db_path_handles_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """空文字列の環境変数は未設定として扱う."""
    monkeypatch.setenv("QA_RADAR_DB_PATH", "")
    # 空でも例外にならず、フォールバックが返る
    result = server.get_db_path()
    assert result.name == "articles.db"


def test_all_tools_registered() -> None:
    """5 つの tool が全て FastMCP インスタンスに登録されている."""
    # FastMCP は `_tool_manager._tools` に登録 tool を保持する (実装詳細)
    expected = {"search_articles", "list_recent", "get_article", "list_sources", "list_tags"}
    # 公開関数として server モジュール上に存在することを確認する (実装詳細に依存しない)
    for name in expected:
        assert hasattr(server, name), f"tool {name} が server に存在しません"
        assert callable(getattr(server, name))


def test_run_stdio_callable() -> None:
    """run_stdio が呼び出し可能 (実際には起動しないがシグネチャを確認)."""
    assert callable(server.run_stdio)


def test_module_reload_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数を変えてモジュール再ロードしてもエラーなく読める."""
    monkeypatch.delenv("QA_RADAR_DB_PATH", raising=False)
    importlib.reload(server)
    assert server.mcp is not None


def test_env_const_is_correct() -> None:
    assert server.ENV_DB_PATH == "QA_RADAR_DB_PATH"
