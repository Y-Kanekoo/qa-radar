#!/bin/sh

set -eu

if [ "$#" -ne 5 ]; then
  echo "使い方: $0 <リポジトリ名> <ワークフロー名> <ジョブ名> <run URL> <状況説明>" >&2
  exit 2
fi

repository_name=$1
workflow_name=$2
job_name=$3
run_url=$4
status_description=$5

if [ -z "${DISCORD_ALERT_WEBHOOK_URL:-}" ]; then
  # GitHub Actions annotation 形式にしておくと workflow の run summary に
  # 表示され、シークレット未設定に人間が気づきやすくなる
  echo "::warning title=Alert skipped::DISCORD_ALERT_WEBHOOK_URL 未設定のためアラート送信をスキップしました"
  exit 0
fi

message=$(printf '%s\n%s\n%s\n%s\n%s' \
  "【GitHub Actions アラート】" \
  "リポジトリ: $repository_name" \
  "ワークフロー: $workflow_name" \
  "ジョブ: $job_name" \
  "状況: $status_description / Run: $run_url")

# JSON組み立ては手組みエスケープではなく jq に委譲する
# (POSIX awk の予約語 `index` を変数名に使ってしまい構文エラーで落ちる、といった
#  自前実装特有の事故を避けるため。ubuntu-latest ランナーには jq がプリインストール済み)
payload=$(jq -n --arg content "$message" '{content: $content}')

# webhook URL 自体はログに出さない (Discord webhook URL は事実上の秘密情報のため)
if ! curl \
  --fail \
  --silent \
  --show-error \
  --max-time 10 \
  --retry 3 \
  --retry-connrefused \
  --retry-all-errors \
  --request POST \
  --header "Content-Type: application/json" \
  --data "$payload" \
  "$DISCORD_ALERT_WEBHOOK_URL"; then
  echo "Discordへのアラート送信に失敗しました" >&2
  exit 1
fi

echo "Discordへアラートを送信しました"
