"""未通知記事を Discord webhook に送信する CLI.

環境変数 `DISCORD_WEBHOOK_URL` から webhook URL を読む. 未設定なら警告して終了 (exit 0).

使用例:
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... \\
        uv run python scripts/notify_discord.py
    uv run python scripts/notify_discord.py --limit 10 --rate-delay 1.5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from qa_radar.db import init_db
from qa_radar.publisher.discord import DEFAULT_RATE_LIMIT_DELAY, send_batch
from qa_radar.publisher.notification_state import (
    DISCORD_CHANNEL,
    fetch_unnotified,
    mark_notified,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

ENV_WEBHOOK = "DISCORD_WEBHOOK_URL"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar Discord 通知")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "data" / "articles.db",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="1回の実行で送信する最大件数 (既定 20)",
    )
    parser.add_argument(
        "--rate-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY,
        help="連続送信間隔 (秒)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際には送信せず、対象を表示するだけ",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.notify_discord")

    if not args.db_path.exists():
        log.error("DB が存在しません: %s", args.db_path)
        return 1

    webhook = os.environ.get(ENV_WEBHOOK, "").strip()
    if not webhook and not args.dry_run:
        log.warning("環境変数 %s が未設定のため通知をスキップします", ENV_WEBHOOK)
        return 0

    conn = init_db(args.db_path)
    try:
        targets = fetch_unnotified(conn, limit=args.limit)
        if not targets:
            log.info("未通知記事なし")
            return 0

        log.info("未通知 %d 件", len(targets))

        if args.dry_run:
            for t in targets:
                log.info("[DRY] %s — %s", t.item.title, t.item.url)
            return 0

        items = [(t.article_id, t.item) for t in targets]
        result = asyncio.run(send_batch(items, webhook, rate_limit_delay=args.rate_delay))
        log.info(
            "送信完了: success=%d failure=%d",
            result.success_count,
            result.failure_count,
        )

        # 記事ID単位の成功結果のみ mark する
        # (途中で失敗しても後続の送信は続行するため、先頭からN件という
        #  位置ベースの判定はできない。失敗記事は mark せず再送対象として残す)
        for article_id in result.succeeded_ids:
            mark_notified(conn, article_id, channel=DISCORD_CHANNEL)

        if result.failed_ids:
            log.warning(
                "Discord 送信失敗: %d 件 (article_ids=%s)",
                result.failure_count,
                result.failed_ids,
            )

        return 0 if result.failure_count == 0 else 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
