"""GitHub Pages 用 HTML 生成.

`feedgen` を使わず、文字列テンプレートで出力する. 依存追加を避けるため、
HTML エスケープは `html.escape` で対応する.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from qa_radar.publisher.rss import FeedItem, main_feed_url, tag_feed_url


@dataclass(frozen=True)
class SourceSummary:
    """sources.html 表示用のソース統計."""

    slug: str
    name: str
    site_url: str | None
    language: str
    category: str
    article_count: int
    latest_published_at: int | None


@dataclass(frozen=True)
class TagSummary:
    """tags.html 表示用のタグ統計."""

    tag: str
    article_count: int


@dataclass(frozen=True)
class Digest:
    """digest.html 表示用の週刊ダイジェスト."""

    id: int
    created_at: int
    period_start: int
    period_end: int
    content_md: str


# ---------------- 共通テンプレート ----------------

_HTML_HEADER = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{css_path}">
<link rel="alternate" type="application/atom+xml" title="qa-radar" href="{atom_path}">
<link rel="alternate" type="application/rss+xml" title="qa-radar" href="{rss_path}">
<meta name="description" content="{description}">
</head>
<body>
<header>
<h1><a href="{root_path}">qa-radar</a></h1>
<p class="subtitle">QA/テスト自動化のニュースアグリゲーター</p>
<nav>
<a href="{root_path}">最新記事</a>
<a href="digest.html">週刊ダイジェスト</a>
<a href="sources.html">ソース一覧</a>
<a href="tags.html">タグ一覧</a>
<a href="feed.atom">Atom</a>
<a href="feed.xml">RSS</a>
<a href="https://github.com/Y-Kanekoo/qa-radar">GitHub</a>
</nav>
</header>
<main>
"""

_HTML_FOOTER = """</main>
<footer>
<p>
<a href="https://github.com/Y-Kanekoo/qa-radar">qa-radar</a>
&copy; 2026 Y-Kanekoo &middot; MIT License &middot;
<a href="https://github.com/Y-Kanekoo/qa-radar/blob/main/NOTICE">データ取扱方針</a>
</p>
<p class="legal">
本サイトは公開RSSフィードを集約しています. 各記事の著作権は配信元・著者に帰属します.
本サイトは情報所在検索 (著作権法47条の5) として、タイトル・100字以内の抜粋・元記事リンクのみを表示します.
削除依頼は <a href="https://github.com/Y-Kanekoo/qa-radar/issues">GitHub Issues</a> へ.
</p>
<p class="generated">最終生成: {generated_at}</p>
</footer>
</body>
</html>
"""


def _meta_description(source_count: int | None) -> str:
    """meta description を生成する. source_count 不明時はソース数を省略する."""
    if source_count is None:
        return "QA/テスト自動化のニュースアグリゲーター. 日英記事を集約."
    return f"QA/テスト自動化のニュースアグリゲーター. {source_count}ソースから日英記事を集約."


def _render_header(title: str, *, in_subdir: bool = False, source_count: int | None = None) -> str:
    root = "../" if in_subdir else "./"
    return _HTML_HEADER.format(
        title=escape(title),
        css_path=f"{root}style.css",
        atom_path=f"{root}feed.atom",
        rss_path=f"{root}feed.xml",
        root_path=root,
        description=_meta_description(source_count),
    )


def _render_footer() -> str:
    return _HTML_FOOTER.format(generated_at=datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))


def _format_date(unix_seconds: int) -> str:
    """unix秒を YYYY-MM-DD で表示."""
    return datetime.fromtimestamp(unix_seconds, tz=UTC).strftime("%Y-%m-%d")


# ---------------- 個別ページ ----------------


def _render_article_card(item: FeedItem) -> str:
    tags_html = " ".join(
        f'<a class="tag" href="tags/{escape(t)}.xml">#{escape(t)}</a>' for t in item.tags
    )
    return (
        '<article class="card">'
        f'<h2><a href="{escape(item.url)}" rel="external nofollow">{escape(item.title)}</a></h2>'
        f'<p class="meta">'
        f"<time>{_format_date(item.published_at)}</time>"
        f' &middot; <span class="source">{escape(item.source_name)}</span>'
        f"{' &middot; ' + escape(item.author) if item.author else ''}"
        f"</p>"
        f'<p class="snippet">{escape(item.snippet)}</p>'
        f'<p class="tags">{tags_html}</p>'
        "</article>"
    )


def render_index(items: list[FeedItem], *, source_count: int | None = None) -> str:
    """トップページ HTML を返す.

    Args:
        items: 表示する記事一覧.
        source_count: 有効ソース数 (呼び出し側の `qa_radar.sources.load_sources()`
            集計結果を渡す想定). 不明な場合は None でソース数表記を省略する.
    """
    hero_lead = f"{source_count} ソースから集約した" if source_count is not None else "集約した"
    body = (
        f'<section class="hero">'
        f"<p>{hero_lead} QA/テスト自動化の記事 {len(items)} 件 (最新順).</p>"
        f"</section>"
    )
    body += '<section class="articles">'
    body += "\n".join(_render_article_card(it) for it in items)
    body += "</section>"
    return (
        _render_header(f"{len(items)} 件の最新記事 — qa-radar", source_count=source_count)
        + body
        + _render_footer()
    )


def render_sources_page(sources: list[SourceSummary], *, source_count: int | None = None) -> str:
    body = "<h2>集約ソース一覧</h2>"
    body += '<table class="sources">'
    body += (
        "<thead><tr>"
        "<th>名称</th><th>カテゴリ</th><th>言語</th><th>件数</th><th>最新</th>"
        "</tr></thead>"
    )
    body += "<tbody>"
    for s in sources:
        latest = _format_date(s.latest_published_at) if s.latest_published_at else "-"
        site_link = (
            f'<a href="{escape(s.site_url)}" rel="external nofollow">{escape(s.name)}</a>'
            if s.site_url
            else escape(s.name)
        )
        body += (
            f"<tr>"
            f"<td>{site_link}</td>"
            f"<td>{escape(s.category)}</td>"
            f"<td>{escape(s.language)}</td>"
            f"<td>{s.article_count}</td>"
            f"<td>{latest}</td>"
            f"</tr>"
        )
    body += "</tbody></table>"
    return (
        _render_header("ソース一覧 — qa-radar", source_count=source_count) + body + _render_footer()
    )


def render_tags_page(tags: list[TagSummary], *, source_count: int | None = None) -> str:
    body = "<h2>タグ一覧</h2>"
    body += "<p>各タグをクリックすると、そのタグの Atom フィードが取得できます.</p>"
    body += '<ul class="tag-cloud">'
    for t in tags:
        body += (
            f'<li><a href="tags/{escape(t.tag)}.xml">#{escape(t.tag)}</a> '
            f'<span class="count">({t.article_count})</span></li>'
        )
    body += "</ul>"
    return (
        _render_header("タグ一覧 — qa-radar", source_count=source_count) + body + _render_footer()
    )


def _render_digest_markdown(content_md: str) -> str:
    """許可した最小限の Markdown を HTML に変換する.

    見出し (`#` / `##`)、箇条書き (`-`)、通常行だけを扱い、すべての本文を
    `html.escape` に通す。リンク等の Markdown 構文は意図的に解釈しない。
    """
    rendered: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            rendered.append("</ul>")
            in_list = False

    for line in content_md.splitlines():
        if line.startswith("## "):
            close_list()
            rendered.append(f"<h3>{escape(line[3:])}</h3>")
        elif line.startswith("# "):
            close_list()
            rendered.append(f"<h2>{escape(line[2:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                rendered.append("<ul>")
                in_list = True
            rendered.append(f"<li>{escape(line[2:])}</li>")
        elif line.strip():
            close_list()
            rendered.append(f"<p>{escape(line)}</p>")
        else:
            close_list()
    close_list()
    return "\n".join(rendered)


def render_digest_page(digest: Digest | None, *, source_count: int | None = None) -> str:
    """最新の週刊ダイジェストを表示する HTML を返す."""
    if digest is None:
        body = (
            '<section class="digest">'
            "<h2>週刊 LLM ダイジェスト</h2>"
            "<p>ダイジェストはまだ生成されていません。</p>"
            "</section>"
        )
    else:
        period = f"{_format_date(digest.period_start)}〜{_format_date(digest.period_end)}"
        body = (
            '<section class="digest">'
            f'<p class="meta">対象期間: {period}</p>'
            f"{_render_digest_markdown(digest.content_md)}"
            "</section>"
        )
    return (
        _render_header("週刊ダイジェスト — qa-radar", source_count=source_count)
        + body
        + _render_footer()
    )


# ---------------- 書き出し ----------------


def write_html(content: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


# 公開 URL ヘルパ (将来 about.html などで利用)
__all__ = [
    "Digest",
    "FeedItem",
    "SourceSummary",
    "TagSummary",
    "main_feed_url",
    "render_digest_page",
    "render_index",
    "render_sources_page",
    "render_tags_page",
    "tag_feed_url",
    "write_html",
]
