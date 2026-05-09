"""parse.parse_feed() のユニットテスト."""

from __future__ import annotations

from qa_radar.crawler.parse import parse_feed

ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test feed</title>
  <updated>2024-01-15T12:00:00Z</updated>
  <entry>
    <id>https://example.com/1</id>
    <link href="https://example.com/article-1"/>
    <title>記事1</title>
    <author><name>Alice</name></author>
    <published>2024-01-15T10:00:00Z</published>
    <content type="html">&lt;p&gt;本文1&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>https://example.com/2</id>
    <link href="https://example.com/article-2"/>
    <title>記事2</title>
    <summary>サマリ2</summary>
  </entry>
</feed>
""".encode()

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test RSS</title>
    <link>https://example.com</link>
    <description>desc</description>
    <item>
      <guid>https://example.com/r1</guid>
      <title>RSS1</title>
      <link>https://example.com/r1</link>
      <description>RSS本文1</description>
      <pubDate>Mon, 15 Jan 2024 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
""".encode()


def test_parses_atom_feed() -> None:
    result = parse_feed(ATOM_SAMPLE)
    assert not result.bozo
    assert len(result.items) == 2


def test_extracts_atom_fields() -> None:
    item = parse_feed(ATOM_SAMPLE).items[0]
    assert item.guid == "https://example.com/1"
    assert item.url == "https://example.com/article-1"
    assert item.title == "記事1"
    assert item.author == "Alice"
    assert "本文1" in item.body
    assert item.published_struct is not None
    assert item.published_struct[:3] == (2024, 1, 15)


def test_falls_back_to_summary_when_no_content() -> None:
    item = parse_feed(ATOM_SAMPLE).items[1]
    assert item.body == "サマリ2"
    assert item.author is None


def test_parses_rss20() -> None:
    result = parse_feed(RSS_SAMPLE)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.guid == "https://example.com/r1"
    assert item.body == "RSS本文1"
    assert item.published_struct is not None
    assert item.published_struct[:3] == (2024, 1, 15)


def test_garbage_returns_no_items() -> None:
    """完全に壊れたXMLは items が空."""
    result = parse_feed(b"<not-xml>garbage</not-xml>")
    assert result.items == []
