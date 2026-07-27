"""Web Learning Lab — 自分自身の動きを解説しながら動く TODO アプリのサーバー.

このファイルは「バックエンド(サーバー側プログラム)」のすべてです。
Python の標準ライブラリだけで書かれているので、pip install は不要。

    python3 server.py        # http://localhost:8000 で起動

■ このサーバーの仕事は、突き詰めると次の2つだけ:
  1. 静的ファイル配信 …… ブラウザに HTML/CSS/JS のファイルをそのまま渡す
  2. API 処理        …… /api/... へのリクエストを受け、DBを読み書きし、JSONで結果を返す

■ 読む順番のおすすめ:
  main() → LearningLabHandler(リクエストの入り口) → API_ROUTES(URLと関数の対応表)
  → 各ハンドラー関数(list_todos など) → DebugTrace(可視化のための記録係)

■ 本アプリ最大の特徴:
  サーバーは処理しながら「今なにをしたか」を DebugTrace に記録し、
  レスポンス JSON の "debug" フィールドに同梱して返します。
  ブラウザ側の X-Ray パネルは、この debug を表示しているだけ。
  つまり可視化の仕組み自体も、ただの HTTP レスポンスで実現されています。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 設定値 (どこに何があるか)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent  # このファイルがあるディレクトリ
STATIC_DIR = BASE_DIR / "static"  # HTML/CSS/JS の置き場所
DATA_DIR = BASE_DIR / "data"  # データベースファイルの置き場所
DB_PATH = DATA_DIR / "todos.db"  # SQLite は「ただの1ファイル」がDBになる

DEFAULT_PORT = 8000

# 拡張子 → Content-Type の対応表。
# ブラウザはこのヘッダーを見て「HTMLとして描画する/CSSとして適用する」を判断する。
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

TITLE_MAX_LEN = 100  # TODO タイトルの最大文字数 (バリデーションで使う)


# ---------------------------------------------------------------------------
# DebugTrace — サーバー内部の動きを記録する「ブラックボックスレコーダー」
# ---------------------------------------------------------------------------


@dataclass
class DebugTrace:
    """1つのリクエストを処理する間の出来事をぜんぶ記録するメモ帳.

    記録した内容はレスポンス JSON の "debug" に同梱され、
    ブラウザの X-Ray パネルにそのまま表示される。
    """

    method: str
    path: str
    # perf_counter() は「処理時間の計測」専用の高精度ストップウォッチ
    started: float = field(default_factory=time.perf_counter)
    steps: list[dict[str, Any]] = field(default_factory=list)
    sql: list[dict[str, Any]] = field(default_factory=list)

    def step(self, name: str, detail: str) -> None:
        """「いま○○をした」を経過時間(ミリ秒)つきで記録する."""
        elapsed_ms = (time.perf_counter() - self.started) * 1000
        self.steps.append({"name": name, "detail": detail, "at_ms": round(elapsed_ms, 2)})

    def record_sql(self, query: str, params: tuple[Any, ...], rows: int) -> None:
        """実行した SQL 文・パラメータ・対象行数を記録する."""
        self.sql.append({"query": query, "params": list(params), "rows": rows})

    def to_dict(self) -> dict[str, Any]:
        """レスポンスに載せられる形 (ただの辞書) に変換する."""
        total_ms = (time.perf_counter() - self.started) * 1000
        return {
            "method": self.method,
            "path": self.path,
            "steps": self.steps,
            "sql": self.sql,
            "total_ms": round(total_ms, 2),
        }


# ---------------------------------------------------------------------------
# データベース (SQLite)
# ---------------------------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    """DB への接続を開く。リクエストのたびに開いて、使い終わったら閉じる方式.

    row_factory を設定すると、結果を row["title"] のように列名で取り出せる。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初回起動時にテーブルを作る。既にあれば何もしない (IF NOT EXISTS)."""
    DATA_DIR.mkdir(exist_ok=True)
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自動で 1,2,3… と採番
                title      TEXT    NOT NULL,                   -- やることの内容
                done       INTEGER NOT NULL DEFAULT 0,         -- 0=未完了 / 1=完了
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


@dataclass
class SqlResult:
    """SQL を1本実行した結果のまとめ."""

    rows: list[sqlite3.Row]  # SELECT で取得した行 (それ以外の文では空)
    affected: int  # INSERT/UPDATE/DELETE が影響した行数
    last_id: int | None  # INSERT で自動採番された id


def run_sql(trace: DebugTrace, query: str, params: tuple[Any, ...] = ()) -> SqlResult:
    """SQL を1本実行し、DebugTrace に記録してから結果を返す共通関数.

    ★重要: 値の埋め込みは必ず `?` プレースホルダーを使う。
      文字列連結で SQL を組み立てると「SQLインジェクション」という
      重大なセキュリティ事故につながる (教科書タブ第7章参照)。
    """
    with closing(get_connection()) as conn, conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        # SELECT なら取得行数、INSERT/UPDATE/DELETE なら影響行数を記録する
        is_select = query.lstrip().upper().startswith("SELECT")
        affected = len(rows) if is_select else cursor.rowcount
        trace.record_sql(query.strip(), params, affected)
        return SqlResult(rows=rows, affected=affected, last_id=cursor.lastrowid)


def row_to_todo(row: sqlite3.Row) -> dict[str, Any]:
    """DB の1行を、JSON にしやすい辞書へ変換する.

    SQLite に真偽値型はないので、done は 0/1 で保存されている。
    JavaScript 側で扱いやすいよう、ここで true/false に直して返す。
    """
    return {
        "id": row["id"],
        "title": row["title"],
        "done": bool(row["done"]),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# API ハンドラー — 「1つのURL+メソッド = 1つの関数」
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """「クライアントへエラーを返したい」ときに投げる例外.

    例外として投げると、途中の処理をすべてスキップして
    エラーレスポンス生成の場所まで一気に戻れる。
    """

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def list_todos(trace: DebugTrace, params: dict[str, str], body: Any) -> tuple[HTTPStatus, Any]:
    """GET /api/todos — 全 TODO を返す."""
    result = run_sql(trace, "SELECT id, title, done, created_at FROM todos ORDER BY id")
    trace.step("SQL実行", f"SELECT で {len(result.rows)} 件のTODOを取得")
    return HTTPStatus.OK, [row_to_todo(r) for r in result.rows]


def create_todo(trace: DebugTrace, params: dict[str, str], body: Any) -> tuple[HTTPStatus, Any]:
    """POST /api/todos — TODO を1件追加する."""
    # --- バリデーション (入力チェック) --------------------------------
    # ブラウザ側でもチェックしているが、サーバー側のチェックは省略できない。
    # ブラウザを介さず curl 等で直接リクエストを送ることもできるから。
    title = (body or {}).get("title", "") if isinstance(body, dict) else ""
    title = str(title).strip()
    if not title:
        trace.step("バリデーション", "title が空 → 400 Bad Request で拒否")
        raise ApiError(HTTPStatus.BAD_REQUEST, "タイトルを入力してください")
    if len(title) > TITLE_MAX_LEN:
        trace.step("バリデーション", f"title が {len(title)} 文字 (上限{TITLE_MAX_LEN}) → 400")
        raise ApiError(HTTPStatus.BAD_REQUEST, f"タイトルは{TITLE_MAX_LEN}文字以内にしてください")
    trace.step("バリデーション", f"title は1〜{TITLE_MAX_LEN}文字 → OK")

    # --- DB へ書き込み --------------------------------------------------
    insert = run_sql(trace, "INSERT INTO todos (title) VALUES (?)", (title,))
    trace.step("SQL実行", f"INSERT で1行追加 (自動採番された id={insert.last_id})")
    # 採番された id を使って、作成した行を完全な形で読み直して返す
    result = run_sql(
        trace,
        "SELECT id, title, done, created_at FROM todos WHERE id = ?",
        (insert.last_id,),
    )
    # 「新しく作った」ときは 200 OK ではなく 201 Created を返すのが HTTP の作法
    return HTTPStatus.CREATED, row_to_todo(result.rows[0])


def update_todo(trace: DebugTrace, params: dict[str, str], body: Any) -> tuple[HTTPStatus, Any]:
    """PATCH /api/todos/{id} — done や title を部分的に更新する."""
    todo_id = int(params["todo_id"])  # URLの中の数字 (ルーターが正規表現で取り出した)

    if not isinstance(body, dict) or not ({"done", "title"} & body.keys()):
        trace.step("バリデーション", "done も title も含まれていない → 400")
        raise ApiError(HTTPStatus.BAD_REQUEST, "done または title を指定してください")

    # 対象の行が存在するか先に確認する (なければ 404 Not Found)
    found = run_sql(trace, "SELECT id, title, done, created_at FROM todos WHERE id = ?", (todo_id,))
    if not found.rows:
        trace.step("存在チェック", f"id={todo_id} は見つからない → 404 Not Found")
        raise ApiError(HTTPStatus.NOT_FOUND, f"id={todo_id} のTODOは存在しません")
    trace.step("存在チェック", f"id={todo_id} は存在する → OK")

    if "done" in body:
        run_sql(trace, "UPDATE todos SET done = ? WHERE id = ?", (int(bool(body["done"])), todo_id))
        trace.step("SQL実行", f"UPDATE で done を {bool(body['done'])} に変更")
    if "title" in body:
        new_title = str(body["title"]).strip()
        if not new_title or len(new_title) > TITLE_MAX_LEN:
            trace.step("バリデーション", "新しい title が不正 → 400")
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"タイトルは1〜{TITLE_MAX_LEN}文字にしてください"
            )
        run_sql(trace, "UPDATE todos SET title = ? WHERE id = ?", (new_title, todo_id))
        trace.step("SQL実行", "UPDATE で title を変更")

    result = run_sql(
        trace, "SELECT id, title, done, created_at FROM todos WHERE id = ?", (todo_id,)
    )
    return HTTPStatus.OK, row_to_todo(result.rows[0])


def delete_todo(trace: DebugTrace, params: dict[str, str], body: Any) -> tuple[HTTPStatus, Any]:
    """DELETE /api/todos/{id} — TODO を1件削除する."""
    todo_id = int(params["todo_id"])
    result = run_sql(trace, "DELETE FROM todos WHERE id = ?", (todo_id,))
    if result.affected == 0:
        # DELETE 自体は成功するが「消えた行が0行」= そのIDは存在しなかった
        trace.step("存在チェック", f"id={todo_id} は見つからない → 404 Not Found")
        raise ApiError(HTTPStatus.NOT_FOUND, f"id={todo_id} のTODOは存在しません")
    trace.step("SQL実行", f"DELETE で id={todo_id} を削除")
    return HTTPStatus.OK, {"deleted_id": todo_id}


# ルーティング表: 「メソッド + URLパターン → 担当関数」の対応表。
# Flask の @app.route(...) や FastAPI の @app.get(...) がやっていることの正体は、
# 本質的にはこういう表を作ることに過ぎない。
API_ROUTES = [
    ("GET", re.compile(r"^/api/todos$"), list_todos),
    ("POST", re.compile(r"^/api/todos$"), create_todo),
    ("PATCH", re.compile(r"^/api/todos/(?P<todo_id>\d+)$"), update_todo),
    ("DELETE", re.compile(r"^/api/todos/(?P<todo_id>\d+)$"), delete_todo),
]


# ---------------------------------------------------------------------------
# HTTP リクエストの入り口 — ハンドラークラス
# ---------------------------------------------------------------------------


class LearningLabHandler(BaseHTTPRequestHandler):
    """ブラウザからの HTTP リクエストを1本ずつ処理するクラス.

    ThreadingHTTPServer がリクエストを受けるたびに、
    メソッド名に応じて do_GET / do_POST / … を呼んでくれる。
    """

    server_version = "WebLearningLab/1.0"  # レスポンスの Server ヘッダーに入る名前
    protocol_version = "HTTP/1.1"

    # -- 各 HTTP メソッドの入り口 (ここが最初に呼ばれる) -------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path  # "?query" 部分を取り除いてパスだけにする
        if path.startswith("/api/"):
            self._handle_api("GET")
        else:
            self._serve_static(path)  # API 以外はぜんぶ静的ファイル配信

    def do_POST(self) -> None:
        self._handle_api("POST")

    def do_PATCH(self) -> None:
        self._handle_api("PATCH")

    def do_DELETE(self) -> None:
        self._handle_api("DELETE")

    def do_PUT(self) -> None:
        # PUT に対応するAPIはこのアプリには無いが、入り口だけ用意しておく。
        # こうするとルーティング表に該当が無いため 405 Method Not Allowed が返り、
        # 「URLはあるがそのメソッドは受け付けない」を体験できる (実験: curl -X PUT)
        self._handle_api("PUT")

    # -- 静的ファイル配信 (HTML/CSS/JS をそのまま返す) ---------------------

    def _serve_static(self, path: str) -> None:
        """static/ ディレクトリの中のファイルを読んで、そのまま返す.

        Web サーバーの最も原始的な仕事。「URLのパス = ファイルの場所」という
        素朴な対応で、ブラウザにファイルの中身を運ぶだけ。
        """
        if path == "/":
            path = "/index.html"  # トップページは index.html を返す慣習

        # /static/app.js → static/app.js のようにファイルパスへ変換
        relative = path.removeprefix("/static/").lstrip("/")
        file_path = (STATIC_DIR / relative).resolve()

        # ★セキュリティ: "/../../etc/passwd" のような URL で static/ の外の
        #   ファイルを盗み見られないよう、解決後のパスが static/ 配下か確認する
        if not file_path.is_relative_to(STATIC_DIR) or not file_path.is_file():
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                "text/plain; charset=utf-8",
                f"404 Not Found: {path} というファイルはありません\n".encode(),
            )
            return

        content_type = MIME_TYPES.get(file_path.suffix, "application/octet-stream")
        self._send_bytes(HTTPStatus.OK, content_type, file_path.read_bytes())

    # -- API 処理 (このアプリの本体) ---------------------------------------

    def _handle_api(self, method: str) -> None:
        """API リクエスト1本の一生: ルーティング → ボディ解析 → 処理 → JSON応答."""
        path = urlparse(self.path).path
        trace = DebugTrace(method=method, path=path)  # 記録係を起動

        try:
            # ① ルーティング: 対応表から「この URL の担当関数」を探す
            handler, params = self._route(method, path, trace)
            # ② リクエストボディ (JSON) を読み取る
            body = self._read_json_body(trace)
            # ③ 担当関数に処理させる
            status, data = handler(trace, params, body)
            # ④ 成功レスポンスを組み立てて送信
            self._send_json(status, {"ok": True, "data": data, "error": None}, trace)
        except ApiError as exc:
            # バリデーション等で意図的に投げられたエラー → その内容をそのまま返す
            self._send_json(
                exc.status, {"ok": False, "data": None, "error": {"message": exc.message}}, trace
            )
        except Exception as exc:
            # 想定外の事故 (バグ) → 500 Internal Server Error
            trace.step("サーバーエラー", f"想定外の例外: {exc!r}")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "data": None,
                    "error": {"message": "サーバー内部でエラーが発生しました"},
                },
                trace,
            )

    def _route(self, method: str, path: str, trace: DebugTrace):
        """ルーティング表を上から順に照合し、担当関数と URL 内パラメータを返す."""
        for route_method, pattern, handler in API_ROUTES:
            match = pattern.match(path)
            if match and route_method == method:
                trace.step("ルーティング", f"{method} {path} → {handler.__name__}() が担当と判明")
                return handler, match.groupdict()
        # パスは合うがメソッドが違う場合は 405、どれにも合わなければ 404
        if any(p.match(path) for _, p, _ in API_ROUTES):
            trace.step("ルーティング", f"{path} は存在するがメソッド {method} は未対応 → 405")
            raise ApiError(
                HTTPStatus.METHOD_NOT_ALLOWED, f"{method} メソッドはこのURLでは使えません"
            )
        trace.step("ルーティング", f"{method} {path} に該当する処理がない → 404")
        raise ApiError(HTTPStatus.NOT_FOUND, f"{path} というAPIはありません")

    def _read_json_body(self, trace: DebugTrace) -> Any:
        """リクエストボディを読み、JSON として解析する.

        HTTP のボディは「Content-Length ヘッダーに書かれたバイト数だけ読む」
        というルールで受け取る。ボディがなければ None を返す。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            trace.step("ボディ解析", f"JSONとして壊れている ({exc.msg}) → 400")
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "リクエストボディが正しいJSONではありません"
            ) from exc
        trace.step("ボディ解析", f"JSON {length} バイトを読み取り成功")
        return body

    # -- レスポンス送信 (低レベルな共通処理) --------------------------------

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], trace: DebugTrace) -> None:
        """辞書を JSON に変換し、debug 情報を同梱して送信する."""
        trace.step("レスポンス生成", f"{status.value} {status.phrase} をJSONで返す")
        payload["debug"] = trace.to_dict()  # ★X-Rayパネルの表示元データを同梱
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        """HTTP レスポンスの決まりごとに従ってバイト列を送信する.

        レスポンスは「①ステータス行 → ②ヘッダー → ③空行 → ④ボディ」の順。
        以下の3つの send_* がその形式どおりに書き出してくれる。
        """
        self.send_response(status.value)  # ① HTTP/1.1 200 OK など
        self.send_header("Content-Type", content_type)  # ② ボディの種類
        self.send_header("Content-Length", str(len(body)))  # ② ボディの長さ
        self.end_headers()  # ③ 空行 (ヘッダー終了の合図)
        self.wfile.write(body)  # ④ ボディ本体

    def log_message(self, fmt: str, *args: Any) -> None:
        """ターミナルに出すアクセスログの形式 (1リクエスト=1行)."""
        print(f"[アクセス] {self.address_string()} - {fmt % args}")


# ---------------------------------------------------------------------------
# 起動処理
# ---------------------------------------------------------------------------


def main() -> None:
    """サーバーの起動: DB準備 → ポートで待ち受け開始."""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    init_db()

    # ThreadingHTTPServer = 「リクエストごとにスレッドを分けて並行処理できるサーバー」。
    # 第1引数 ("", port) は「このマシンの port 番ポートで待つ」という意味。
    server = ThreadingHTTPServer(("", port), LearningLabHandler)

    print("=" * 60)
    print("🔬 Web Learning Lab サーバーが起動しました")
    print(f"   ブラウザで  http://localhost:{port}  を開いてください")
    print(f"   データベース: {DB_PATH}")
    print("   停止するには Ctrl+C を押します")
    print("=" * 60)

    try:
        server.serve_forever()  # ここで無限ループに入り、リクエストを待ち続ける
    except KeyboardInterrupt:
        print("\nサーバーを停止しました。おつかれさまでした!")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
