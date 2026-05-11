"""qa-radar — QA/テスト自動化に関する記事を集約するMCPサーバー兼RSSアグリゲーター."""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "main"]


def main() -> None:
    """`qa-radar` スクリプトと `python -m qa_radar` のエントリポイント.

    MCP サーバーを stdio で起動する. DB が存在しない場合はヒント付きエラーを
    stderr に出力して終了する.
    """
    from qa_radar.server import run_stdio

    run_stdio()
