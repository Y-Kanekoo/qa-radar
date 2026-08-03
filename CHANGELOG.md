# Changelog

All notable changes to qa-radar will be documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Phase C-3**: クロスソース転載重複の配線と DB マイグレーション基盤
  - `src/qa_radar/db.py`: バージョン別マイグレーション関数を逐次適用する基盤を追加
    (`MIGRATIONS` dict + `_apply_migrations()`)。列追加のような ALTER を伴う変更に対応。
    各適用は `BEGIN IMMEDIATE` で開始し、トランザクション内でバージョンを読み直すため、
    別プロセス (常駐 MCP サーバ等) と同時実行しても二重適用しない
  - schema **v3**: `articles.duplicate_of INTEGER REFERENCES articles(id)` を追加
    (NULL = 非重複)。新規 DB の CREATE と v2→v3 の ALTER の両方に反映
  - クロール時に別ソースの同一 body_hash を検出し `duplicate_of` でマーク
    (記事は削除せず保持 = 誤判定から復元可能)。正規化本文 200 文字未満は
    定型文のハッシュ衝突を避けるため判定しない
  - **元記事の定義**: 同一本文グループのうち `published_at` が最古の記事。転載を先に
    クロールした後で本家が届いた場合は、本家を元記事にしてグループ全体の
    `duplicate_of` を付け替える (INSERT と同一トランザクション)
  - 出力からの除外: Discord 通知 (`fetch_unnotified`)、RSS/Pages の記事一覧・
    ソース別件数・タグ集計、MCP の `list_recent` / `list_sources` / `list_tags`。
    MCP の `search_articles` だけはコーパス全体の発見性を優先して除外しない
  - クロールサマリに「重複マーク件数」を追加 (`scripts/run_crawl.py`)
  - **既知の制限**: v3 マイグレーションはバックフィルを行わない。移行前から DB にある
    転載重複は `duplicate_of` が NULL のままで、guid 重複により再 INSERT もされないため、
    一覧・通知・集計に出続ける。除外対象は v3 化以降に新規取得した記事のみ

- **Phase B**: 監視・信頼性向上。過去に GitHub Pages 設定ミスで約2ヶ月間 crawl.yml が
  全滅していても誰も気づけなかった事故を踏まえ、「失敗が握りつぶされる/区別できない」
  構造を解消
  - `crawl.yml`: Discord 通知失敗を握りつぶさず (`continue-on-error` + 可視化)、
    独立ジョブ `alert` で crawl-and-build / deploy いずれかの失敗や通知の部分失敗を
    専用 Discord webhook (`DISCORD_ALERT_WEBHOOK_URL`) に通知。新規スクリプト
    `scripts/notify_alert.sh` (POSIX sh + curl + jq)
    - `Crawl + tag` ステップに `shell: bash` を明示し pipefail を有効化(既定シェルだと
      `run_crawl.py | tee` の失敗が tee の exit 0 に隠れて検知できなかった問題を修正)
  - `scripts/run_crawl.py`: 全ソース失敗時に exit 1 を返すよう修正(従来は常に exit 0
    で全滅でも成功扱いだった)。部分失敗時は `::warning title=Crawl partial failure::`
    annotation を出力
  - `src/qa_radar/crawler/store.py`: `get_repeatedly_failing_sources()` を追加し、
    書き込まれるだけだった `consecutive_errors` を読み取って N 回(既定9=1日3回×3日)
    連続失敗しているソースを `run_crawl.py` のサマリで警告表示(自動無効化はしない)
  - `src/qa_radar/crawler/fetch.py`: `fetch_feed()` にタイムアウト/接続エラー/5xx を
    対象とした指数バックオフ付きリトライ(最大2回、計3試行)を追加。4xx・robots.txt
    拒否・パース失敗はリトライ対象外
- **Phase 11**: AI/LLM testing ソース 4 本追加 (40→44 本)。差別化タグ `ai-testing`
  に専門ソースがなかった問題を解消
  - GitHub Releases Atom (tool カテゴリ): promptfoo, DeepEval (confident-ai), Giskard, Langfuse
  - `config/tag_rules.yaml` の `source_tags` に各ソースを `[ai-testing, tooling]` で固定付与
  - `ragas` (explodinggradients/ragas) は直近リリースが2026-01-13で以降6ヶ月活動なしのため見送り
  - Autify ブログ日本語版はRSSフィード自体が見つからず(サイトが403を返しegressポリシーでも
    ブロック対象)、見送り
  - 44 ソース内訳: tool 13 / blog 20 / community 6 / note 4 / paper 1 (language: ja 16 / en 28)
- **Phase C-2**: ソース健全性の週次 Discord レポート。実フィードへの追加アクセスは行わず、
  本番 cron (`crawl.yml`) が既に DB に書き込んでいる信号を集計するだけにとどめる設計
  - `.github/workflows/health.yml` (新規): 毎週月曜 09:00 JST + `workflow_dispatch`。
    DB スナップショット復元 → `scripts/health_report.py` → 実フィード疎通の統合テスト
    (`pytest --integration -v -m integration`、代表2ソースのみ) の順で実行
  - `src/qa_radar/crawler/store.py`: `get_sources_with_errors` / `get_source_staleness` /
    `get_overall_stats` を追加 (週次レポート専用の読み取り関数)
  - `scripts/health_report.py` (新規): 上記を集計して digest を組み立て Discord へ送信する CLI。
    429 Rate Limit は Retry-After に従って再送 (`src/qa_radar/publisher/discord.py` と同方針)。
    webhook URL は事実上のシークレットのため、ログ・例外経路のいずれにも出力しない
  - `docs/operations.md`: 「週次ヘルスレポートの見方」節を追加
- **Phase C-4**: MCP サーバーの実プロトコル E2E テスト追加(`tests/unit/test_server_e2e.py`)。
  `mcp.shared.memory.create_connected_server_and_client_session()` で in-memory ストリーム上に
  initialize ハンドシェイク込みの実 JSON-RPC 接続を張り、tool スキーマ・実呼び出し・lifespan
  異常系・`summarize_article` の条件付き登録を検証。`src/qa_radar/server.py` のカバレッジが
  55% → 94% に向上

### Fixed

- `scripts/publish_release.py`: release 保持判定を `publishedAt` 基準に一本化
  (`createdAt` フォールバックを削除、`publishedAt` が null の release はスキップ)。
  TypedDict 化して型を明確化

## [0.2.0] - 2026-07-10

Phase 6〜9 の成果物を統合. PyPI Trusted Publishing ワークフローは実装済みだが、
本バージョン時点でも `v*.*.*` タグの push は未実施のため **PyPI への実公開はまだ行われていない**
(`uvx qa-radar` は次回の実タグ push 後に有効になる予定).

### Added

- **Phase 6**: PyPI 公開用 Trusted Publishing ワークフロー (`pypi.yml`、OIDC 経由、`v*.*.*` タグ push で TestPyPI → PyPI へ公開)
- **Phase 7**: cron 自動化 (`crawl.yml`、毎日 9:00 / 15:00 / 21:00 JST に定期クロール) + DB スナップショットの GitHub Releases 配布 (`scripts/publish_release.py`)
- **Phase 8**: LLM 要約 (`summarize_article` MCP tool、Claude Haiku 4.5、opt-in — `ANTHROPIC_API_KEY` env + `pip install qa-radar[ai]` が必要)
- **Phase 9**: QA ソース 10 本追加 (30→40 本)
  - 英語ブログ 7 本: Snyk Blog, TestRail Blog, Maestro Blog, Grafana Labs Blog, Cypress Blog, Semaphore CI/CD Blog, Software Testing Magazine
  - 日本語コミュニティ 3 本: Qiita タグ「テスト自動化」「QA」, Zenn トピック「testing」
  - 40 ソース内訳: tool 9 / blog 20 / community 6 / note 4 / paper 1 (language: ja 16 / en 24)

### Fixed

- `scripts/publish_release.py`: DB スナップショットの retention 判定を `createdAt`(タグが指すコミットのコミット日時)
  から `publishedAt`(release の実際の公開日時)基準に修正。main への直近コミットから日数が経過していると
  作成直後のスナップショットが同一実行内で即削除される不具合を解消し、あわせて最新 release は経過日数に
  関わらず削除しない安全ガードを追加
- `.github/workflows/crawl.yml`: `pages_artifact` の job output が実際にはセットされない不具合を修正
  (該当ステップに `id: mark_uploaded` を付与し、output 参照を対応させた)
- `src/qa_radar/publisher/discord.py` / `scripts/notify_discord.py`: Discord 通知の部分失敗時に
  通知済みマークがずれる不具合を修正。`send_batch` が成功/失敗の件数のみを返す設計だったため、
  `notify_discord.py` は「先頭 success 件を通知済みマークする」実装になっており、途中の1件が
  失敗すると以降の記事の成否と位置がずれて誤マークされ得た。`send_batch` の戻り値を
  article_id 単位の成否を保持する `BatchSendResult` に変更し、実際に成功した記事だけを
  mark するよう修正

### Changed

- README.md / README.ja.md の Status 表を実態(Phase 0〜9 完了)に更新し、
  `9. Source expansion (30→40)` / `9. ソース拡充 (30→40本)` の行を追加
- README.md / README.ja.md のソース件数表記を「30+」から実数「40」に更新し、
  カテゴリ内訳(tool 9 / blog 20 / community 6 / note 4 / paper 1)を実態に合わせて修正
- CHANGELOG の `[0.1.0]` 節にあった「初回 PyPI リリース」という記載を実態に合わせて訂正
  (下記参照。実際の初回公開はまだ行われていない)

## [0.1.0] - 2026-05-12

Phase 0〜5 の成果物を統合. ※当初「初回 PyPI リリース」と記載していたが、実際には
`v0.1.0` タグの push が行われず PyPI へは一度も公開されていなかった。PyPI への実公開は
v0.2.0 以降で行う予定(訂正: 2026-07-10).

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

Claude Desktop / Claude Code への登録例は [README](README.md) を参照.
(この時点では PyPI 未公開のため `uvx qa-radar` は動作しない。ローカルクローンからの
`uv run python -m qa_radar` 起動が唯一の実用手段だった)

[Unreleased]: https://github.com/Y-Kanekoo/qa-radar/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Y-Kanekoo/qa-radar/releases/tag/v0.2.0
[0.1.0]: https://github.com/Y-Kanekoo/qa-radar/releases/tag/v0.1.0
