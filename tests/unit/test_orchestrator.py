"""orchestrator.run_crawl() のユニットテスト. httpx を MockTransport で差し替える."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from qa_radar.crawler.orchestrator import _is_blocked, run_crawl
from qa_radar.db import init_db
from qa_radar.sources import BlockedConfig, FetchPolicy, SourceConfig

ATOM_2_ENTRIES = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>x</title><updated>2024-01-15T12:00:00Z</updated>
  <entry><id>g1</id><link href="https://e.com/1"/><title>記事1</title>
    <published>2024-01-15T00:00:00Z</published><content>本文1</content></entry>
  <entry><id>g2</id><link href="https://e.com/2"/><title>記事2</title>
    <published>2024-01-16T00:00:00Z</published><content>本文2</content></entry>
</feed>""".encode()


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
