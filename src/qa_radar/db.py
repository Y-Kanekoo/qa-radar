"""SQLite + FTS5 のスキーマ管理と接続オープン.

スキーマは init_db() で冪等に作成される. 重要な設計判断:

- WAL モード: 並列読み取り（クローラー実行中に MCP も同DBを開く想定）
- 外部コンテンツ FTS5 (`content='articles'`): ストレージ二重持ちを避け、トリガで同期
- `tokenize='trigram'`: 3文字以上の日本語・英語を部分一致検索. 3文字未満の語を
    含むクエリは検索ツール側で FTS と LIKE を組み合わせる.
- `schema_version` テーブル: 将来のマイグレーション用バージョン番号を保持

**注意 (foot-gun)**: `_SCHEMA_SQL` は **新規 DB の作成にしか使われない**.
既存 DB には一切流れないため、テーブル・インデックス・トリガを追加するときは
`_SCHEMA_SQL` と `MIGRATIONS` の **両方** に書くこと. 片方だけだと、新規 DB では
存在するのに既存 DB には永久に作られないオブジェクトが生まれる.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 4  # v4: FTS5 を trigram トークナイザで再構築

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
    duplicate_of INTEGER REFERENCES articles(id),
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
    tokenize='trigram'
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

Migration = Callable[[sqlite3.Connection], None]


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 から v2 へ通知状態テーブルを追加する."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_notifications (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            channel TEXT NOT NULL,
            notified_at INTEGER NOT NULL,
            UNIQUE(article_id, channel)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_channel ON article_notifications(channel)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_article ON article_notifications(article_id)"
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 から v3 へ転載元記事 ID 列を追加する."""
    conn.execute("ALTER TABLE articles ADD COLUMN duplicate_of INTEGER REFERENCES articles(id)")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v3 から v4 へ FTS5 を trigram トークナイザで再構築する.

    同期トリガは articles テーブルに属し、FTS テーブルを DROP しても残る。呼び出し元の
    BEGIN IMMEDIATE が同時書き込みを防ぐため、DROP から再作成までの間にトリガが発火する
    こともない。再作成後に articles 本体の全件を rebuild する。
    """
    conn.execute("DROP TABLE articles_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE articles_fts USING fts5(
            title, body, tags_json,
            content='articles', content_rowid='id',
            tokenize='trigram'
        )
        """
    )
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")


# キーは適用後のバージョン。将来の変更も version: migration の形で逐次追加する。
# 新しいオブジェクトを足すときは _SCHEMA_SQL への追記だけで済ませないこと (冒頭の注意参照)。
MIGRATIONS: dict[int, Migration] = {
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
}


def _get_schema_version(conn: sqlite3.Connection) -> int | None:
    """バージョンを返す. schema_version テーブル自体が無い新規 DB では None.

    Raises:
        RuntimeError: テーブルはあるのにバージョン行が無い場合. 空の新規 DB と
            区別できずに最新スキーマを刻むと、既存 articles に列が追加されないまま
            最新版と記録されて自己修復不能になるため、明示的に異常として止める.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        raise RuntimeError(
            "schema_version テーブルにバージョン行がありません. DB が破損している可能性が"
            "あります. GitHub Releases の data-* スナップショットから復元してください "
            "(uv run python scripts/publish_release.py --mode download "
            "--repo Y-Kanekoo/qa-radar --download-to data/articles.db)."
        )
    return int(row["version"])


def _apply_migrations(conn: sqlite3.Connection, current_version: int) -> bool:
    """current_version の次から SCHEMA_VERSION まで逐次適用する.

    別プロセス (常駐 MCP サーバ等) が同時に init_db を実行しても二重適用しないよう、
    書き込みロックを取る `BEGIN IMMEDIATE` で開始し、トランザクション内でバージョンを
    読み直してから適用する. この接続で1件以上適用した場合は True を返す.
    """
    applied = False
    for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS.get(target_version)
        if migration is None:
            raise RuntimeError(
                f"スキーマ v{target_version} へのマイグレーションが登録されていません"
            )
        # sqlite3 は DDL の前に暗黙 BEGIN しないため、明示的に開始する。
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest_version = _get_schema_version(conn)
            if latest_version is not None and latest_version >= target_version:
                # 直前に別プロセスが適用済み。二重適用を避けて次へ進む。
                conn.rollback()
                continue
            migration(conn)
            conn.execute("UPDATE schema_version SET version = ?", (target_version,))
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            applied = True
    return applied


def init_db(path: Path) -> sqlite3.Connection:
    """DBファイルを開きスキーマを冪等に適用する.

    親ディレクトリが無ければ作成する. WAL モードで開く.

    Args:
        path: DBファイルのパス. 通常 `data/articles.db`.

    Returns:
        オープンした sqlite3.Connection. 利用後は呼び出し側で `close()` する.

    Raises:
        RuntimeError: DB がコードより新しい場合、マイグレーションが未登録の場合、
            または schema_version テーブルにバージョン行が無い破損状態の場合.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")

    try:
        current_version = _get_schema_version(conn)
        if current_version is None:
            # schema_version テーブル自体が無い = 完全な新規 DB。最新 CREATE 文で
            # 直接作成し、過去のマイグレーションを経ない。
            # (テーブルはあるが行が無いケースは _get_schema_version が RuntimeError)
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()
        elif current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"スキーマバージョン不一致: DB={current_version} > コード={SCHEMA_VERSION}. "
                "より新しいコードでDBが作られている可能性があります."
            )
        else:
            migrations_applied = _apply_migrations(conn, current_version)
            if migrations_applied:
                # DROP した旧 FTS の free page を配布スナップショットに残さない。
                # VACUUM はトランザクション内では実行できないため、全適用後に1回だけ行う。
                conn.execute("VACUUM")
    except Exception:
        conn.close()
        raise
    return conn
