"""直近7日の公開済み記事情報から週刊 LLM ダイジェストを生成・配信する CLI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from qa_radar.db import init_db
from qa_radar.publisher.discord import _parse_retry_after
from qa_radar.publisher.queries import insert_digest
from qa_radar.summarizer.anthropic_client import ENV_API_KEY, is_available
from qa_radar.summarizer.digest import generate_weekly_digest
from qa_radar.tools import list_recent_impl

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_WEBHOOK = "DISCORD_WEBHOOK_URL"
DISCORD_CONTENT_LIMIT = 2000
DEFAULT_MAX_RETRIES = 2
PERIOD_DAYS = 7
ARTICLE_LIMIT = 120
NO_ARTICLES_DIGEST = "# 今週のハイライト\n\n今週は新着なし。\n\n## 件数サマリ\n\n- 新着記事: 0件"

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


def split_for_discord(text: str, *, limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    """Discord の文字数制限に合わせ、行境界で本文を分割する."""
    chunks: list[str] = []
    current = ""
    for original_line in text.split("\n"):
        line = original_line
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _send_chunk(
    client: httpx.Client,
    chunk: str,
    webhook_url: str,
    *,
    max_retries: int,
) -> bool:
    """Discord へ1チャンクを送り、429 の場合だけ再試行する."""
    log = logging.getLogger("qa_radar.weekly_digest")
    for attempt in range(max_retries + 1):
        try:
            response = client.post(webhook_url, json={"content": chunk})
        except httpx.HTTPError:
            log.error("Discord 送信失敗: ネットワークエラー")
            return False
        if 200 <= response.status_code < 300:
            return True
        if response.status_code == 429 and attempt < max_retries:
            retry_after = _parse_retry_after(response)
            log.info("Discord rate limited, %ss 待機して再試行", retry_after)
            time.sleep(retry_after)
            continue
        log.error(
            "Discord 送信失敗: status=%d body=%r",
            response.status_code,
            response.text[:200],
        )
        return False
    return False


def send_to_discord(
    chunks: list[str],
    webhook_url: str,
    *,
    client: httpx.Client | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> bool:
    """全チャンクを Discord webhook へ順番に送信する."""
    own_client = client is None
    used = client if client is not None else httpx.Client(timeout=10.0)
    try:
        return all(
            _send_chunk(used, chunk, webhook_url, max_retries=max_retries) for chunk in chunks
        )
    finally:
        if own_client:
            used.close()


def _set_generated_output(generated: bool) -> None:
    """GitHub Actions にダイジェスト生成有無を通知する."""
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"generated={'true' if generated else 'false'}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.weekly_digest")
    _set_generated_output(False)

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
        # list_recent_impl の公開カードは SQL 段階で body を取得しない。さらに
        # build_digest_prompt が許可5項目だけを再選択し、LLM への本文流入を防ぐ。
        items = list_recent_impl(conn, days=PERIOD_DAYS, limit=ARTICLE_LIMIT)
        content_md = NO_ARTICLES_DIGEST if not items else generate_weekly_digest(items)
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

    print(content_md)
    _set_generated_output(True)

    webhook_url = os.environ.get(ENV_WEBHOOK, "").strip()
    if not webhook_url:
        log.info("環境変数 %s が未設定のため Discord 配信をスキップします", ENV_WEBHOOK)
        return 0
    if not send_to_discord(split_for_discord(content_md), webhook_url):
        log.error(
            "Discord 配信に失敗しました。ダイジェストは DB 保存済みのため次回 Pages に反映されます"
        )
        return 1

    log.info("週刊ダイジェストを Discord へ配信しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
