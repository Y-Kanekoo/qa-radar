/*
  Web Learning Lab — 「動き」を担当するファイル (JavaScript)

  このファイルの仕事は大きく3つ:
    1. TODOアプリとしての動作 …… ボタン操作に反応し、fetch() でサーバーと通信する
    2. X-Rayパネルへの描画   …… 実際に流れたリクエスト/レスポンス/サーバー内部記録を表示
    3. 「旅の地図」のアニメ  …… 直前の通信の流れを6ステージで再生する

  ★すべての通信は apiCall() という1つの関数を通る。
    ここさえ読めば「fetchでHTTP通信する」の全体像がつかめる。
*/

"use strict";

/* ===== 小道具 ===== */

// document.querySelector の短縮形。$("#todo-list") のように使う
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

// 指定ミリ秒だけ待つ Promise。アニメーションの「間」を作るのに使う
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ステータスコード → 意味の対応表 (レスポンス表示・解説に使う)
const HTTP_PHRASES = {
  200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found",
  405: "Method Not Allowed", 500: "Internal Server Error",
};

/* ===== アプリの状態 (メモリ上に持つデータ) ===== */

let todos = [];        // いま画面に出しているTODOの配列 (サーバーのDBの写し)
let historyLog = [];   // このページで発生した通信の記録
let slowMode = false;  // じっくりモードON/OFF
let animToken = 0;     // アニメーションの世代番号 (連打時に古い再生を止めるため)

/* =====================================================================
   1. サーバーとの通信 — すべての fetch はこの関数を通る
   ===================================================================== */

/**
 * サーバーのAPIを呼び、通信の全記録を取ってX-Rayパネルに反映する。
 * @param {string} method  - HTTPメソッド (GET / POST / PATCH / DELETE)
 * @param {string} path    - URLのパス (例: "/api/todos")
 * @param {object=} body   - 送るデータ (JSONにして送信。不要なら undefined)
 * @param {string} meaning - この通信の意味 (履歴・ナレーション表示用)
 * @returns サーバーが返したJSON (通信自体に失敗したら null)
 */
async function apiCall(method, path, body, meaning) {
  // --- 送信前: リクエストの中身を組み立てて記録する -------------------
  const bodyText = body === undefined ? null : JSON.stringify(body);
  const requestText = buildRequestText(method, path, bodyText);
  const startedAt = performance.now(); // 往復時間の計測開始

  let response, json;
  try {
    // ★ここが心臓部。fetch() がHTTPリクエストを送信し、返事を待つ。
    //   await は「返事が来るまでこの行で待つ」という意味 (待つ間も画面は固まらない)
    response = await fetch(path, {
      method: method,
      headers: bodyText !== null ? { "Content-Type": "application/json" } : {},
      body: bodyText ?? undefined,
    });
    json = await response.json(); // レスポンスボディ(JSONテキスト)をオブジェクトに変換
  } catch (error) {
    // ここに来るのは「サーバーに届かなかった」とき (サーバー停止など)。
    // 400や404は「届いて返事が来た」のでここには来ない — 大事な区別!
    setNarration("🚫 通信そのものに失敗しました。サーバー(server.py)は起動していますか?");
    return null;
  }

  const elapsedMs = Math.round((performance.now() - startedAt) * 10) / 10;

  // --- 受信後: 1回の通信を「レコード」としてまとめる -------------------
  const record = {
    method, path, meaning,
    status: response.status,
    statusText: response.statusText || HTTP_PHRASES[response.status] || "",
    requestText,
    responseText: buildResponseText(response, json),
    json,
    elapsedMs,
    time: new Date().toLocaleTimeString(),
  };

  historyLog.unshift(record); // 履歴の先頭に追加 (新しいものが上)
  renderHistory();
  showRecord(record);         // リクエスト/レスポンス/サーバー内部タブを更新
  playFlowAnimation(record);  // 旅の地図を再生 (await しない = 裏で流しておく)

  return json;
}

/** HTTPリクエストの生テキスト(に相当するもの)を組み立てる */
function buildRequestText(method, path, bodyText) {
  let text = `${method} ${path} HTTP/1.1\n`;   // ①リクエスト行
  text += `Host: ${location.host}\n`;          // ②ヘッダー
  text += "Accept: application/json\n";
  if (bodyText !== null) {
    text += "Content-Type: application/json\n";
    text += "\n";                              // ③空行 (ヘッダー終わりの合図)
    text += bodyText;                          // ④ボディ
  } else {
    text += "\n(ボディなし — GETやDELETEでは送らないのが普通)";
  }
  return text;
}

/** HTTPレスポンスの生テキスト(に相当するもの)を組み立てる */
function buildResponseText(response, json) {
  const phrase = response.statusText || HTTP_PHRASES[response.status] || "";
  let text = `HTTP/1.1 ${response.status} ${phrase}\n`;        // ①ステータス行
  for (const name of ["content-type", "content-length", "server", "date"]) {
    const value = response.headers.get(name);                  // ②ヘッダー
    if (value) text += `${name}: ${value}\n`;
  }
  text += "\n";                                                // ③空行
  // ④ボディ。debug は長いので「サーバー内部タブを見て」という印に置き換える
  const bodyForDisplay = { ...json, debug: "(⚙️サーバー内部タブに展開表示中)" };
  text += JSON.stringify(bodyForDisplay, null, 2);
  return text;
}

/* =====================================================================
   2. X-Rayパネルへの描画
   ===================================================================== */

/** 1つの通信レコードを各タブに表示する */
function showRecord(record) {
  $("#request-raw").textContent = record.requestText;
  $("#response-raw").textContent = record.responseText;
  renderServerSteps(record.json?.debug);
}

/** サーバー内部タブ: debug.steps を表に、debug.sql をリストにする */
function renderServerSteps(debug) {
  const stepsBox = $("#server-steps");
  const sqlBox = $("#sql-list");
  if (!debug) {
    stepsBox.textContent = "(debug情報がありません)";
    sqlBox.textContent = "(なし)";
    return;
  }

  // 処理ステップの表を DOM 操作で組み立てる
  // (innerHTML に文字列を流し込まないのは、値を安全に扱う習慣づけのため)
  const table = document.createElement("table");
  table.innerHTML =
    "<thead><tr><th>at</th><th>処理</th><th>くわしく</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const step of debug.steps) {
    const tr = document.createElement("tr");
    const tdMs = document.createElement("td");
    tdMs.className = "step-ms";
    tdMs.textContent = `${step.at_ms} ms`;
    const tdName = document.createElement("td");
    tdName.textContent = step.name;
    const tdDetail = document.createElement("td");
    tdDetail.textContent = step.detail;
    tr.append(tdMs, tdName, tdDetail);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  stepsBox.replaceChildren(table);

  const total = document.createElement("p");
  total.className = "tab-hint";
  total.textContent = `サーバー内の総処理時間: ${debug.total_ms} ms — 1秒の何百分の1で全部終わっている!`;
  stepsBox.appendChild(total);

  // 実行されたSQL
  if (!debug.sql || debug.sql.length === 0) {
    sqlBox.textContent = "(この通信ではSQLは実行されませんでした)";
    return;
  }
  sqlBox.replaceChildren();
  for (const item of debug.sql) {
    const wrap = document.createElement("div");
    wrap.className = "sql-item";
    const pre = document.createElement("pre");
    pre.className = "http-raw";
    pre.textContent = item.query;
    const params = document.createElement("div");
    params.className = "sql-params";
    params.textContent =
      `? に入った値: ${JSON.stringify(item.params)} ／ 対象行数: ${item.rows}`;
    wrap.append(pre, params);
    sqlBox.appendChild(wrap);
  }
}

/** 履歴タブの一覧を描き直す */
function renderHistory() {
  const list = $("#history-list");
  list.replaceChildren();
  for (const record of historyLog) {
    const li = document.createElement("li");
    li.title = "クリックで内容を再表示";

    const badge = document.createElement("span");
    badge.className = `badge badge-${String(record.status)[0]}xx`;
    badge.textContent = record.status;

    const method = document.createElement("span");
    method.className = "history-method";
    method.textContent = record.method;

    const pathSpan = document.createElement("span");
    pathSpan.className = "history-path";
    pathSpan.textContent = `${record.path} — ${record.meaning}`;

    const ms = document.createElement("span");
    ms.className = "history-ms";
    ms.textContent = `${record.time} / ${record.elapsedMs}ms`;

    li.append(badge, method, pathSpan, ms);
    // クリックしたら、その通信を各タブに再表示してアニメも再生
    li.addEventListener("click", () => {
      showRecord(record);
      playFlowAnimation(record);
      switchTab("request");
    });
    list.appendChild(li);
  }
}

/* =====================================================================
   3. 「旅の地図」アニメーション
   ===================================================================== */

/** 通信レコードから、6ステージぶんのナレーション文を作る */
function buildNarrations(record) {
  const debug = record.json?.debug;
  const ok = record.json?.ok;
  const sqlCount = debug?.sql?.length ?? 0;
  const statusLabel = `${record.status} ${record.statusText}`;

  return [
    // ステージ1: ブラウザのJS
    `あなたの操作をJavaScriptが受け取り、fetch()で「${record.meaning}」のリクエストを組み立てました。`,
    // ステージ2: リクエスト送信
    `HTTPリクエスト「${record.method} ${record.path}」が ${location.host} のサーバーへ送られました (📤リクエストタブに実物)。`,
    // ステージ3: サーバー処理
    "サーバーがルーティング表からこのURLの担当関数を選び、届いたデータを検査しました (⚙️サーバー内部タブに記録)。",
    // ステージ4: DB
    sqlCount > 0
      ? `データベースに対してSQLを${sqlCount}本実行しました。実行文はサーバー内部タブで確認できます。`
      : "今回の処理ではデータベースまで到達しませんでした (手前のチェックで止まったか、DB不要の処理でした)。",
    // ステージ5: レスポンス
    `結果をJSONにまとめ、ステータス「${statusLabel}」として返送しました (サーバー内処理はわずか ${debug?.total_ms ?? "?"}ms)。`,
    // ステージ6: 画面更新
    ok
      ? "JavaScriptが返事のJSONを受け取り、ページを移動せずに必要な部分だけ描き直しました。これが「Webアプリが動く」の正体です。"
      : `エラーの返事 (${record.json?.error?.message ?? "不明"}) を受け取りました。失敗しても画面が壊れないのは、JSが返事を検査してから使うからです。`,
  ];
}

/** 旅の地図を1→6ステージの順に点灯させ、ナレーションを流す */
async function playFlowAnimation(record) {
  const myToken = ++animToken; // 新しい再生が始まったら古い再生は途中でやめる
  const stages = [...$$(".stage")];
  const narrations = buildNarrations(record);
  const stepDuration = slowMode ? 1400 : 400; // じっくりモードはゆっくり

  for (let i = 0; i < stages.length; i++) {
    if (myToken !== animToken) return; // 新しい通信が始まっていたら中断
    stages.forEach((el, j) => {
      el.classList.toggle("active", j === i);   // いまのステージを点灯
      el.classList.toggle("passed", j < i);     // 通過済みはうっすら点灯
    });
    setNarration(`【${i + 1}/6】${narrations[i]}`);
    await sleep(stepDuration);
  }

  if (myToken !== animToken) return;
  stages.forEach((el) => { el.classList.remove("active"); el.classList.add("passed"); });
  const okMark = record.json?.ok ? "✅" : "⚠️";
  setNarration(
    `${okMark} 往復完了: ${record.method} ${record.path} → ${record.status} ` +
    `${record.statusText} (ブラウザから見た往復時間 ${record.elapsedMs}ms)`
  );
}

function setNarration(text) {
  $("#narration").textContent = text;
}

/* =====================================================================
   4. TODOアプリとしての動作 (CRUD)
   ===================================================================== */

/** 一覧を取得して描画する (Read = GET) */
async function loadTodos() {
  const json = await apiCall("GET", "/api/todos", undefined, "TODO一覧を取得");
  if (json?.ok) {
    todos = json.data;
    renderTodos();
  }
}

/** 追加 (Create = POST) */
async function addTodo(title) {
  const json = await apiCall("POST", "/api/todos", { title }, `「${title}」を追加`);
  if (json?.ok) {
    todos.push(json.data); // サーバーが確定させた1件(id採番済み)を手元にも反映
    renderTodos();
    $("#todo-input").value = ""; // 入力欄を空に戻す
  }
}

/** 完了/未完了の切り替え (Update = PATCH) */
async function toggleTodo(todo) {
  const json = await apiCall(
    "PATCH", `/api/todos/${todo.id}`, { done: !todo.done },
    `「${todo.title}」を${todo.done ? "未完了" : "完了"}に変更`
  );
  if (json?.ok) {
    todos = todos.map((t) => (t.id === todo.id ? json.data : t));
    renderTodos();
  }
}

/** 削除 (Delete = DELETE) */
async function deleteTodo(todo) {
  const json = await apiCall(
    "DELETE", `/api/todos/${todo.id}`, undefined, `「${todo.title}」を削除`
  );
  if (json?.ok) {
    todos = todos.filter((t) => t.id !== todo.id);
    renderTodos();
  }
}

/** 手元の todos 配列から <li> 要素を組み立て、一覧を描き直す */
function renderTodos() {
  const list = $("#todo-list");
  list.replaceChildren(); // いったん空にして作り直す (素朴だが確実な方法)

  for (const todo of todos) {
    const li = document.createElement("li");
    if (todo.done) li.classList.add("done");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = todo.done;
    checkbox.addEventListener("change", () => toggleTodo(todo));

    const title = document.createElement("span");
    title.className = "todo-title";
    // textContent を使えば「<script>」のような文字列もただの文字として表示される。
    // innerHTML に流し込むと XSS という攻撃の入口になる — 安全第一!
    title.textContent = todo.title;

    const del = document.createElement("button");
    del.className = "todo-delete";
    del.textContent = "🗑";
    del.title = "削除 (DELETE リクエストを送る)";
    del.addEventListener("click", () => deleteTodo(todo));

    li.append(checkbox, title, del);
    list.appendChild(li);
  }

  // 1件もなければ「まだありません」の文を出す
  $("#todo-empty").style.display = todos.length === 0 ? "" : "none";
}

/* =====================================================================
   5. 画面部品のイベント設定 (起動時に1回だけ実行)
   ===================================================================== */

// --- TODO追加フォーム: submit イベントを横取りして fetch に置き換える ---
$("#todo-form").addEventListener("submit", (event) => {
  // preventDefault しないとブラウザ標準の「フォーム送信 = ページ全体を再読込」が起きる。
  // それを止めて fetch で通信するのが、いわゆる SPA/Ajax の第一歩。
  event.preventDefault();
  addTodo($("#todo-input").value.trim());
});

// --- 実験コーナー: わざと失敗するリクエストを送る ---
$("#exp-empty-title").addEventListener("click", () => {
  apiCall("POST", "/api/todos", { title: "" }, "💥実験: 空のタイトルを送信");
});
$("#exp-missing-id").addEventListener("click", () => {
  apiCall("DELETE", "/api/todos/99999", undefined, "💥実験: 存在しないIDを削除");
});

// --- X-Rayパネルのタブ切り替え ---
function switchTab(name) {
  $$(".tab-btn").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.tab === name));
  $$(".tab-body").forEach((body) =>
    body.classList.toggle("hidden", body.id !== `tab-${name}`));
}
$$(".tab-btn").forEach((btn) =>
  btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

// --- 実験室/教科書のビュー切り替え ---
function switchView(name) {
  $("#view-lab").classList.toggle("hidden", name !== "lab");
  $("#view-textbook").classList.toggle("hidden", name !== "textbook");
  $("#nav-lab").classList.toggle("active", name === "lab");
  $("#nav-textbook").classList.toggle("active", name === "textbook");
}
$("#nav-lab").addEventListener("click", () => switchView("lab"));
$("#nav-textbook").addEventListener("click", () => switchView("textbook"));

// --- じっくりモード ---
$("#slow-mode").addEventListener("change", (event) => {
  slowMode = event.target.checked;
  setNarration(slowMode
    ? "🐢 じっくりモードON: 次の操作から、通信の旅をゆっくり再生します。"
    : "🐇 通常モードに戻しました。");
});

/* ===== 起動: ページを開いたらまず一覧を取得する =====
   ここで最初の GET /api/todos が飛ぶ。
   「リロードしてもTODOが残っている」のは、この取得のたびに
   サーバーがDBから読み直しているから (=永続化)。 */
loadTodos();
