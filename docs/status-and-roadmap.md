# qa-radar 現状調査とロードマップ (2026-07-10)

Phase 0〜9 完了時点の全体調査。コードベース・設定・GitHub 実運用状況を横断的に
調べた結果と、これからの開発プランをまとめる。

> **2026-07-31 追記**: 本文は Phase 0〜9 完了時点 (2026-07-10) の調査であり、
> 以下の項目は Phase 10 で解消済み。
> - GitHub Pages 有効化 (2026-07-30、Settings > Pages の Source を
>   `build_type=workflow` に設定)
> - DB スナップショット即削除バグ ([`4ad5400`](https://github.com/Y-Kanekoo/qa-radar/commit/4ad5400))
> - Discord 通知の部分失敗時マークずれ ([`e7fdc8f`](https://github.com/Y-Kanekoo/qa-radar/commit/e7fdc8f) 以降)
>
> 下表の技術的負債のうち、Discord 部分失敗時の誤マーク (P1、`e7fdc8f` で解消済み)、
> README の Phase 表 / ソース数表記乖離 (P1、本 PR #17 で解消済み)、`pages_artifact`
> output 未設定 (P3、既に解消済み) は解消済み。それ以外の項目は Phase C 相当として
> 引き続き有効。本文 (以下) は原調査時点の記録として全面書き換えはしていない。
>
> **2026-07-31 追記(2)**: 本 PR #18(Phase B: 監視・信頼性向上)で以下も解消済み。
> - fetch 層のリトライ/バックオフなし (P2) — `crawler/fetch.py` に 5xx / タイムアウト /
>   接続エラー対象の指数バックオフ付きリトライ (最大2回) を実装
> - `consecutive_errors` の読み取り側未実装 (P2) — `get_repeatedly_failing_sources()` を
>   追加し、`run_crawl.py` のサマリで N 回連続失敗ソースを workflow warning として可視化
> - crawl.yml の失敗握りつぶし(`notify_discord.py ... || true`)— `continue-on-error` +
>   独立 `alert` ジョブによる可視化に置き換え。`run_crawl.py` も全滅時に exit 1 を返すよう修正
>
> **2026-08-03 追記(3)**: PR #19(Phase C-3)で以下も解消済み。
> - クロスソース転載検出が未配線 (P2) — クロール時に `duplicate_of` をマークし、
>   RSS / Pages / Discord / MCP 一覧・集計から除外(検索のみ全コーパス対象を維持)
> - DB マイグレーションが ALTER 非対応 (P3) — `MIGRATIONS` による逐次適用基盤を実装し、
>   schema v3 (`articles.duplicate_of`) を ALTER TABLE で追加
> - 制限: 既存 DB のバックフィルは行わないため、v3 化前から存在する転載重複は除外されない

## TL;DR

**開発は高品質に完了しているが、本番運用は 3 チャネルとも機能していない。**

- コード: 240 テスト / カバレッジ 93%、47条の5 境界の型的担保など設計は一貫して高水準
- **GitHub Pages: リポジトリ設定で未有効化** → cron クロールが 2026-05-11 以来ほぼ全回失敗。
  RSS / HTML サイトは一度も公開されたことがない
- **PyPI: 未公開**(タグ push 0 回、pypi.yml 実行 0 回、pypi.org は 404)。
  README の `uvx qa-radar` は動作しない。CHANGELOG の「初回 PyPI リリース」記載は実態と乖離
- **Discord: `DISCORD_WEBHOOK_URL` シークレット未設定**で毎回スキップ
- **DB が蓄積されない実バグ**: `publish_release.py` が release の古さを `createdAt` で判定しているが、
  GitHub Releases の `createdAt` は「タグが指すコミットのコミット日時」。main への最終コミット
  (2026-05-24) から 7 日経過後は、**作成した直後のスナップショットが同一実行内で即削除**され、
  記事 DB が実行間で全く引き継がれない(現在 Releases は 0 件)

→ 新機能より先に「運用復旧 (Phase 10)」を最優先で行うべき。修正自体はいずれも小さい。

---

## 1. 現状

### 1.1 何ができているか

| コンポーネント | 実装 | 品質所見 |
|---|---|---|
| クローラー (`crawler/`) | ✅ | httpx + ETag/If-Modified-Since、robots.txt 遵守、エラー集約設計。リトライなし(PR #18 で解消) |
| DB (`db.py`) | ✅ | SQLite WAL + FTS5(外部 content)、schema v2、前方マイグレーション |
| タガー (`tagger/`) | ✅ | 10 固定タグ、キーワードスコア + source_tags + 共起の 3 層 |
| RSS/Pages (`publisher/`) | ✅ | body 非露出を全レイヤーで徹底(47条の5 境界) |
| Discord 通知 | ✅ | embed、429 リトライ。ただし部分失敗時の既送信マークにバグ(後述) |
| MCP サーバー (`server.py`) | ✅ | FastMCP / stdio / 5+1 ツール。E2E テストなし(カバレッジ 55%) |
| LLM 要約 (`summarizer/`) | ✅ | opt-in 設計(API キー + extra 必須)。Haiku 4.5 |
| ソース定義 | ✅ 40 本 | tool 9 / blog 20 / community 6 / note 4 / paper 1。ja 16 / en 24 |
| CI | ✅ | ruff + pytest + coverage≥80%、Python 3.11/3.12 マトリクス、直近全成功 |

### 1.2 何が動いていないか(運用)

| 項目 | 状態 | 根本原因 |
|---|---|---|
| crawl.yml (cron 1 日 3 回) | ❌ 導入以来ほぼ全回失敗(179 回中) | `actions/configure-pages` が "Not Found" — **Settings > Pages が未有効化** |
| GitHub Pages サイト / RSS | ❌ 一度も公開されず | 同上(`has_pages: false`) |
| DB スナップショット (Releases) | ❌ 0 件、蓄積なし | `cleanup_old_releases` が `createdAt`(=コミット日時)で判定 → 即削除 |
| Discord 通知 | ⏸ 毎回スキップ | `DISCORD_WEBHOOK_URL` シークレット未設定 |
| PyPI 公開 | ❌ 未実施 | `v*.*.*` タグが一度も push されていない |
| dependabot PR | ⏸ 5 件が 1〜2 ヶ月放置 | — |

### 1.3 コード上の不具合・技術的負債

優先度順。

| P | 内容 | 場所 |
|---|---|---|
| P0 | release 保持判定が `createdAt`(コミット日時)基準。`publishedAt` に変えるべき | `scripts/publish_release.py:91` |
| P1 | Discord 部分失敗時、`send_batch` が件数のみ返すため「先頭 success 件を mark」が誤マーク(失敗記事の永久欠落 / 成功記事の重複再送) | `publisher/discord.py:128-158`, `scripts/notify_discord.py:96-99` |
| P1 | README の Phase 表が古い(1〜4 が 🚧/⏳)、「30 sources」表記も実態(40)と乖離。CHANGELOG の「PyPI リリース済み」記載も未実施 | `README.md`, `README.ja.md`, `CHANGELOG.md` |
| P2 | ~~クロスソース転載検出が実装・テスト済みだが未配線~~(PR #19 で解消: `resolve_cross_source_original()` を配線し出力から除外) | `crawler/dedup.py` |
| P2 | `consecutive_errors` は書き込むだけで読む側(退避・アラート)が未実装(PR #18 で解消: `get_repeatedly_failing_sources()` + workflow warning) | `crawler/store.py:97-124` |
| P2 | fetch 層にリトライ/バックオフなし(5xx・タイムアウトは即失敗)(PR #18 で解消: 指数バックオフ付きリトライ実装) | `crawler/fetch.py` |
| P2 | MCP サーバーの Context 経由呼び出し・lifespan の E2E テストなし(server.py 55%) | `src/qa_radar/server.py` |
| ~~P3~~ | ~~`weight_tags_text` が YAML にあるが未実装(デッドコンフィグ)~~(2026-08-03 解消: `weight_tags_text` を削除し冒頭コメントを実態に修正)。「タグ 0 件は LLM フォールバック」コメントも未実装 | `config/tag_rules.yaml:9` |
| P3 | crawl.yml の `pages_artifact` output が実際にはセットされない(echo ステップに id がない) | `.github/workflows/crawl.yml:33,97-99` |
| P3 | FTS5 `unicode61` は日本語を分かち書きしないため、日本語の部分一致精度が低い(既知の制約) | `db.py` |
| P3 | 非 UTF-8 フィード(Shift-JIS 等)のパースが未検証 | `crawler/parse.py` |
| P3 | ~~DB マイグレーションが「新テーブル追加」しか想定していない(ALTER 非対応)~~(PR #19 で解消: `MIGRATIONS` による逐次適用基盤 + schema v3) | `db.py` |

---

## 2. ロードマップ

### Phase 10 — 運用復旧(最優先・作業量は小さい)

1. **[手動] Settings > Pages で Source を "GitHub Actions" に設定**
   — これだけで crawl.yml の失敗が止まり、RSS / サイトが公開される。コードでは直せない唯一の項目
2. **`publish_release.py` の保持判定を `publishedAt` 基準に修正**(+ 万一のための「直近 1 件は絶対に消さない」ガード)
   — DB 蓄積が機能し始める
3. **README / README.ja / CHANGELOG の実態合わせ**(Phase 表、40 ソース、PyPI 記載)
4. **v0.1.0(または 0.2.0)タグを push して PyPI 公開を実際に通す**
   — TestPyPI → PyPI の Trusted Publishing パイプラインは実装済み・未検証。
   Phase 7〜9 の変更を含むため `0.2.0` に bump してからのリリースを推奨
5. [手動・任意] `DISCORD_WEBHOOK_URL` シークレット設定
6. dependabot PR 5 件の消化(actions メジャー更新はワークフロー動作確認込みで)

**完了判定**: cron が緑になり、`https://y-kanekoo.github.io/qa-radar/` で feed.xml が見え、
Releases に data-* が残り続け、`uvx qa-radar` が動くこと。

### Phase 11 — 信頼性(運用が回り始めたら)

- Discord `send_batch` を per-item 結果(成功した article_id のリスト)を返す設計に変更し、
  `notify_discord.py` は成功分だけ mark する。部分失敗の統合テスト追加
- ~~fetch リトライ(指数バックオフ 2 回程度)~~(PR #18 で解消)+ ホスト単位の同時実行制限
  (github.com に 9 ソース集中、こちらは未着手)
- ~~`consecutive_errors` の配線: N 回連続失敗ソースを workflow summary で警告(自動 disable はしない)~~
  (PR #18 で解消)
- crawl.yml の結果可視化: 追加件数・失敗ソースを GitHub Actions の Step Summary に出力
- MCP サーバーの E2E テスト(FastMCP の in-memory クライアントで 6 ツールを実呼び出し)
- ~~週 1 の scheduled workflow で `--integration`(実フィード疎通)を実行し、死んだフィードを早期検知~~
  (Phase C-2 で解消: `.github/workflows/health.yml` — DB 信号 (`consecutive_errors` /
  新着日時) の週次 Discord レポートに加え、`--integration` 実行で実フィード疎通も検証)
- (PR #18 レビューでの積み残し、優先度低) `httpx.TransportError` を毎回律儀にリトライしており、
  ホスト全体がダウンしている場合の恒久エラー検知・早期打ち切り(サーキットブレーカー)がない
  (2026-08-03 一部前進: `UnsupportedProtocol` のみリトライ対象外化。ホスト単位サーキットブレーカーは未着手)
- ~~(PR #18 レビューでの積み残し) `.github/workflows/*.yml` の `actionlint` / `scripts/*.sh` の
  `shellcheck` を CI に導入し、YAML/シェル構文エラーを自動検知できるようにする~~
  (2026-08-03 解消: `workflow-lint` ジョブで actionlint(バージョン固定)+ shellcheck を導入)
- (PR #18 レビューでの積み残し) 全ソース失敗(exit 1)で crawl.yml が pipefail により早期停止すると、
  当該実行中に増分した `consecutive_errors` が DB スナップショット公開(Publish DB snapshot)前に
  失われ、次回実行時に古い DB から再開して連続失敗カウントが巻き戻る可能性がある
- ~~(PR #18 レビューでの積み残し) `deploy` / `alert` ジョブに `timeout-minutes` が未設定
  (`crawl-and-build` のみ 30 分を設定済み)~~
  (2026-08-03 解消: `deploy` に10分、`alert` に5分の `timeout-minutes` を追加)

### Phase 12 — 機能強化

- ~~**クロスソース重複検出の配線**(RSS / Pages / Discord 出力前に body_hash で抑制)~~
  (PR #19 で解消。残タスク: v3 化前から DB にある転載重複のバックフィル)
- **日本語検索の改善**: FTS5 `trigram` トークナイザの併用検討(unicode61 は CJK を分かち書きしない)
- **タグ 0 件記事への LLM フォールバック**(tag_rules.yaml に構想のみ存在。Haiku でバッチ処理、opt-in)
- **arxiv のノイズ削減**: cs.SE 全件は QA 以外が大半。arxiv API クエリでキーワード
  (testing / fault / bug / verification 等)を事前フィルタ
- **MCP サーバーの DB 自動取得**: 初回起動時に最新 data-* release から articles.db を
  匿名ダウンロードするオプション。現状「DB を自分で用意する」フリクションが利用の最大障壁

### Phase 13 — 差別化を伸ばす(アイデア)

- **週刊 LLM ダイジェスト**: 週 1 で新着を Haiku がまとめ、Discord / RSS / Pages に「今週の QA ニュース」
  を配信。単なるアグリゲーターから「まとめてくれる」価値へ — 本プロジェクトのキラー機能候補
- 月次の恒久スナップショット(retention 対象外タグ)でデータ資産を保全
- 公開後の周知: Qiita / Zenn / note で紹介記事、JaSST・テスト自動化コミュニティへの共有

---

## 3. 追加ソース候補

現 40 本のバランスは良いが、「日本のテスト自動化専門チーム」「モダン OSS ツール」「AI テスティング」
に穴がある。追加時は CONTRIBUTING.md の手順(ToS 確認・活動頻度確認)に従うこと。

### 日本語(優先度高)

| 候補 | 理由 |
|---|---|
| DeNA SWET ブログ (swet.dena.com) | 国内では稀有な「テスト自動化専門チーム」のブログ。本プロジェクトの読者と完全一致 |
| Autify ブログ (ja) | AI × QA の日本発ベンダー。ai-testing タグとの親和性 |
| MagicPod ブログ | 国内モバイル E2E ベンダー。mobile タグ強化 |
| メルカリ Engineering Blog | QA/Automation 記事が定期的に出る |
| LINEヤフー Tech Blog | テスト関連記事多数。フィードあり |
| SmartHR Tech Blog / freee Developers Hub | QA 組織の発信が活発 |

### 英語ブログ

| 候補 | 理由 |
|---|---|
| Test Guild (testguild.com) | テスト自動化専門メディアの定番 |
| Automation Panda | Andy Knight。設計論・ベストプラクティス系 |
| Martin Fowler (martinfowler.com/feed.atom) | テスト戦略の一次資料級記事 |
| StickyMinds | QA 総合メディア |
| Sauce Labs / LambdaTest ブログ | クラウドテスティング動向 |

### ツールリリース(GitHub Atom、追加コストほぼゼロ)

| 候補 | タグ |
|---|---|
| WebdriverIO / Robot Framework / Nightwatch | e2e, tooling |
| Testcontainers (java) | integration, tooling |
| MSW (mswjs/msw) | integration/api, tooling |
| Detox (wix/Detox) | mobile, tooling |
| Stryker (mutation testing) / fast-check (property-based) | unit, tooling — カバー領域の新規開拓 |
| JUnit 5 / Locust / Gatling | unit / performance |
| **promptfoo / DeepEval** | **ai-testing, tooling — 差別化ポイントの ai-testing を実データで裏付ける本命** |

注意: ソースを増やすほどクロール時間・ノイズ・死活監視コストが増える。一気に足さず、
まず Phase 10 の運用復旧 → 数ヶ月の実データでタグ分布・重複率を見てから 40 → 50 程度に。

---

## 4. 設定・運用への意見

- **cron 1 日 3 回(JST 9/15/21)は適切**。フィードのポーリングとして行儀も良い
- **retention 7 日は「全滅リスク」がある**: cron が 7 日止まると全データ消失。
  月次の恒久スナップショット(cleanup 対象外)を 1 本残す設計を推奨
- crawl.yml で `notify_discord.py ... || true` は失敗を握りつぶす。通知失敗は
  workflow を落とさないまでも warning annotation を出すべき
- integration テストが CI で常時 skip されており、実フィードの死活は本番 cron でしか分からない。
  週 1 の専用 workflow に分離するのが良い(Phase 11 に記載)
- ブランチ保護(main への直 push 禁止 + CI 必須)を設定しておくと安心
- coverage ≥80% ゲート、ruff 設定、Trusted Publishing(OIDC)採用はいずれも現代的で維持推奨

## 5. プロダクト全体への意見

**方向性は正しい。** MCP 対応 × 日英横断 × FTS 全文検索 × arxiv というポジションは
競合(yoshikiito/test-qa-rss-feed、汎用 RSS-MCP)に対して明確に差別化できており、
「日本の QA エンジニア」という刺さる読者層も具体的。法務設計(47条の5 境界の型的担保、
NOTICE、takedown フロー)は個人 OSS として出色の丁寧さで、これ自体が信頼の源泉になる。

その上で、いま最大の問題は機能不足ではなく **「作ったものが誰にも届いていない」** こと
(Pages 未公開・PyPI 未公開・star 0)。Phase 10 の復旧はどれも小さい作業なので、
新機能・新ソースより先にここを片付けるのが投資対効果として圧倒的に高い。

その次に効くのは利用体験のフリクション除去(MCP サーバーの DB 自動取得)と、
「集める」から「まとめる」への一歩(週刊 LLM ダイジェスト)。この 2 つが揃うと
「Claude に今週の QA ニュースを聞ける」という一言で説明できるプロダクトになる。
