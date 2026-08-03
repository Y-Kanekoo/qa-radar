"""MCP tool 実装 (pure functions).

MCP プロトコルに依存しない純粋関数として実装する. server.py が FastMCP の
`@mcp.tool()` デコレータでラップして公開する.

設計:
- 47条の5境界: `_row_to_card` は body 列を含めない. get_article で
  `include_body=True` のみ body を含める.
- BM25 重み: title=5.0, body=1.0, tags=2.0 (タイトル/タグマッチを優先)
- date_from/date_to は ISO8601 文字列 (例: "2024-01-15", "2024-01-15T00:00:00Z")
- snippet ハイライト機能は将来追加 (現状は格納済みの snippet をそのまま返す)
- 転載重複 (`articles.duplicate_of IS NOT NULL`) の扱い:
    一覧・集計系 (`list_recent` / `list_sources` / `list_tags`) は RSS/Pages と
    同じ件数になるよう **除外する**. `search_articles` だけはコーパス全体の発見性を
    優先して除外しない. `get_article` は id 直接指定なので常に返す.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

# 検索 BM25 重み
BM25_WEIGHT_TITLE = 5.0
BM25_WEIGHT_BODY = 1.0
BM25_WEIGHT_TAGS = 2.0
MAX_SEARCH_TERMS = 50


def _fts5_safe_query(query: str) -> str:
    """ユーザクエリを安全な FTS5 式に変換する.

    各単語をダブルクオートで囲み、複数語は AND として連結する.
    FTS5 のメタ文字 (アスタリスク、プラス、マイナス、括弧、ダブルクオート) は
    クオートで無効化される. 入力中のダブルクオートは2連続化してエスケープ.
    """
    terms = query.split()
    if not terms:
        return '""'  # 空クエリ
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _escape_like_term(term: str) -> str:
    """LIKE の検索語に含まれるエスケープ文字とワイルドカードを無効化する."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _iso_to_unix(iso: str) -> int:
    """ISO8601 文字列を unix 秒へ変換. Z 表記もサポート."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return int(datetime.fromisoformat(iso).timestamp())


def _unix_to_iso(unix_seconds: int) -> str:
    """unix 秒を ISO8601 (UTC) 文字列に変換."""
    return datetime.fromtimestamp(unix_seconds, tz=UTC).isoformat()


def _row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    """記事行を公開可能な dict に変換する. **body は含まない**."""
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "url": row["url"],
        "snippet": row["snippet"],
        "author": row["author"],
        "published_at": _unix_to_iso(int(row["published_at"])),
        "source_name": row["source_name"],
        "tags": json.loads(row["tags_json"]) or [],
    }


# ---------------- search_articles ----------------


def search_articles_impl(
    conn: sqlite3.Connection,
    query: str,
    *,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """記事を全文検索する.

    全語が3文字以上なら FTS5 + BM25、長語と短語が混在する場合は3文字以上の
    語を FTS5、3文字未満の語を LIKE として AND 検索し、BM25 順で返す。全語が
    3文字未満の場合は LIKE のみを使い、公開日時の降順で返す。

    LIKE で扱う3文字未満の語は部分一致であり、語境界を見ない。
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit は 1〜100 の範囲で指定してください")
    if offset < 0:
        raise ValueError("offset は 0 以上で指定してください")

    # MCP 検索はコーパス全体の発見性を優先し、転載重複も意図的に除外しない。
    terms = query.split()
    if len(terms) > MAX_SEARCH_TERMS:
        raise ValueError(f"検索クエリの語数が多すぎます(上限{MAX_SEARCH_TERMS}語)")

    long_terms = [term for term in terms if len(term) >= 3]
    short_terms = [term for term in terms if len(term) < 3]
    where: list[str] = []
    params: list[Any] = []

    if not terms or long_terms:
        where.append("articles_fts MATCH ?")
        params.append(_fts5_safe_query(" ".join(long_terms) if terms else query))
        from_clause = (
            "articles_fts JOIN articles a ON a.id = articles_fts.rowid "
            "JOIN sources s ON a.source_id = s.id"
        )
        order_by = (
            f"bm25(articles_fts, {BM25_WEIGHT_TITLE}, {BM25_WEIGHT_BODY}, {BM25_WEIGHT_TAGS})"
        )
    else:
        from_clause = "articles a JOIN sources s ON a.source_id = s.id"
        order_by = "a.published_at DESC, a.id DESC"

    # trigram では3文字未満の語を検索できない。混在時は長語の FTS 絞り込みを維持し、
    # 短語だけを LIKE にする。全短語時の約2千件規模の全走査は許容済み。
    for term in short_terms:
        where.append(
            "(a.title LIKE ? ESCAPE '\\' OR a.body LIKE ? ESCAPE '\\' "
            "OR a.tags_json LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{_escape_like_term(term)}%"
        params.extend([pattern, pattern, pattern])

    if date_from:
        where.append("a.published_at >= ?")
        params.append(_iso_to_unix(date_from))
    if date_to:
        where.append("a.published_at <= ?")
        params.append(_iso_to_unix(date_to))
    if tags:
        where.append("(" + " AND ".join(["a.tags_json LIKE ? ESCAPE '\\'"] * len(tags)) + ")")
        params.extend([f'%"{_escape_like_term(tag)}"%' for tag in tags])

    where_clause = " AND ".join(where)
    sql = f"""
        SELECT a.id, a.title, a.url, a.snippet, a.author, a.published_at, a.tags_json,
               s.name AS source_name
        FROM {from_clause}
        WHERE {where_clause}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    # limit+1 を取得して has_more を判定
    rows = conn.execute(sql, [*params, limit + 1, offset]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_row_to_card(r) for r in rows],
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


# ---------------- list_recent ----------------


def list_recent_impl(
    conn: sqlite3.Connection,
    *,
    days: int = 1,
    source: str | None = None,
    tag: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """直近 N 日の新着記事を新しい順で返す.

    RSS/Pages の一覧と同じ内容になるよう、転載重複 (duplicate_of) は除外する.
    重複も含めて探したい場合は `search_articles` を使う.
    """
    if not 1 <= days <= 365:
        raise ValueError("days は 1〜365 の範囲で指定してください")
    if not 1 <= limit <= 100:
        raise ValueError("limit は 1〜100 の範囲で指定してください")

    since = int(datetime.now(tz=UTC).timestamp()) - days * 86400
    where: list[str] = ["a.duplicate_of IS NULL", "a.published_at >= ?"]
    params: list[Any] = [since]

    if source:
        where.append("s.slug = ?")
        params.append(source)
    if tag:
        where.append("a.tags_json LIKE ? ESCAPE '\\'")
        params.append(f'%"{_escape_like_term(tag)}"%')

    where_clause = " AND ".join(where)
    sql = f"""
        SELECT a.id, a.title, a.url, a.snippet, a.author, a.published_at, a.tags_json,
               s.name AS source_name
        FROM articles a JOIN sources s ON a.source_id = s.id
        WHERE {where_clause}
        ORDER BY a.published_at DESC
        LIMIT ?
    """
    rows = conn.execute(sql, [*params, limit]).fetchall()
    return [_row_to_card(r) for r in rows]


# ---------------- get_article ----------------


def get_article_impl(
    conn: sqlite3.Connection,
    article_id: int,
    *,
    include_body: bool = False,
) -> dict[str, Any]:
    """記事 ID から詳細を取得する.

    `include_body=False` (既定) では body 列を返さない. ローカル MCP からの
    自分用利用に限り、`include_body=True` で取得可能.

    Raises:
        ValueError: 指定 ID が存在しない場合.
    """
    columns = (
        "a.id, a.title, a.url, a.snippet, a.author, a.published_at, a.tags_json, "
        "s.name AS source_name"
    )
    if include_body:
        columns += ", a.body"

    sql = f"""
        SELECT {columns}
        FROM articles a JOIN sources s ON a.source_id = s.id
        WHERE a.id = ?
    """
    row = conn.execute(sql, [article_id]).fetchone()
    if row is None:
        raise ValueError(f"記事 id={article_id} は存在しません")
    card = _row_to_card(row)
    if include_body:
        card["body"] = row["body"]
    return card


# ---------------- list_sources ----------------


def list_sources_impl(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """集約しているソース一覧を返す. 各ソースの記事数 / 最終取得日を含む.

    件数は Pages の sources.html と揃えるため転載重複を除外して数える.
    """
    sql = """
        SELECT s.slug, s.name, s.site_url, s.language, s.category, s.enabled,
               COUNT(a.id) AS article_count,
               MAX(a.published_at) AS latest_at
        FROM sources s LEFT JOIN articles a
          ON a.source_id = s.id AND a.duplicate_of IS NULL
        GROUP BY s.id
        ORDER BY article_count DESC, s.slug
    """
    rows = conn.execute(sql).fetchall()
    return [
        {
            "slug": r["slug"],
            "name": r["name"],
            "site_url": r["site_url"],
            "language": r["language"],
            "category": r["category"],
            "enabled": bool(r["enabled"]),
            "article_count": int(r["article_count"]),
            "latest_at": _unix_to_iso(int(r["latest_at"])) if r["latest_at"] else None,
        }
        for r in rows
    ]


# ---------------- list_tags ----------------


def list_tags_impl(
    conn: sqlite3.Connection,
    *,
    min_count: int = 5,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """利用可能なタグの一覧と各タグの記事数を返す.

    `min_count` 未満のロングテールは除外する.
    件数は Pages のタグ集計と揃えるため転載重複を除外して数える.
    """
    if min_count < 1:
        raise ValueError("min_count は 1 以上で指定してください")
    if not 1 <= limit <= 500:
        raise ValueError("limit は 1〜500 の範囲で指定してください")

    sql = """
        SELECT je.value AS tag, COUNT(*) AS cnt
        FROM articles a, json_each(a.tags_json) je
        WHERE a.duplicate_of IS NULL
        GROUP BY tag
        HAVING cnt >= ?
        ORDER BY cnt DESC, tag
        LIMIT ?
    """
    rows = conn.execute(sql, [min_count, limit]).fetchall()
    return [{"tag": r["tag"], "count": int(r["cnt"])} for r in rows]
