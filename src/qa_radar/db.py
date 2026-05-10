"""SQLite + FTS5 のスキーマ管理と接続オープン.

スキーマは init_db() で冪等に作成される. 重要な設計判断:

- WAL モード: 並列読み取り（クローラー実行中に MCP も同DBを開く想定）
- 外部コンテンツ FTS5 (`content='articles'`): ストレージ二重持ちを避け、トリガで同期
- `tokenize='porter unicode61 remove_diacritics 2'`:
    英語は porter stemming, アクセント記号は除去. 日本語は分かち書きしないが
    タイトル・タグの完全一致検索は機能する.
- `schema_version` テーブル: 将来のマイグレーション用バージョン番号を保持
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2  # v2: article_notifications テーブルを追加 (Phase 4)

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    site_url TEXT,
    language TEXT NOT NULL,
    category TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fetched_at INTEGER,
    last_etag TEXT,
    last_modified TEXT,
    consecutive_errors INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    guid TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    snippet TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    body TEXT,
    author TEXT,
    published_at INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(source_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_body_hash ON articles(body_hash);

-- 外部コンテンツ FTS5: 列名は articles テーブルの列名と完全一致させる必要がある.
-- (FTS5 が articles テーブルから直接列を読み取るため)
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, body, tags_json,
    content='articles', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, title, body, tags_json)
  VALUES (new.id, new.title, COALESCE(new.body, ''), new.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, body, tags_json)
  VALUES('delete', old.id, old.title, COALESCE(old.body, ''), old.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, body, tags_json)
  VALUES('delete', old.id, old.title, COALESCE(old.body, ''), old.tags_json);
  INSERT INTO articles_fts(rowid, title, body, tags_json)
  VALUES (new.id, new.title, COALESCE(new.body, ''), new.tags_json);
END;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    sources_processed INTEGER DEFAULT 0,
    articles_added INTEGER DEFAULT 0,
    errors_json TEXT
);

-- v2 (Phase 4): 記事通知状態. channel ごとに既送信を追跡する.
CREATE TABLE IF NOT EXISTS article_notifications (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    channel TEXT NOT NULL,
    notified_at INTEGER NOT NULL,
    UNIQUE(article_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_notifications_channel ON article_notifications(channel);
CREATE INDEX IF NOT EXISTS idx_notifications_article ON article_notifications(article_id);
"""


def init_db(path: Path) -> sqlite3.Connection:
    """DBファイルを開きスキーマを冪等に適用する.

    親ディレクトリが無ければ作成する. WAL モードで開く.

    Args:
        path: DBファイルのパス. 通常 `data/articles.db`.

    Returns:
        オープンした sqlite3.Connection. 利用後は呼び出し側で `close()` する.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)

    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    elif row["version"] < SCHEMA_VERSION:
        # 前方マイグレーション: 新テーブルは CREATE TABLE IF NOT EXISTS で既に作成済み.
        # 列追加が無いバージョン差分なら version 番号の更新のみで十分.
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.commit()
    elif row["version"] > SCHEMA_VERSION:
        raise RuntimeError(
            f"スキーマバージョン不一致: DB={row['version']} > コード={SCHEMA_VERSION}. "
            "より新しいコードでDBが作られている可能性があります."
        )
    return conn
