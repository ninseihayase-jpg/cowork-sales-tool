"""スプレッドシート1行 → テーマDB(todos/theme) のカラム値へのマッピング。

秘書プロジェクトの scripts/import_sales_xlsx.py の実証済みロジックを移植・共通化したもの。
スキーマ仕様の正本は 秘書/docs/db_schema_design.md。スキーマを変えたら本書と設計構想も更新する。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

# UPDATE / INSERT が対象にするテーマDBのカラム（順序が params と対応する）。
THEME_COLUMNS = [
    "title", "category", "importance", "status", "note", "goal",
    "business_type", "business_type_l1", "business_type_l2",
    "deal_stage", "lead_pattern", "deal_owner",
    "deal_value_lumpsum", "deal_value_lumpsum_monthly", "deal_value_recurring",
    "approach_value", "client_name", "deal_name", "meeting_dates",
    "client_budget", "industry", "company_size", "start_date", "end_date",
    "milestone_date", "milestone_label",
    "approach_rate", "reduction_rate", "fee_rate", "diagnosis_cost", "cost_stage",
    "in_delivery", "utilization",
]


def excel_to_iso(serial) -> str | None:
    """Excel日付シリアル値 / date / 文字列 → 'YYYY-MM-DD'。不正値はNone。"""
    if serial is None:
        return None
    if isinstance(serial, datetime):
        return serial.date().isoformat()
    if isinstance(serial, date):
        return serial.isoformat()
    if isinstance(serial, str):
        s = serial.strip()
        if not s:
            return None
        try:
            date.fromisoformat(s[:10])
            return s[:10]
        except ValueError:
            return None
    try:
        n = int(serial)
    except (TypeError, ValueError):
        return None
    return (date(1899, 12, 30) + timedelta(days=n)).isoformat()


def _f(v):
    """数値化（空・不正はNone）。"""
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _s(v):
    """文字列正規化（空はNone）。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def normalize_header(h) -> str:
    """ヘッダの分類プレフィックス（'Sales/Delivery_\\n…' 等）を除去して列名だけ返す。"""
    if h is None:
        return ""
    return str(h).split("\n")[-1].strip()


def build_meeting_dates(row: dict) -> str | None:
    """面談1〜9 列 → ISO日付の JSON list。"""
    dates = []
    for i in range(1, 10):
        iso = excel_to_iso(row.get(f"面談{i}"))
        if iso:
            dates.append(iso)
    return json.dumps(dates) if dates else None


def row_to_theme(row: dict) -> dict:
    """スプレッドシート1行（ヘッダ正規化済みdict）→ テーマDBカラム値dict。

    返り値は THEME_COLUMNS のキー＋ 'id' を含む。'id' が取れない行は ValueError。
    """
    if row.get("ID") in (None, ""):
        raise ValueError("ID列が空")
    theme_id = int(row["ID"])

    # Status: On→open / Off→closed（Off=Close でダッシュボード非表示）
    status = "closed" if (str(row.get("Status") or "").strip() == "Off") else "open"

    business_type_l2 = _s(row.get("事業種別L2"))
    fields = {
        "id": theme_id,
        "title": _s(row.get("テーマ名")) or "",
        "category": _s(row.get("分類")),
        "importance": _s(row.get("重要度")),
        "status": status,
        "note": _s(row.get("現状メモ")),
        "goal": _s(row.get("ゴール")),
        "business_type": business_type_l2,  # 後方互換: 旧単一カラムにL2を入れる
        "business_type_l1": _s(row.get("事業種別L1")),
        "business_type_l2": business_type_l2,
        "deal_stage": _s(row.get("案件Stage")),
        "lead_pattern": _s(row.get("リード経路")),
        "deal_owner": _s(row.get("担当")),
        "deal_value_lumpsum": _f(row.get("単発総額(万円)")),
        "deal_value_lumpsum_monthly": _f(row.get("単発月額(万円)")),
        "deal_value_recurring": _f(row.get("継続月額(万円)")),
        "approach_value": _f(row.get("アプローチ額(億円)")),  # 単位は億円
        "client_name": _s(row.get("クライアント")),
        "deal_name": _s(row.get("案件名")),
        "meeting_dates": build_meeting_dates(row),
        "client_budget": _s(row.get("クライアント予算(万円)")),
        "industry": _s(row.get("業界")),
        "company_size": _s(row.get("企業規模")),
        "start_date": excel_to_iso(row.get("開始日")),
        "end_date": excel_to_iso(row.get("終了日")),
        "milestone_date": excel_to_iso(row.get("マイルストン日")),
        "milestone_label": _s(row.get("マイルストンラベル")),
        "approach_rate": _f(row.get("アプローチ率(%)")),
        "reduction_rate": _f(row.get("コスト削減率(%)")),
        "fee_rate": _f(row.get("成果報酬率(%)")),
        "diagnosis_cost": _f(row.get("診断原価(万円)")),
        "cost_stage": _s(row.get("コスト削減ステージ")),
        "in_delivery": _s(row.get("稼働対象")),
        "utilization": _f(row.get("稼働率(%)")),
    }
    return fields


def build_update_sql() -> str:
    sets = ", ".join(f"{c}=?" for c in THEME_COLUMNS)
    return f"UPDATE todos SET {sets} WHERE id=?"


def build_update_params(fields: dict) -> list:
    return [fields[c] for c in THEME_COLUMNS] + [fields["id"]]


def build_insert_sql(user_id_placeholder: bool = True) -> str:
    cols = ["id", "user_id", "node_type"] + THEME_COLUMNS
    placeholders = ", ".join("?" for _ in cols) + ", datetime('now'), datetime('now')"
    collist = ", ".join(cols) + ", created_at, last_updated"
    return f"INSERT INTO todos ({collist}) VALUES ({placeholders})"


def build_insert_params(fields: dict, user_id: str) -> list:
    return [fields["id"], user_id, "theme"] + [fields[c] for c in THEME_COLUMNS]
