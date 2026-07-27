"""Web Learning Lab の自動テスト.

実際にサーバーを起動し、本物の HTTP リクエストを送って動作を確かめる
「結合テスト」形式。テスト自体も HTTP の学習材料になるよう書いてある。

実行方法 (どちらでも可):
    cd learning-lab && python3 test_server.py     # unittest として
    uv run pytest learning-lab/ -v                # pytest として

テスト用に一時ディレクトリの DB を使うので、手元の data/todos.db は汚れない。
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

# このファイルと同じ場所にある server.py を import できるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server as lab  # パス設定の後で import する必要がある


class LearningLabApiTest(unittest.TestCase):
    """API と静的配信の振る舞いを HTTP 越しに検証する."""

    tmpdir: tempfile.TemporaryDirectory
    httpd: ThreadingHTTPServer
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        """テスト用の一時DBを用意し、空きポートでサーバーを起動する."""
        cls.tmpdir = tempfile.TemporaryDirectory()
        lab.DATA_DIR = Path(cls.tmpdir.name)
        lab.DB_PATH = lab.DATA_DIR / "todos.db"
        lab.init_db()
        # ポート 0 を指定すると OS が空きポートを自動で割り当ててくれる
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), lab.LearningLabHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmpdir.cleanup()

    # -- ヘルパー -----------------------------------------------------------

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """HTTP リクエストを1本送り、(ステータスコード, JSONボディ) を返す."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            # urllib は 4xx/5xx を例外にするが、ボディは普通に読める
            return err.code, json.loads(err.read())

    def create(self, title: str) -> dict[str, Any]:
        """TODO を1件作って data 部分を返す (各テストの前提づくり用)."""
        status, payload = self.request("POST", "/api/todos", {"title": title})
        assert status == 201, f"前提の作成に失敗: {payload}"
        return payload["data"]

    # -- 正常系 (CRUD) ------------------------------------------------------

    def test_create_returns_201_with_new_todo(self) -> None:
        status, payload = self.request("POST", "/api/todos", {"title": "牛乳を買う"})
        self.assertEqual(status, 201)  # 「作成成功」は 200 ではなく 201 Created
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["title"], "牛乳を買う")
        self.assertFalse(payload["data"]["done"])
        self.assertIsInstance(payload["data"]["id"], int)  # id が採番されている

    def test_list_returns_created_todos(self) -> None:
        created = self.create("一覧テスト用")
        status, payload = self.request("GET", "/api/todos")
        self.assertEqual(status, 200)
        ids = [todo["id"] for todo in payload["data"]]
        self.assertIn(created["id"], ids)

    def test_patch_toggles_done(self) -> None:
        created = self.create("完了にするやつ")
        status, payload = self.request("PATCH", f"/api/todos/{created['id']}", {"done": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["done"])

    def test_patch_updates_title(self) -> None:
        created = self.create("古いタイトル")
        status, payload = self.request(
            "PATCH", f"/api/todos/{created['id']}", {"title": "新しいタイトル"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["title"], "新しいタイトル")

    def test_delete_removes_todo(self) -> None:
        created = self.create("消されるやつ")
        status, payload = self.request("DELETE", f"/api/todos/{created['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["deleted_id"], created["id"])
        # 消した後の一覧には含まれない
        _, listing = self.request("GET", "/api/todos")
        self.assertNotIn(created["id"], [t["id"] for t in listing["data"]])

    # -- エラー系 (教材の「実験コーナー」と同じ内容) --------------------------

    def test_create_empty_title_returns_400(self) -> None:
        status, payload = self.request("POST", "/api/todos", {"title": "   "})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("タイトル", payload["error"]["message"])

    def test_create_too_long_title_returns_400(self) -> None:
        status, _ = self.request("POST", "/api/todos", {"title": "あ" * 101})
        self.assertEqual(status, 400)

    def test_broken_json_returns_400(self) -> None:
        # わざと壊れた JSON を送る (urllib ではなく低レベル API で生のまま送信)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST",
            "/api/todos",
            body=b"{broken",
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        conn.close()

    def test_patch_missing_id_returns_404(self) -> None:
        status, payload = self.request("PATCH", "/api/todos/99999", {"done": True})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_delete_missing_id_returns_404(self) -> None:
        status, _ = self.request("DELETE", "/api/todos/99999")
        self.assertEqual(status, 404)

    def test_unknown_api_path_returns_404(self) -> None:
        status, _ = self.request("GET", "/api/nazo")
        self.assertEqual(status, 404)

    def test_unsupported_method_returns_405(self) -> None:
        status, payload = self.request("PUT", "/api/todos", {"title": "x"})
        self.assertEqual(status, 405)  # パスはあるがメソッドが違う
        self.assertIn("PUT", payload["error"]["message"])

    # -- 可視化 (debug 情報) ------------------------------------------------

    def test_debug_envelope_is_included(self) -> None:
        """X-Rayパネルの表示元になる debug 情報が同梱されていること."""
        status, payload = self.request("POST", "/api/todos", {"title": "debug検証"})
        self.assertEqual(status, 201)
        debug = payload["debug"]
        self.assertEqual(debug["method"], "POST")
        self.assertEqual(debug["path"], "/api/todos")
        step_names = [step["name"] for step in debug["steps"]]
        # ルーティング → ボディ解析 → バリデーション → SQL実行 → レスポンス生成
        for expected in [
            "ルーティング",
            "ボディ解析",
            "バリデーション",
            "SQL実行",
            "レスポンス生成",
        ]:
            self.assertIn(expected, step_names)
        self.assertGreaterEqual(len(debug["sql"]), 2)  # INSERT + 読み直しSELECT
        self.assertIn("INSERT INTO todos", debug["sql"][0]["query"])
        self.assertGreater(debug["total_ms"], 0)

    def test_error_response_also_has_debug(self) -> None:
        """エラー時にも「どこで弾いたか」の記録が残っていること."""
        _, payload = self.request("POST", "/api/todos", {"title": ""})
        details = [step["detail"] for step in payload["debug"]["steps"]]
        self.assertTrue(any("400" in detail for detail in details))

    # -- 静的ファイル配信 -----------------------------------------------------

    def test_root_serves_index_html(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as res:
            self.assertEqual(res.status, 200)
            self.assertIn("text/html", res.headers["Content-Type"])
            self.assertIn("Web Learning Lab", res.read().decode())

    def test_missing_static_file_returns_404(self) -> None:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/nai.js")
            self.fail("404 になるはず")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 404)

    def test_path_traversal_is_blocked(self) -> None:
        """ "/static/../server.py" のような URL でソースコードを盗めないこと."""
        # urllib は "../" を正規化してしまうので、生のパスを送れる低レベル API を使う
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/static/../server.py")
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
