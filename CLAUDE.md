# 営業支援ツール(SFA-CRM) — 開発ガイド

親フォルダの [`../CLAUDE.md`](../CLAUDE.md) と、連携仕様の [`../INTEGRATION.md`](../INTEGRATION.md) を先に読むこと。
両ツールにまたがる変更（同期・スキーマ・定数）をする場合は `秘書Bot(Hisho)/CLAUDE.md` も読むこと。

## アーキテクチャ方針

Webアプリ本体（`cowork/webapp.py`）は **Python標準ライブラリのみ** で書かれている。
`http.server`（`BaseHTTPRequestHandler` + `ThreadingHTTPServer`）でHTTPサーバーを実装し、
Flask/Django等のWebフレームワークは一切使わない。DBはSQLite（`sqlite3`）で、ORMも使わない。
「挙動安定・依存ゼロ優先」が設計方針（`webapp.py` 冒頭docstring）。
※ ヒアリング結果のExcel/Word出力機能のみ `openpyxl` / `python-docx` を限定使用する（`requirements.txt`）。

## 主要ファイル構成（`cowork/`）

- `webapp.py`（約6,500行）— 全ページのHTML生成 + ルーティング + フォーム処理。本ツールの心臓部。
- `sfa_db.py`（約1,260行）— DBスキーマ（`SCHEMA`）・マイグレーション（`init_db()`）・全CRUD関数・
  マスタ定数（`DEAL_STAGES`, `DEV_PROJECT_STATUSES`等）・祝日/営業日計算。
- `slack_bot.py` — `#sales` スレッドで `@NegoCollection` にメンションすると、Claudeがスレッド内容を
  商談情報に構造化転記するSlack Bot（`/slack/events` 経由でwebapp.pyから呼ばれる）。
- `theme_link.py` — 商談(deal) → Hisho側 `todos` テーブルへの同期（`docs/00_設計構想.md §7`）。
- `dev_project_link.py` — 開発案件(dev_project) → Hisho側 `dev_projects` テーブルへの同期。
- `theme_db.py` — Hisho側 `/api/execute` を叩く薄いHTTPクライアント（`ThemeDBClient`）。
- `mapping.py` — テーマDBカラムへのマッピング定義。フェーズ1由来だが `theme_link.py` が現役で
  import しており削除不可。`sources.py`・`scripts/sync_cli.py`等フェーズ1固有スクリプトはレガシー。
- `leads_csv.py` / `deals_csv.py` / `meishi_import.py` — CSV/名刺画像の一括取込。

## webapp.pyのルーティング構造

`_make_handler(db_path, theme_client)` がハンドラクラス `H(BaseHTTPRequestHandler)` を生成する
クロージャで、`do_GET`/`do_POST`の中に **`if path == "..." / elif path == "..." /
elif path.startswith("...") and path.endswith("...")` の巨大なif-elif連鎖**でルーティングしている
（フレームワークのルーターは無い）。ルート追加時は:

1. 既存の`elif`連鎖に、パターンが近い箇所を探して `elif path == "/foo"` や
   `elif path.startswith("/foo/") and path.endswith("/bar")` を追記する（先勝ちなので、より
   限定的なパスは広いパスより先に置く）。
2. ページ本体は別関数（例: `xxx_page(con, ...) -> str`）として実装し、`do_GET`/`do_POST`からは
   呼んで `self._send(render(...))` するだけにする（既存の`*_page`関数群を参考にする）。

## 共通ヘルパー（新規実装前に必ず探すこと）

**同じような処理を新規に書く前に、必ず既存のヘルパーを `grep` で探すこと。** 主なもの:

- `_esc(v)` — HTMLエスケープ（`html.escape`のラッパー）。ユーザー入力を出力する箇所は必ず通す。
- `_opt(values, selected)` / `_opt_kv(pairs, selected)` / `_opt_l2(l1, selected)` — `<select>`の
  `<option>`群を生成。
- `_sticky_th(label, width=None)` — テーブルのスクロール追従ヘッダ`<th>`セルを生成。
- `_tool_link_btn(url, label=..., tool_id=None, tool_password=None)` — 外部ツールへのリンクボタン
  （制作ツールのログインID/PASS表示込み）。
- `render(body, flash="")` — `PAGE`定数（HTML雛形/CSS/JS込みの共通レイアウト文字列）に本文を埋め込み、
  レスポンスバイト列を返す。全ページの出口はここを通る。
- `_call_claude_haiku(prompt, timeout=20, max_wait=25)` — Claude Haiku呼び出しの共通ラッパー
  （AI要約・下書き生成などで使用）。
- `_chips(values)` / `_ai_prompt_block(...)` / `_content_disposition(filename)` など多数。

## スキーマ変更の作法

- スキーマ本体は `sfa_db.py` の `SCHEMA`（`CREATE TABLE IF NOT EXISTS ...`の塊）。
- 既存本番DBへの後方互換マイグレーションは `init_db()` 内で `PRAGMA table_info(...)` を見て
  `ALTER TABLE ... ADD COLUMN` を都度追記する方式（マイグレーション管理ツールは無い）。
- **警告（実事故あり）**: SQLiteは列のNOT NULL制約等を直接ALTERできないため、テーブルの作り直しが
  必要な場合は「新テーブル作成→データコピー→DROP→RENAME」の手順を踏む。この際
  `PRAGMA foreign_keys = ON` のまま元テーブルを`DROP TABLE`すると、`ON DELETE CASCADE`が
  紐づく子テーブルの全行に適用され、既存データが全消失する（`deal_issues`再構築で実際に発生）。
  `DROP TABLE`の前に必ず `PRAGMA foreign_keys = OFF` にし、作り直し完了後に `ON` へ戻すこと
  （`sfa_db.py` の`deal_issues`マイグレーション処理が実例。行番号は変わりうるので
  `init_db()`内を`grep`して確認すること）。

## ローカル起動

```bash
python scripts/run_webapp.py        # → http://localhost:8787
```

`.env` の `THEME_API_TOKEN` が設定されていれば「テーマDB/Hishoへ同期」が有効になる。
テストスイートは無いため、最低限の構文チェックとして以下を実行してから保存/デプロイすること。

```bash
python3 -m py_compile cowork/webapp.py cowork/sfa_db.py   # 変更した.pyファイルを都度指定
```

## 本番環境

`render.yaml` にサービス定義がある。Webサービス `sfa-crm`（`python scripts/run_webapp.py`）。
DBは永続ディスク `/data/cowork_sfa.db`。cronは3本: `sfa-weekly-notify`（週次商談確認Slack通知、
水17:30 JST）、`sfa-daily-appt-notify`（翌日アポのSlackスレ立て、日次06:00 JST）、
`sfa-daily-export`（商談情報をGoogle Sheetsへエクスポート、日次09:00 JST）。

## 触ってはいけないもの

- `.env` / `service_account.json` — 認証情報。誤って書き換え・コミットしないこと。
- `cowork_sfa.db` — 実データ（クライアント名・金額等の機微情報を含む本番相当データ）。
  スキーマ確認等はコピーを取って行う。
- `scripts/`配下のワンショット移行スクリプト（`migrate_themes.py`等）— 本番DBに再実行しない。

## テスト

現状テストコードは無し（`pytest`未導入）。整備予定（祝日/営業日計算・DB層CRUD・重要ルートの
スモークテストを想定）。変更時は手動でのページ動作確認と `py_compile` を最低限行うこと。
