"""RSS / Atom フィード生成. 47条の5軽微利用境界を遵守.

公開する情報は **タイトル + snippet (≤100字) + 元URL + 出所明示** のみ.
本文 (body) は絶対に出力に含めない.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from feedgen.feed import FeedGenerator

# サイトメタデータ. GitHub Pages の URL に対応.
SITE_BASE_URL = "https://Y-Kanekoo.github.io/qa-radar"
SITE_TITLE = "qa-radar"
SITE_SUBTITLE = "QA/テスト自動化のニュースアグリゲーター (30ソース、日英対応)"
SITE_LANGUAGE = "ja"
SITE_AUTHOR = {"name": "qa-radar", "uri": "https://github.com/Y-Kanekoo/qa-radar"}


@dataclass(frozen=True)
class FeedItem:
    """フィード1エントリの公開可能なメタデータ.

    本文 (body) は意図的にフィールドから除外している. 47条の5軽微利用の
    境界として、`snippet` は ≤100字、`url` は元記事への誘導リンクとする.
    """

    id: str  # 一意ID. 通常 url を使う
    url: str
    title: str
    snippet: str  # ≤100字、元本文の抜粋
    source_name: str  # 出所明示用
    author: str | None
    published_at: int  # unix秒 (UTC)
    tags: tuple[str, ...]


def _build_feed_generator(
    *,
    feed_url: str,
    title: str = SITE_TITLE,
    subtitle: str = SITE_SUBTITLE,
) -> FeedGenerator:
    """共通の FeedGenerator を生成する."""
    fg = FeedGenerator()
    fg.id(feed_url)
    fg.title(title)
    fg.subtitle(subtitle)
    fg.author(SITE_AUTHOR)
    fg.link(href=SITE_BASE_URL + "/", rel="alternate")
    fg.link(href=feed_url, rel="self")
    fg.language(SITE_LANGUAGE)
    return fg


def _attach_item(fg: FeedGenerator, item: FeedItem) -> None:
    """1エントリを FeedGenerator に追加する."""
    fe = fg.add_entry()
    fe.id(item.id)
    fe.title(item.title)
    fe.link(href=item.url, rel="alternate")
    # 出所明示を含む summary. 本文は出力しない (47条の5境界).
    summary = item.snippet
    if item.source_name:
        summary = f"{summary}\n\n出所: {item.source_name}"
    fe.summary(summary)
    fe.author({"name": item.author or item.source_name})
    fe.published(datetime.fromtimestamp(item.published_at, tz=UTC))
    fe.updated(datetime.fromtimestamp(item.published_at, tz=UTC))
    for tag in item.tags:
        fe.category({"term": tag, "label": tag})


def write_feed(
    items: list[FeedItem],
    output_path: Path,
    *,
    feed_url: str,
    title: str = SITE_TITLE,
    subtitle: str = SITE_SUBTITLE,
    fmt: Literal["atom", "rss"] = "atom",
) -> Path:
    """フィードファイルを書き出し、書き込み先パスを返す.

    Args:
        items: フィードに含めるエントリ.
        output_path: 出力先パス.
        feed_url: 自身を指す URL (self link、id にも使う).
        title: フィードタイトル.
        subtitle: フィード概要.
        fmt: "atom" または "rss".

    Returns:
        書き込んだパス.
    """
    fg = _build_feed_generator(feed_url=feed_url, title=title, subtitle=subtitle)
    for item in items:
        _attach_item(fg, item)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "atom":
        fg.atom_file(str(output_path), pretty=True)
    elif fmt == "rss":
        fg.rss_file(str(output_path), pretty=True)
    else:  # pragma: no cover
        raise ValueError(f"不正な fmt: {fmt}")
    return output_path


def main_feed_url(fmt: Literal["atom", "rss"]) -> str:
    """メインフィードの公開 URL."""
    return f"{SITE_BASE_URL}/feed.{fmt if fmt == 'atom' else 'xml'}"


def tag_feed_url(tag: str) -> str:
    """タグ別フィードの公開 URL."""
    return f"{SITE_BASE_URL}/tags/{tag}.xml"
