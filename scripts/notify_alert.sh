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
  echo "DISCORD_ALERT_WEBHOOK_URL が未設定のためアラート送信をスキップします"
  exit 0
fi

# JSON文字列として必要なバックスラッシュ、二重引用符、改行、タブ、復帰をエスケープする。
json_escape() {
  LC_ALL=C awk '
    function escape_line(value, escaped, index, character) {
      escaped = ""
      for (index = 1; index <= length(value); index++) {
        character = substr(value, index, 1)
        if (character == "\\") {
          escaped = escaped "\\\\"
        } else if (character == "\"") {
          escaped = escaped "\\\""
        } else if (character == "\t") {
          escaped = escaped "\\t"
        } else if (character == "\r") {
          escaped = escaped "\\r"
        } else {
          escaped = escaped character
        }
      }
      return escaped
    }

    {
      if (NR > 1) {
        printf "\\n"
      }
      printf "%s", escape_line($0)
    }
  '
}

message=$(printf '%s\n%s\n%s\n%s\n%s' \
  "【GitHub Actions アラート】" \
  "リポジトリ: $repository_name" \
  "ワークフロー: $workflow_name" \
  "ジョブ: $job_name" \
  "状況: $status_description / Run: $run_url")
escaped_message=$(printf '%s' "$message" | json_escape)
payload=$(printf '{"content":"%s"}' "$escaped_message")

if ! curl \
  --fail \
  --silent \
  --show-error \
  --max-time 10 \
  --request POST \
  --header "Content-Type: application/json" \
  --data "$payload" \
  "$DISCORD_ALERT_WEBHOOK_URL"; then
  echo "Discordへのアラート送信に失敗しました" >&2
  exit 1
fi

echo "Discordへアラートを送信しました"
