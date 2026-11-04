"""scripts/notify_discord.py の統合テスト.

`send_batch` が記事ID単位の結果を返すようになったことを受け、
`main()` が成功した記事のみを mark_notified することを、モックした
webhook 送信 (途中失敗あり) と実DBを使って検証する.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from qa_radar.crawler.store import ArticleRow, insert_article, upsert_source
from qa_radar.db import init_db
from qa_radar.sources import FetchPolicy, SourceConfig

# scripts/ を import path に追加 (publish_release のテストと同じ手法)
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import notify_discord  # noqa: E402

# monkeypatch 前の本物の httpx.AsyncClient を保持しておく.
# (テスト内で httpx.AsyncClient を差し替えた後に再度 _client_class() を
#  呼ぶと、差し替え後の httpx.AsyncClient を継承してしまい前回のモック
#  ハンドラが残ってしまうため、常に本物のクラスを継承元にする)
_RealAsyncClient = httpx.AsyncClient


def _src(slug: str = "s1") -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name=f"Source {slug}",
        feed_url=f"https://{slug}.example.com/feed",
        site_url=None,
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
        license_note="",
    )


def _article(sid: int, guid: str, *, published_at: int) -> ArticleRow:
    return ArticleRow(
        source_id=sid,
        guid=guid,
        url=f"https://e.com/{guid}",
        title=f"title-{guid}",
        snippet=f"snip-{guid}",
        body_hash=guid,
        body="full body",
        author="Alice",
        published_at=published_at,
        tags=["e2e"],
    )


def _get_article_id(conn: sqlite3.Connection, guid: str) -> int:
    return int(conn.execute("SELECT id FROM articles WHERE guid = ?", (guid,)).fetchone()["id"])


def _notified_article_ids(db_path: Path) -> set[int]:
    conn = init_db(db_path)
    try:
        rows = conn.execute(
            "SELECT article_id FROM article_notifications WHERE channel = 'discord'"
        ).fetchall()
        return {int(r["article_id"]) for r in rows}
    finally:
        conn.close()


def _client_class(
    handler: Callable[[httpx.Request], httpx.Response],
) -> type[httpx.AsyncClient]:
    """モック webhook を積んだ httpx.AsyncClient の差し替えクラスを作る.

    `send_batch` (discord.py) が内部で `httpx.AsyncClient(timeout=...)` を
    生成するため、`httpx.AsyncClient` そのものをこのクラスに差し替える.
    """

    class _MockAsyncClient(_RealAsyncClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockAsyncClient


def _setup_three_articles(db_path: Path) -> tuple[int, int, int]:
    """published_at DESC で a, b, c の順に並ぶ3記事を作成する."""
    conn = init_db(db_path)
    try:
        sid = upsert_source(conn, _src())
        insert_article(conn, _article(sid, "a", published_at=300))
        insert_article(conn, _article(sid, "b", published_at=200))
        insert_article(conn, _article(sid, "c", published_at=100))
        return (
            _get_article_id(conn, "a"),
            _get_article_id(conn, "b"),
            _get_article_id(conn, "c"),
        )
    finally:
        conn.close()


def test_notify_discord_marks_only_succeeded_articles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """途中(2番目)の1件だけ失敗した場合、成功した記事のみ mark される."""
    db_path = tmp_path / "articles.db"
    aid_a, aid_b, aid_c = _setup_three_articles(db_path)

    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(400)
        return httpx.Response(204)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/wh")
    monkeypatch.setattr(httpx, "AsyncClient", _client_class(handler))

    exit_code = notify_discord.main(["--db-path", str(db_path), "--rate-delay", "0"])

    assert exit_code == 2  # 一部失敗
    notified = _notified_article_ids(db_path)
    # fetch_unnotified は published_at DESC 順 (a, b, c) で送信される.
    # 2番目 (b) だけ失敗するので、a と c のみが mark される.
    assert notified == {aid_a, aid_c}
    assert aid_b not in notified


def test_notify_discord_does_not_duplicate_or_lose_articles_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1回目で失敗した記事だけが2回目の実行で再送対象として残る.

    (旧実装のバグでは、失敗記事が誤ってmarkされ二度と再送されない、
    または成功記事が再度送信されて重複する、という不整合が起きていた)
    """
    db_path = tmp_path / "articles.db"
    aid_a, aid_b, aid_c = _setup_three_articles(db_path)

    call_count = 0

    def failing_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(400)
        return httpx.Response(204)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/wh")
    monkeypatch.setattr(httpx, "AsyncClient", _client_class(failing_handler))
    notify_discord.main(["--db-path", str(db_path), "--rate-delay", "0"])
    assert _notified_article_ids(db_path) == {aid_a, aid_c}

    # 2回目はすべて成功させる. 未通知として残っているのは b のみのはず.
    sent_urls: list[str] = []

    def success_handler(req: httpx.Request) -> httpx.Response:
        sent_urls.append(req.read().decode())
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", _client_class(success_handler))
    exit_code = notify_discord.main(["--db-path", str(db_path), "--rate-delay", "0"])

    assert exit_code == 0
    assert len(sent_urls) == 1  # b だけが再送される (a, c は重複送信されない)
    assert "title-b" in sent_urls[0]
    assert _notified_article_ids(db_path) == {aid_a, aid_b, aid_c}
