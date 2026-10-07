# QA新技術ウォッチ

既存の `qa-radar` クロール・SQLite DB・Discord送信台帳を利用して、公式OSS
リリースからQAに応用しやすい変化を日次でまとめる。対象は
`config/technology_watch.yaml` の8プロジェクト。Playwright MCP、Browser Use、
Stagehand、Skyvernの公式GitHub releaseフィードを既存の44ソースに追加し、
promptfoo、DeepEval、Giskard、Langfuseの既存フィードも利用する。

## 実行と費用

既存 `crawl.yml` の3回/日のうち、09:00 JST（UTC 00:00）の実行内でだけ
ダイジェストを作る。Actionsの新規実行はなく、有料LLM APIも使用しない。
増分は4フィードへの通常のHTTP取得と、最大1件のDiscord webhookリクエスト。
各フィードは既存の取得間隔・ETag/Last-Modified・エラー記録を使う。

`DISCORD_TECH_WATCH_WEBHOOK_URL` が未設定なら送信・既読登録をしない。
新規4ソースは設定前も通常の記事Webhookには流さない。設定された場合、
既存4ソースも通常の記事Webhookから除き、専用宛先へ移す。
設定前に通常記事チャンネルへ送信済みのreleaseは専用宛先で再送しない。
通常の記事・RSS・Pages・旧DBデータは保持する。これらのフィード自体は公開RSSにも
掲載される。

## 候補と本文の契約

- 公式GitHub release URLだけを対象にする。タイトルと100字以内の既存snippetで
  「新規公開」「重要リリース」「機能追加」「QA関連修正」を判定する。単なるpatch版番号や依存更新は
  通知しない。alpha・beta・devなどの試験版も除く。これは決定的なルールであり、
  重要な変更の網羅を保証しない。
- プロジェクトの機能・QA用途・試し方・制限・成熟度・ライセンス・料金は、
  一次情報へのリンクを持つレビュー済みカタログから表示する。料金の具体額は固定せず
  公式ページで現行条件を確認する。差分欄は公式告知の冒頭snippetをそのまま示す。
  記事本文や未確認の詳細を生成しない。
- すべて「公式告知・動作未検証」と表示する。実際に試した証拠がない限り
  「検証済み」にはしない。実検証は別の記録が必要。
- 1日最大5件を1リクエストに集約し、残りは次回へ繰り越す。最初の有効化時は
  直近7日以内に取得した記事だけを候補化する。件数制限で先送りした候補も
  `pending` としてDBに保存し、7日を過ぎても対象に残す。取得から30日で
  未送信候補を `expired` / `pending_ttl_30d` として明示除外する。

## 台帳と障害

既送信は `article_notifications` の別channel
`discord-technology-watch` で管理する。`technology_delivery_attempts` は記事IDごとの
pending・試行回数・時刻・固定status・期限・Discord message ID・除外理由を記録する。
WebhookにはDiscord公式APIの `wait=true` を指定し、作成済みmessage IDを持つ
応答だけを送達確定として全件一括で既送信にする。2xxでもIDがない応答やtimeout、
5xxは送達不明として `manual_reconciliation` で保留し、自動再送しない。
429・明確な4xx・接続確立失敗は30日の期限内で再試行する。
429は`Retry-After`を最大5秒待って1回だけ再試行する。失敗してもDB snapshot公開は
続け、既存アラートjobで失敗を見えるようにする。Webhook URL・レスポンス本文・
例外本文はログに出さず、HTTPXの通信INFOログも抑制する。Discordの
[Webhook実行仕様](https://docs.discord.com/developers/resources/webhook#execute-webhook)に
よると、既定 `wait=false` は保存されなくてもエラーを返さない場合がある。

送達不明は台帳を確認し、Discord側の実在とmessage IDを人が照合してから
再送または既送信登録を判断する。`manual_reconciliation` の間は専用Webhookを
外しても通常記事チャンネルへ流れない。実在しないことを確認して再送する場合だけ
台帳の `excluded_reason` を解除し、再選択を許可する。送信成功後のDB保存・
Release公開失敗では、
次回に同じダイジェストを送る可能性が残る。WebhookとSQLiteを単一
トランザクションにはできないため、厳密なexactly-once配信は保証しない。
専用Webhookの設定を外しても、専用チャネルで送達済みの記事は通常記事Webhookへ
再送しない。

## オフライン試用

まず既存DBまたは合成データを入れたDBで表示だけ確認する:

```bash
uv run python scripts/notify_technology_watch.py --db-path data/articles.db --dry-run
uv run pytest -q tests/unit/test_technology_watch.py
```

`--dry-run` はSQLite backupで作った一時コピーに対してmigrationと候補選定を行い、
元DB・Webhook・認証情報を変更しない。実通知の確認は宛先の一意性と安全な
Webhook登録が済んだ後の運用作業。Webhookの作成・Secret登録はこのPRに含まない。

## 出典と追加基準

ソースは各プロジェクトの公式GitHub releaseフィード。初期4ソースのrelease履歴、
README、LICENSEは2026-10-07に確認した。新しいツールを追加する際は
`config/sources.yaml` と `config/technology_watch.yaml` の両方を更新し、
公式release URL、試し方、ライセンス、料金、QAへの具体的用途を確認する。
既存ソースとのURL重複も確認する。記事や第三者の評判は一次情報・実検証として
扱わない。
