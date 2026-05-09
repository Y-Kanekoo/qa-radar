"""DB上の記事にタグを適用する."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from qa_radar.tagger.engine import assign_tags
from qa_radar.tagger.rules import TaggerConfig

logger = logging.getLogger("qa_radar.tagger")


@dataclass
class RetagStats:
    """`retag_all()` の結果."""

    total: int
    tagged: int
    untagged: int

    @property
    def coverage(self) -> float:
        """タグ付与率 (0.0〜1.0)."""
        return 0.0 if self.total == 0 else self.tagged / self.total


def update_article_tags(conn: sqlite3.Connection, article_id: int, tags: list[str]) -> None:
    """記事の tags_json 列を更新する."""
    conn.execute(
        "UPDATE articles SET tags_json = ? WHERE id = ?",
        (json.dumps(tags, ensure_ascii=False), article_id),
    )


def retag_all(conn: sqlite3.Connection, config: TaggerConfig) -> RetagStats:
    """DB内の全記事にタグを再適用する.

    各記事のソース slug を結合して `source_tags` を反映できるようにする.

    Args:
        conn: SQLite 接続.
        config: TaggerConfig.

    Returns:
        統計.
    """
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.body, s.slug
        FROM articles a JOIN sources s ON a.source_id = s.id
        """
    ).fetchall()
    total = 0
    tagged = 0
    for row in rows:
        tags = assign_tags(row["title"], row["body"] or "", config, source_slug=row["slug"])
        update_article_tags(conn, row["id"], tags)
        total += 1
        if tags:
            tagged += 1
    conn.commit()
    untagged = total - tagged
    logger.info(
        "retag完了: total=%d tagged=%d (%.1f%%) untagged=%d",
        total,
        tagged,
        (tagged / total * 100 if total else 0.0),
        untagged,
    )
    return RetagStats(total=total, tagged=tagged, untagged=untagged)
