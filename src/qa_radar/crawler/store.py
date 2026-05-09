"""DB書き込み層. sources / articles / crawl_runs への upsert と更新."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from qa_radar.sources import SourceConfig


@dataclass
class ArticleRow:
    """`insert_article()` への入力."""

    source_id: int
    guid: str
    url: str
    title: str
    snippet: str
    body_hash: str
    body: str | None
    author: str | None
    published_at: int


def upsert_source(conn: sqlite3.Connection, source: SourceConfig) -> int:
    """sources テーブルに upsert し、source_id を返す.

    slug は UNIQUE なので、既存があれば name/feed_url/言語/カテゴリ/enabled
    を更新する. last_fetched_at 等の状態列は更新しない.
    """
    conn.execute(
        """
        INSERT INTO sources (slug, name, feed_url, site_url, language, category, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            feed_url = excluded.feed_url,
            site_url = excluded.site_url,
            language = excluded.language,
            category = excluded.category,
            enabled = excluded.enabled
        """,
        (
            source.slug,
            source.name,
            source.feed_url,
            source.site_url,
            source.language,
            source.category,
            int(source.enabled),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM sources WHERE slug = ?", (source.slug,)).fetchone()
    return int(row["id"])


def insert_article(conn: sqlite3.Connection, article: ArticleRow) -> bool:
    """記事を articles へ INSERT する. 重複時は False を返し例外を出さない.

    Returns:
        新規挿入で True、UNIQUE 制約による重複で False.
    """
    try:
        conn.execute(
            """
            INSERT INTO articles
                (source_id, guid, url, title, snippet, body_hash, body, author,
                 published_at, fetched_at, tags_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
            """,
            (
                article.source_id,
                article.guid,
                article.url,
                article.title,
                article.snippet,
                article.body_hash,
                article.body,
                article.author,
                article.published_at,
                int(time.time()),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def update_source_fetch_state(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    etag: str | None,
    last_modified: str | None,
    success: bool,
) -> None:
    """sources の取得状態を更新する.

    success=True なら last_fetched_at を更新し consecutive_errors=0 にする.
    success=False なら consecutive_errors を1増やす (退避判定用).
    """
    if success:
        conn.execute(
            """
            UPDATE sources
            SET last_fetched_at = ?, last_etag = ?, last_modified = ?, consecutive_errors = 0
            WHERE id = ?
            """,
            (int(time.time()), etag, last_modified, source_id),
        )
    else:
        conn.execute(
            "UPDATE sources SET consecutive_errors = consecutive_errors + 1 WHERE id = ?",
            (source_id,),
        )
    conn.commit()


def get_source_fetch_state(
    conn: sqlite3.Connection, source_id: int
) -> tuple[str | None, str | None, int | None]:
    """(etag, last_modified, last_fetched_at) を返す. 行が無ければ全て None."""
    row = conn.execute(
        "SELECT last_etag, last_modified, last_fetched_at FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return (None, None, None)
    return (row["last_etag"], row["last_modified"], row["last_fetched_at"])


def start_crawl_run(conn: sqlite3.Connection) -> int:
    """crawl_runs に新規行を作成し ID を返す."""
    cur = conn.execute("INSERT INTO crawl_runs (started_at) VALUES (?)", (int(time.time()),))
    conn.commit()
    rowid = cur.lastrowid
    if rowid is None:  # pragma: no cover
        raise RuntimeError("crawl_runs INSERT後の lastrowid が None")
    return rowid


def finish_crawl_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    sources_processed: int,
    articles_added: int,
    errors: list[dict[str, object]] | None = None,
) -> None:
    """crawl_runs のサマリを更新する."""
    conn.execute(
        """
        UPDATE crawl_runs
        SET finished_at = ?, sources_processed = ?, articles_added = ?, errors_json = ?
        WHERE id = ?
        """,
        (
            int(time.time()),
            sources_processed,
            articles_added,
            json.dumps(errors or [], ensure_ascii=False),
            run_id,
        ),
    )
    conn.commit()
