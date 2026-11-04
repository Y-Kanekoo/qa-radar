"""radar-core への委譲と、qa-radar に残す互換境界を検証する。"""

from __future__ import annotations

import hashlib

import pytest
from radar_core import normalize as core_normalize

from qa_radar.crawler import normalize


def test_normalize_published_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    def convert(value: tuple[int, ...] | None) -> int:
        assert value == (2024, 1, 15)
        return 1705276800

    monkeypatch.setattr(core_normalize, "normalize_published", convert)
    assert normalize.normalize_published((2024, 1, 15)) == 1705276800


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ((1970, 1, 1), 0),
        ((2024, 1, 15, 12, 30, 45), 1705321845),
        ((2024, 1, 15, 12, 30, 45, 0, 0, 0), 1705321845),
        (None, 1700000000),
    ],
)
def test_normalize_published_compat(
    value: tuple[int, ...] | None, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_normalize.time, "time", lambda: 1700000000.9)
    assert normalize.normalize_published(value) == expected


@pytest.mark.parametrize(
    ("body", "plain", "normalized"),
    [
        ("", "", ""),
        ("<p>日本語 &amp; English</p>", "日本語 & English", "日本語 & English"),
        ("<p>A</p><p>B</p>", "AB", "AB"),
        ("<p>A&nbsp; B</p>\n\tC", "A\xa0 B\n\tC", "A B C"),
        ("<p>閉じ忘れ", "閉じ忘れ", "閉じ忘れ"),
        ("<!-- コメント --><script>x()</script>", "x()", "x()"),
        ("<p>😀 &lt; &#x1F600;</p>", "😀 < 😀", "😀 < 😀"),
    ],
)
def test_html_and_body_hash_compat(body: str, plain: str, normalized: str) -> None:
    assert normalize.strip_html(body) == plain
    assert normalize.normalize_body_text(body) == normalized
    expected_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert normalize.compute_body_hash(body) == expected_hash
    assert normalize.hash_normalized_body(normalized) == expected_hash


def test_strip_html_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    def strip(text: str) -> str:
        assert text == "<p>本文</p>"
        return "本文"

    monkeypatch.setattr(core_normalize, "strip_html", strip)
    assert normalize.strip_html("<p>本文</p>") == "本文"


def test_compute_body_hash_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    def compute(body: str) -> str:
        assert body == "<p>本文</p>"
        return "a" * 64

    monkeypatch.setattr(core_normalize, "compute_body_hash", compute)
    assert normalize.compute_body_hash("<p>本文</p>") == "a" * 64


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/path/", "https://example.com/path/"),
        ("https://arxiv.org/abs/2301.12345v2", "https://arxiv.org/abs/2301.12345v2"),
        (
            "https://example.com/?twclid=x&mc_cid=y&UTM_SOURCE=z#top",
            "https://example.com/?twclid=x&mc_cid=y",
        ),
    ],
)
def test_url_keeps_qa_radar_rules(url: str, expected: str) -> None:
    assert normalize.normalize_url(url) == expected


@pytest.mark.parametrize(("max_chars", "expected"), [(0, "…"), (-1, "abcde…")])
def test_snippet_keeps_nonpositive_limit_behavior(max_chars: int, expected: str) -> None:
    assert normalize.make_snippet("abcdef", max_chars) == expected


def test_snippet_keeps_ellipsis_outside_limit() -> None:
    assert normalize.make_snippet("あ" * 101) == "あ" * 100 + "…"
