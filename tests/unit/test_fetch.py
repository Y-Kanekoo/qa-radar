"""fetch.fetch_feed() のユニットテスト. httpx.MockTransport で外部依存を切る."""

from __future__ import annotations

import httpx
import pytest

from qa_radar.crawler.fetch import USER_AGENT, RobotsCache, fetch_feed


@pytest.mark.asyncio
async def test_returns_content_on_200() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            content=b"<feed/>",
            headers={"etag": "etag-new", "last-modified": "Wed, 21 Oct 2025 07:28:00 GMT"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        r = await fetch_feed("https://example.com/feed", client=client)
    assert r.is_modified
    assert r.content == b"<feed/>"
    assert r.etag == "etag-new"
    assert r.last_modified == "Wed, 21 Oct 2025 07:28:00 GMT"


@pytest.mark.asyncio
async def test_returns_304_when_etag_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == "etag-cached":
            return httpx.Response(304)
        return httpx.Response(200, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await fetch_feed("https://example.com/feed", etag="etag-cached", client=client)
    assert r.is_not_modified
    assert r.content is None
    assert r.etag == "etag-cached"


@pytest.mark.asyncio
async def test_user_agent_header_set() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_feed("https://example.com/feed", client=client)
    assert captured["ua"] == USER_AGENT


@pytest.mark.asyncio
async def test_if_modified_since_header_set() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ims"] = request.headers.get("if-modified-since", "")
        return httpx.Response(200, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_feed(
            "https://example.com/feed",
            last_modified="X",
            client=client,
        )
    assert captured["ims"] == "X"


@pytest.mark.asyncio
async def test_handles_500_as_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as client:
        r = await fetch_feed("https://example.com/", client=client)
    assert not r.is_modified
    assert r.error is not None


@pytest.mark.asyncio
async def test_handles_network_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("接続失敗")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        r = await fetch_feed("https://example.com/", client=client)
    assert r.error is not None
    assert "接続失敗" in r.error
    assert r.status_code == 0


@pytest.mark.asyncio
async def test_creates_own_client_when_none() -> None:
    """client=None でも動作する (関数内で生成・破棄)."""
    # 実ネットワークを叩かないため、httpx の AsyncClient は呼ばれるが
    # invalid host で即座に ConnectError → error フィールドに格納される.
    r = await fetch_feed("http://0.0.0.0:1/feed", timeout=2.0)
    assert r.error is not None


# ---------------- RobotsCache ----------------


@pytest.mark.asyncio
async def test_robots_disallow_blocks() -> None:
    """Disallow に該当するパスは is_allowed=False, それ以外は True."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = RobotsCache()
        assert await cache.is_allowed("https://example.com/private/feed", client) is False
        assert await cache.is_allowed("https://example.com/public/feed", client) is True


@pytest.mark.asyncio
async def test_robots_missing_allows() -> None:
    """robots.txt が 404 のときは許可."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(404))
    ) as client:
        cache = RobotsCache()
        assert await cache.is_allowed("https://example.com/feed", client) is True


@pytest.mark.asyncio
async def test_robots_caches_per_host() -> None:
    """同じホストへの再問い合わせは robots.txt を1回しか取得しない."""
    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        if request.url.path == "/robots.txt":
            fetch_count += 1
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = RobotsCache()
        await cache.is_allowed("https://example.com/a", client)
        await cache.is_allowed("https://example.com/b", client)
        await cache.is_allowed("https://example.com/c", client)

    assert fetch_count == 1


@pytest.mark.asyncio
async def test_robots_network_error_allows() -> None:
    """robots.txt 取得時のネットワークエラーは許可で安全側."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("接続失敗")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        cache = RobotsCache()
        assert await cache.is_allowed("https://example.com/feed", client) is True


@pytest.mark.asyncio
async def test_robots_no_netloc_allows() -> None:
    """netloc が空の URL (相対 URL 等) は許可."""
    cache = RobotsCache()
    async with httpx.AsyncClient() as client:
        assert await cache.is_allowed("/relative/path", client) is True
