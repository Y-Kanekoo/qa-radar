"""DB内の全記事にタグを再適用する CLI.

タグルール (config/tag_rules.yaml) を更新した後に走らせる.

使用例:
    uv run python scripts/retag_all.py
    uv run python scripts/retag_all.py --db-path data/articles.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from qa_radar.db import init_db
from qa_radar.tagger.apply import retag_all
from qa_radar.tagger.rules import load_tagger_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar タグ再適用")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "data" / "articles.db",
    )
    parser.add_argument(
        "--rules-yaml",
        type=Path,
        default=REPO_ROOT / "config" / "tag_rules.yaml",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.retag")

    config = load_tagger_config(args.rules_yaml)
    conn = init_db(args.db_path)
    try:
        stats = retag_all(conn, config)
    finally:
        conn.close()

    log.info(
        "再適用完了: total=%d tagged=%d (%.1f%%) untagged=%d",
        stats.total,
        stats.tagged,
        stats.coverage * 100,
        stats.untagged,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
