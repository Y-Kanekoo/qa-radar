"""Discord webhook 通知層. embed 形式で記事を送信する.

47条の5境界遵守:
- 出力は title + snippet + 元URL + 出所明示のみ
- 本文 (body) は absolutely 含めない (FeedItem に存在しない)

レート制限:
- Discord webhook はバースト時 1秒/req 程度で安全
- 429 Too Many Requests を受けたら Retry-After に従って再送
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from qa_radar.publisher.rss import FeedItem

logger = logging.getLogger("qa_radar.discord")

DEFAULT_RATE_LIMIT_DELAY = 1.0  # 連続送信時の最小間隔 (秒)
DEFAULT_TIMEOUT = 10.0
DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 2048
DISCORD_FOOTER_LIMIT = 2048
EMBED_COLOR = 0x0969DA  # GitHub blue


def build_embed(item: FeedItem) -> dict[str, object]:
    """FeedItem から Discord embed dict を生成する.

    Discord の各フィールド長制限を遵守する.
    """
    tag_text = " ".join(f"#{t}" for t in item.tags) if item.tags else ""
    description = item.snippet
    if tag_text:
        description = f"{description}\n\n{tag_text}"
    if len(description) > DISCORD_DESCRIPTION_LIMIT:
        description = description[: DISCORD_DESCRIPTION_LIMIT - 1] + "…"

    title = item.title[:DISCORD_TITLE_LIMIT]
    footer_text = item.source_name[:DISCORD_FOOTER_LIMIT]
    if item.author:
        footer_text = f"{footer_text} / {item.author}"[:DISCORD_FOOTER_LIMIT]

    return {
        "title": title,
        "url": item.url,
        "description": description,
        "color": EMBED_COLOR,
        "footer": {"text": footer_text},
        "timestamp": datetime.fromtimestamp(item.published_at, tz=UTC).isoformat(),
    }


def build_payload(item: FeedItem) -> dict[str, object]:
    """webhook に送信する完全なペイロードを生成する."""
    return {"embeds": [build_embed(item)]}


async def send_notification(
    item: FeedItem,
    webhook_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 1,
) -> bool:
    """1記事を Discord webhook に送信する.

    429 Rate Limit を受けた場合は Retry-After に従って max_retries 回再送する.

    Returns:
        成功で True, 永続失敗で False.
    """
    payload = build_payload(item)

    own_client = client is None
    used = client if client is not None else httpx.AsyncClient(timeout=timeout)
    try:
        for attempt in range(max_retries + 1):
            try:
                resp = await used.post(webhook_url, json=payload)
            except httpx.HTTPError as e:
                logger.warning("Discord 送信失敗 (network attempt=%d): %s", attempt, e)
                return False

            if 200 <= resp.status_code < 300:
                return True

            if resp.status_code == 429 and attempt < max_retries:
                # Discord は秒またはJSON形式で retry_after を返す
                retry_after = _parse_retry_after(resp)
                logger.info("Discord rate limited, %ss 待機して再試行", retry_after)
                await asyncio.sleep(retry_after)
                continue

            logger.warning(
                "Discord 送信失敗 status=%d body=%r",
                resp.status_code,
                resp.text[:200],
            )
            return False
        return False
    finally:
        if own_client:
            await used.aclose()


def _parse_retry_after(resp: httpx.Response) -> float:
    """Retry-After ヘッダ or JSON ボディから待機秒を取得する."""
    header = resp.headers.get("retry-after")
    if header is not None:
        try:
            return max(0.5, float(header))
        except ValueError:
            pass
    try:
        body = resp.json()
        return max(0.5, float(body.get("retry_after", 1.0)))
    except (ValueError, TypeError, KeyError):
        return 1.0


async def send_batch(
    items: list[FeedItem],
    webhook_url: str,
    *,
    rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, int]:
    """記事リストを順次 Discord に通知する.

    各送信間に `rate_limit_delay` 秒の sleep を挟む.

    Returns:
        (成功件数, 失敗件数).
    """
    success = 0
    failure = 0
    own_client = client is None
    used = client if client is not None else httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    try:
        for i, item in enumerate(items):
            if i > 0 and rate_limit_delay > 0:
                await asyncio.sleep(rate_limit_delay)
            ok = await send_notification(item, webhook_url, client=used)
            if ok:
                success += 1
            else:
                failure += 1
    finally:
        if own_client:
            await used.aclose()
    return (success, failure)
