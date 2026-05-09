"""HTTP フェッチ層. httpx + ETag/If-Modified-Since + robots.txt 遵守."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from qa_radar import __version__

USER_AGENT = f"qa-radar/{__version__} (+https://github.com/Y-Kanekoo/qa-radar)"
DEFAULT_TIMEOUT = 30.0
ACCEPT_HEADER = "application/atom+xml, application/rss+xml, application/xml, text/xml, */*"


@dataclass
class FetchResult:
    """`fetch_feed()` の戻り値."""

    url: str
    status_code: int
    content: bytes | None  # 304 Not Modified 時は None
    etag: str | None
    last_modified: str | None
    error: str | None = None

    @property
    def is_modified(self) -> bool:
        """200 で本文が取れている."""
        return self.status_code == 200 and self.content is not None

    @property
    def is_not_modified(self) -> bool:
        """304 Not Modified."""
        return self.status_code == 304


async def fetch_feed(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> FetchResult:
    """RSS/Atom フィードを取得する.

    ETag / Last-Modified が与えられた場合は条件付きGETで送信し、サーバが
    304 Not Modified を返したら本文の再取得をスキップできる.

    Args:
        url: フィード URL.
        etag: 前回取得時の ETag (あれば).
        last_modified: 前回取得時の Last-Modified (あれば).
        timeout: HTTP タイムアウト秒.
        client: 共有 httpx.AsyncClient. None なら関数内で生成する.

    Returns:
        FetchResult. ネットワーク例外は `error` フィールドに格納する.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    own_client = client is None
    used_client = (
        client if client is not None else httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    )
    try:
        try:
            resp = await used_client.get(url, headers=headers)
        except httpx.HTTPError as e:
            return FetchResult(
                url=url,
                status_code=0,
                content=None,
                etag=None,
                last_modified=None,
                error=str(e),
            )

        if resp.status_code == 304:
            return FetchResult(
                url=url,
                status_code=304,
                content=None,
                etag=etag,
                last_modified=last_modified,
            )

        if resp.status_code != 200:
            return FetchResult(
                url=url,
                status_code=resp.status_code,
                content=None,
                etag=None,
                last_modified=None,
                error=f"unexpected status: {resp.status_code}",
            )

        return FetchResult(
            url=url,
            status_code=200,
            content=resp.content,
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
    finally:
        if own_client:
            await used_client.aclose()


class RobotsCache:
    """robots.txt をホスト単位でキャッシュする.

    クロール1セッションを通して使い回す. 同じホストへの複数リクエストでも
    robots.txt の取得は1回だけになる. キャッシュは在メモリで永続化しない.
    """

    def __init__(self) -> None:
        # None は「robots.txt 取得失敗 (= 制限なしと解釈)」を表す
        self._cache: dict[str, RobotFileParser | None] = {}

    async def is_allowed(self, url: str, client: httpx.AsyncClient) -> bool:
        """`url` のクロールが robots.txt 上許可されているか.

        robots.txt が存在しない/取得失敗の場合は許可とする (47条の5政令要件は
        robots.txt の Disallow を遵守することを求めるので、不在は許可解釈で安全).
        """
        parsed = urlparse(url)
        if not parsed.netloc:
            return True
        if parsed.netloc not in self._cache:
            self._cache[parsed.netloc] = await self._fetch_robots(
                parsed.netloc, parsed.scheme, client
            )
        rp = self._cache[parsed.netloc]
        if rp is None:
            return True
        return rp.can_fetch(USER_AGENT, url)

    async def _fetch_robots(
        self, host: str, scheme: str, client: httpx.AsyncClient
    ) -> RobotFileParser | None:
        """robots.txt を取得しパースする. 失敗時は None を返す."""
        try:
            resp = await client.get(
                f"{scheme}://{host}/robots.txt",
                headers={"User-Agent": USER_AGENT},
                timeout=10.0,
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp
