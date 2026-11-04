"""Discord webhook へプレーンテキストを同期送信する共有部品."""

from __future__ import annotations

import logging
import time

import httpx

from qa_radar.publisher.discord import parse_retry_after

DISCORD_CONTENT_LIMIT = 2000
DEFAULT_MAX_RETRIES = 2

logger = logging.getLogger("qa_radar.discord_content")


def split_for_discord(text: str, *, limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    """Discord の文字数制限に合わせ、行境界で本文を分割する.

    1行だけで ``limit`` を超える場合は、切り詰めを示す省略記号を末尾に付ける。
    """
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
    index: int,
    total: int,
    max_retries: int,
) -> bool:
    """1チャンクを送信し、429 の場合だけ再試行する."""
    for attempt in range(max_retries + 1):
        try:
            response = client.post(webhook_url, json={"content": chunk})
        except (httpx.HTTPError, httpx.InvalidURL):
            # InvalidURL は HTTPError を継承しない。どちらも URL を含み得る例外本文は
            # ログへ出さず、固定文言に閉じ込める。
            logger.error("Discord 送信失敗 (chunk=%d/%d): ネットワークエラー", index, total)
            return False

        if 200 <= response.status_code < 300:
            return True

        if response.status_code == 429 and attempt < max_retries:
            retry_after = parse_retry_after(response)
            logger.info(
                "Discord rate limited (chunk=%d/%d), %ss 待機して再試行",
                index,
                total,
                retry_after,
            )
            time.sleep(retry_after)
            continue

        logger.error(
            "Discord 送信失敗 (chunk=%d/%d): status=%d body=%r",
            index,
            total,
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
        for index, chunk in enumerate(chunks, start=1):
            if not _send_chunk(
                used,
                chunk,
                webhook_url,
                index=index,
                total=len(chunks),
                max_retries=max_retries,
            ):
                return False
        return True
    finally:
        if own_client:
            used.close()
