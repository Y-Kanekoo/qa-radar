"""db.init_db() の冪等性とスキーマ生成."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import qa_radar.db as db_module
from qa_radar.db import SCHEMA_VERSION, init_db

_V2_SCHEMA_SQL = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);
INSERT INTO schema_version(version) VALUES (2);

CREATE TABLE sources (
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

CREATE TABLE articles (
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

CREATE TABLE article_notifications (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    channel TEXT NOT NULL,
    notified_at INTEGER NOT NULL,
    UNIQUE(article_id, channel)
);
CREATE INDEX idx_notifications_channel ON article_notifications(channel);
CREATE INDEX idx_notifications_article ON article_notifications(article_id);
"""


def _create_v2_db(path: Path) -> None:
    """テスト内に固定した v2 DDL で既存 DB を作る."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V2_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO sources
                (id, slug, name, feed_url, language, category)
            VALUES (1, 'legacy', 'Legacy', 'https://example.com/feed', 'en', 'blog')
            """
        )
        conn.execute(
            """
            INSERT INTO articles
                (id, guid, source_id, url, title, snippet, body_hash, body,
                 published_at, fetched_at)
            VALUES
                (10, 'legacy-guid', 1, 'https://example.com/article',
                 'Legacy article', 'Legacy snippet', 'legacy-hash', 'Legacy body',
                 1700000000, 1700000100)
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    """sources / articles / crawl_runs / schema_version がすべて生成される."""
    conn = init_db(tmp_path / "test.db")
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r["name"] for r in rows}
        assert "sources" in names
        assert "articles" in names
        assert "crawl_runs" in names
        assert "schema_version" in names
    finally:
        conn.close()


def test_init_db_creates_fts5_virtual_table(tmp_path: Path) -> None:
    """articles_fts (FTS5 virtual table) が生成される."""
    conn = init_db(tmp_path / "test.db")
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE name='articles_fts'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_schema_version_recorded(tmp_path: Path) -> None:
    """初回実行で schema_version が SCHEMA_VERSION に設定される."""
    conn = init_db(tmp_path / "test.db")
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION
    finally:
        conn.close()


def test_new_db_has_duplicate_of_column_from_initial_schema(tmp_path: Path) -> None:
    """新規 DB はマイグレーションなしで v3 列を持つ."""
    conn = init_db(tmp_path / "test.db")
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert "duplicate_of" in columns
        assert version == 3
    finally:
        conn.close()


def test_v2_db_migrates_to_v3_without_data_loss(tmp_path: Path) -> None:
    """v2 の既存記事を保ったまま duplicate_of 列を追加する."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)

    conn = init_db(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        article = conn.execute("SELECT * FROM articles WHERE id = 10").fetchone()
        assert "duplicate_of" in columns
        assert version == 3
        assert article["guid"] == "legacy-guid"
        assert article["body"] == "Legacy body"
        assert article["duplicate_of"] is None
    finally:
        conn.close()


def test_migrations_are_applied_sequentially_through_dummy_v4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2→v3 の後に一時登録した v4 が順番に適用される."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    applied: list[int] = []

    def migrate_to_v4(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        assert "duplicate_of" in columns
        conn.execute("ALTER TABLE articles ADD COLUMN migration_probe INTEGER")
        applied.append(4)

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 4)
    monkeypatch.setitem(db_module.MIGRATIONS, 4, migrate_to_v4)

    conn = init_db(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert applied == [4]
        assert "migration_probe" in columns
        assert version == 4
    finally:
        conn.close()


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DDL 途中の失敗時も列とバージョン更新を残さない."""
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    conn.close()

    def failing_migration(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE articles ADD COLUMN unfinished INTEGER")
        raise RuntimeError("意図した失敗")

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 4)
    monkeypatch.setitem(db_module.MIGRATIONS, 4, failing_migration)

    with pytest.raises(RuntimeError, match="意図した失敗"):
        init_db(db_path)

    raw_conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in raw_conn.execute("PRAGMA table_info(articles)")}
        version = raw_conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert "unfinished" not in columns
        assert version == 3
    finally:
        raw_conn.close()


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    """コードより新しい DB を前方保護で拒否する."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    raw_conn.execute("UPDATE schema_version SET version = 4")
    raw_conn.commit()
    raw_conn.close()

    with pytest.raises(RuntimeError, match=r"DB=4 > コード=3"):
        init_db(db_path)


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    """既存DBで init_db を再実行しても問題なく動作する."""
    db_path = tmp_path / "test.db"
    conn1 = init_db(db_path)
    conn1.close()
    conn2 = init_db(db_path)
    try:
        row = conn2.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION
    finally:
        conn2.close()


def test_init_db_creates_parent_dir(tmp_path: Path) -> None:
    """親ディレクトリが無くても自動作成される."""
    db_path = tmp_path / "subdir" / "nested" / "test.db"
    assert not db_path.parent.exists()
    conn = init_db(db_path)
    try:
        assert db_path.parent.exists()
    finally:
        conn.close()


def test_articles_fts_trigger_on_insert(tmp_path: Path) -> None:
    """記事を INSERT すると FTS5 にも自動で同期される."""
    conn = init_db(tmp_path / "test.db")
    try:
        conn.execute(
            "INSERT INTO sources (slug, name, feed_url, language, category) "
            "VALUES ('s', 'S', 'https://e.com/feed', 'en', 'blog')"
        )
        conn.execute(
            "INSERT INTO articles (source_id, guid, url, title, snippet, body_hash, "
            "body, published_at, fetched_at) "
            "VALUES (1, 'g1', 'https://e.com/1', 'Hello world', 'snip', 'h', "
            "'Hello world body', 1700000000, 1700000000)"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM articles_fts WHERE articles_fts MATCH 'hello'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()
