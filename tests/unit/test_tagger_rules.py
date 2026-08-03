"""tagger/rules.py のユニットテスト."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa_radar.tagger.rules import (
    CoOccurrenceRule,
    TaggerConfig,
    TagRule,
    load_tagger_config,
)


def test_loads_real_tag_rules() -> None:
    """config/tag_rules.yaml が正常にパースできる."""
    config = load_tagger_config()
    assert isinstance(config, TaggerConfig)
    assert len(config.rules) >= 10


def test_keywords_are_lowercased() -> None:
    config = load_tagger_config()
    for rule in config.rules:
        for keyword in rule.keywords:
            assert keyword == keyword.lower(), f"{rule.tag}: {keyword!r} が小文字化されていない"


def test_critical_tags_present() -> None:
    """10個の固定タグが全て定義されている."""
    config = load_tagger_config()
    expected = {
        "e2e",
        "unit",
        "integration",
        "performance",
        "security",
        "api",
        "mobile",
        "ai-testing",
        "process",
        "tooling",
    }
    actual = {r.tag for r in config.rules}
    missing = expected - actual
    assert not missing, f"必須タグが欠落: {missing}"


def test_tooling_requires_co_tag() -> None:
    """tooling タグは requires_co_tag=True."""
    config = load_tagger_config()
    tooling = next((r for r in config.rules if r.tag == "tooling"), None)
    assert tooling is not None
    assert tooling.requires_co_tag is True


def test_defaults_loaded() -> None:
    config = load_tagger_config()
    assert config.max_tags == 3
    assert config.threshold == 2
    assert config.weight_title == 2
    assert config.weight_body == 1


def test_load_with_custom_yaml(tmp_path: Path) -> None:
    yaml_text = """
version: 1
defaults:
  max_tags: 5
  threshold: 3
  weight_title: 4
  weight_body: 2
rules:
  - tag: foo
    keywords: [Foo, BAR]
  - tag: bar
    keywords: [baz]
    requires_co_tag: true
co_occurrence:
  - if_any: [foo]
    add: [auto]
source_tags:
  src1: [foo, bar]
  src2: [auto]
"""
    p = tmp_path / "rules.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    config = load_tagger_config(p)
    assert config.max_tags == 5
    assert config.threshold == 3
    assert config.weight_title == 4
    assert config.weight_body == 2
    assert len(config.rules) == 2
    foo = config.rules[0]
    assert foo.tag == "foo"
    assert foo.keywords == ("foo", "bar")  # 小文字化済
    assert foo.requires_co_tag is False
    bar = config.rules[1]
    assert bar.requires_co_tag is True
    assert len(config.co_occurrence) == 1
    co = config.co_occurrence[0]
    assert isinstance(co, CoOccurrenceRule)
    assert co.if_any == ("foo",)
    assert co.add == ("auto",)
    assert config.get_source_tags("src1") == ("foo", "bar")
    assert config.get_source_tags("src2") == ("auto",)
    assert config.get_source_tags("unknown") == ()


def test_real_yaml_has_source_tags_for_tools() -> None:
    """実際の tag_rules.yaml に主要ツールの source_tags が定義されている."""
    config = load_tagger_config()
    assert config.get_source_tags("playwright-releases") == ("e2e", "tooling")
    assert config.get_source_tags("pytest-releases") == ("unit", "tooling")
    assert config.get_source_tags("appium-releases") == ("mobile", "tooling")
    assert config.get_source_tags("k6-releases") == ("performance", "tooling")
    assert config.get_source_tags("allure-releases") == ("tooling", "process")


def test_empty_yaml_returns_empty_config(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("version: 1\n", encoding="utf-8")
    config = load_tagger_config(p)
    assert config.rules == ()
    assert config.co_occurrence == ()


def test_tag_rule_dataclass_is_frozen() -> None:
    import dataclasses

    import pytest

    rule = TagRule(tag="x", keywords=("a", "b"), requires_co_tag=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.tag = "y"  # type: ignore[misc]


def test_unknown_defaults_key_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "rules.yaml"
    p.write_text("defaults: {mystery_key: 1, max_tags: 3}\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "mystery_key" in str(exc_info.value)


def test_unknown_rule_key_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "rules.yaml"
    p.write_text(
        "rules: [{tag: foo, keywords: [bar], mystery_key: 1}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "mystery_key" in str(exc_info.value)


def test_unknown_co_occurrence_key_raises_value_error(tmp_path: Path) -> None:
    """co_occurrence の typo (例: if_anyy) は無発火のデッドルールになるため検出する."""
    p = tmp_path / "rules.yaml"
    p.write_text(
        "co_occurrence: [{if_anyy: [foo], add: [tooling]}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "if_anyy" in str(exc_info.value)


def test_unknown_top_level_key_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "rules.yaml"
    p.write_text("version: 1\nmystery_top_key: 1\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "mystery_top_key" in str(exc_info.value)


def test_invalid_source_tags_shape_raises_value_error(tmp_path: Path) -> None:
    """source_tags の値がリストでない (例: 文字列) 場合は1文字ずつ分解される
    サイレントな設定ミスになるため検出する."""
    p = tmp_path / "rules.yaml"
    p.write_text("source_tags: {foo: e2e}\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "source_tags" in str(exc_info.value)


def test_source_tags_not_a_mapping_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "rules.yaml"
    p.write_text("source_tags: [foo, bar]\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "source_tags" in str(exc_info.value)


def test_non_dict_rule_element_raises_value_error(tmp_path: Path) -> None:
    """rules の要素が dict でない場合も AttributeError ではなく
    日本語 ValueError に統一する."""
    p = tmp_path / "rules.yaml"
    p.write_text("rules: [null]\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "rules[0]" in str(exc_info.value)


def test_non_dict_co_occurrence_element_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "rules.yaml"
    p.write_text("co_occurrence: ['e2e']\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_tagger_config(p)

    assert "co_occurrence[0]" in str(exc_info.value)
