"""pytest 共通設定. integration marker のスキップ制御."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--integration` フラグを追加する."""
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="ネットワーク経由の統合テストを実行する",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`--integration` 未指定時は integration marker のテストをスキップ."""
    if config.getoption("--integration"):
        return
    skip_integration = pytest.mark.skip(reason="--integration フラグが必要")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
