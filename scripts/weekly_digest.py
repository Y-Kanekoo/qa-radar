"""直近7日の公開済み記事情報から週刊 LLM ダイジェストを生成・配信する CLI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from qa_radar.db import init_db
from qa_radar.publisher.discord_content import send_to_discord, split_for_discord
from qa_radar.publisher.pages import format_digest_meta
from qa_radar.publisher.queries import fetch_digest_articles, fetch_digest_stats, insert_digest
from qa_radar.summarizer.anthropic_client import ENV_API_KEY, is_available
from qa_radar.summarizer.digest import DigestInput, generate_weekly_digest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_WEBHOOK = "DISCORD_WEBHOOK_URL"
PERIOD_DAYS = 7
ARTICLE_LIMIT = 120
NO_ARTICLES_DIGEST = "# 今週のハイライト\n\n今週は新着なし。"

# httpx の INFO ログには webhook URL が含まれるため、API キーと同じくシークレットとして
# 扱う URL をログ・例外経路に出さないよう import 時点で抑止する。
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar 週刊 LLM ダイジェスト")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "data" / "articles.db",
        help="DB ファイルパス (既定 data/articles.db)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def _set_output(name: str) -> None:
    """GitHub Actions に真偽出力を通知する."""
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"{name}=true\n")


def _set_generated_output() -> None:
    """DB 保存成功後に generated=true を1回だけ書く."""
    _set_output("generated")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.weekly_digest")

    if not is_available():
        log.warning("環境変数 %s が未設定または AI 機能が利用不可のためスキップします", ENV_API_KEY)
        return 0
    if not args.db_path.exists():
        log.error("DB が存在しません: %s", args.db_path)
        return 1

    now = int(time.time())
    period_start = now - PERIOD_DAYS * 24 * 3600
    conn = None
    try:
        conn = init_db(args.db_path)
        stats = fetch_digest_stats(conn, period_start=period_start, period_end=now)
        # 専用クエリは SQL 段階で body を取得しない。さらに
        # build_digest_prompt が許可5項目だけを再選択し、LLM への本文流入を防ぐ。
        items = fetch_digest_articles(
            conn,
            period_start=period_start,
            period_end=now,
            limit=ARTICLE_LIMIT,
        )
        digest_input = DigestInput(items, total_count=stats.article_count)
        content_md = NO_ARTICLES_DIGEST if not items else generate_weekly_digest(digest_input)
        insert_digest(
            conn,
            created_at=now,
            period_start=period_start,
            period_end=now,
            content_md=content_md,
        )
    except Exception:
        # Anthropic の例外本文にはリクエスト情報が含まれる可能性があるため、API キーを
        # 含み得る例外自体はログへ出さず、固定メッセージで失敗を通知する。
        log.error("週刊ダイジェストの生成または DB 保存に失敗しました")
        return 1
    finally:
        if conn is not None:
            conn.close()

    meta = format_digest_meta(
        period_start=period_start,
        period_end=now,
        content_md=content_md,
        article_count=stats.article_count,
        source_count=stats.source_count,
    )
    published_content = f"📡 qa-radar 週刊 LLM ダイジェスト\n{meta}\n\n{content_md}"
    print(published_content)
    _set_generated_output()

    webhook_url = os.environ.get(ENV_WEBHOOK, "").strip()
    if not webhook_url:
        log.info("環境変数 %s が未設定のため Discord 配信をスキップします", ENV_WEBHOOK)
        return 0
    if not send_to_discord(split_for_discord(published_content), webhook_url):
        _set_output("discord_failed")
        log.error("Discord 配信に失敗しました。ダイジェストは DB 保存済みです")
        return 1

    log.info("週刊ダイジェストを Discord へ配信しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
