"""orchestrator.run_crawl() のユニットテスト. httpx を MockTransport で差し替える."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import qa_radar.crawler.fetch as fetch_module
from qa_radar.crawler.orchestrator import _is_blocked, run_crawl
from qa_radar.db import init_db
from qa_radar.publisher.queries import fetch_recent_articles
from qa_radar.sources import BlockedConfig, FetchPolicy, SourceConfig

ATOM_2_ENTRIES = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>x</title><updated>2024-01-15T12:00:00Z</updated>
  <entry><id>g1</id><link href="https://e.com/1"/><title>記事1</title>
    <published>2024-01-15T00:00:00Z</published><content>本文1</content></entry>
  <entry><id>g2</id><link href="https://e.com/2"/><title>記事2</title>
    <published>2024-01-16T00:00:00Z</published><content>本文2</content></entry>
</feed>""".encode()


@pytest.fixture(autouse=True)
def _fast_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_feed() のリトライ待機 (既定 1s→2s) を無効化し、実待機なしで回す.

    orchestrator は fetch_feed() の retry_base_delay を明示指定しないため、
    500/接続エラーを返すテストがそのままだと計6秒の実待機になる。
    stdlib の asyncio.sleep をグローバルに差し替えると影響範囲が広すぎるので、
    fetch.py の間接層 `_sleep` だけを monkeypatch する。
    """

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(fetch_module, "_sleep", _no_sleep)


def _src(slug: str = "t1", min_interval: int = 0) -> SourceConfig:
    return SourceConfig(
        slug=slug,
        name="T",
        feed_url="https://example.com/feed",
        site_url=None,
        language="en",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=min_interval, max_items_per_fetch=30),
        license_note="ok",
    )


def _atom_transport() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda req: httpx.Response(200, content=ATOM_2_ENTRIES, headers={"etag": "x"})
    )


def _single_entry_atom(guid: str, body: str, published: str = "2024-01-15T00:00:00Z") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>x</title><updated>2024-01-15T12:00:00Z</updated>
  <entry><id>{guid}</id><link href="https://e.com/{guid}"/><title>{guid}</title>
    <published>{published}</published><content>{body}</content></entry>
</feed>""".encode()


def test_is_blocked_exact_match() -> None:
    blocked = BlockedConfig(blocked_domains=frozenset({"jiji.com"}))
    assert _is_blocked("https://www.jiji.com/feed", blocked) is True
    assert _is_blocked("https://jiji.com/feed", blocked) is True
    assert _is_blocked("https://example.com/feed", blocked) is False


def test_is_blocked_subdomain() -> None:
    blocked = BlockedConfig(blocked_domains=frozenset({"jiji.com"}))
    assert _is_blocked("https://news.jiji.com/x", blocked) is True


def test_is_blocked_empty() -> None:
    assert _is_blocked("", BlockedConfig(blocked_domains=frozenset())) is False


@pytest.mark.asyncio
async def test_full_crawl_inserts_2_articles(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=_atom_transport()) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(blocked_domains=frozenset()),
                client=client,
            )
        finally:
            conn.close()
    assert result.sources_processed == 1
    assert result.articles_added == 2
    assert result.errors == []


@pytest.mark.asyncio
async def test_crawl_tags_articles(tmp_path: Path) -> None:
    """run_crawl は記事に tag を付与して挿入する (Phase 2 統合確認)."""
    import json as _json

    # ATOM のタイトルが Playwright/E2E にヒットするカスタムフィードを作る (ASCIIのみ)
    content = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>x</title><updated>2024-01-15T12:00:00Z</updated>
  <entry><id>g1</id><link href="https://e.com/1"/>
    <title>Playwright v1.50.0 released</title>
    <published>2024-01-15T00:00:00Z</published>
    <content>New release with improvements</content></entry>
</feed>"""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=content))

    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            await run_crawl(
                conn,
                [_src()],
                BlockedConfig(frozenset()),
                client=client,
            )
            row = conn.execute("SELECT tags_json FROM articles").fetchone()
            tags = _json.loads(row["tags_json"])
        finally:
            conn.close()
    # Playwright タイトルなので e2e タグが付くはず
    assert "e2e" in tags


@pytest.mark.asyncio
async def test_dedup_on_second_run(tmp_path: Path) -> None:
    """2回目のクロールで articles_added=0."""
    async with httpx.AsyncClient(transport=_atom_transport()) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            r1 = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(blocked_domains=frozenset()),
                client=client,
            )
            assert r1.articles_added == 2
            r2 = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(blocked_domains=frozenset()),
                client=client,
            )
        finally:
            conn.close()
    assert r2.articles_added == 0


@pytest.mark.asyncio
async def test_cross_source_duplicate_is_marked_with_original_id(tmp_path: Path) -> None:
    """別ソースの同一長文は保持しつつ元記事 ID を設定する."""
    content = _single_entry_atom("shared", "x" * 200)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=content))
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            first = await run_crawl(
                conn,
                [_src("origin")],
                BlockedConfig(frozenset()),
                client=client,
            )
            origin = conn.execute(
                "SELECT id, duplicate_of FROM articles WHERE source_id = "
                "(SELECT id FROM sources WHERE slug = 'origin')"
            ).fetchone()
            second = await run_crawl(
                conn,
                [_src("repost")],
                BlockedConfig(frozenset()),
                client=client,
            )
            repost = conn.execute(
                "SELECT duplicate_of FROM articles WHERE source_id = "
                "(SELECT id FROM sources WHERE slug = 'repost')"
            ).fetchone()
        finally:
            conn.close()

    assert first.duplicates_marked == 0
    assert origin["duplicate_of"] is None
    assert second.articles_added == 1
    assert second.duplicates_marked == 1
    assert repost["duplicate_of"] == origin["id"]


@pytest.mark.asyncio
async def test_original_crawled_after_repost_takes_over_the_group(tmp_path: Path) -> None:
    """転載を先にクロールし本家が後から届いても、本家が一覧に残り転載が消える.

    並列クロールでは到着順がネットワーク依存のため、本家が後着するのは常態。
    published_at が古い本家を元記事とし、既存の転載側を付け替える。
    """
    repost_feed = _single_entry_atom("repost", "x" * 200, published="2024-03-01T00:00:00Z")
    origin_feed = _single_entry_atom("origin", "x" * 200, published="2024-01-15T00:00:00Z")
    feeds = {"repost": repost_feed, "origin": origin_feed}
    current = {"slug": "repost"}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=feeds[current["slug"]]))
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            first = await run_crawl(
                conn, [_src("repost")], BlockedConfig(frozenset()), client=client
            )
            current["slug"] = "origin"
            second = await run_crawl(
                conn, [_src("origin")], BlockedConfig(frozenset()), client=client
            )
            rows = {
                row["guid"]: row
                for row in conn.execute("SELECT guid, id, duplicate_of FROM articles").fetchall()
            }
            published_urls = [item.url for item in fetch_recent_articles(conn)]
        finally:
            conn.close()

    assert first.duplicates_marked == 0
    assert second.articles_added == 1
    # 既存の転載1件を付け替えたので重複マーク件数は 1
    assert second.duplicates_marked == 1
    assert rows["origin"]["duplicate_of"] is None
    assert rows["repost"]["duplicate_of"] == rows["origin"]["id"]
    assert published_urls == ["https://e.com/origin"]


@pytest.mark.asyncio
async def test_same_source_same_guid_is_not_inserted_or_marked(tmp_path: Path) -> None:
    """同一ソースの既知 guid は転載判定前にスキップする."""
    content = _single_entry_atom("same-guid", "x" * 200)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=content))
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            await run_crawl(
                conn,
                [_src("same-source")],
                BlockedConfig(frozenset()),
                client=client,
            )
            second = await run_crawl(
                conn,
                [_src("same-source")],
                BlockedConfig(frozenset()),
                client=client,
            )
            rows = conn.execute("SELECT duplicate_of FROM articles").fetchall()
        finally:
            conn.close()

    assert second.articles_added == 0
    assert second.duplicates_marked == 0
    assert len(rows) == 1
    assert rows[0]["duplicate_of"] is None


@pytest.mark.asyncio
async def test_body_shorter_than_200_characters_skips_duplicate_mark(tmp_path: Path) -> None:
    """199文字の同一本文は別ソースでも重複マークしない."""
    content = _single_entry_atom("short", "x" * 199)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=content))
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            await run_crawl(
                conn,
                [_src("first")],
                BlockedConfig(frozenset()),
                client=client,
            )
            second = await run_crawl(
                conn,
                [_src("second")],
                BlockedConfig(frozenset()),
                client=client,
            )
            duplicate_values = [
                row["duplicate_of"]
                for row in conn.execute("SELECT duplicate_of FROM articles").fetchall()
            ]
        finally:
            conn.close()

    assert second.duplicates_marked == 0
    assert duplicate_values == [None, None]


@pytest.mark.asyncio
async def test_min_interval_skips_recent_fetch(tmp_path: Path) -> None:
    """min_interval=3600 の場合、直後の再実行ではフェッチしない."""
    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        fetch_count += 1
        return httpx.Response(200, content=ATOM_2_ENTRIES, headers={"etag": "x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            src = _src(min_interval=3600)
            await run_crawl(conn, [src], BlockedConfig(frozenset()), client=client)
            await run_crawl(conn, [src], BlockedConfig(frozenset()), client=client)
        finally:
            conn.close()
    assert fetch_count == 1, "min_interval が機能していない"


@pytest.mark.asyncio
async def test_blocked_source_skipped(tmp_path: Path) -> None:
    """blocked_domains に該当するソースはフェッチされず error 記録."""
    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, content=ATOM_2_ENTRIES)

    bad_src = SourceConfig(
        slug="bad",
        name="Bad",
        feed_url="https://www.jiji.com/feed",
        site_url=None,
        language="ja",
        category="blog",
        enabled=True,
        fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=30),
        license_note="",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(
                conn,
                [bad_src],
                BlockedConfig(blocked_domains=frozenset({"jiji.com"})),
                client=client,
            )
        finally:
            conn.close()
    assert fetch_count == 0
    assert result.articles_added == 0
    assert any(e.get("reason") == "blocked_domain" for e in result.errors)


@pytest.mark.asyncio
async def test_disabled_source_not_fetched(tmp_path: Path) -> None:
    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, content=ATOM_2_ENTRIES)

    disabled = replace(_src("d1"), enabled=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(
                conn,
                [disabled],
                BlockedConfig(frozenset()),
                client=client,
            )
        finally:
            conn.close()
    assert fetch_count == 0
    assert result.articles_added == 0


@pytest.mark.asyncio
async def test_handles_304_not_modified(tmp_path: Path) -> None:
    """既知のetagで再実行→304→articles_added=0、エラーなし."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, content=ATOM_2_ENTRIES, headers={"etag": "etag-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            r1 = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(frozenset()),
                client=client,
            )
            assert r1.articles_added == 2
            r2 = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(frozenset()),
                client=client,
            )
        finally:
            conn.close()
    assert r2.articles_added == 0
    assert r2.errors == []


@pytest.mark.asyncio
async def test_fetch_error_recorded(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(frozenset()),
                client=client,
            )
        finally:
            conn.close()
    assert result.articles_added == 0
    assert any(e.get("reason") == "fetch_error" for e in result.errors)


@pytest.mark.asyncio
async def test_parse_error_recorded(tmp_path: Path) -> None:
    """完全に壊れたXMLは parse_error として記録."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"\x00not xml\x00"))
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(
                conn,
                [_src()],
                BlockedConfig(frozenset()),
                client=client,
            )
        finally:
            conn.close()
    # feedparser は壊れたXMLでも items=[] を返すので parse_error として記録される
    assert result.articles_added == 0
    assert any(e.get("reason") == "parse_error" for e in result.errors)


@pytest.mark.asyncio
async def test_max_items_per_fetch_limits(tmp_path: Path) -> None:
    """max_items_per_fetch=1 の場合、1記事しか取り込まない."""
    transport = _atom_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            limited = SourceConfig(
                slug="lim",
                name="L",
                feed_url="https://example.com/feed",
                site_url=None,
                language="en",
                category="blog",
                enabled=True,
                fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=1),
                license_note="",
            )
            result = await run_crawl(
                conn,
                [limited],
                BlockedConfig(frozenset()),
                client=client,
            )
        finally:
            conn.close()
    assert result.articles_added == 1


@pytest.mark.asyncio
async def test_robots_disallow_skips_source(tmp_path: Path) -> None:
    """robots.txt が Disallow を返したら fetch_feed しない."""
    feed_fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal feed_fetch_count
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        feed_fetch_count += 1
        return httpx.Response(200, content=ATOM_2_ENTRIES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(conn, [_src()], BlockedConfig(frozenset()), client=client)
        finally:
            conn.close()
    assert feed_fetch_count == 0
    assert result.articles_added == 0
    assert any(e.get("reason") == "robots_disallow" for e in result.errors)


@pytest.mark.asyncio
async def test_all_sources_exception_counts_as_processed_with_exception_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全ソースが処理中に予期しない例外で落ちても sources_processed は len(sources) と一致する.

    `asyncio.gather(..., return_exceptions=True)` が例外を返したソースの扱いを
    検証する回帰テスト。修正前の実装は `sources_processed += 1` が
    `isinstance(res, BaseException)` の判定より後にあったため、例外ソースが
    処理数にカウントされず、全滅時でも `sources_processed == 0` のままで
    「全ソースがエラー (sources_processed > 0 かつ errors == sources_processed)」の
    判定が成立しないバグがあった。
    """

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("想定外の例外")

    monkeypatch.setattr("qa_radar.crawler.orchestrator.parse_feed", boom)

    sources = [_src("a"), _src("b"), _src("c")]
    async with httpx.AsyncClient(transport=_atom_transport()) as client:
        conn = init_db(tmp_path / "test.db")
        try:
            result = await run_crawl(
                conn,
                sources,
                BlockedConfig(blocked_domains=frozenset()),
                client=client,
            )
        finally:
            conn.close()

    assert result.sources_processed == len(sources)
    assert len(result.errors) == len(sources)
    assert all(e["reason"] == "exception" for e in result.errors)


@pytest.mark.asyncio
async def test_run_crawl_owns_client_when_none(tmp_path: Path) -> None:
    """client=None で実行できる (実ネットワークを叩くがエラーで終わる)."""
    conn = init_db(tmp_path / "test.db")
    try:
        bad_src = SourceConfig(
            slug="bad",
            name="B",
            feed_url="http://0.0.0.0:1/feed",
            site_url=None,
            language="en",
            category="blog",
            enabled=True,
            fetch_policy=FetchPolicy(min_interval_seconds=0, max_items_per_fetch=10),
            license_note="",
        )
        result = await run_crawl(
            conn,
            [bad_src],
            BlockedConfig(frozenset()),
            concurrency=1,
        )
    finally:
        conn.close()
    assert result.errors  # 接続失敗が記録される
