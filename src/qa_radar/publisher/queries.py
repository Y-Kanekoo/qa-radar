"""DB 上の articles を Publisher 用データクラスに変換するクエリ層.

publisher/rss.py と pages.py の両方から呼ばれる. SQL を一箇所にまとめ、
本文 (body) を絶対にロードしないようにすることで、47条の5境界を担保する.
"""

from __future__ import annotations

import json
import sqlite3

from qa_radar.publisher.pages import SourceSummary, TagSummary
from qa_radar.publisher.rss import FeedItem


def fetch_recent_articles(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    tag: str | None = None,
) -> list[FeedItem]:
    """最近の記事を `published_at DESC` で取得する.

    body 列は **絶対にロードしない** (公開境界).
    """
    if tag:
        sql = """
            SELECT
                a.url, a.title, a.snippet, a.author, a.published_at, a.tags_json,
                s.name AS source_name
            FROM articles a JOIN sources s ON a.source_id = s.id
            WHERE a.tags_json LIKE ?
            ORDER BY a.published_at DESC
            LIMIT ?
        """
        rows = conn.execute(sql, [f'%"{tag}"%', limit]).fetchall()
    else:
        sql = """
            SELECT
                a.url, a.title, a.snippet, a.author, a.published_at, a.tags_json,
                s.name AS source_name
            FROM articles a JOIN sources s ON a.source_id = s.id
            ORDER BY a.published_at DESC
            LIMIT ?
        """
        rows = conn.execute(sql, [limit]).fetchall()

    items: list[FeedItem] = []
    for r in rows:
        tags = tuple(json.loads(r["tags_json"]) or [])
        items.append(
            FeedItem(
                id=r["url"],
                url=r["url"],
                title=r["title"],
                snippet=r["snippet"],
                source_name=r["source_name"],
                author=r["author"],
                published_at=int(r["published_at"]),
                tags=tags,
            )
        )
    return items


def fetch_source_summaries(conn: sqlite3.Connection) -> list[SourceSummary]:
    """sources.html 用にソースと件数・最終公開日を取得."""
    sql = """
        SELECT s.slug, s.name, s.site_url, s.language, s.category,
               COUNT(a.id) AS article_count,
               MAX(a.published_at) AS latest
        FROM sources s LEFT JOIN articles a ON a.source_id = s.id
        GROUP BY s.id
        ORDER BY article_count DESC, s.slug
    """
    rows = conn.execute(sql).fetchall()
    return [
        SourceSummary(
            slug=r["slug"],
            name=r["name"],
            site_url=r["site_url"],
            language=r["language"],
            category=r["category"],
            article_count=int(r["article_count"]),
            latest_published_at=int(r["latest"]) if r["latest"] is not None else None,
        )
        for r in rows
    ]


def fetch_tag_summaries(conn: sqlite3.Connection, *, min_count: int = 1) -> list[TagSummary]:
    """タグ別件数を集計する.

    tags_json は JSON 配列. SQLite の json_each で展開する.
    """
    sql = """
        SELECT je.value AS tag, COUNT(*) AS cnt
        FROM articles a, json_each(a.tags_json) je
        GROUP BY tag
        HAVING cnt >= ?
        ORDER BY cnt DESC, tag
    """
    rows = conn.execute(sql, [min_count]).fetchall()
    return [TagSummary(tag=r["tag"], article_count=int(r["cnt"])) for r in rows]
