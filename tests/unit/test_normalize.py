"""normalize.py のユニットテスト."""

from __future__ import annotations

import time

from qa_radar.crawler.normalize import (
    compute_body_hash,
    make_snippet,
    normalize_published,
    normalize_url,
    strip_html,
)


class TestNormalizeUrl:
    def test_strips_utm_params(self) -> None:
        url = "https://example.com/article?utm_source=twitter&utm_medium=social&id=42"
        assert normalize_url(url) == "https://example.com/article?id=42"

    def test_strips_fbclid(self) -> None:
        url = "https://example.com/?fbclid=ABC&page=1"
        assert normalize_url(url) == "https://example.com/?page=1"

    def test_strips_fragment(self) -> None:
        url = "https://example.com/article#section1"
        assert normalize_url(url) == "https://example.com/article"

    def test_lowercases_scheme_and_host(self) -> None:
        url = "HTTPS://Example.COM/Path"
        assert normalize_url(url) == "https://example.com/Path"

    def test_path_case_preserved(self) -> None:
        url = "https://example.com/Foo/Bar"
        assert normalize_url(url) == "https://example.com/Foo/Bar"

    def test_no_scheme_returns_unchanged(self) -> None:
        url = "example.com/path"
        assert normalize_url(url) == url

    def test_strips_all_tracking_params(self) -> None:
        url = (
            "https://example.com/?utm_source=a&utm_medium=b&utm_campaign=c"
            "&utm_term=d&utm_content=e&fbclid=f&gclid=g&yclid=h&ref=i&ref_src=j&keep=ok"
        )
        assert normalize_url(url) == "https://example.com/?keep=ok"


class TestStripHtml:
    def test_basic_tags(self) -> None:
        assert strip_html("<p>Hello <b>World</b></p>") == "Hello World"

    def test_japanese(self) -> None:
        assert strip_html("<p>こんにちは<b>世界</b></p>") == "こんにちは世界"

    def test_empty_string(self) -> None:
        assert strip_html("") == ""

    def test_decodes_entities(self) -> None:
        assert strip_html("Tom &amp; Jerry") == "Tom & Jerry"

    def test_nested_tags(self) -> None:
        assert strip_html("<div><p><span>nested</span></p></div>") == "nested"


class TestMakeSnippet:
    def test_short_text_unchanged(self) -> None:
        assert make_snippet("Hello world", 100) == "Hello world"

    def test_truncates_at_word_boundary(self) -> None:
        text = (
            "The quick brown fox jumps over the lazy dog and continues running "
            "through the dense forest under the bright moonlight"
        )
        result = make_snippet(text, 50)
        assert result.endswith("…")
        # 単語境界で切れていれば後半が ".." で終わるなどなく綺麗
        assert " " not in result[-2:-1]

    def test_strips_html(self) -> None:
        result = make_snippet("<p>Hello <b>world</b></p>", 100)
        assert result == "Hello world"

    def test_collapses_whitespace(self) -> None:
        assert make_snippet("Hello\n\n\nworld", 100) == "Hello world"

    def test_at_or_below_max_chars_strict(self) -> None:
        """100字以内ルール厳守: 切り詰め後の文字数 <= max_chars + 1 (… を含む)."""
        text = "あ" * 200  # 全角200字
        snippet = make_snippet(text, 100)
        # 全角でスペース無し → 単語境界補正は効かず、100文字+… になる
        assert len(snippet) == 101
        assert snippet.endswith("…")

    def test_no_word_boundary_falls_back(self) -> None:
        """スペースが60%閾値より前にしかない場合は単純切り詰め."""
        text = "a" * 30 + " " + "b" * 100  # スペースは21%地点
        snippet = make_snippet(text, 50)
        assert snippet.endswith("…")
        # スペース位置は < 60% なので採用されず、50文字目で切れる
        assert len(snippet) == 51

    def test_empty_input(self) -> None:
        assert make_snippet("") == ""


class TestNormalizePublished:
    def test_none_returns_now(self) -> None:
        result = normalize_published(None)
        assert abs(result - int(time.time())) < 5

    def test_struct_to_unix_2024(self) -> None:
        # 2024-01-15 12:30:45 UTC = 1705321845
        struct = (2024, 1, 15, 12, 30, 45)
        assert normalize_published(struct) == 1705321845

    def test_short_struct_padded(self) -> None:
        # (2024, 1, 15) → (2024,1,15,0,0,0) → 2024-01-15 00:00:00 UTC
        assert normalize_published((2024, 1, 15)) == 1705276800


class TestComputeBodyHash:
    def test_deterministic(self) -> None:
        assert compute_body_hash("Hello") == compute_body_hash("Hello")

    def test_strips_html_for_hashing(self) -> None:
        assert compute_body_hash("<p>Hello</p>") == compute_body_hash("<div>Hello</div>")

    def test_collapses_whitespace_for_hashing(self) -> None:
        assert compute_body_hash("Hello world") == compute_body_hash("Hello\n\nworld")

    def test_different_text_different_hash(self) -> None:
        assert compute_body_hash("Hello") != compute_body_hash("World")

    def test_returns_64_char_hex(self) -> None:
        h = compute_body_hash("any input")
        assert len(h) == 64
        int(h, 16)  # 16進数として解釈可能
