# qa-radar

> QA / テスト自動化のニュースアグリゲーター（公開RSS + ローカルMCP）

[![CI](https://github.com/Y-Kanekoo/qa-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/Y-Kanekoo/qa-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**qa-radar** は QA・テスト自動化に関する 44 のソース（日本語 + 英語）から、
記事・論文・ツールリリースを自動収集し、以下 3 つの形式で配信します:

1. **公開 RSS フィード** — GitHub Pages でホスト
2. **ローカル MCP サーバー** — Claude Desktop / Claude Code から自然言語で問い合わせ
3. **Discord webhook** — 新着記事をプッシュ通知

## 開発状況

🚧 現在開発中。

| Phase | 状態 |
|-------|------|
| 0. リポジトリ初期化 | ✅ |
| 1. クローラー + DB | ✅ |
| 2. タグ付け | ✅ |
| 3. RSS + Pages | ✅ |
| 4. Discord 通知 | ✅ |
| 5. MCP サーバー | ✅ |
| 6. PyPI 公開 | ⏳ workflow整備済み・未公開 |
| 7. cron 自動化 | ✅ |
| 8. LLM 要約 (任意) | ✅ |
| 9. ソース拡充 (30→40本) | ✅ |
| 10. 運用復旧 | ✅ |
| 11. AI/LLMテスティングソース追加 (40→44本) | ✅ |

## 差別化ポイント

| 機能 | qa-radar | yoshikiito/test-qa-rss-feed | 汎用 RSS-MCP |
|------|----------|-----------------------------|---------------|
| MCP 対応 | ✅ | ❌ | ✅ |
| 多言語対応（日本語+英語） | ✅ | 日本語のみ | 依存 |
| AI/ML タグ付け | ✅ ルールベース + LLM任意 | ❌ | ❌ |
| 全文検索 (FTS5) | ✅ | ❌ | ❌ |
| ツールリリース統合（13リポジトリ） | ✅ | ❌ | ❌ |
| 学術論文（arxiv） | ✅ | ❌ | ❌ |

## 集約ソース

44 ソース（5 カテゴリ、ja 16 / en 28）:
- **ツールリリース (13)**: Playwright / Cypress / Selenium / Jest / Vitest / pytest / Appium / k6 / Allure —
  Phase 11 で AI/LLM テスティングツール promptfoo / DeepEval / Giskard / Langfuse を追加
- **ブログ (20)**: Google Testing Blog, mabl, Applitools, BrowserStack, m3 Tech Blog, Cybozu, Sansan,
  KAKEHASHI, BASE, nihonbuson, kawaguti, goyoki, mybest (Zenn) ほか — Phase 9 で
  Snyk, TestRail, Maestro, Grafana Labs, Cypress Blog, Semaphore, Software Testing Magazine を追加
- **コミュニティ (6)**: Ministry of Testing, DEV.to (qa), Medium (test-automation) —
  Phase 9 で Qiita（テスト自動化 / QA タグ）, Zenn（testing トピック）を追加
- **note (4)**: 秋山浩一, tarappo, 湯本剛, QAを楽しむ者
- **論文 (1)**: arxiv cs.SE

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
