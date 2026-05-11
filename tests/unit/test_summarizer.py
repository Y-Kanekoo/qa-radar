"""summarizer/anthropic_client.py のユニットテスト. anthropic を mock で代替する."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from qa_radar.summarizer.anthropic_client import (
    DEFAULT_MODEL,
    ENV_API_KEY,
    is_available,
    summarize,
)

# ---------------- is_available ----------------


def test_is_available_false_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    assert is_available() is False


def test_is_available_false_with_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "")
    assert is_available() is False


def test_is_available_false_when_anthropic_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """anthropic がインポート不可なら False."""
    monkeypatch.setenv(ENV_API_KEY, "sk-test")
    # import を失敗させる: sys.modules で偽の ImportError 経路
    with patch.dict("sys.modules", {"anthropic": None}):
        assert is_available() is False


def test_is_available_true_when_env_and_pkg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "sk-test")
    fake_anthropic = MagicMock()
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        assert is_available() is True


# ---------------- summarize ----------------


def test_summarize_raises_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        summarize("text")


def test_summarize_calls_anthropic_with_expected_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """API を mock し、正しい引数で呼ばれることを確認."""
    monkeypatch.setenv(ENV_API_KEY, "sk-test")

    fake_block = MagicMock()
    fake_block.text = "要約された3行のテキスト"
    fake_msg = MagicMock()
    fake_msg.content = [fake_block]

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = summarize("元の本文 100 字程度", max_tokens=200)

    assert result == "要約された3行のテキスト"
    # 呼出し検証
    call = fake_client.messages.create.call_args
    assert call.kwargs["model"] == DEFAULT_MODEL
    assert call.kwargs["max_tokens"] == 200
    assert "system" in call.kwargs
    assert call.kwargs["messages"][0]["content"] == "元の本文 100 字程度"


def test_summarize_concatenates_multiple_text_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_API_KEY, "sk-test")

    block1 = MagicMock()
    block1.text = "前半 "
    block2 = MagicMock()
    block2.text = "後半"
    fake_msg = MagicMock()
    fake_msg.content = [block1, block2]

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = summarize("any text")

    assert result == "前半 後半"


def test_summarize_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "sk-test")

    block = MagicMock()
    block.text = "  要約 \n\n"
    fake_msg = MagicMock()
    fake_msg.content = [block]

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = summarize("any")

    assert result == "要約"


def test_summarize_handles_blocks_without_text_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    """text 属性のないブロック (画像等) はスキップする."""
    monkeypatch.setenv(ENV_API_KEY, "sk-test")

    block_text = MagicMock()
    block_text.text = "テキスト部"
    block_image = MagicMock(spec=[])  # text 属性なし

    fake_msg = MagicMock()
    fake_msg.content = [block_image, block_text]

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = summarize("any")

    assert result == "テキスト部"
