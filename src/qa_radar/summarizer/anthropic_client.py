"""Claude Haiku 4.5 で記事を要約する.

`anthropic` パッケージは optional. import 失敗時は `is_available()` が False を返す.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("qa_radar.summarizer")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 300
ENV_API_KEY = "ANTHROPIC_API_KEY"

_SYSTEM_PROMPT = (
    "あなたは QA / テスト自動化の記事を要約するアシスタントです. "
    "与えられた記事を日本語で 3 行 (各行 ≤60字) に要約してください. "
    "本文をそのまま引用せず、自分の言葉で書き直してください. 要約のみ返してください."
)


def is_available() -> bool:
    """環境変数 ANTHROPIC_API_KEY が設定されていれば True.

    anthropic パッケージが import 不可なら False.
    """
    if not os.environ.get(ENV_API_KEY, "").strip():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        logger.warning("anthropic パッケージが未インストール. `pip install qa-radar[ai]`")
        return False
    return True


def summarize(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """記事本文を 3 行に要約する.

    Args:
        text: 要約対象テキスト.
        model: Claude モデル ID. 既定 claude-haiku-4-5.
        max_tokens: 出力上限トークン.

    Returns:
        要約文字列.

    Raises:
        RuntimeError: ANTHROPIC_API_KEY が未設定 or anthropic 未インストール.
    """
    if not is_available():
        raise RuntimeError(
            "ANTHROPIC_API_KEY が未設定、または anthropic パッケージが未インストールです."
        )

    # 遅延 import で `ai` extra 未インストール環境でもモジュール自体は import 可能に保つ
    from anthropic import Anthropic

    client = Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    # content は TextBlock リスト. text 属性を集約
    parts: list[str] = []
    for block in msg.content:
        text_attr = getattr(block, "text", None)
        if text_attr:
            parts.append(text_attr)
    return "".join(parts).strip()
