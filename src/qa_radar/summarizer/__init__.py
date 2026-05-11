"""LLM 要約モジュール (Phase 8).

ANTHROPIC_API_KEY が設定されている場合のみ MCP server で summarize_article tool が
有効化される. 未設定時は tool 自体が登録されないので、LLM 依存をオプションに
保てる. `anthropic` パッケージは optional-dependencies の `ai` extra に分離.

インストール:
    uvx --with anthropic qa-radar
    pip install qa-radar[ai]
"""
