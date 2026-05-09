"""Phase 0: CI緑化のためのスモークテスト."""

from __future__ import annotations

import pytest

import qa_radar


def test_version() -> None:
    """パッケージのバージョン文字列が期待通り定義されている."""
    assert qa_radar.__version__ == "0.1.0"


def test_main_raises_until_phase5() -> None:
    """`main()` は Phase 5 完了まで SystemExit を投げる."""
    with pytest.raises(SystemExit):
        qa_radar.main()


def test_module_invocation_raises() -> None:
    """`python -m qa_radar` 経路でも SystemExit になる."""
    import runpy

    with pytest.raises(SystemExit):
        runpy.run_module("qa_radar", run_name="__main__")
