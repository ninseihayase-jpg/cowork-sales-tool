"""フェーズ1 同期CLI。

スプレッドシート（Google Sheets）またはローカルxlsx → テーマDB へ同期する。

使い方:
  # Google Sheets から同期（本番）。.env の SALES_SHEET_ID 等を使用
  python scripts/sync_cli.py

  # ローカルxlsx から同期（Google接続前のテスト/フォールバック）
  python scripts/sync_cli.py --xlsx ../秘書/sales_themes_input.xlsx

  # 計画だけ表示（DBに書かない）
  python scripts/sync_cli.py --xlsx <path> --dry-run

環境変数（.env / シェル）:
  THEME_API_URL, THEME_API_TOKEN          … テーマDB API
  SALES_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON, SALES_WORKSHEETS … Google Sheets
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# パッケージ解決（リポジトリ直下から実行する想定）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cowork import sources, sync  # noqa: E402
from cowork.theme_db import ThemeDBClient  # noqa: E402


def _load_dotenv():
    """.env を最小実装で読み込む（外部依存なし）。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_dotenv()
    ap = argparse.ArgumentParser(description="スプシ→テーマDB 同期（フェーズ1）")
    ap.add_argument("--xlsx", help="ローカルxlsxパス（指定時はGoogle Sheetsでなくこれを読む）")
    ap.add_argument("--sheet", default="テーマKPI", help="xlsxのシート名（--xlsx時）")
    ap.add_argument("--dry-run", action="store_true", help="DBに書かず計画のみ表示")
    args = ap.parse_args()

    # --- データソースの読み込み ---
    if args.xlsx:
        rows = sources.read_xlsx(args.xlsx, args.sheet)
        src_desc = f"xlsx={args.xlsx} (sheet={args.sheet})"
    else:
        sheet_id = os.environ.get("SALES_SHEET_ID", "").strip()
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()
        worksheets = [w.strip() for w in os.environ.get("SALES_WORKSHEETS", "Sales,Sales以外").split(",") if w.strip()]
        if not sheet_id:
            print("エラー: SALES_SHEET_ID 未設定（.env を設定するか --xlsx を使ってください）", file=sys.stderr)
            sys.exit(1)
        if not Path(sa_json).exists():
            print(f"エラー: サービスアカウント鍵が見つかりません: {sa_json}", file=sys.stderr)
            sys.exit(1)
        rows = sources.read_google_sheets(sheet_id, worksheets, sa_json)
        src_desc = f"GoogleSheets id={sheet_id} worksheets={worksheets}"

    print(f"ソース: {src_desc}")
    print(f"読み込み行数: {len(rows)}")

    # --- 同期 ---
    client = None
    if not args.dry_run:
        api_url = os.environ.get("THEME_API_URL", "https://hisho-ohxe.onrender.com")
        token = os.environ.get("THEME_API_TOKEN", "")
        if not token:
            print("エラー: THEME_API_TOKEN 未設定", file=sys.stderr)
            sys.exit(1)
        client = ThemeDBClient(api_url, token)

    result = sync.sync_rows(rows, client, dry_run=args.dry_run)

    if args.dry_run:
        print("\n=== DRY RUN（DBには書き込みません）===")
        for line in result.planned:
            print(" ", line)
    print(f"\n完了: {result.summary()}")
    for err in result.errors:
        print("  [ERROR]", err, file=sys.stderr)
    sys.exit(1 if result.errors else 0)


if __name__ == "__main__":
    main()
