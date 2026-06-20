"""テーマDB → Google Sheets エクスポート（Sales除外）。

テーマDBから全テーマを取得し、Sales を除外した上で
  - 分類=Delivery → "Delivery" シート
  - それ以外       → "その他" シート
に書き出す。既存シートの内容は上書きされる。

使い方:
  python scripts/export_themes_to_sheets.py
  python scripts/export_themes_to_sheets.py --dry-run   # スプシ書き込みをスキップ
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

BASE_URL = os.environ.get("THEME_API_URL", "https://hisho-ohxe.onrender.com")
TOKEN = os.environ.get("THEME_API_TOKEN", "")
SHEET_ID = os.environ.get("SALES_SHEET_ID", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")

# Delivery / その他 シートに出力する列（ヘッダ名, テーマDBフィールド名）
EXPORT_COLUMNS = [
    ("ID",               "id"),
    ("テーマ名",           "title"),
    ("分類",              "category"),
    ("重要度",             "importance"),
    ("Status",           "status"),
    ("クライアント",        "client_name"),
    ("案件名",             "deal_name"),
    ("事業種別L1",         "business_type_l1"),
    ("事業種別L2",         "business_type_l2"),
    ("担当",              "deal_owner"),
    ("ワンタイム総額(万円)",  "deal_value_lumpsum"),
    ("ワンタイム月額(万円)",  "deal_value_lumpsum_monthly"),
    ("継続月額(万円)",      "deal_value_recurring"),
    ("クライアント予算(万円)", "client_budget"),
    ("業界",              "industry"),
    ("企業規模",           "company_size"),
    ("稼働対象",           "in_delivery"),
    ("稼働率(%)",          "utilization"),
    ("開始日",             "start_date"),
    ("終了日",             "end_date"),
    ("現状メモ",            "note"),
    ("ゴール",             "goal"),
]
HEADERS = [h for h, _ in EXPORT_COLUMNS]
FIELDS  = [f for _, f in EXPORT_COLUMNS]


def fetch_themes() -> list[dict]:
    url = f"{BASE_URL}/api/themes?token={TOKEN}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def theme_to_row(t: dict) -> list:
    return [t.get(f) if t.get(f) is not None else "" for f in FIELDS]


def write_sheet(gc, sh, sheet_name: str, rows: list[list]) -> None:
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except Exception:
        ws = sh.add_worksheet(title=sheet_name, rows=max(len(rows) + 10, 50), cols=len(HEADERS))
    data = [HEADERS] + rows
    ws.update(data, value_input_option="USER_ENTERED")
    print(f"  シート「{sheet_name}」: {len(rows)}行 書き込み完了")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="スプシへの書き込みをスキップ")
    args = ap.parse_args()

    if not TOKEN:
        print("エラー: THEME_API_TOKEN 未設定", file=sys.stderr)
        sys.exit(1)

    print("テーマDB から取得中…")
    themes = fetch_themes()
    print(f"  全テーマ: {len(themes)}件")

    non_sales = [t for t in themes if t.get("category") != "Sales"]
    delivery  = [t for t in non_sales if t.get("category") == "Delivery"]
    others    = [t for t in non_sales if t.get("category") != "Delivery"]

    print(f"  Sales除外後: {len(non_sales)}件  (Delivery={len(delivery)}, その他={len(others)})")

    delivery_rows = [theme_to_row(t) for t in delivery]
    others_rows   = [theme_to_row(t) for t in others]

    if args.dry_run:
        print("\n--- DRY RUN: スプシには書き込みません ---")
        print(f"Delivery シートに書く予定: {len(delivery_rows)}行")
        for r in delivery_rows:
            print(f"  id={r[0]}  {r[1]}  ({r[2]})")
        print(f"その他 シートに書く予定: {len(others_rows)}行")
        for r in others_rows:
            print(f"  id={r[0]}  {r[1]}  ({r[2]})")
        return

    if not SHEET_ID:
        print("エラー: SALES_SHEET_ID 未設定", file=sys.stderr)
        sys.exit(1)
    if not Path(SA_JSON).exists():
        print(f"エラー: サービスアカウント鍵が見つかりません: {SA_JSON}", file=sys.stderr)
        sys.exit(1)

    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SA_JSON, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    print(f"\nGoogle Sheets (id={SHEET_ID}) へ書き込み中…")
    write_sheet(gc, sh, "Delivery", delivery_rows)
    write_sheet(gc, sh, "その他",   others_rows)
    print("完了")


if __name__ == "__main__":
    main()
