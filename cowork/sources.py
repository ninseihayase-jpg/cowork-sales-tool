"""入力データソース。ローカルxlsx と Google Sheets を同じインターフェイスで読む。

どちらも「ヘッダ正規化済みの dict のリスト」を返す。
これにより同期ロジック（sync.py）はソースの違いを意識しない。
"""

from __future__ import annotations

from pathlib import Path

from .mapping import normalize_header


def read_xlsx(path: str | Path, sheet_name: str = "テーマKPI") -> list[dict]:
    """ローカルxlsxを読む（フェーズ1のテスト/フォールバック用）。"""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb[wb.sheetnames[0]]
    headers = [normalize_header(c.value) for c in ws[1]]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:  # ID列空はスキップ
            continue
        rows.append(dict(zip(headers, row)))
    return rows


def read_google_sheets(
    sheet_id: str,
    worksheet_names: list[str],
    service_account_json: str | Path,
) -> list[dict]:
    """Google Sheets を読む（フェーズ1本番）。複数ワークシートを連結して返す。

    Sales / Sales以外 のように分割したシートをまとめて1つのテーマ集合として扱う。
    各ワークシートは1行目がヘッダ（テーマKPIと同じ列定義）であること。
    """
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(str(service_account_json), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    rows: list[dict] = []
    for name in worksheet_names:
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            continue
        values = ws.get_all_values()
        if not values:
            continue
        headers = [normalize_header(h) for h in values[0]]
        for raw in values[1:]:
            # ID列（先頭）空はスキップ
            if not raw or not str(raw[0]).strip():
                continue
            # 行長をヘッダに合わせてパディング
            padded = list(raw) + [""] * (len(headers) - len(raw))
            rows.append(dict(zip(headers, padded)))
    return rows
