"""tag_rules.yaml のロードと frozen dataclass 定義."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# config/tag_rules.yaml の既定パス (src/qa_radar/tagger/rules.py から4階層上)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_TAG_RULES_PATH = _REPO_ROOT / "config" / "tag_rules.yaml"


@dataclass(frozen=True)
class TagRule:
    """1タグのキーワードルール."""

    tag: str
    keywords: tuple[str, ...]  # 全て小文字化済
    requires_co_tag: bool


@dataclass(frozen=True)
class CoOccurrenceRule:
    """共起ルール (特定キーワードの出現で別タグを補完)."""

    if_any: tuple[str, ...]  # キーワードリスト (小文字、いずれか1つ以上ヒットで発火)
    if_all: tuple[str, ...]  # キーワードリスト (小文字、全ヒットで発火)
    add: tuple[str, ...]  # 補完するタグ


@dataclass(frozen=True)
class TaggerConfig:
    """tag_rules.yaml 全体をロードした結果."""

    rules: tuple[TagRule, ...]
    co_occurrence: tuple[CoOccurrenceRule, ...]
    # ソース別固定タグ: ((slug, (tag, ...)), ...). frozen を保つため tuple 構造.
    source_tags: tuple[tuple[str, tuple[str, ...]], ...]
    max_tags: int
    threshold: int
    weight_title: int
    weight_body: int

    def get_source_tags(self, slug: str) -> tuple[str, ...]:
        """指定 slug のソース固定タグを返す. 未定義なら空 tuple."""
        for s, tags in self.source_tags:
            if s == slug:
                return tags
        return ()


def load_tagger_config(path: Path = DEFAULT_TAG_RULES_PATH) -> TaggerConfig:
    """tag_rules.yaml を読み TaggerConfig を返す.

    Args:
        path: YAML パス. 既定は `config/tag_rules.yaml`.

    Returns:
        TaggerConfig.

    Raises:
        FileNotFoundError: ファイル不在.
        KeyError: 必須キー欠落.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {}) or {}

    rules = tuple(
        TagRule(
            tag=str(r["tag"]),
            keywords=tuple(str(k).lower() for k in (r.get("keywords") or [])),
            requires_co_tag=bool(r.get("requires_co_tag", False)),
        )
        for r in (data.get("rules") or [])
    )

    co_occurrence = tuple(
        CoOccurrenceRule(
            if_any=tuple(str(k).lower() for k in (c.get("if_any") or [])),
            if_all=tuple(str(k).lower() for k in (c.get("if_all") or [])),
            add=tuple(str(t) for t in (c.get("add") or [])),
        )
        for c in (data.get("co_occurrence") or [])
    )

    source_tags_raw = data.get("source_tags") or {}
    source_tags = tuple(
        (str(slug), tuple(str(t) for t in (tags or []))) for slug, tags in source_tags_raw.items()
    )

    return TaggerConfig(
        rules=rules,
        co_occurrence=co_occurrence,
        source_tags=source_tags,
        max_tags=int(defaults.get("max_tags", 3)),
        threshold=int(defaults.get("threshold", 2)),
        weight_title=int(defaults.get("weight_title", 2)),
        weight_body=int(defaults.get("weight_body", 1)),
    )
