"""
SFA DBの商談一覧をGoogleスプシに転記。
毎日Renderのcron jobで自動実行。

使用方法: python scripts/export_deals_to_sheets.py

環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON - サービスアカウントJSONのパス（またはJSON文字列）
  WEEKLY_SHEET_ID - 転記先スプシのID
  SFA_API_URL  - SFA WebアプリのURL（Render cron環境用。設定時はAPIで取得）
  SFA_API_TOKEN - SFA APIトークン（SFA_API_URL設定時に使用）
  SFA_DB_PATH  - DBパス（ローカル実行時フォールバック）
"""
from __future__ import annotations

import os
import sys
import urllib.request
import urllib.parse
import json
from pathlib import Path

# --- .env 読み込み（ローカル実行時） ---
ROOT = Path(__file__).resolve().parent.parent
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# --- cowork パッケージをパスに追加（ローカル実行時に使用） ---
sys.path.insert(0, str(ROOT))

# --- 設定 ---
WEEKLY_SHEET_ID = os.environ.get("WEEKLY_SHEET_ID", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
DB_PATH = os.environ.get("SFA_DB_PATH", str(ROOT / "cowork_sfa.db"))
SFA_API_URL = os.environ.get("SFA_API_URL", "").rstrip("/")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")
SHEET_NAME = "商談一覧"

# スプシのカラム定義: (ヘッダ表示名, deals dictのキー名)
COLUMNS = [
    ("ID",              "id"),
    ("アカウント",        "account_name"),
    ("案件名",           "deal_name"),
    ("ステージ",          "stage"),
    ("担当",             "owner"),
    ("事業種別L1",        "business_type_l1"),
    ("リード経路",         "lead_pattern"),
    ("重要度",            "importance"),
    ("ワンタイム総額(万円)", "value_lumpsum"),
    ("継続月額(万円)",     "value_recurring"),
    ("次回MS日",          "next_milestone_date"),
    ("次回MSラベル",       "next_milestone_label"),
    ("現状メモ",           "note"),
    ("更新日",            "updated_at"),
]
HEADERS = [h for h, _ in COLUMNS]
FIELDS  = [f for _, f in COLUMNS]


def deal_to_row(d: dict) -> list:
    row = []
    for f in FIELDS:
        v = d.get(f)
        if v is None:
            row.append("")
        else:
            row.append(v)
    return row


def write_sheet(gc, sheet_id: str, sheet_name: str, rows: list[list]) -> None:
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(sheet_name)
        ws.clear()
    except Exception:
        ws = sh.add_worksheet(
            title=sheet_name,
            rows=max(len(rows) + 20, 100),
            cols=len(HEADERS),
        )
    data = [HEADERS] + rows
    ws.update(data, value_input_option="USER_ENTERED")
    print(f"  シート「{sheet_name}」: {len(rows)}行 書き込み完了")


def fetch_deals_via_api() -> list[dict]:
    """SFA WebアプリのAPIから商談一覧を取得する（Renderのcron環境用）。"""
    url = f"{SFA_API_URL}/api/deals?status=open&token={urllib.parse.quote(SFA_API_TOKEN)}"
    print(f"SFA API ({SFA_API_URL}/api/deals) から商談を取得中...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"API エラー: {data['error']}")
    return data


def fetch_deals_via_db() -> list[dict]:
    """ローカルSQLite DBから商談一覧を取得する（ローカル実行時フォールバック）。"""
    from cowork import sfa_db
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"エラー: SFA DB が見つかりません: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    print(f"SFA DB ({DB_PATH}) から商談を取得中...")
    con = sfa_db.connect(DB_PATH)
    try:
        return [dict(d) for d in sfa_db.list_deals(con, status="open")]
    finally:
        con.close()


def main() -> None:
    # --- バリデーション ---
    if not WEEKLY_SHEET_ID:
        print("エラー: WEEKLY_SHEET_ID 未設定", file=sys.stderr)
        sys.exit(1)

    sa_path = Path(SA_JSON) if not SA_JSON.strip().startswith("{") else None
    if sa_path and not sa_path.exists():
        print(f"エラー: サービスアカウント鍵が見つかりません: {SA_JSON}", file=sys.stderr)
        sys.exit(1)

    # --- 商談一覧を取得（API優先、なければDB直接） ---
    if SFA_API_URL and SFA_API_TOKEN:
        deals = fetch_deals_via_api()
    else:
        deals = fetch_deals_via_db()

    print(f"  open 商談: {len(deals)}件")
    rows = [deal_to_row(d) for d in deals]

    # --- gspread でスプシに書き込み ---
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    if sa_path:
        creds = Credentials.from_service_account_file(str(sa_path), scopes=scopes)
    else:
        # JSON文字列として渡された場合
        import json, tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(SA_JSON)
        tmp.close()
        creds = Credentials.from_service_account_file(tmp.name, scopes=scopes)

    gc = gspread.authorize(creds)

    print(f"Google Sheets (id={WEEKLY_SHEET_ID}) へ書き込み中...")
    write_sheet(gc, WEEKLY_SHEET_ID, SHEET_NAME, rows)
    print("完了")


if __name__ == "__main__":
    main()
