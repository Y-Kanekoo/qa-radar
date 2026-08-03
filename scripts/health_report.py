"""週次ソース健全性レポートを生成し、Discordへ送信するCLI.

設計方針: 実フィードへの追加アクセスは行わない。本番 cron (crawl.yml, 1日3回) が
既に DB に書き込んでいる信号 (sources.consecutive_errors, articles の
published_at/fetched_at) を集計するだけで、死活監視の取得経路を二重化しない。

環境変数 `DISCORD_ALERT_WEBHOOK_URL` から webhook URL を読む。未設定なら警告して
Discord送信をスキップし exit 0 で終了する (digest 自体は必ず stdout に出力する)。
送信を試みて失敗した場合は exit 非0.

使用例:
    uv run python scripts/health_report.py
    uv run python scripts/health_report.py --db-path data/articles.db
    DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/... \\
        uv run python scripts/health_report.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from qa_radar.crawler.store import (
    DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
    OverallStats,
    SourceErrorStatus,
    SourceStaleness,
    get_overall_stats,
    get_source_staleness,
    get_sources_with_errors,
)
from qa_radar.db import init_db
from qa_radar.publisher.discord import _parse_retry_after

REPO_ROOT = Path(__file__).resolve().parent.parent

ENV_ALERT_WEBHOOK = "DISCORD_ALERT_WEBHOOK_URL"
DISCORD_CONTENT_LIMIT = 2000
DEFAULT_MAX_RETRIES = 2

# httpx は各リクエストを INFO でログするが、そのメッセージには webhook URL 全文が
# 含まれる (webhook URL は事実上のシークレット)。呼び出し経路 (main() 経由か、
# send_to_discord を直接呼ぶか) によらず必ず抑止されるよう、import 時点で設定する。
logging.getLogger("httpx").setLevel(logging.WARNING)

STALE_WARNING_DAYS = 30
STALE_CRITICAL_DAYS = 90
SECONDS_PER_DAY = 24 * 3600


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar ソース健全性レポート")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "data" / "articles.db",
        help="DB ファイルパス (既定 data/articles.db)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def _format_error_lines(sources: list[SourceErrorStatus]) -> list[str]:
    """consecutive_errors > 0 のソース一覧を表示行に変換する."""
    if not sources:
        return ["  エラー中のソースはありません"]
    lines = []
    for s in sources:
        label = "⚠️危険" if s.consecutive_errors >= DEFAULT_CONSECUTIVE_ERROR_THRESHOLD else "注意"
        lines.append(f"  [{label}] {s.slug} — {s.consecutive_errors}回連続失敗")
    return lines


def _format_staleness_lines(staleness: list[SourceStaleness], *, now: int) -> list[str]:
    """新着停滞の疑いがあるソースのみを抽出して表示行に変換する.

    低頻度ソース (論文誌等) が存在するため、断定的な表現は避け「疑い」「参考情報」の
    トーンに統一する。
    """
    flagged: list[str] = []
    for s in staleness:
        if s.latest_activity_at is None:
            flagged.append(f"  [長期停止疑い] {s.slug} — 記事取得実績なし")
            continue
        age_days = (now - s.latest_activity_at) // SECONDS_PER_DAY
        if age_days >= STALE_CRITICAL_DAYS:
            flagged.append(f"  [長期停止疑い] {s.slug} — 最終更新 {age_days}日前")
        elif age_days >= STALE_WARNING_DAYS:
            flagged.append(f"  [新着なし(注意)] {s.slug} — 最終更新 {age_days}日前")
    if not flagged:
        return ["  該当ソースはありません"]
    return flagged


def build_digest(
    *,
    error_sources: list[SourceErrorStatus],
    staleness: list[SourceStaleness],
    stats: OverallStats,
    db_size_bytes: int,
    now: int | None = None,
) -> str:
    """集計結果から日本語 digest 文字列を組み立てる (副作用なし・純粋関数)."""
    now_ts = now if now is not None else int(time.time())
    report_date = time.strftime("%Y-%m-%d", time.gmtime(now_ts))
    db_size_mb = db_size_bytes / (1024 * 1024)

    lines = [
        f"📊 qa-radar ソース健全性レポート ({report_date})",
        "",
        "■ 全体統計",
        f"  総記事数: {stats.total_articles}件",
        f"  DBファイルサイズ: {db_size_mb:.2f}MB",
        f"  直近7日の新規記事: {stats.recent_7d_count}件",
        "",
        f"■ エラー中のソース ({len(error_sources)}件)",
        *_format_error_lines(error_sources),
        "",
        "■ 新着停滞の疑いがあるソース",
        *_format_staleness_lines(staleness, now=now_ts),
        "",
        "※ 論文誌等の低頻度ソースは元々更新間隔が長いため、"
        "「長期停止疑い」は必ずしも障害を意味しません。参考情報としてご確認ください。",
    ]
    return "\n".join(lines)


def split_for_discord(text: str, *, limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    """Discord の1メッセージ2000文字制限に合わせ、行境界でチャンク分割する.

    1行だけで limit を超える異常系は、その行を安全側で切り詰める。
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(line) > limit:
            # 切り詰めたことが受信側で判別できるよう末尾にマーカーを付ける
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
    used: httpx.Client,
    chunk: str,
    webhook_url: str,
    *,
    index: int,
    total: int,
    max_retries: int,
    log: logging.Logger,
) -> bool:
    """1チャンクを Discord webhook へ POST する (429 は Retry-After に従い再送).

    webhook URL は事実上のシークレットのため、あらゆるログ・例外経路に含めない。
    `httpx.HTTPStatusError` (raise_for_status) は例外メッセージに URL 全文を
    含むため使わず、status_code のみを判定・ログ出力する
    (`src/qa_radar/publisher/discord.py` の `send_notification` と同じ方針)。
    """
    for attempt in range(max_retries + 1):
        try:
            resp = used.post(webhook_url, json={"content": chunk})
        except httpx.HTTPError:
            log.error("Discord 送信失敗 (chunk=%d/%d): ネットワークエラー", index, total)
            return False

        if 200 <= resp.status_code < 300:
            return True

        if resp.status_code == 429 and attempt < max_retries:
            retry_after = _parse_retry_after(resp)
            log.info(
                "Discord rate limited (chunk=%d/%d), %ss 待機して再試行",
                index,
                total,
                retry_after,
            )
            time.sleep(retry_after)
            continue

        log.error(
            "Discord 送信失敗 (chunk=%d/%d): status=%d body=%r",
            index,
            total,
            resp.status_code,
            resp.text[:200],
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
    """digest のチャンクを順に Discord webhook へ POST する.

    Returns:
        全チャンク送信成功で True, いずれか失敗で False.
    """
    log = logging.getLogger("qa_radar.health_report")
    own_client = client is None
    used = client if client is not None else httpx.Client(timeout=10.0)
    try:
        for i, chunk in enumerate(chunks):
            ok = _send_chunk(
                used,
                chunk,
                webhook_url,
                index=i + 1,
                total=len(chunks),
                max_retries=max_retries,
                log=log,
            )
            if not ok:
                return False
        return True
    finally:
        if own_client:
            used.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.health_report")

    if not args.db_path.exists():
        log.error("DB が存在しません: %s", args.db_path)
        return 1

    conn = init_db(args.db_path)
    try:
        error_sources = get_sources_with_errors(conn)
        staleness = get_source_staleness(conn)
        stats = get_overall_stats(conn)
    finally:
        conn.close()

    db_size_bytes = args.db_path.stat().st_size
    digest = build_digest(
        error_sources=error_sources,
        staleness=staleness,
        stats=stats,
        db_size_bytes=db_size_bytes,
    )
    print(digest)

    webhook = os.environ.get(ENV_ALERT_WEBHOOK, "").strip()
    if not webhook:
        log.warning("環境変数 %s が未設定のため Discord 送信をスキップします", ENV_ALERT_WEBHOOK)
        return 0

    chunks = split_for_discord(digest)
    ok = send_to_discord(chunks, webhook)
    if not ok:
        log.error("Discord への健全性レポート送信に失敗しました")
        return 1

    log.info("Discord へ健全性レポートを送信しました (%d チャンク)", len(chunks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
