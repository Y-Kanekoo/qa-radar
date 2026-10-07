"""通知状態 (article_notifications テーブル) の管理層."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from qa_radar.publisher.rss import FeedItem

logger = logging.getLogger("qa_radar.notification_state")

DISCORD_CHANNEL = "discord"


@dataclass(frozen=True)
class UnnotifiedArticle:
    """通知対象記事. article_id を持つ FeedItem ペア."""

    article_id: int
    item: FeedItem


def fetch_unnotified(
    conn: sqlite3.Connection,
    *,
    channel: str = DISCORD_CHANNEL,
    limit: int = 100,
    since_unix: int | None = None,
    exclude_source_slugs: frozenset[str] = frozenset(),
    exclude_notified_channels: frozenset[str] = frozenset(),
    exclude_held_technology_watch: bool = False,
) -> list[UnnotifiedArticle]:
    """指定 channel に未通知の記事を取得する.

    body 列は SELECT しない (47条の5境界).

    Args:
        conn: SQLite 接続.
        channel: 通知チャネル名 (既定 'discord').
        limit: 最大取得件数.
        since_unix: 指定するとこの時刻以降に fetch された記事のみ返す.
        exclude_held_technology_watch: 送達不明で手動照合中の記事を除く.

    Returns:
        UnnotifiedArticle のリスト. published_at DESC 順.
    """
    where_extra = ""
    all_channels = (channel, *sorted(exclude_notified_channels - {channel}))
    channel_placeholders = ", ".join("?" for _ in all_channels)
    params: list[object] = list(all_channels)
    if since_unix is not None:
        where_extra = "AND a.fetched_at >= ?"
        params.append(since_unix)
    if exclude_source_slugs:
        placeholders = ", ".join("?" for _ in exclude_source_slugs)
        where_extra += f" AND s.slug NOT IN ({placeholders})"
        params.extend(sorted(exclude_source_slugs))
    if exclude_held_technology_watch:
        where_extra += """
        AND NOT EXISTS (
            SELECT 1 FROM technology_delivery_attempts t
            WHERE t.article_id = a.id AND t.excluded_reason = 'manual_reconciliation'
        )
        """

    sql = f"""
        SELECT a.id, a.url, a.title, a.snippet, a.author, a.published_at, a.tags_json,
               s.name AS source_name
        FROM articles a
        JOIN sources s ON a.source_id = s.id
        WHERE NOT EXISTS (
            SELECT 1 FROM article_notifications n
            WHERE n.article_id = a.id AND n.channel IN ({channel_placeholders})
        )
        AND a.duplicate_of IS NULL
        {where_extra}
        ORDER BY a.published_at DESC
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    result: list[UnnotifiedArticle] = []
    for r in rows:
        tags = tuple(json.loads(r["tags_json"]) or [])
        item = FeedItem(
            id=r["url"],
            url=r["url"],
            title=r["title"],
            snippet=r["snippet"],
            source_name=r["source_name"],
            author=r["author"],
            published_at=int(r["published_at"]),
            tags=tags,
        )
        result.append(UnnotifiedArticle(article_id=int(r["id"]), item=item))
    return result


def mark_notified(
    conn: sqlite3.Connection,
    article_id: int,
    *,
    channel: str = DISCORD_CHANNEL,
) -> None:
    """記事を通知済みとしてマークする. 既にマークされていれば何もしない (UNIQUE)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO article_notifications (article_id, channel, notified_at)
        VALUES (?, ?, ?)
        """,
        (article_id, channel, int(time.time())),
    )
    conn.commit()


def mark_notified_bulk(
    conn: sqlite3.Connection,
    article_ids: list[int],
    *,
    channel: str = DISCORD_CHANNEL,
) -> int:
    """複数記事を通知済みとして一括マークする. 実際に挿入された件数を返す."""
    if not article_ids:
        return 0
    now = int(time.time())
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO article_notifications (article_id, channel, notified_at)
        VALUES (?, ?, ?)
        """,
        [(aid, channel, now) for aid in article_ids],
    )
    conn.commit()
    return cur.rowcount or 0
