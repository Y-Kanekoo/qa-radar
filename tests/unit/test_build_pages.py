"""scripts/build_pages.py のダイジェスト統合テスト."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qa_radar.db import init_db
from qa_radar.publisher.queries import insert_digest

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import build_pages  # noqa: E402


def test_build_pages_writes_latest_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "articles.db"
    output = tmp_path / "site"
    conn = init_db(db_path)
    try:
        insert_digest(
            conn,
            created_at=100,
            period_start=0,
            period_end=100,
            content_md="# 公開する週報",
        )
    finally:
        conn.close()
    monkeypatch.setattr(build_pages, "load_sources", lambda: [])

    exit_code = build_pages.main(["--db-path", str(db_path), "--output", str(output)])

    assert exit_code == 0
    assert "公開する週報" in (output / "digest.html").read_text(encoding="utf-8")


def test_build_pages_uses_placeholder_when_digest_fetch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = tmp_path / "articles.db"
    output = tmp_path / "site"
    conn = init_db(db_path)
    conn.close()
    monkeypatch.setattr(build_pages, "load_sources", lambda: [])

    def fail_fetch(_conn: object) -> None:
        raise RuntimeError("想定した取得失敗")

    monkeypatch.setattr(build_pages, "fetch_latest_digest", fail_fetch)
    with caplog.at_level("WARNING"):
        exit_code = build_pages.main(["--db-path", str(db_path), "--output", str(output)])

    assert exit_code == 0
    assert "最新ダイジェストの取得に失敗" in caplog.text
    assert "ダイジェストはまだ生成されていません" in (output / "digest.html").read_text(
        encoding="utf-8"
    )
