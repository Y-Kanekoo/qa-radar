"""正規化層. URL / 日付 / 本文 / ハッシュをDB保存可能な形に整える.

47条の5 軽微利用の境界として `make_snippet` は **必ず** 100字以下に切り詰める.
これより長いテキストを公開してはいけない.
"""

from __future__ import annotations

import calendar
import hashlib
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 削除する追跡パラメータ. 大文字小文字区別なし.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "yclid",
        "ref",
        "ref_src",
    }
)


def normalize_url(url: str) -> str:
    """URL から追跡パラメータと fragment を除去し、scheme/host を小文字化."""
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    qs = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in TRACKING_PARAMS]
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            urlencode(qs),
            "",  # fragment は削除
        )
    )


def normalize_published(struct_time: tuple[int, ...] | None) -> int:
    """published_parsed (UTC tuple) をUNIX秒に変換. None は現在時刻を返す."""
    if struct_time is None:
        return int(time.time())
    padded = struct_time + (0,) * (9 - len(struct_time))
    return calendar.timegm(padded)


class _StripTags(HTMLParser):
    """HTMLタグを除去しテキストノードのみ抽出する補助パーサ."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def strip_html(text: str) -> str:
    """HTMLタグを除去しエンティティをデコードする."""
    if not text:
        return ""
    parser = _StripTags()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError):
        # 壊れたHTMLの場合は元テキストをそのまま返す
        return text
    return parser.get_text()


_WS = re.compile(r"\s+")


def make_snippet(body: str, max_chars: int = 100) -> str:
    """本文から **100字以内** の抜粋を生成する (47条の5軽微利用境界).

    HTML を除去 → 連続空白を1つに → max_chars で切り詰め. 切る位置は60%以降に
    スペースが見つかればそこを単語境界として採用する (英語向け補正).

    Args:
        body: 元本文 (HTML 可).
        max_chars: 最大文字数. 既定100.

    Returns:
        抜粋. max_chars を超える場合は末尾に `…` を付ける.
    """
    plain = strip_html(body)
    plain = _WS.sub(" ", plain).strip()
    if len(plain) <= max_chars:
        return plain
    truncated = plain[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.6:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "…"


def compute_body_hash(body: str) -> str:
    """正規化後本文の SHA256 hex digest. クロスソース重複検出用."""
    plain = _WS.sub(" ", strip_html(body)).strip()
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()
