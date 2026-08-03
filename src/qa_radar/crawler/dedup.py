"""重複検出層. (source_id, guid) UNIQUE と body_hash の2系統."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from qa_radar.crawler.normalize import collapse_whitespace

# 極短本文は定型文同士のハッシュ衝突が起きやすいため転載判定から除外する。
MIN_BODY_LENGTH_FOR_DEDUP = 200


def normalize_body_for_dedup(body: str | None) -> str:
    """HTML 除去済み本文をハッシュと文字数ガード用に正規化する."""
    return collapse_whitespace(body or "")


def is_normalized_body_eligible_for_dedup(normalized_body: str) -> bool:
    """正規化本文が転載判定の最低文字数を満たすか返す."""
    return len(normalized_body) >= MIN_BODY_LENGTH_FOR_DEDUP


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


@dataclass(frozen=True)
class CrossSourceDecision:
    """転載判定の結果.

    Attributes:
        original_id: 挿入する記事に設定する `duplicate_of`.
            None なら挿入記事自身が元記事 (非重複) として保存される.
        regrouped_ids: 挿入記事を新しい元記事として付け替える既存記事の id 群.
            挿入記事が既存グループのどれよりも古い場合にのみ空でなくなる.
    """

    original_id: int | None = None
    regrouped_ids: tuple[int, ...] = ()


def resolve_cross_source_original(
    conn: sqlite3.Connection,
    body_hash: str,
    source_id: int,
    published_at: int,
) -> CrossSourceDecision:
    """別ソースにある同一本文グループと、挿入記事の関係を決定する.

    元記事は「公開日時が最も古い記事」と定義する. クロール順は並列実行と
    ネットワーク遅延に左右されるため、**転載を先に取り込んだ後に本家が来る**
    ケースが普通に起きる. そのため既存グループだけを見るのではなく、いま挿入
    しようとしている記事の published_at も比較に含める:

    - 既存の元記事が挿入記事と同時刻かそれより古い → 挿入記事を重複としてマーク
      (公開日時が同値なら先に保存された既存側を元記事として優先する)
    - 挿入記事の方が古い → 挿入記事を元記事とし、既存グループ全体の
      `duplicate_of` を挿入記事へ付け替える (`regrouped_ids`)
    - 別ソースに元記事候補が無い → どちらの処理も行わない

    Args:
        conn: DB 接続.
        body_hash: 挿入記事の正規化本文ハッシュ.
        source_id: 挿入記事のソース id. 同一ソース内は guid 重複で守られるため除外する.
        published_at: 挿入記事の公開日時 (UNIX 秒).

    Returns:
        CrossSourceDecision.
    """
    rows = conn.execute(
        """
        SELECT id, published_at, duplicate_of
        FROM articles
        WHERE body_hash = ? AND source_id != ?
        ORDER BY published_at ASC, id ASC
        """,
        (body_hash, source_id),
    ).fetchall()
    if not rows:
        return CrossSourceDecision()

    original = next((row for row in rows if row["duplicate_of"] is None), None)
    if original is None:
        # 別ソース側が全て重複マーク済み (元記事は挿入記事と同一ソース側にある) 状態.
        # 付け替えの基準が無いため何もしない.
        return CrossSourceDecision()

    if int(original["published_at"]) <= published_at:
        return CrossSourceDecision(original_id=int(original["id"]))

    # 挿入記事が既存グループの最古より古い → グループ全体を挿入記事の配下に付け替える.
    return CrossSourceDecision(regrouped_ids=tuple(int(row["id"]) for row in rows))
