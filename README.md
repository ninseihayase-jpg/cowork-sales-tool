# 営業支援ツール（Cowork）

InProc社内の営業管理（簡易Salesforce）。アカウント・商談を管理し、既存の**テーマDB／Salesダッシュボード**（秘書プロジェクト）へタイムリーに連携する。

- 設計の正本: [`docs/00_設計構想.md`](docs/00_設計構想.md)
- Googleセットアップ: [`docs/01_Googleセットアップ手順.md`](docs/01_Googleセットアップ手順.md)
- 自動化・デプロイ: [`docs/02_自動化とデプロイ.md`](docs/02_自動化とデプロイ.md)

## フェーズ

| Phase | 内容 | 状態 |
|---|---|---|
| 1 | スプレッドシート(Google Sheets)手編集 → テーマDB自動反映（SFAツールなし） | ✅ 実装・検証済（Google接続待ち） |
| 2-1 | ブラウザ入力画面＋独立した営業情報DB → テーマDB連携 | ✅ ローカル稼働（基本機能） |
| 2-2 | Slack商談メモ → AIが構造化転記＋不足ヒアリング | ⬜（Go/NoGo検証前） |

## フェーズ2-1の使い方（ブラウザ入力画面）

```bash
python scripts/run_webapp.py        # → http://localhost:8787
```

- アカウント（企業）／商談／活動を画面から登録。ステージ等はプルダウンで入力負荷を最小化。
- 各商談の「テーマDB／ダッシュボードへ同期」ボタンで、SalesテーマとしてテーマDBへ反映（`.env` に `THEME_API_TOKEN` がある時のみ有効）。
- 営業情報DBは独立した SQLite（`cowork_sfa.db`、`.gitignore`済み）。外部依存なし（Python標準ライブラリのみ）。

## フェーズ1の使い方

```bash
pip install --user -r requirements.txt    # venvが作れる環境なら venv 推奨

# 初期データCSV生成（既存xlsx → Sales/Sales以外 に分割）
python scripts/seed_sheet.py

# 同期（DRY-RUN: DBに書かず計画表示）
python scripts/sync_cli.py --xlsx <既存xlsxパス> --dry-run   # ローカルxlsxで検証
python scripts/sync_cli.py --dry-run                          # Google Sheetsで検証（.env必要）

# 本同期（テーマDB→ダッシュボードに反映）
python scripts/sync_cli.py
```

詳細なGoogle接続手順は `docs/01_Googleセットアップ手順.md`。

## 構成

```
cowork/              Pythonパッケージ（同期エンジン）
  mapping.py         スプシ行 → テーマDBカラムのマッピング（実証済みロジック）
  sources.py         入力ソース（ローカルxlsx / Google Sheets）
  theme_db.py        テーマDB(/api/execute)クライアント
  sync.py            同期オーケストレーション（UPSERT・冪等・dry-run）
scripts/
  sync_cli.py        同期CLI
  seed_sheet.py      Google Sheet初期投入用CSV生成
docs/                設計書・手順書
.github/workflows/   定時同期(GitHub Actions)
```

## テーマDBとの関係

- テーマDBの正本は秘書プロジェクト（Render `hisho-ohxe.onrender.com` の SQLite `todos/theme`）。
- 本ツールは API（`/api/execute`・`/api/themes.csv`）経由で**疎結合**に連携する独立プロジェクト。
- スキーマ仕様の正本は 秘書側 `docs/db_schema_design.md`。
</content>
