"""publisher/discord.py のユニットテスト."""

from __future__ import annotations

import httpx
import pytest

from qa_radar.publisher.discord import (
    DISCORD_DESCRIPTION_LIMIT,
    DISCORD_TITLE_LIMIT,
    build_embed,
    build_payload,
    send_batch,
    send_notification,
)
from qa_radar.publisher.rss import FeedItem


def _item(**kw: object) -> FeedItem:
    defaults: dict[str, object] = {
        "id": "https://example.com/1",
        "url": "https://example.com/1",
        "title": "Test article",
        "snippet": "サマリ100字以内",
        "source_name": "Source",
        "author": "Alice",
        "published_at": 1700000000,
        "tags": ("e2e", "tooling"),
    }
    defaults.update(kw)
    return FeedItem(**defaults)  # type: ignore[arg-type]


# ---------------- build_embed ----------------


def test_build_embed_basic() -> None:
    embed = build_embed(_item())
    assert embed["title"] == "Test article"
    assert embed["url"] == "https://example.com/1"
    assert "サマリ100字以内" in embed["description"]
    assert "#e2e" in embed["description"]
    assert "#tooling" in embed["description"]
    footer = embed["footer"]
    assert isinstance(footer, dict)
    assert "Source" in footer["text"]
    assert "Alice" in footer["text"]


def test_build_embed_truncates_long_title() -> None:
    long_title = "X" * 500
    embed = build_embed(_item(title=long_title))
    assert len(embed["title"]) == DISCORD_TITLE_LIMIT


def test_build_embed_truncates_long_description() -> None:
    long_snippet = "X" * (DISCORD_DESCRIPTION_LIMIT + 100)
    embed = build_embed(_item(snippet=long_snippet, tags=()))
    desc = embed["description"]
    assert isinstance(desc, str)
    assert len(desc) <= DISCORD_DESCRIPTION_LIMIT


def test_build_embed_no_tags() -> None:
    embed = build_embed(_item(tags=()))
    assert embed["description"] == "サマリ100字以内"


def test_build_embed_no_author_uses_source_only() -> None:
    embed = build_embed(_item(author=None))
    footer = embed["footer"]
    assert isinstance(footer, dict)
    assert footer["text"] == "Source"


def test_build_payload_wraps_in_embeds_array() -> None:
    payload = build_payload(_item())
    assert "embeds" in payload
    assert isinstance(payload["embeds"], list)
    assert len(payload["embeds"]) == 1


def test_build_embed_includes_iso_timestamp() -> None:
    embed = build_embed(_item(published_at=1700000000))
    ts = embed["timestamp"]
    assert isinstance(ts, str)
    # ISO8601 形式
    assert "T" in ts
    assert ts.startswith("2023-")


# ---------------- send_notification ----------------


@pytest.mark.asyncio
async def test_send_notification_success() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await send_notification(_item(), "https://discord/wh", client=client)
    assert ok is True
    assert captured["url"] == "https://discord/wh"
    assert "Test article" in str(captured["body"])


@pytest.mark.asyncio
async def test_send_notification_handles_4xx() -> None:
    """400/401 などは即時失敗、再試行しない."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(400, text="Bad Request"))
    ) as client:
        ok = await send_notification(_item(), "https://discord/wh", client=client)
    assert ok is False


@pytest.mark.asyncio
async def test_send_notification_handles_network_error() -> None:
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        ok = await send_notification(_item(), "https://discord/wh", client=client)
    assert ok is False


@pytest.mark.asyncio
async def test_send_notification_retries_on_429() -> None:
    """429 を受けたら Retry-After に従って再試行し、2回目で成功する."""
    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"retry-after": "0.5"})
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await send_notification(_item(), "https://discord/wh", client=client)
    assert ok is True
    assert call_count == 2


@pytest.mark.asyncio
async def test_send_notification_429_retry_after_in_body() -> None:
    """Discord は JSON ボディで retry_after を返すケースもある."""
    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, json={"retry_after": 0.5, "global": False})
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await send_notification(_item(), "https://discord/wh", client=client)
    assert ok is True


@pytest.mark.asyncio
async def test_send_notification_429_giveup_after_retries() -> None:
    """max_retries 回 429 を受けた後は False を返す."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(429, headers={"retry-after": "0.1"})
        )
    ) as client:
        ok = await send_notification(_item(), "https://discord/wh", client=client, max_retries=1)
    assert ok is False


# ---------------- send_batch ----------------


@pytest.mark.asyncio
async def test_send_batch_all_success() -> None:
    counts: dict[str, int] = {"calls": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        counts["calls"] += 1
        return httpx.Response(204)

    items = [_item(url=f"https://e.com/{i}", title=f"記事{i}") for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        success, failure = await send_batch(
            items,
            "https://discord/wh",
            rate_limit_delay=0,
            client=client,
        )
    assert success == 3
    assert failure == 0
    assert counts["calls"] == 3


@pytest.mark.asyncio
async def test_send_batch_partial_failure() -> None:
    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(400)
        return httpx.Response(204)

    items = [_item(url=f"https://e.com/{i}") for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        success, failure = await send_batch(
            items,
            "https://discord/wh",
            rate_limit_delay=0,
            client=client,
        )
    assert success == 2
    assert failure == 1


@pytest.mark.asyncio
async def test_send_batch_empty_list() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(204))
    ) as client:
        success, failure = await send_batch(
            [],
            "https://discord/wh",
            rate_limit_delay=0,
            client=client,
        )
    assert success == 0
    assert failure == 0
