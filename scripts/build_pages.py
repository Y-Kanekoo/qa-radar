"""GitHub Pages 用のフィード・HTML 一括生成.

DB から記事を読み、`docs/_build/` 配下に `feed.atom`, `feed.xml`, `index.html`,
`sources.html`, `tags.html`, `tags/<tag>.xml` を生成する.

使用例:
    uv run python scripts/build_pages.py
    uv run python scripts/build_pages.py --output docs/_build --limit 100
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from qa_radar.db import init_db
from qa_radar.publisher.pages import (
    render_index,
    render_sources_page,
    render_tags_page,
    write_html,
)
from qa_radar.publisher.queries import (
    fetch_recent_articles,
    fetch_source_summaries,
    fetch_tag_summaries,
)
from qa_radar.publisher.rss import main_feed_url, tag_feed_url, write_feed

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar Pages ビルド")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "data" / "articles.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "docs" / "_build",
        help="生成先ディレクトリ (既定: docs/_build)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="メインフィード/トップに含める最大件数",
    )
    parser.add_argument(
        "--tag-limit",
        type=int,
        default=50,
        help="タグ別フィードに含める最大件数",
    )
    parser.add_argument(
        "--min-tag-count",
        type=int,
        default=1,
        help="タグ別フィードを生成する最小記事数",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.build_pages")

    if not args.db_path.exists():
        log.error("DB が存在しません: %s", args.db_path)
        return 1

    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)

    conn = init_db(args.db_path)
    try:
        articles = fetch_recent_articles(conn, limit=args.limit)
        sources = fetch_source_summaries(conn)
        tags = fetch_tag_summaries(conn, min_count=args.min_tag_count)

        log.info("articles=%d, sources=%d, tags=%d", len(articles), len(sources), len(tags))

        # メインフィード (Atom + RSS)
        write_feed(articles, output / "feed.atom", feed_url=main_feed_url("atom"), fmt="atom")
        write_feed(articles, output / "feed.xml", feed_url=main_feed_url("rss"), fmt="rss")

        # タグ別フィード
        for ts in tags:
            tag_articles = fetch_recent_articles(conn, tag=ts.tag, limit=args.tag_limit)
            write_feed(
                tag_articles,
                output / "tags" / f"{ts.tag}.xml",
                feed_url=tag_feed_url(ts.tag),
                title=f"qa-radar #{ts.tag}",
                subtitle=f"{ts.tag} タグの記事 ({ts.article_count} 件)",
                fmt="atom",
            )

        # HTML
        write_html(render_index(articles), output / "index.html")
        write_html(render_sources_page(sources), output / "sources.html")
        write_html(render_tags_page(tags), output / "tags.html")

        # docs/style.css をコピー (ビルド時の依存性を _build に閉じ込める)
        css_src = REPO_ROOT / "docs" / "style.css"
        if css_src.exists():
            (output / "style.css").write_text(css_src.read_text(encoding="utf-8"), encoding="utf-8")
    finally:
        conn.close()

    log.info("ビルド完了: %s", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
