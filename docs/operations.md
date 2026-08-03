# qa-radar 運用手順書

> 最終更新: 2026-08-03 (Phase C-2)

実運用 (GitHub Actions による自動クロール・配信) を維持するための手順書。
開発手順は [README.md](../README.md) / [README.ja.md](../README.ja.md)、
過去の障害調査は [docs/status-and-roadmap.md](status-and-roadmap.md) を参照。

## 全体像

GitHub Actions の cron ワークフローは以下の2本立て。

- `.github/workflows/crawl.yml`: 毎日3回、クロール〜配信〜デプロイの本番パイプライン
- `.github/workflows/health.yml`: 毎週月曜、ソース健全性の Discord レポート + 実フィード疎通確認
  (詳細は後述「週次ヘルスレポートの見方」)

### crawl.yml

`.github/workflows/crawl.yml` が GitHub Actions の cron で以下を毎日 3 回実行する。

- **スケジュール**: JST 9:00 / 15:00 / 21:00 (= UTC 0:00 / 6:00 / 12:00、`cron: "0 0,6,12 * * *"`)
- **実行内容** (`crawl-and-build` ジョブ → `deploy` ジョブ):
  1. 前回 DB スナップショットを GitHub Releases から復元 (無ければ新規作成)
  2. クロール + 自動タグ付け (`scripts/run_crawl.py`)
  3. Discord 通知 (`scripts/notify_discord.py`、`DISCORD_WEBHOOK_URL` 未設定時はスキップ)
  4. Pages 用フィード/HTML ビルド (`scripts/build_pages.py`)
  5. DB スナップショットを GitHub Releases に再アップロード (`scripts/publish_release.py --mode full`、retention 7 日)
  6. GitHub Pages へデプロイ (`configure-pages` → `upload-pages-artifact` → `deploy-pages`)

DB スナップショット (ステップ 5) は Pages 関連ステップ (ステップ 6) より **前** に実行される。
そのため **Configure Pages / Upload Pages artifact / Deploy が失敗しても、DB スナップショットは
既に Release へ保存済みなので失われない**。

ただし `crawl.yml` の各ステップは直列実行で `if: always()` は設定されていないため、
**ステップ 4 (`Build pages`) が失敗すると、後続のステップ 5 (DB スナップショット) 自体が
スキップされる**。この場合、その回でクロールした記事は Release に残らない (ただし記事自体は
次回実行時に再クロールされて拾われるため、恒久的なデータ消失にはならない)。

## 初期セットアップ (手動作業)

以下はコードでは自動化できない、リポジトリ設定 (Settings) 側の作業。

### 1. GitHub Pages の Source を「GitHub Actions」に設定

**Settings > Pages > Build and deployment > Source** を `GitHub Actions` にする。

> **注意**: これが未設定 (デフォルトの `Deploy from a branch` のまま) だと、
> ワークフロー内の `actions/configure-pages` が `Not Found` エラーで毎回失敗する。
> 実際に 2026-05〜07 の約 2 ヶ月間、この設定漏れにより cron 実行が全滅していた
> (詳細は [docs/status-and-roadmap.md](status-and-roadmap.md) 参照)。**リポジトリを
> fork/再作成した場合は必ず最初に確認すること。**

### 2. Secrets の登録

| Secret 名 | 用途 | 状態 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | 新着記事の通知用 Discord webhook | 推奨 (未設定でも実行は継続、通知のみスキップされる) |
| `DISCORD_ALERT_WEBHOOK_URL` | 運用アラート通知用 (crawl 失敗検知)、および週次ヘルスレポート (`health.yml`) の送信先 | 使用中。未設定でも実行は継続 (crawl.yml のアラートはスキップ、health.yml のレポートは stdout 出力のみで exit 0) |

登録方法 (`gh` CLI):

```bash
gh secret set DISCORD_WEBHOOK_URL --repo Y-Kanekoo/qa-radar
gh secret set DISCORD_ALERT_WEBHOOK_URL --repo Y-Kanekoo/qa-radar
```

(実行するとプロンプトで値の入力を求められる。標準入力から渡す場合は
`echo "$WEBHOOK_URL" | gh secret set DISCORD_WEBHOOK_URL --repo Y-Kanekoo/qa-radar`)

Web UI から設定する場合: **Settings > Secrets and variables > Actions > New repository secret**。

## 障害時の一次切り分け

### 1. 実行状況の確認

```bash
gh run list --workflow=crawl.yml --limit 10 --repo Y-Kanekoo/qa-radar
```

失敗している run があれば、詳細を見る:

```bash
gh run view <run-id> --repo Y-Kanekoo/qa-radar
```

### 2. 失敗ステップによる切り分け

| 失敗ステップ | 所属ジョブ | 分類 | 影響・対応 |
|---|---|---|---|
| `Crawl + tag (自動でタグ付け)` | `crawl-and-build` | 収集系 | 記事収集そのものが失敗。フィード取得先の障害・パースエラー・依存関係エラーなどを疑う。ログの `crawl.log` 相当を確認 |
| `Notify Discord (idempotent, skips if URL unset)` | `crawl-and-build` | 通知系 | `\|\| true` で握りつぶされ、後続ステップには影響しない。Secrets 未設定 or webhook 側の問題を疑う |
| `Build pages` | `crawl-and-build` | ビルド系 | この失敗により後続の `Publish DB snapshot to GH Releases` も**スキップされる**(直列実行・`if: always()` なし)。今回クロールした記事は Release に残らないが、次回実行時に再クロールされるため取りこぼしにはならない |
| `Publish DB snapshot to GH Releases` | `crawl-and-build` | 永続化系 | `Crawl + tag` / `Build pages` が成功していればここは通常失敗しない。`gh` CLI の権限 (`contents: write`) や API 制限を疑う |
| `Configure Pages` / `Upload Pages artifact` | `crawl-and-build` | Pages 設定系 | 上記「初期セットアップ 1」の設定漏れが最有力原因。**この時点で DB スナップショットは既に Release へ保存済み**なので、データ消失の心配はない |
| `Deploy` | `deploy` | Pages 設定系 | 同上。`crawl-and-build` ジョブが正常終了した後の別ジョブなので、DB スナップショットには影響しない |

つまり: **`Crawl + tag` / `Build pages` の失敗 = 今回分のデータが Release に残らない (要調査。
ただし次回実行時に再クロールされる)**、**`Configure Pages` 以降の Pages 系ステップの失敗 =
サイト更新が反映されていないだけ (DB スナップショットは既に健在)**、と切り分けられる。

## DB 容量の方針

`data/articles.db` は 1 記事あたり約 11KB (本文・スニペット・メタデータ込み、実測値) で増加する。
2026-08 時点の想定クロール量(44 ソース、1 日 3 回)であれば、当面は articles テーブルの
retention (自動削除) を実装しない。

- **理由**: 現状の増加ペースでは、閾値 200MB に達するまで年単位の猶予がある。retention は
  「消してよい記事」の判断基準(法務上の掲載期限・ユーザー要望など)が定まっていない状態で
  実装すると、後から要件が変わった際の手戻りリスクの方が大きい
- **再検討のトリガー**: `data/articles.db` が **200MB** を超えた時点で、記事の
  アーカイブ/削除方針(例: 1年以上前の記事を別ファイルに退避)を検討する
- **監視方法**: 後述の週次ヘルスレポート (`health.yml` / `scripts/health_report.py`) が
  毎週 DB ファイルサイズを Discord へ報告するため、閾値接近は自然に気づける設計になっている
  (専用の容量アラートは設けていない)

## 週次ヘルスレポートの見方

`.github/workflows/health.yml` が毎週月曜 (JST 9:00 = UTC 0:00) に `scripts/health_report.py` を
実行し、Discord (`DISCORD_ALERT_WEBHOOK_URL`) へソース健全性のダイジェストを送信する。

**設計方針**: 実フィードへの追加アクセスは行わない。本番 cron (`crawl.yml`) が既に DB に
書き込んでいる信号 (`sources.consecutive_errors`、`articles.published_at` /
`articles.fetched_at`) を集計するだけにとどめ、死活監視の取得経路を二重化しない。
実フィード疎通そのものの検証は、同じ `health.yml` 内で `uv run pytest --integration -v -m integration`
(`tests/integration/test_crawl_e2e.py` が対象とする `arxiv-cs-se` / `playwright-releases` の
**代表2ソース**への実フィード疎通確認。通常の CI では `--integration` フラグ未指定のため常時 skip)
を別ステップとして実行することで担保する。44 ソース全ての疎通を毎週検証しているわけではない。

この統合テストのステップは DB 復元の成否や DB スナップショットの有無に関わらず**常に実行**される
(DB に依存しないテストのため)。DB スナップショットが Releases に存在しない場合 (初回実行時など) は
「全体統計 + Discord送信」の健全性レポートのみスキップされ、GitHub Actions の run summary に
warning として記録される。

DB 復元ステップ (`scripts/publish_release.py --mode download`) の終了コードは以下のように扱う:

| 終了コード | 意味 | 挙動 |
|---|---|---|
| `0` | 復元成功 | 続行 |
| `2` | 過去 release が1件も無い (初回実行時など) | `::warning` を出し、ジョブは成功のまま続行 (レポートのみスキップ) |
| それ以外 (`1` 等) | `gh` の認証切れ・API レート制限・ネットワーク断等の想定外エラー | ステップ失敗としてジョブを failure にし、`alert` ジョブから Discord へ通知 |

レポートは以下の 3 セクションで構成される:

1. **全体統計**: 総記事数・DB ファイルサイズ・直近 7 日の新規記事数
2. **エラー中のソース**: `consecutive_errors > 0` の有効ソースを回数の多い順に列挙。
   `DEFAULT_CONSECUTIVE_ERROR_THRESHOLD` (9 回) 以上は `⚠️危険`、それ未満は `注意` ラベル
3. **新着停滞の疑いがあるソース**: 最終記事取得実績 (`published_at` / `fetched_at` の新しい方)
   から 30 日以上経過を `新着なし(注意)`、90 日以上経過または記事取得実績なしを
   `長期停止疑い` として抽出。論文誌等の低頻度ソースは正常でも該当しうるため断定表現は避けている

**運用アクション**: `⚠️危険` ラベルのソースはフィード URL の死活・ToS 変更を疑って確認する。
`長期停止疑い` はまず該当ソースが低頻度更新かどうかを確認し、そうでなければフィード停止を疑う。

**注意 (webhook 自体が障害原因のケース)**: `health-report` ジョブの失敗は `alert` ジョブが
同じ `DISCORD_ALERT_WEBHOOK_URL` を使って `scripts/notify_alert.sh` 経由で通知する。webhook
自体の失効・設定ミスが失敗原因の場合はこの通知自体も失敗しうる (循環)。その場合でも GitHub
Actions 上のジョブは赤 (failure) のまま残るため、Actions の run 一覧を定期的に確認すること。

## DB 復旧

DB (`data/articles.db`) は cron 実行のたびに GitHub Releases の `data-*` prerelease に
スナップショットとして保存される (retention 7 日、最新 1 件は保持し続ける安全ガードあり)。
ローカルや別環境で DB が失われた場合は以下で復元できる:

```bash
uv run python scripts/publish_release.py \
  --mode download \
  --repo Y-Kanekoo/qa-radar \
  --download-to data/articles.db
```

過去 release が 1 件も無い場合は終了コード `2` で失敗する (`過去 release なし` の warning)。

## 手動実行

`workflow_dispatch` で手動トリガーできる。以下の入力を指定可能:

```bash
# 通常実行 (全ステップ実行)
gh workflow run crawl.yml --repo Y-Kanekoo/qa-radar

# Discord 通知をスキップ (デバッグ・検証実行時など)
gh workflow run crawl.yml --repo Y-Kanekoo/qa-radar -f skip_discord=true

# Pages デプロイをスキップ (クロール動作のみ確認したい場合)
gh workflow run crawl.yml --repo Y-Kanekoo/qa-radar -f skip_deploy=true
```

実行後は `gh run list --workflow=crawl.yml --repo Y-Kanekoo/qa-radar --limit 1` で
起動を確認し、上記「障害時の一次切り分け」の手順で結果を追う。

> **注意: `pages.yml` を安易に使わないこと**。`.github/workflows/pages.yml` も
> `workflow_dispatch` で手動実行できるが、これは緊急時の即時再生成用に残された別ワークフローで、
> **GitHub Releases からの DB 復元を行わず、毎回ゼロから新規クロールした DB でビルドする**。
> 実行すると、これまで蓄積してきた記事履歴がほぼ空のサイトで上書き公開されてしまう。
> 通常の手動実行は必ず本項の `crawl.yml` を使うこと。
> また `run_crawl.py` が全ソース失敗時に exit 1 を返すようになったため(Phase B)、
> `pages.yml` も全滅時は「Crawl sources」ステップで中断する(意図した動作)。
