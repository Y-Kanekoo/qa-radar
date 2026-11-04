# radar-core の段階的導入

## 第1段階

[radar-core v0.1.0](https://github.com/Y-Kanekoo/radar-core/tree/v0.1.0) を
`radar-core @ git+https://github.com/Y-Kanekoo/radar-core@v0.1.0` で指定する。
`uv.lock` はタグの参照先コミット `d4301e178c1fd17e1c4688e9442f4ddd0c2ebe02` を固定する。
ローカルの読み取り用クローンも同じタグ・コミットで比較した。

`qa_radar.crawler.normalize` の `normalize_published`・`strip_html`・
`compute_body_hash` のみを薄いラッパーから委譲する。日時変換・HTML除去は同一処理で、
本文ハッシュも同じ HTML 除去・空白圧縮・UTF-8 の SHA256 である。
公開関数の引数・戻り値・モジュールパス、CLI、出力、ログ、DB、通知内容は維持する。
空白圧縮・正規化済み本文のハッシュは共有側に公開APIがないため既存実装を残す。

## 次の段階の候補

| 対象 | qa-radar と radar-core v0.1.0 の差分・今回見送る理由 |
|---|---|
| `normalize_url` | qa-radar の除去対象は10パラメータ、共有側は20。共有側は末尾スラッシュと arXiv version も除去する。保存済みURL・重複判定が変わるため、DBとの差分確認と移行方針が必要。 |
| `make_snippet` | 正の上限では同じだが、共有側は `max_chars < 1` を `ValueError` にする。qa-radar は0や負値でも従来のスライス処理を行うため、引数の境界で互換にならない。 |
| `fetch_feed` | qa-radar は既定2回リトライ、共有側は0回。User-Agent、エラー文字列、不正URLの扱い、注入クライアントのタイムアウト上書き、負の待機秒数の扱いも異なる。ログや取得結果を維持する調整が必要。 |
| `RobotsCache` | 共有側に User-Agent の注入口がなく、robots.txt の要求ヘッダと `can_fetch` の判定対象が radar-core になる。qa-radar の識別子を維持できない。 |
| `parse_feed` | 共有側は記事リンクがない音声フィードに enclosure URL を補完し、GUIDにも使う。文字列を本文として固定し、辞書内の型・日付値も検証するため、URLやパス文字列、非標準エントリの処理が異なる。解析結果と入力契約の確認が必要。 |
| Discord | 共有側は UTF-16 単位の省略記号付き切り詰め・description 4096文字・合計6000文字制限・空フィールド除去・メンション無効化・分割送信を適用する。qa-radar は Python の文字数で切り、description 2048文字、記事単位で送る。共有側は既定 `wait=true` とID確認、429は JSON 優先かつ待機の下限なし。qa-radar は2xx成功、ヘッダ優先かつ最低0.5秒。タイムアウト・リトライ既定値・URL処理・ログも異なり、ペイロード・成功判定・通知履歴の維持を確認する必要がある。 |

既存の説明には抜粋「100字以内」とあるが、コードと既存テストは本文100字＋`…` の
最大101字を許容する。今回はコードに従って維持し、上限仕様の見直しは別途判断する。

## インストールと検証

Git と公開 GitHub リポジトリへの HTTPS 接続が必要。追加のAPIキーや認証登録は不要。
Hatchling は直接参照依存を既定で拒否するため、`allow-direct-references = true` を設定する。
既存の全 workflow は uv で依存を取得しており、`--require-hashes` は使っていないため、
Git依存と矛盾せず workflow の変更は不要。CIの Python 3.11 / 3.12 で以下を検証する。

```sh
uv sync --locked --all-extras --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run pytest -v --cov=qa_radar --cov-report=term-missing
uv run coverage report --fail-under=80
actionlint -color
shellcheck scripts/*.sh
uv build
uv run --with twine -- twine check dist/*
```

CIには型検査の設定がないため、変更した実装に対して次を実行する。

```sh
uv run --with mypy -- mypy --strict --follow-imports=silent src/qa_radar/crawler/normalize.py
```

互換性テストは通信しない。既存の実フィード疎通テスト2件は、既存設定どおり
`--integration` 未指定ではスキップする。`health.yml` の実通信テストや公開・通知処理は
ローカル検証では実行しない。GitHub Actions 自体は無料枠切れのため実行しない。

Python 3.11 / 3.12 とも437件成功・既存の2件スキップ、カバレッジ約97%を確認した
（新規互換性テスト20件を含む）。既存テストのローカルHTTPサーバーと接続失敗確認を
許可した通信制限付きの追加検証は次のとおり。

```sh
uv run --locked --all-extras --with pytest-socket -- pytest --allow-hosts=127.0.0.1,0.0.0.0,::1 --allow-unix-socket
```

## 配布上の制限

GitHubからのインストールとローカルビルドは可能だが、直接URL依存を持つ配布物は
[PyPIでは受け付けられない](https://setuptools.pypa.io/en/stable/userguide/dependency_management.html#direct-url-dependencies)。
`twine check` は PyPI へのアップロード可否を保証しない。既存の `pypi.yml` は変更せず、
TestPyPI / PyPI 公開前に radar-core の配布と依存指定の方針を監督側で決める。
この制限は指定された Git 依存に伴うもので、CIや収集・Pagesの依存インストールとは別である。
