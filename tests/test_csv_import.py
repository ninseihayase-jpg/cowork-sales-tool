"""CSV一括取込パーサ(cowork/leads_csv.py, cowork/deals_csv.py, cowork/csv_utils.py)の検証。

DBやネットワークには一切触れず、公開されているパース関数を直接呼び出して
正常系・不正値フォールバック・空欄処理を確認する。
"""
from __future__ import annotations

import io

import openpyxl

from cowork import csv_utils
from cowork import deals_csv
from cowork import leads_csv
from cowork import sfa_db


# ---- csv_utils.normalize_csv_text ----

def test_normalize_csv_text_strips_bom():
    text = "﻿名前,会社名\n田中,サンプル社\n"
    normalized = csv_utils.normalize_csv_text(text)
    assert not normalized.startswith("﻿")
    assert normalized.startswith("名前,会社名")


def test_normalize_csv_text_unifies_newlines_and_strips():
    text = "a,b\r\nc,d\r  \n  "
    normalized = csv_utils.normalize_csv_text(text)
    assert "\r" not in normalized
    assert normalized == "a,b\nc,d"


# ---- csv_utils.xlsx_to_csv_text ----

def test_xlsx_to_csv_text_roundtrip_with_blank_cell():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["名前", "会社名", "メモ"])
    ws.append(["田中", None, "備考あり"])  # 空欄セルを含む
    buf = io.BytesIO()
    wb.save(buf)

    csv_text = csv_utils.xlsx_to_csv_text(buf.getvalue())
    lines = csv_text.strip().splitlines()
    assert lines[0] == "名前,会社名,メモ"
    # 空欄セルは""になり、列がズレないこと
    assert lines[1] == "田中,,備考あり"


# ---- leads_csv.parse_leads_csv ----

def _leads_csv_text(rows: list[list[str]]) -> str:
    header = ",".join(leads_csv.TEMPLATE_HEADERS)
    body = "\n".join(",".join(row) for row in rows)
    return header + "\n" + body + "\n"


def test_parse_leads_csv_happy_path():
    themes_by_name = {"AI活用テーマ": 7}
    text = _leads_csv_text([[
        "田中 太郎", "株式会社サンプル商事", "商社・卸売", "1000億未満", "営業部長",
        "tanaka@example.com", "090-1234-5678", "exhibition", "new", "AI活用テーマ", "吉江", "展示会で名刺交換",
    ]])
    rows = leads_csv.parse_leads_csv(text, themes_by_name)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "田中 太郎"
    assert r["company"] == "株式会社サンプル商事"
    assert r["industry"] == "商社・卸売"
    assert r["company_size"] == "1000億未満"
    assert r["title"] == "営業部長"
    assert r["email"] == "tanaka@example.com"
    assert r["phone"] == "090-1234-5678"
    assert r["source"] == "exhibition"
    assert r["lead_status"] == "new"
    assert r["pitch_theme_id"] == 7
    assert r["assigned_to"] == "吉江"
    assert r["notes"] == "展示会で名刺交換"
    assert r["deal_id"] is None


def test_parse_leads_csv_skips_rows_missing_required_fields():
    # 名前が空欄の行、会社名が空欄の行はどちらもスキップされる
    text = _leads_csv_text([
        ["", "会社A", "", "", "", "", "", "", "", "", "", ""],
        ["名前だけ", "", "", "", "", "", "", "", "", "", "", ""],
    ])
    rows = leads_csv.parse_leads_csv(text, {})
    assert rows == []


def test_parse_leads_csv_invalid_source_and_status_fallback():
    text = _leads_csv_text([[
        "山田", "会社B", "", "", "", "", "", "存在しない経路", "存在しないステータス", "", "", "",
    ]])
    rows = leads_csv.parse_leads_csv(text, {})
    assert len(rows) == 1
    assert rows[0]["source"] == "other"
    assert rows[0]["lead_status"] == "new"


def test_parse_leads_csv_unknown_master_values_become_none():
    text = _leads_csv_text([[
        "佐藤", "会社C", "存在しない業界", "存在しない規模", "", "", "", "", "", "",
        "存在しない担当者", "",
    ]])
    rows = leads_csv.parse_leads_csv(
        text, {}, owners=["吉江"], industries=["商社・卸売"], company_sizes=["1000億未満"],
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["industry"] is None
    assert r["company_size"] is None
    assert r["assigned_to"] is None


def test_parse_leads_csv_blank_optional_fields_become_none():
    text = _leads_csv_text([[
        "鈴木", "会社D", "", "", "", "", "", "", "", "", "", "",
    ]])
    rows = leads_csv.parse_leads_csv(text, {})
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] is None
    assert r["email"] is None
    assert r["phone"] is None
    assert r["notes"] is None
    assert r["pitch_theme_id"] is None
    assert r["assigned_to"] is None  # 空欄は valid_owners に含まれないため None


def test_parse_leads_csv_handles_bom_and_alt_header_names():
    # BOM付き・英語ヘッダ名でも取り込めること(normalize_csv_text + row.get fallback)
    text = "﻿name,company,source,status\n田中,会社E,referral,following\n"
    rows = leads_csv.parse_leads_csv(text, {})
    assert len(rows) == 1
    assert rows[0]["name"] == "田中"
    assert rows[0]["company"] == "会社E"
    assert rows[0]["source"] == "referral"
    assert rows[0]["lead_status"] == "following"


# ---- deals_csv.parse_deals_csv ----

def _deals_csv_text(rows: list[list[str]]) -> str:
    header = ",".join(deals_csv.TEMPLATE_HEADERS)
    body = "\n".join(",".join(row) for row in rows)
    return header + "\n" + body + "\n"


def test_parse_deals_csv_happy_path_deal_row():
    text = _deals_csv_text([[
        "商談", "株式会社サンプル商事", "商社・卸売", "1000億未満", "コスト削減提案",
        "初回アポ実施", "コスト削減", "コスト診断(無償)", "Exh.", "吉江", "中島",
        "500", "10", "20", "300万円程度", "2026-08-01", "初回訪問", "高", "コスト15%削減", "展示会で名刺交換",
    ]])
    rows = deals_csv.parse_deals_csv(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == deals_csv.KIND_DEAL
    assert r["company"] == "株式会社サンプル商事"
    assert r["deal_name"] == "コスト削減提案"
    assert r["stage"] == "初回アポ実施"
    assert r["business_type_l1"] == "コスト削減"
    assert r["business_type_l2"] == "コスト診断(無償)"
    assert r["lead_pattern"] == "Exh."
    assert r["owner"] == "吉江"
    assert r["sub_owner"] == "中島"
    assert r["value_lumpsum"] == 500.0
    assert r["value_lumpsum_monthly"] == 10.0
    assert r["value_recurring"] == 20.0
    assert r["client_budget"] == "300万円程度"
    assert r["next_milestone_date"] == "2026-08-01"
    assert r["next_milestone_label"] == "初回訪問"
    assert r["importance"] == "高"
    assert r["goal"] == "コスト15%削減"
    assert r["note"] == "展示会で名刺交換"


def test_parse_deals_csv_account_row():
    text = _deals_csv_text([[
        "アカウント", "株式会社サンプル製作所", "製造業(電機・電子・精密)", "500億未満",
        "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "今期は見送り",
    ]])
    rows = deals_csv.parse_deals_csv(text)
    assert len(rows) == 1
    assert rows[0]["kind"] == deals_csv.KIND_ACCOUNT
    assert rows[0]["deal_name"] is None
    assert rows[0]["company"] == "株式会社サンプル製作所"


def test_parse_deals_csv_kind_inferred_when_blank():
    # 種別が空欄でも商談名の有無でkindが推定される
    text = _deals_csv_text([
        ["", "会社F", "", "", "商談名あり", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ["", "会社G", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ])
    rows = deals_csv.parse_deals_csv(text)
    assert len(rows) == 2
    assert rows[0]["kind"] == deals_csv.KIND_DEAL
    assert rows[1]["kind"] == deals_csv.KIND_ACCOUNT


def test_parse_deals_csv_deal_row_without_name_is_skipped():
    text = _deals_csv_text([[
        "商談", "会社H", "", "", "",  # 商談名が空欄
        "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
    ]])
    rows = deals_csv.parse_deals_csv(text)
    assert rows == []


def test_parse_deals_csv_row_without_company_is_skipped():
    text = _deals_csv_text([[
        "商談", "", "", "", "商談名だけある",
        "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
    ]])
    rows = deals_csv.parse_deals_csv(text)
    assert rows == []


def test_parse_deals_csv_invalid_master_values_fallback_to_none():
    text = _deals_csv_text([[
        "商談", "会社I", "", "", "商談I",
        "存在しないステージ", "存在しないL1", "存在しないL2", "存在しない経路",
        "存在しない担当者", "存在しないサブ担当", "", "", "", "", "", "", "存在しない重要度", "", "",
    ]])
    rows = deals_csv.parse_deals_csv(
        text, stages=["初回アポ実施"], biz_l1=["コスト削減"], owners=["吉江"], lead_patterns=["Exh."],
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["stage"] is None
    assert r["business_type_l1"] is None
    assert r["business_type_l2"] is None
    assert r["lead_pattern"] is None
    assert r["owner"] is None
    assert r["sub_owner"] is None
    assert r["importance"] is None


def test_parse_deals_csv_business_type_l2_depends_on_l1():
    # L1に対応しないL2値は無効化される(コスト削減にはAI開発(軽)は含まれない)
    text = _deals_csv_text([[
        "商談", "会社J", "", "", "商談J",
        "", "コスト削減", "AI開発(軽)", "", "", "", "", "", "", "", "", "", "", "", "",
    ]])
    rows = deals_csv.parse_deals_csv(text)
    assert len(rows) == 1
    assert rows[0]["business_type_l1"] == "コスト削減"
    assert rows[0]["business_type_l2"] is None

    # 対応するL2値なら有効
    text2 = _deals_csv_text([[
        "商談", "会社J", "", "", "商談J",
        "", "コスト削減", "コスト診断(有償)", "", "", "", "", "", "", "", "", "", "", "", "",
    ]])
    rows2 = deals_csv.parse_deals_csv(text2)
    assert rows2[0]["business_type_l2"] == "コスト診断(有償)"


def test_parse_deals_csv_numeric_fields_invalid_and_blank():
    text = _deals_csv_text([[
        "商談", "会社K", "", "", "商談K",
        "", "", "", "", "", "", "数値じゃない", "", "", "", "", "", "", "", "",
    ]])
    rows = deals_csv.parse_deals_csv(text)
    assert len(rows) == 1
    assert rows[0]["value_lumpsum"] is None  # 数値変換失敗はNone
    assert rows[0]["value_lumpsum_monthly"] is None  # 空欄もNone
    assert rows[0]["value_recurring"] is None


def test_import_leads_and_deals_do_not_touch_production_db(tmp_path):
    """importはDB操作を行うが、テスト用tmp DBに対してのみ実行し本番DBには触れない。"""
    db_path = str(tmp_path / "csv_import_test.db")
    sfa_db.init_db(db_path)
    con = sfa_db.connect(db_path)
    try:
        leads_text = _leads_csv_text([[
            "山本", "会社L", "", "", "", "", "", "", "", "", "", "",
        ]])
        ok, skip = leads_csv.import_leads(con, leads_text)
        assert (ok, skip) == (1, 0)
        assert len(sfa_db.list_leads(con)) == 1

        deals_text = _deals_csv_text([[
            "商談", "会社M", "", "", "商談M",
            "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
        ]])
        ok_deals, ok_accounts, dup_skip, skip2 = deals_csv.import_deals(con, deals_text)
        assert (ok_deals, ok_accounts, dup_skip, skip2) == (1, 1, 0, 0)
        assert len(sfa_db.list_deals(con, status=None)) == 1
    finally:
        con.close()
