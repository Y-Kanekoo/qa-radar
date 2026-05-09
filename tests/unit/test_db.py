"""db.init_db() の冪等性とスキーマ生成."""

from __future__ import annotations

from pathlib import Path

from qa_radar.db import SCHEMA_VERSION, init_db


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
