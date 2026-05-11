# qa-radar

> QA / Test Automation news aggregator with public RSS feed and local MCP server.

[![CI](https://github.com/Y-Kanekoo/qa-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/Y-Kanekoo/qa-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**qa-radar** collects articles, papers, and tool releases from 30+ QA/testing
sources (Japanese + English) and serves them through three channels:

1. **Public RSS feed** — hosted on GitHub Pages
2. **Local MCP server** — query the corpus from Claude Desktop / Claude Code
3. **Discord webhook** — push notifications for new articles

For the Japanese readme, see [README.ja.md](README.ja.md).

## Status

🚧 Under active development.

| Phase | Status |
|-------|--------|
| 0. Repo setup | ✅ |
| 1. Crawler + DB | 🚧 |
| 2. Tagger | ⏳ |
| 3. RSS + Pages | ⏳ |
| 4. Discord notifier | ⏳ |
| 5. MCP server | ✅ |
| 6. PyPI publish | ✅ |
| 7. Cron automation | ⏳ |
| 8. LLM summarizer (optional) | ⏳ |

## Differentiation

| Feature | qa-radar | yoshikiito/test-qa-rss-feed | Generic RSS-MCP |
|---------|----------|------------------------------|------------------|
| MCP support | ✅ | ❌ | ✅ |
| Multi-language (JP+EN) | ✅ | JP only | depends |
| AI/ML tagging | ✅ rule-based + opt LLM | ❌ | ❌ |
| Full-text search (FTS5) | ✅ | ❌ | ❌ |
| Tool releases (9 repos) | ✅ | ❌ | ❌ |
| Academic papers (arxiv) | ✅ | ❌ | ❌ |

## Sources

30 verified sources across 4 categories:
- **Tools (9)**: Playwright, Cypress, Selenium, Jest, Vitest, pytest, Appium, k6, Allure
- **Blogs (16)**: Google Testing Blog, mabl, Applitools, BrowserStack, m3 Tech, Cybozu, Sansan, etc.
- **Communities (3)**: Ministry of Testing, DEV.to (qa), Medium (test-automation)
- **Papers (1)**: arxiv cs.SE
- **Notes (5+)**: Akiyama Kouichi, tarappo, Yumoto Tsuyoshi, etc.

See [config/sources.yaml](config/sources.yaml) for the full list and
[docs/sources.md](docs/sources.md) for ToS analysis.

## MCP server usage

The local MCP server exposes 5 tools you can call from Claude Desktop / Claude Code:

- `search_articles(query, tags?, date_from?, date_to?, limit, offset)` — full-text search via SQLite FTS5+BM25
- `list_recent(days, source?, tag?, limit)` — recent articles
- `get_article(article_id, include_body)` — article details (body defaults to off)
- `list_sources()` — aggregated sources with counts
- `list_tags(min_count, limit)` — tag occurrence counts

Register it in your MCP client config (`claude_desktop_config.json` or `.mcp.json`):

```json
{
  "mcpServers": {
    "qa-radar": {
      "command": "uvx",
      "args": ["qa-radar"],
      "env": {
        "QA_RADAR_DB_PATH": "/path/to/articles.db"
      }
    }
  }
}
```

(`uvx qa-radar` pulls the latest published version from PyPI on first run and caches it.)

If you want to pin a specific version or pre-release, use `uvx qa-radar==0.1.0` or
`uvx --from git+https://github.com/Y-Kanekoo/qa-radar qa-radar` for the latest `main`.

For development with a local clone:

```json
{
  "mcpServers": {
    "qa-radar": {
      "command": "uv",
      "args": ["--directory", "/path/to/qa-radar", "run", "python", "-m", "qa_radar"]
    }
  }
}
```

DB path resolution order: `QA_RADAR_DB_PATH` env var > repo-local `data/articles.db` > `~/Library/Caches/qa-radar/articles.db` (macOS).


## Development setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Y-Kanekoo/qa-radar.git
cd qa-radar
uv sync --all-extras --dev
uv run pytest -v
uv run ruff check .

# Start the MCP server locally (stdio) for manual testing
uv run python -m qa_radar
# Or open the MCP Inspector:
uv run mcp dev src/qa_radar/server.py
```

## License

MIT — see [LICENSE](LICENSE). For data handling guidelines, see [NOTICE](NOTICE).
