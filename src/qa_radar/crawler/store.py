"""DB書き込み層. sources / articles / crawl_runs への upsert と更新."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass

from qa_radar.sources import SourceConfig

DEFAULT_CONSECUTIVE_ERROR_THRESHOLD = 9


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
    tags: list[str] | None = None  # None なら空タグで挿入 (Phase 2 から指定)
    duplicate_of: int | None = None


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


def insert_article(
    conn: sqlite3.Connection,
    article: ArticleRow,
    *,
    regrouped_ids: Sequence[int] | None = None,
) -> bool:
    """記事を articles へ INSERT する. 重複時は False を返し例外を出さない.

    Args:
        conn: DB 接続.
        article: 挿入する記事.
        regrouped_ids: 挿入記事を新しい元記事として `duplicate_of` を付け替える
            既存記事の id 群. INSERT と同一トランザクションで UPDATE するため、
            付け替えだけが適用された中途半端な状態にはならない.

    Returns:
        新規挿入で True、UNIQUE 制約による重複で False.
    """
    tags_json = json.dumps(article.tags or [], ensure_ascii=False)
    try:
        cur = conn.execute(
            """
            INSERT INTO articles
                (source_id, guid, url, title, snippet, body_hash, body, author,
                 published_at, fetched_at, tags_json, duplicate_of)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                tags_json,
                article.duplicate_of,
            ),
        )
        if regrouped_ids:
            new_id = cur.lastrowid
            if new_id is None:  # pragma: no cover
                raise RuntimeError("articles INSERT後の lastrowid が None")
            # プレースホルダ数は id 件数から生成するだけで、値は全てバインドする
            placeholders = ", ".join("?" * len(regrouped_ids))
            conn.execute(
                f"UPDATE articles SET duplicate_of = ? WHERE id IN ({placeholders})",
                (new_id, *regrouped_ids),
            )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # INSERT で開いた暗黙トランザクションを畳んでから呼び出し元に戻す.
        conn.rollback()
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


def get_repeatedly_failing_sources(
    conn: sqlite3.Connection,
    *,
    threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
) -> list[tuple[str, int]]:
    """連続失敗回数が閾値以上の有効なソースを返す."""
    rows = conn.execute(
        """
        SELECT slug, consecutive_errors
        FROM sources
        WHERE enabled = 1 AND consecutive_errors >= ?
        ORDER BY slug
        """,
        (threshold,),
    ).fetchall()
    return [(str(row["slug"]), int(row["consecutive_errors"])) for row in rows]


# ---------------------------------------------------------------------------
# 週次ヘルスレポート (scripts/health_report.py) 用の読み取り関数.
#
# 実フィードへ追加でアクセスする死活監視は行わず、本番 cron (crawl.yml) が
# 既に書き込んでいる signal (consecutive_errors / published_at / fetched_at) を
# 集計するだけにとどめる (取得経路の二重化を避ける設計判断)。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceErrorStatus:
    """週次ヘルスレポート用: 連続エラー中のソース1件分."""

    slug: str
    consecutive_errors: int


def get_sources_with_errors(conn: sqlite3.Connection) -> list[SourceErrorStatus]:
    """consecutive_errors > 0 の有効なソースを、エラー回数の多い順に返す.

    `get_repeatedly_failing_sources()` (閾値以上のみ対象) と異なり、1回でも
    連続失敗しているソースを網羅する (週次ヘルスレポートでの注意喚起用途)。
    """
    rows = conn.execute(
        """
        SELECT slug, consecutive_errors
        FROM sources
        WHERE enabled = 1 AND consecutive_errors > 0
        ORDER BY consecutive_errors DESC, slug
        """
    ).fetchall()
    return [
        SourceErrorStatus(slug=str(row["slug"]), consecutive_errors=int(row["consecutive_errors"]))
        for row in rows
    ]


@dataclass(frozen=True)
class SourceStaleness:
    """週次ヘルスレポート用: ソース1件の最終新着状態."""

    slug: str
    # published_at と fetched_at のうち新しい方の、記事間での最大値. 記事が0件なら None.
    latest_activity_at: int | None


def get_source_staleness(conn: sqlite3.Connection) -> list[SourceStaleness]:
    """有効な全ソースについて articles.published_at / fetched_at の最大値を返す.

    「新着なし」「長期停止疑い」等の段階判定は行わない (生の集計値のみ返す).
    判定は呼び出し側 (scripts/health_report.py) が現在時刻と比較して行う.
    """
    rows = conn.execute(
        """
        SELECT s.slug,
               MAX(MAX(a.published_at, a.fetched_at)) AS latest_activity_at
        FROM sources s
        LEFT JOIN articles a ON a.source_id = s.id
        WHERE s.enabled = 1
        GROUP BY s.id
        ORDER BY s.slug
        """
    ).fetchall()
    return [
        SourceStaleness(
            slug=str(row["slug"]),
            latest_activity_at=(
                int(row["latest_activity_at"]) if row["latest_activity_at"] is not None else None
            ),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class OverallStats:
    """週次ヘルスレポート用: DB全体の統計 (記事数系)."""

    total_articles: int
    recent_7d_count: int


def get_overall_stats(conn: sqlite3.Connection, *, now: int | None = None) -> OverallStats:
    """総記事数と、直近7日以内に取得 (fetched_at) された記事数を返す.

    「直近7日の新着」は published_at ではなく fetched_at を基準にする
    (低頻度ソースが古い published_at の記事を後から配信するケースがあり、
    「いつDBに取り込まれたか」の方がクロール健全性の指標として安定するため)。
    """
    now_ts = now if now is not None else int(time.time())
    cutoff = now_ts - 7 * 24 * 3600
    total_row = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()
    recent_row = conn.execute(
        "SELECT COUNT(*) AS c FROM articles WHERE fetched_at >= ?", (cutoff,)
    ).fetchone()
    return OverallStats(
        total_articles=int(total_row["c"]),
        recent_7d_count=int(recent_row["c"]),
    )


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
