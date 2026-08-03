"""tag_rules.yaml のロードと frozen dataclass 定義."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# config/tag_rules.yaml の既定パス (src/qa_radar/tagger/rules.py から4階層上)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_TAG_RULES_PATH = _REPO_ROOT / "config" / "tag_rules.yaml"

# defaults: 配下で load_tagger_config() が解釈する既知キー.
# `case_sensitive` は現状スコアリングロジック (常に小文字化して比較) では未使用だが、
# 将来の大文字小文字区別マッチング用に予約されたドキュメント済みキーのため許容する.
_KNOWN_DEFAULTS_KEYS = frozenset(
    {
        "case_sensitive",
        "max_tags",
        "threshold",
        "weight_title",
        "weight_body",
    }
)

# rules: の各エントリで load_tagger_config() が解釈する既知キー.
_KNOWN_RULE_KEYS = frozenset({"tag", "keywords", "requires_co_tag"})

# co_occurrence: の各エントリで load_tagger_config() が解釈する既知キー.
_KNOWN_CO_OCCURRENCE_KEYS = frozenset({"if_any", "if_all", "add"})

# tag_rules.yaml トップレベルで load_tagger_config() が解釈する既知キー.
_KNOWN_TOP_LEVEL_KEYS = frozenset({"version", "defaults", "rules", "co_occurrence", "source_tags"})


def _reject_unknown_keys(mapping: object, known: frozenset[str], context: str) -> None:
    """`mapping` が dict でない、または `known` にないキーを含む場合に ValueError を送出する.

    Args:
        mapping: YAML から読んだ値 (トップレベル辞書、defaults 全体、
            rules/co_occurrence の1エントリ等). dict でない場合もここで検出する.
        known: 許容するキー集合.
        context: エラーメッセージに含める文脈 (例: "defaults", "rules[2] (tag=e2e)").

    Raises:
        ValueError: `mapping` が dict でない場合、または未知キーが1つ以上ある場合.
            いずれも日本語メッセージに文脈とキー名/値を含める.
    """
    if not isinstance(mapping, dict):
        raise ValueError(
            f"tag_rules.yaml の {context} はマッピング (dict) である必要があります: {mapping!r}"
        )
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise ValueError(
            f"tag_rules.yaml の {context} に未知のキーがあります: {unknown}. "
            f"既知キー: {sorted(known)}"
        )


def _reject_invalid_source_tags(source_tags_raw: object) -> None:
    """`source_tags` の形状 (slug -> タグのリスト) を検証する.

    `source_tags` はキーがソース slug (任意の文字列) であり、既知キー検証の
    対象にはならない。代わりに値がリストであることを検証する。値が文字列の
    場合、後続処理の `str(t) for t in tags` がリストではなく文字列の1文字ずつを
    イテレートしてしまい (例: `source_tags: {foo: e2e}` と誤って書いた場合)、
    タグが1文字ごとに分解されるサイレントな設定ミスになるため.

    Raises:
        ValueError: `source_tags_raw` が dict でない、またはいずれかの値が
            リストでない場合. 日本語メッセージに slug と値を含める.
    """
    if not isinstance(source_tags_raw, dict):
        raise ValueError(
            f"tag_rules.yaml の source_tags はマッピング (dict) である必要があります: {source_tags_raw!r}"
        )
    for slug, tags in source_tags_raw.items():
        if not isinstance(tags, list):
            raise ValueError(
                f"tag_rules.yaml の source_tags['{slug}'] はリストである必要があります: {tags!r}"
            )


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
        ValueError: トップレベル/defaults/rules/co_occurrence に未知キーがある場合、
            rules/co_occurrence の各要素が dict でない場合、
            または source_tags の形状 (slug -> リスト) が不正な場合.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    _reject_unknown_keys(data, _KNOWN_TOP_LEVEL_KEYS, "トップレベル")

    defaults = data.get("defaults", {}) or {}
    _reject_unknown_keys(defaults, _KNOWN_DEFAULTS_KEYS, "defaults")

    raw_rules = data.get("rules") or []
    for i, r in enumerate(raw_rules):
        context = f"rules[{i}] (tag={r.get('tag')!r})" if isinstance(r, dict) else f"rules[{i}]"
        _reject_unknown_keys(r, _KNOWN_RULE_KEYS, context)

    rules = tuple(
        TagRule(
            tag=str(r["tag"]),
            keywords=tuple(str(k).lower() for k in (r.get("keywords") or [])),
            requires_co_tag=bool(r.get("requires_co_tag", False)),
        )
        for r in raw_rules
    )

    raw_co_occurrence = data.get("co_occurrence") or []
    for i, c in enumerate(raw_co_occurrence):
        _reject_unknown_keys(c, _KNOWN_CO_OCCURRENCE_KEYS, f"co_occurrence[{i}]")

    co_occurrence = tuple(
        CoOccurrenceRule(
            if_any=tuple(str(k).lower() for k in (c.get("if_any") or [])),
            if_all=tuple(str(k).lower() for k in (c.get("if_all") or [])),
            add=tuple(str(t) for t in (c.get("add") or [])),
        )
        for c in raw_co_occurrence
    )

    source_tags_raw = data.get("source_tags") or {}
    _reject_invalid_source_tags(source_tags_raw)
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
