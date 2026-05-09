# qa-radar

> QA / テスト自動化のニュースアグリゲーター（公開RSS + ローカルMCP）

[![CI](https://github.com/Y-Kanekoo/qa-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/Y-Kanekoo/qa-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**qa-radar** は QA・テスト自動化に関する 30 以上のソース（日本語 + 英語）から、
記事・論文・ツールリリースを自動収集し、以下 3 つの形式で配信します:

1. **公開 RSS フィード** — GitHub Pages でホスト
2. **ローカル MCP サーバー** — Claude Desktop / Claude Code から自然言語で問い合わせ
3. **Discord webhook** — 新着記事をプッシュ通知

## 開発状況

🚧 現在開発中。

| Phase | 状態 |
|-------|------|
| 0. リポジトリ初期化 | ✅ |
| 1. クローラー + DB | 🚧 |
| 2. タグ付け | ⏳ |
| 3. RSS + Pages | ⏳ |
| 4. Discord 通知 | ⏳ |
| 5. MCP サーバー | ⏳ |
| 6. PyPI 公開 | ⏳ |
| 7. cron 自動化 | ⏳ |
| 8. LLM 要約 (任意) | ⏳ |

## 差別化ポイント

| 機能 | qa-radar | yoshikiito/test-qa-rss-feed | 汎用 RSS-MCP |
|------|----------|-----------------------------|---------------|
| MCP 対応 | ✅ | ❌ | ✅ |
| 多言語対応（日本語+英語） | ✅ | 日本語のみ | 依存 |
| AI/ML タグ付け | ✅ ルールベース + LLM任意 | ❌ | ❌ |
| 全文検索 (FTS5) | ✅ | ❌ | ❌ |
| ツールリリース統合（9リポジトリ） | ✅ | ❌ | ❌ |
| 学術論文（arxiv） | ✅ | ❌ | ❌ |

## 集約ソース

30 ソース（カテゴリ別）:
- **ツールリリース (9)**: Playwright / Cypress / Selenium / Jest / Vitest / pytest / Appium / k6 / Allure
- **ブログ (16)**: Google Testing Blog, mabl, Applitools, BrowserStack, m3 Tech Blog, Cybozu, Sansan ほか
- **コミュニティ (3)**: Ministry of Testing, DEV.to (qa), Medium (test-automation)
- **論文 (1)**: arxiv cs.SE
- **個人ブログ・note (5+)**: 秋山浩一, tarappo, 湯本剛 ほか

詳細は [config/sources.yaml](config/sources.yaml)、利用規約状況は
[docs/sources.md](docs/sources.md) を参照。

## 開発環境

Python 3.11+ と [uv](https://docs.astral.sh/uv/) が必要です。

```bash
git clone https://github.com/Y-Kanekoo/qa-radar.git
cd qa-radar
uv sync --all-extras --dev
uv run pytest -v
uv run ruff check .
```

## ライセンス

MIT — [LICENSE](LICENSE) 参照。データ取扱いガイドラインは [NOTICE](NOTICE) を参照。
