"""重複検出層. (source_id, guid) UNIQUE と body_hash の2系統."""

from __future__ import annotations

import sqlite3


def is_known(conn: sqlite3.Connection, source_id: int, guid: str) -> bool:
    """同一 (source_id, guid) が既にDBに存在するか.

    一次的な重複検出. UNIQUE 制約に依存するため insert_article() の
    IntegrityError も同等の保護を持つが、こちらは insert 前に判定して
    無駄なクエリを避けたい場合に使う.
    """
    cur = conn.execute(
        "SELECT 1 FROM articles WHERE source_id = ? AND guid = ? LIMIT 1",
        (source_id, guid),
    )
    return cur.fetchone() is not None


def is_cross_source_duplicate(conn: sqlite3.Connection, body_hash: str, source_id: int) -> bool:
    """別ソースに同一 body_hash の記事があるか (転載検出).

    現在は collected メタデータのみで使用予定. Phase 3 以降の RSS 出力で
    重複を抑制する用途を想定.
    """
    cur = conn.execute(
        "SELECT 1 FROM articles WHERE body_hash = ? AND source_id != ? LIMIT 1",
        (body_hash, source_id),
    )
    return cur.fetchone() is not None
