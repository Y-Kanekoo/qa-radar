"""scripts/notify_alert.sh のスモークテスト.

過去に awk の関数パラメータ名 `index` (POSIX awk の予約語) が原因で
JSONエスケープ処理が構文エラーで必ず落ち、アラートが1通も送れないという
バグがあった。シェルスクリプト自体は pytest から subprocess 経由で
実行しないと検出できないため、実プロセスとして起動するスモークテストを置く。
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "notify_alert.sh"

requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq がインストールされていない環境ではスキップ"
)


def _run_script(*, webhook_url: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if webhook_url is None:
        env.pop("DISCORD_ALERT_WEBHOOK_URL", None)
    else:
        env["DISCORD_ALERT_WEBHOOK_URL"] = webhook_url

    return subprocess.run(
        [
            str(SCRIPT),
            "Y-Kanekoo/qa-radar",
            "Crawl, build, and deploy",
            "crawl-and-build",
            "https://github.com/Y-Kanekoo/qa-radar/actions/runs/1/attempts/1",
            "テスト用の状況説明",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_skips_when_webhook_url_unset() -> None:
    """DISCORD_ALERT_WEBHOOK_URL 未設定なら送信せず exit 0.

    GitHub Actions の run summary に表示されるよう ::warning:: annotation 形式で
    出力する (人間がシークレット未設定に気づきやすくするため)。
    """
    result = _run_script(webhook_url=None)
    assert result.returncode == 0
    assert "::warning title=Alert skipped::" in result.stdout
    assert "DISCORD_ALERT_WEBHOOK_URL" in result.stdout
    assert "スキップ" in result.stdout


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    """1リクエストだけ受信し、bodyをテスト側で参照できるよう保持するハンドラ."""

    captured_body: bytes | None = None
    captured_content_type: str | None = None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        type(self).captured_body = self.rfile.read(length)
        type(self).captured_content_type = self.headers.get("Content-Type")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        # テスト出力を汚さないよう http.server の標準ログを抑制する
        pass


@requires_jq
def test_sends_valid_json_payload_to_webhook() -> None:
    """実プロセスとして実行し、受信bodyが正しいJSONで content にメッセージが含まれること.

    このテストはシェルスクリプトを実際に起動して検証するため、
    JSON組み立てロジックの構文エラー (過去の awk バグ等) を確実に検出できる。
    """
    _CapturingHandler.captured_body = None
    _CapturingHandler.captured_content_type = None

    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    # スクリプトがリクエストを送らずに落ちた場合でも handle_request() が
    # 無限に待ち続けないよう上限を設ける (この上限が無いと、破損したスクリプトを
    # 対象にした際に pytest プロセスごとハングしてしまう)
    server.timeout = 10
    port = server.server_address[1]
    # daemon=True にして、万一 handle_request() がハングしてもテストプロセスの
    # 終了をブロックしないようにする (timeout設定と合わせた二重の安全策)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()
    try:
        result = _run_script(webhook_url=f"http://127.0.0.1:{port}/webhook")
    finally:
        server_thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, f"stderr={result.stderr}"
    assert _CapturingHandler.captured_body is not None, "webhookへリクエストが届いていない"
    assert _CapturingHandler.captured_content_type == "application/json"

    payload = json.loads(_CapturingHandler.captured_body)
    assert "Y-Kanekoo/qa-radar" in payload["content"]
    assert "crawl-and-build" in payload["content"]
    assert "テスト用の状況説明" in payload["content"]


# 送信失敗パス (curl --retry 3 が尽きて非0 exitになるケース) は、
# --retry の指数バックオフ (1s+2s+4s ≈ 7秒) が必ず実待機として発生するため
# ユニットテストでは検証しない。--fail 相当の挙動は notify_discord.py 等
# 既存スクリプトと同じ curl オプションの組み合わせであり、手動検証済み
# (PR説明を参照)。
