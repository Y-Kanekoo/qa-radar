# qa-radar 運用手順書

> 最終更新: 2026-07-31 (Phase A)

実運用 (GitHub Actions による自動クロール・配信) を維持するための手順書。
開発手順は [README.md](../README.md) / [README.ja.md](../README.ja.md)、
過去の障害調査は [docs/status-and-roadmap.md](status-and-roadmap.md) を参照。

## 全体像

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
| `DISCORD_ALERT_WEBHOOK_URL` | 運用アラート通知用 (crawl 失敗検知など) | Phase B で使用予定。未実装のためまだ参照されない |

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
