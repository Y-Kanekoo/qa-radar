"""db.init_db() の冪等性とスキーマ生成."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import qa_radar.db as db_module
from qa_radar.db import SCHEMA_VERSION, init_db
from qa_radar.publisher.notification_state import fetch_unnotified
from qa_radar.publisher.queries import fetch_recent_articles

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


def _create_v5_db(path: Path) -> None:
    """duplicate_of と digests を持つ実スキーマ相当の v5 DB を作る."""
    _create_v3_db(path)
    conn = sqlite3.connect(path)
    try:
        db_module._migrate_to_v4(conn)
        db_module._migrate_to_v5(conn)
        conn.execute("UPDATE schema_version SET version = 5")
        conn.commit()
    finally:
        conn.close()


def _insert_v5_article(
    conn: sqlite3.Connection,
    *,
    article_id: int,
    source_id: int,
    body_hash: str,
    body: str,
    published_at: int,
    duplicate_of: int | None = None,
) -> None:
    """v5 DB にバックフィル検証用の記事を直接追加する."""
    conn.execute(
        """
        INSERT INTO articles
            (id, guid, source_id, url, title, snippet, body_hash, body,
             published_at, fetched_at, duplicate_of)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            f"guid-{article_id}",
            source_id,
            f"https://example.com/{article_id}",
            f"記事 {article_id}",
            f"抜粋 {article_id}",
            body_hash,
            body,
            published_at,
            published_at,
            duplicate_of,
        ),
    )


def _insert_v5_sources(conn: sqlite3.Connection, count: int = 3) -> None:
    """バックフィル検証用ソースを追加する."""
    conn.executemany(
        """
        INSERT INTO sources (id, slug, name, feed_url, language, category)
        VALUES (?, ?, ?, ?, 'ja', 'blog')
        """,
        [
            (source_id, f"source-{source_id}", f"Source {source_id}", f"https://s{source_id}.com")
            for source_id in range(2, count + 2)
        ],
    )


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


def test_new_db_uses_latest_schema_with_trigram_fts(tmp_path: Path) -> None:
    """新規 DB はマイグレーションなしで最新列と trigram FTS を持つ."""
    conn = init_db(tmp_path / "test.db")
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'articles_fts'"
        ).fetchone()["sql"]
        assert "duplicate_of" in columns
        assert version == SCHEMA_VERSION
        assert "tokenize='trigram'" in fts_sql
    finally:
        conn.close()


def test_v2_db_migrates_to_latest_without_data_loss(tmp_path: Path) -> None:
    """v2 の既存記事を保ったまま最新スキーマへ移行する."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)

    conn = init_db(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        article = conn.execute("SELECT * FROM articles WHERE id = 10").fetchone()
        assert "duplicate_of" in columns
        assert version == SCHEMA_VERSION
        assert article["guid"] == "legacy-guid"
        assert article["body"] == "Legacy body"
        assert article["duplicate_of"] is None
    finally:
        conn.close()


def test_v3_db_migrates_to_latest_and_rebuilds_trigram_fts(tmp_path: Path) -> None:
    """v3 の記事を保ったまま最新版へ移行し、FTS トリガも保持する."""
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
        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]

        assert version == SCHEMA_VERSION
        assert article["guid"] == "legacy-guid"
        assert article["title"] == "品質保証の実践"
        assert article["body"] == "継続的なソフトウェアテストと自動化を紹介します"
        assert article["tags_json"] == '["テスト自動化"]'
        assert article["duplicate_of"] is None
        assert "tokenize='trigram'" in fts_sql
        assert triggers == {"articles_ai", "articles_ad", "articles_au"}
        assert test_hits == 1
        assert automation_hits == 1
        assert freelist_count == 0
    finally:
        conn.close()


def test_v5_db_migrates_to_v6_and_backfills_duplicates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """v5 の未マーク重複を遡及し、既存マークと対象外グループも整合させる."""
    db_path = tmp_path / "test.db"
    _create_v5_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    try:
        raw_conn.execute("DELETE FROM articles")
        _insert_v5_sources(raw_conn)

        # 別ソースの未マーク重複。
        _insert_v5_article(
            raw_conn,
            article_id=101,
            source_id=1,
            body_hash="cross-unmarked",
            body="あ" * 200,
            published_at=100,
        )
        _insert_v5_article(
            raw_conn,
            article_id=102,
            source_id=2,
            body_hash="cross-unmarked",
            body="あ" * 200,
            published_at=200,
        )

        # 正しいマーク済み記事と未マーク記事が混在する3ソースグループ。
        _insert_v5_article(
            raw_conn,
            article_id=201,
            source_id=1,
            body_hash="marked-mixed",
            body="い" * 200,
            published_at=300,
        )
        _insert_v5_article(
            raw_conn,
            article_id=202,
            source_id=2,
            body_hash="marked-mixed",
            body="い" * 200,
            published_at=400,
            duplicate_of=201,
        )
        _insert_v5_article(
            raw_conn,
            article_id=203,
            source_id=3,
            body_hash="marked-mixed",
            body="い" * 200,
            published_at=500,
        )

        # 正規化後199文字は別ソースでも対象外。
        for article_id, source_id in ((301, 1), (302, 2)):
            _insert_v5_article(
                raw_conn,
                article_id=article_id,
                source_id=source_id,
                body_hash="short",
                body="う" * 199,
                published_at=article_id,
            )

        # 同一ソースだけのグループは、既存の誤マークも含めて対象外に戻す。
        _insert_v5_article(
            raw_conn,
            article_id=401,
            source_id=1,
            body_hash="same-source",
            body="え" * 200,
            published_at=600,
        )
        _insert_v5_article(
            raw_conn,
            article_id=402,
            source_id=1,
            body_hash="same-source",
            body="え" * 200,
            published_at=601,
            duplicate_of=401,
        )

        _insert_v5_article(
            raw_conn,
            article_id=501,
            source_id=4,
            body_hash="single",
            body="お" * 200,
            published_at=700,
        )

        # published_at が同値なら id が小さい記事を元にする3ソースグループ。
        for article_id, source_id, published_at in (
            (601, 1, 801),
            (602, 2, 800),
            (603, 3, 800),
        ):
            _insert_v5_article(
                raw_conn,
                article_id=article_id,
                source_id=source_id,
                body_hash="three-sources",
                body="か" * 200,
                published_at=published_at,
            )

        # 元記事の選択が最古記事と食い違う既存マークは正しい参照へ補正する。
        _insert_v5_article(
            raw_conn,
            article_id=701,
            source_id=1,
            body_hash="wrong-marker",
            body="き" * 200,
            published_at=900,
            duplicate_of=702,
        )
        _insert_v5_article(
            raw_conn,
            article_id=702,
            source_id=2,
            body_hash="wrong-marker",
            body="き" * 200,
            published_at=901,
        )
        _insert_v5_article(
            raw_conn,
            article_id=703,
            source_id=3,
            body_hash="wrong-marker",
            body="き" * 200,
            published_at=902,
            duplicate_of=702,
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    with caplog.at_level("INFO", logger="qa_radar.db"):
        conn = init_db(db_path)
    try:
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        actual = {
            int(row["id"]): (int(row["duplicate_of"]) if row["duplicate_of"] is not None else None)
            for row in conn.execute("SELECT id, duplicate_of FROM articles ORDER BY id").fetchall()
        }
        expected = {
            101: None,
            102: 101,
            201: None,
            202: 201,
            203: 201,
            301: None,
            302: None,
            401: None,
            402: None,
            501: None,
            601: 602,
            602: None,
            603: 602,
            701: None,
            702: 701,
            703: 701,
        }

        assert version == 6
        assert actual == expected
        assert "schema v6: 5 件をマーク / 1 件補正 / 2 件解除" in caplog.messages

        # データ移行関数自体を再実行しても期待状態を変えない。
        db_module._migrate_to_v6(conn)
        after_second_run = {
            int(row["id"]): (int(row["duplicate_of"]) if row["duplicate_of"] is not None else None)
            for row in conn.execute("SELECT id, duplicate_of FROM articles").fetchall()
        }
        assert after_second_run == expected
    finally:
        conn.close()


def test_v6_backfill_does_not_mark_articles_from_original_source(tmp_path: Path) -> None:
    """同一ソースの記事を巻き込まず、挿入時と同じクロスソース判定にする."""
    db_path = tmp_path / "test.db"
    _create_v5_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    try:
        raw_conn.execute("DELETE FROM articles")
        _insert_v5_sources(raw_conn, count=1)
        for article_id, source_id, published_at in (
            (801, 1, 100),
            (802, 1, 150),
            (803, 2, 200),
        ):
            _insert_v5_article(
                raw_conn,
                article_id=article_id,
                source_id=source_id,
                body_hash="same-and-cross-source",
                body="く" * 200,
                published_at=published_at,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()

    conn = init_db(db_path)
    try:
        actual = {
            int(row["id"]): (int(row["duplicate_of"]) if row["duplicate_of"] is not None else None)
            for row in conn.execute("SELECT id, duplicate_of FROM articles ORDER BY id").fetchall()
        }
        assert actual == {801: None, 802: None, 803: 801}
    finally:
        conn.close()


def test_v6_backfill_marks_only_eligible_rows_in_mixed_length_group(tmp_path: Path) -> None:
    """同じハッシュの混在グループでも200文字ガードを満たす行だけをマークする."""
    db_path = tmp_path / "test.db"
    _create_v5_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    try:
        raw_conn.execute("DELETE FROM articles")
        _insert_v5_sources(raw_conn, count=2)
        for article_id, source_id, body, published_at in (
            (811, 1, "け" * 200, 100),
            (812, 2, "こ" * 199, 150),
            (813, 3, "さ" * 200, 200),
        ):
            _insert_v5_article(
                raw_conn,
                article_id=article_id,
                source_id=source_id,
                body_hash="mixed-body-length",
                body=body,
                published_at=published_at,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()

    conn = init_db(db_path)
    try:
        actual = {
            int(row["id"]): (int(row["duplicate_of"]) if row["duplicate_of"] is not None else None)
            for row in conn.execute("SELECT id, duplicate_of FROM articles ORDER BY id").fetchall()
        }
        assert actual == {811: None, 812: None, 813: 811}
    finally:
        conn.close()


def test_v6_backfill_is_reflected_in_output_filters(tmp_path: Path) -> None:
    """v6 で遡及マークした記事を未通知取得と一覧クエリから除外する."""
    db_path = tmp_path / "test.db"
    _create_v5_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    try:
        raw_conn.execute("DELETE FROM articles")
        _insert_v5_sources(raw_conn, count=1)
        for article_id, source_id, published_at in ((801, 1, 100), (802, 2, 200)):
            _insert_v5_article(
                raw_conn,
                article_id=article_id,
                source_id=source_id,
                body_hash="output-filter",
                body="く" * 200,
                published_at=published_at,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()

    conn = init_db(db_path)
    try:
        assert [article.article_id for article in fetch_unnotified(conn)] == [801]
        assert [article.url for article in fetch_recent_articles(conn)] == [
            "https://example.com/801"
        ]
    finally:
        conn.close()


def test_vacuum_runs_once_only_when_migration_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """マイグレーション適用後だけ、トランザクション外で VACUUM を1回実行する."""
    db_path = tmp_path / "test.db"
    _create_v3_db(db_path)
    real_connect = sqlite3.connect
    statements: list[str] = []

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db_module.sqlite3, "connect", traced_connect)

    conn = init_db(db_path)
    conn.close()
    assert [sql for sql in statements if sql.strip().upper() == "VACUUM"] == ["VACUUM"]

    statements.clear()
    conn = init_db(db_path)
    conn.close()
    assert not any(sql.strip().upper() == "VACUUM" for sql in statements)


def test_init_db_continues_when_vacuum_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """移行直後に別接続が書き込み中でも VACUUM 失敗だけを隔離する."""
    db_path = tmp_path / "test.db"
    _create_v3_db(db_path)
    lock_conn = sqlite3.connect(db_path)
    real_apply_migrations = db_module._apply_migrations

    def apply_migrations_and_lock(conn: sqlite3.Connection, current_version: int) -> bool:
        applied = real_apply_migrations(conn, current_version)
        conn.execute("PRAGMA busy_timeout = 0")
        lock_conn.execute("BEGIN IMMEDIATE")
        return applied

    monkeypatch.setattr(db_module, "_apply_migrations", apply_migrations_and_lock)

    try:
        with caplog.at_level("WARNING", logger="qa_radar.db"):
            conn = init_db(db_path)
        try:
            version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
            assert version == SCHEMA_VERSION
        finally:
            conn.close()
    finally:
        lock_conn.rollback()
        lock_conn.close()

    assert "VACUUM をスキップしました(他プロセスが DB 使用中)" in caplog.messages


def test_v1_db_migrates_sequentially_to_latest(tmp_path: Path) -> None:
    """v1 の実 DB が最新版まで逐次適用され、既存データと FTS が保たれる."""
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

        assert version == SCHEMA_VERSION
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

    バージョン読み取り後に別プロセスが最新版へ移行した状況を、最新版の DB に対して
    `_apply_migrations(conn, 2)` を直接呼ぶことで再現する。
    """
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    try:
        db_module._apply_migrations(conn, 2)  # 二重適用されると duplicate column で落ちる

        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(articles)")]
        assert version == SCHEMA_VERSION
        assert columns.count("duplicate_of") == 1
    finally:
        conn.close()


def test_migrations_are_applied_sequentially_through_dummy_v7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2→v3→v4→v5→v6 の後に一時登録した v7 が順番に適用される."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    applied: list[int] = []

    def migrate_to_v7(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        assert "duplicate_of" in columns
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'articles_fts'"
        ).fetchone()["sql"]
        assert "tokenize='trigram'" in fts_sql
        conn.execute("ALTER TABLE articles ADD COLUMN migration_probe INTEGER")
        applied.append(7)

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 7)
    monkeypatch.setitem(db_module.MIGRATIONS, 7, migrate_to_v7)

    conn = init_db(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert applied == [7]
        assert "migration_probe" in columns
        assert version == 7
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

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 7)
    monkeypatch.setitem(db_module.MIGRATIONS, 7, failing_migration)

    with pytest.raises(RuntimeError, match="意図した失敗"):
        init_db(db_path)

    raw_conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in raw_conn.execute("PRAGMA table_info(articles)")}
        version = raw_conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert "unfinished" not in columns
        assert version == SCHEMA_VERSION
    finally:
        raw_conn.close()


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    """コードより新しい DB を前方保護で拒否する."""
    db_path = tmp_path / "test.db"
    _create_v2_db(db_path)
    raw_conn = sqlite3.connect(db_path)
    raw_conn.execute("UPDATE schema_version SET version = 7")
    raw_conn.commit()
    raw_conn.close()

    with pytest.raises(RuntimeError, match=r"DB=7 > コード=6"):
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
