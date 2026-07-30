"""qa-radar クローラー実行スクリプト.

使用例:
    uv run python scripts/run_crawl.py
    uv run python scripts/run_crawl.py --db-path data/articles.db
    uv run python scripts/run_crawl.py --source playwright-releases
    uv run python scripts/run_crawl.py --concurrency 3 -v
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from qa_radar.crawler.orchestrator import run_crawl
from qa_radar.crawler.store import get_repeatedly_failing_sources
from qa_radar.db import init_db
from qa_radar.sources import load_blocked, load_sources

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar クローラー実行")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "data" / "articles.db",
        help="DB ファイルパス (既定 data/articles.db)",
    )
    parser.add_argument(
        "--sources-yaml",
        type=Path,
        default=REPO_ROOT / "config" / "sources.yaml",
        help="ソース定義 YAML",
    )
    parser.add_argument(
        "--blocked-yaml",
        type=Path,
        default=REPO_ROOT / "config" / "blocked.yaml",
        help="ブロックリスト YAML",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="指定 slug のソースのみ取得",
    )
    parser.add_argument("--concurrency", type=int, default=5, help="並列度")
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG ログを出す")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("qa_radar.run_crawl")

    sources = load_sources(args.sources_yaml)
    if args.source:
        sources = [s for s in sources if s.slug == args.source]
        if not sources:
            log.error("ソースが見つかりません: %s", args.source)
            return 1

    blocked = load_blocked(args.blocked_yaml)

    log.info("クロール開始: %d ソース, DB=%s", len(sources), args.db_path)
    conn = init_db(args.db_path)
    try:
        result = asyncio.run(run_crawl(conn, sources, blocked, concurrency=args.concurrency))
        repeatedly_failing_sources = get_repeatedly_failing_sources(conn)
    finally:
        conn.close()

    log.info(
        "完了: 処理=%d, 追加=%d, エラー=%d",
        result.sources_processed,
        result.articles_added,
        len(result.errors),
    )
    for err in result.errors:
        log.warning("  %s", err)

    for slug, consecutive_errors in repeatedly_failing_sources:
        print(
            "::warning title=Source failing repeatedly::"
            f"{slug} が {consecutive_errors} 回連続で失敗しています"
        )

    error_count = len(result.errors)
    if result.sources_processed > 0 and error_count == result.sources_processed:
        print(
            f"::error title=Crawl total failure::全 {result.sources_processed} ソースが失敗しました"
        )
        return 1

    if error_count > 0:
        failed_slugs = ",".join(str(err["slug"]) for err in result.errors)
        print(
            "::warning title=Crawl partial failure::"
            f"{error_count}/{result.sources_processed} sources failed: {failed_slugs}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
