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

CREATE INDEX idx_articles_published ON articles(published_at DESC);
CREATE INDEX idx_articles_source ON articles(source_id);
CREATE INDEX idx_articles_body_hash ON articles(body_hash);

CREATE VIRTUAL TABLE articles_fts USING fts5(
    title, body, tags_json,
    content='articles', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, title, body, tags_json)
  VALUES (new.id, new.title, COALESCE(new.body, ''), new.tags_json);
END;

CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, body, tags_json)
  VALUES('delete', old.id, old.title, COALESCE(old.body, ''), old.tags_json);
END;

CREATE TRIGGER articles_au AFTER UPDATE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, body, tags_json)
  VALUES('delete', old.id, old.title, COALESCE(old.body, ''), old.tags_json);
  INSERT INTO articles_fts(rowid, title, body, tags_json)
  VALUES (new.id, new.title, COALESCE(new.body, ''), new.tags_json);
END;

CREATE TABLE crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    sources_processed INTEGER DEFAULT 0,
    articles_added INTEGER DEFAULT 0,
    errors_json TEXT
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


# v1 = article_notifications 導入前の実スキーマ (FTS5・トリガ・crawl_runs は当時から存在)
_V1_SCHEMA_SQL = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);
INSERT INTO schema_version(version) VALUES (1);

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

CREATE INDEX idx_articles_published ON articles(published_at DESC);
CREATE INDEX idx_articles_source ON articles(source_id);
CREATE INDEX idx_articles_body_hash ON articles(body_hash);

CREATE VIRTUAL TABLE articles_fts USING fts5(
    title, body, tags_json,
    content='articles', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, title, body, tags_json)
  VALUES (new.id, new.title, COALESCE(new.body, ''), new.tags_json);
END;

CREATE TABLE crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    sources_processed INTEGER DEFAULT 0,
    articles_added INTEGER DEFAULT 0,
    errors_json TEXT
);
"""

_LEGACY_ROWS_SQL = """
INSERT INTO sources (id, slug, name, feed_url, language, category)
VALUES (1, 'legacy', 'Legacy', 'https://example.com/feed', 'en', 'blog');

INSERT INTO articles
    (id, guid, source_id, url, title, snippet, body_hash, body, published_at, fetched_at)
VALUES
    (10, 'legacy-guid', 1, 'https://example.com/article',
     'Legacy article', 'Legacy snippet', 'legacy-hash', 'Legacy body',
     1700000000, 1700000100);
"""


def _create_v1_db(path: Path) -> None:
    """テスト内に固定した v1 DDL で既存 DB を作る."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA_SQL)
        conn.executescript(_LEGACY_ROWS_SQL)
        conn.commit()
    finally:
        conn.close()


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


def _create_v3_db(path: Path) -> None:
    """porter FTS と日本語記事を含む実スキーマ相当の v3 DB を作る."""
    _create_v2_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN duplicate_of INTEGER REFERENCES articles(id)")
        conn.execute("UPDATE schema_version SET version = 3")
        conn.execute(
            "UPDATE articles SET title = ?, body = ?, tags_json = ? WHERE id = 10",
            (
                "品質保証の実践",
                "継続的なソフトウェアテストと自動化を紹介します",
                '["テスト自動化"]',
            ),
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


def test_new_db_uses_v4_schema_with_trigram_fts(tmp_path: Path) -> None:
    """新規 DB はマイグレーションなしで v4 列と trigram FTS を持つ."""
    conn = init_db(tmp_path / "test.db")
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'articles_fts'"
        ).fetchone()["sql"]
        assert "duplicate_of" in columns
        assert version == 4
        assert "tokenize='trigram'" in fts_sql
    finally:
        conn.close()


def test_v2_db_migrates_through_v3_to_v4_without_data_loss(tmp_path: Path) -> None:
    """v2 の既存記事を保ったまま duplicate_of と trigram FTS を追加する."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)

    conn = init_db(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        article = conn.execute("SELECT * FROM articles WHERE id = 10").fetchone()
        assert "duplicate_of" in columns
        assert version == 4
        assert article["guid"] == "legacy-guid"
        assert article["body"] == "Legacy body"
        assert article["duplicate_of"] is None
    finally:
        conn.close()


def test_v3_db_migrates_to_v4_and_rebuilds_trigram_fts(tmp_path: Path) -> None:
    """v3 の記事を保ったまま FTS を trigram で再構築し、トリガも保持する."""
    db_path = tmp_path / "test.db"
    _create_v3_db(db_path)

    old_conn = sqlite3.connect(db_path)
    try:
        old_hits = old_conn.execute(
            "SELECT COUNT(*) FROM articles_fts WHERE articles_fts MATCH 'テスト'"
        ).fetchone()[0]
        assert old_hits == 0  # porter FTS では和文中の部分一致にならない
    finally:
        old_conn.close()

    conn = init_db(db_path)
    try:
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        article = conn.execute("SELECT * FROM articles WHERE id = 10").fetchone()
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'articles_fts'"
        ).fetchone()["sql"]
        triggers = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'articles_a_'"
            )
        }
        test_hits = conn.execute(
            "SELECT COUNT(*) AS c FROM articles_fts WHERE articles_fts MATCH 'テスト'"
        ).fetchone()["c"]
        automation_hits = conn.execute(
            "SELECT COUNT(*) AS c FROM articles_fts WHERE articles_fts MATCH '自動化'"
        ).fetchone()["c"]

        assert version == 4
        assert article["guid"] == "legacy-guid"
        assert article["title"] == "品質保証の実践"
        assert article["body"] == "継続的なソフトウェアテストと自動化を紹介します"
        assert article["tags_json"] == '["テスト自動化"]'
        assert article["duplicate_of"] is None
        assert "tokenize='trigram'" in fts_sql
        assert triggers == {"articles_ai", "articles_ad", "articles_au"}
        assert test_hits == 1
        assert automation_hits == 1
    finally:
        conn.close()


def test_v1_db_migrates_through_v2_v3_and_v4(tmp_path: Path) -> None:
    """v1 の実 DB が v2→v3→v4 と逐次適用され、既存データと FTS が保たれる."""
    db_path = tmp_path / "test.db"
    _create_v1_db(db_path)

    conn = init_db(db_path)
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        article = conn.execute("SELECT * FROM articles WHERE id = 10").fetchone()
        fts_hits = conn.execute(
            "SELECT COUNT(*) AS c FROM articles_fts WHERE articles_fts MATCH 'legacy'"
        ).fetchone()["c"]

        assert version == 4
        assert "article_notifications" in tables  # v1→v2
        assert {"idx_notifications_channel", "idx_notifications_article"} <= indexes
        assert "duplicate_of" in columns  # v2→v3
        assert article["body"] == "Legacy body"
        assert article["duplicate_of"] is None
        assert fts_hits == 1
    finally:
        conn.close()


def test_schema_version_table_without_row_is_rejected(tmp_path: Path) -> None:
    """行だけ失われた DB を空 DB と誤認せず、最新版を刻まない."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    raw_conn.execute("DELETE FROM schema_version")
    raw_conn.commit()
    raw_conn.close()

    with pytest.raises(RuntimeError, match="バージョン行がありません"):
        init_db(db_path)

    raw_conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in raw_conn.execute("PRAGMA table_info(articles)")}
        rows = raw_conn.execute("SELECT version FROM schema_version").fetchall()
        article_count = raw_conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert "duplicate_of" not in columns
        assert rows == []
        assert article_count == 1
    finally:
        raw_conn.close()


def test_apply_migrations_skips_versions_already_applied_by_another_process(
    tmp_path: Path,
) -> None:
    """トランザクション内でバージョンを読み直し、二重適用を避ける.

    バージョン読み取り後に別プロセスが最新版へ移行した状況を、v4 の DB に対して
    `_apply_migrations(conn, 2)` を直接呼ぶことで再現する。
    """
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    try:
        db_module._apply_migrations(conn, 2)  # 二重適用されると duplicate column で落ちる

        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(articles)")]
        assert version == 4
        assert columns.count("duplicate_of") == 1
    finally:
        conn.close()


def test_migrations_are_applied_sequentially_through_dummy_v5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2→v3→v4 の後に一時登録した v5 が順番に適用される."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    applied: list[int] = []

    def migrate_to_v5(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        assert "duplicate_of" in columns
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'articles_fts'"
        ).fetchone()["sql"]
        assert "tokenize='trigram'" in fts_sql
        conn.execute("ALTER TABLE articles ADD COLUMN migration_probe INTEGER")
        applied.append(5)

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 5)
    monkeypatch.setitem(db_module.MIGRATIONS, 5, migrate_to_v5)

    conn = init_db(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert applied == [5]
        assert "migration_probe" in columns
        assert version == 5
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

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 5)
    monkeypatch.setitem(db_module.MIGRATIONS, 5, failing_migration)

    with pytest.raises(RuntimeError, match="意図した失敗"):
        init_db(db_path)

    raw_conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in raw_conn.execute("PRAGMA table_info(articles)")}
        version = raw_conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert "unfinished" not in columns
        assert version == 4
    finally:
        raw_conn.close()


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    """コードより新しい DB を前方保護で拒否する."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    raw_conn.execute("UPDATE schema_version SET version = 5")
    raw_conn.commit()
    raw_conn.close()

    with pytest.raises(RuntimeError, match=r"DB=5 > コード=4"):
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
