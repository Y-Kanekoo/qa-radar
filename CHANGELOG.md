# Changelog

All notable changes to qa-radar will be documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

(No changes since v0.1.0)

## [0.1.0] - 2026-05-12

初回 PyPI リリース. Phase 0〜5 の成果物を統合.

### Added

- **Phase 0**: リポジトリ初期化 (CI、MIT License、Issue/PR テンプレート、30 ソース定義)
- **Phase 1**: クローラー (`feedparser` + `httpx` + ETag/If-Modified-Since + robots.txt 遵守)
- **Phase 1**: SQLite + FTS5 (外部 content、WAL、冪等 init、`crawl_runs` テーブル)
- **Phase 2**: タガー (10 固定タグ、ルールベース + 共起 + ソース固定タグ)
- **Phase 3**: RSS/Atom 生成 (`feedgen`、メイン + タグ別)、GitHub Pages HTML
- **Phase 3**: GitHub Pages デプロイ ワークフロー (`actions/deploy-pages@v4`)
- **Phase 4**: Discord webhook 通知 (embed 形式、429 再試行、レート制限)
- **Phase 4**: DB スキーマ v2 (`article_notifications` テーブル、チャネル別重複通知防止)
- **Phase 5**: MCP サーバー (`FastMCP`、stdio、5 tool: search_articles / list_recent / get_article / list_sources / list_tags)
- 30 ソース (ツール releases 9 / 企業ブログ 9 / コミュニティ 3 / 日本個人ブログ 3 / note.com 4 / Zenn 1 / arxiv 1)
- 219 unit + 2 integration テスト、coverage 94.43%
- 47条の5境界を型で担保 (本文非露出、抜粋 ≤100字、出所明示)

### MCP tools

- `search_articles(query, tags?, date_from?, date_to?, limit, offset)`
- `list_recent(days, source?, tag?, limit)`
- `get_article(article_id, include_body=False)`
- `list_sources()`
- `list_tags(min_count, limit)`

### インストール (本リリース以降)

```bash
uvx qa-radar  # MCP サーバー起動 (stdio)
```

Claude Desktop / Claude Code への登録例は [README](README.md) を参照.

[Unreleased]: https://github.com/Y-Kanekoo/qa-radar/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Y-Kanekoo/qa-radar/releases/tag/v0.1.0
