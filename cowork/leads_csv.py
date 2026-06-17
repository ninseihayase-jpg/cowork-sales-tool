"""リード CSV一括取込。

引き継ぎ元: CRM cowork_CRM/crm-app/src/lib/storage.ts parseContactsCsv
"""

from __future__ import annotations

import csv
import io

from . import sfa_db

_VALID_SOURCES = set(sfa_db.LEAD_SOURCES)
_VALID_STATUSES = set(sfa_db.LEAD_STATUSES)


def parse_leads_csv(csv_text: str, themes_by_name: dict) -> list[dict]:
    """CSVテキストをリード辞書リストに変換する。

    themes_by_name: {テーマ名 -> pitch_theme_id} のマッピング
    """
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    results = []
    for row in reader:
        name = (row.get("名前") or row.get("name") or "").strip()
        company = (row.get("会社名") or row.get("company") or "").strip()
        if not name or not company:
            continue

        source = (row.get("獲得経路") or row.get("source") or "other").strip()
        if source not in _VALID_SOURCES:
            source = "other"

        status = (row.get("ステータス") or row.get("status") or "new").strip()
        if status not in _VALID_STATUSES:
            status = "new"

        theme_name = (row.get("ピッチテーマ") or row.get("pitch_theme") or "").strip()
        theme_id = themes_by_name.get(theme_name) if theme_name else None

        results.append({
            "name": name,
            "company": company,
            "title": (row.get("役職") or row.get("title") or "").strip() or None,
            "email": (row.get("メール") or row.get("email") or "").strip() or None,
            "phone": (row.get("電話") or row.get("phone") or "").strip() or None,
            "source": source,
            "pitch_theme_id": theme_id,
            "lead_status": status,
            "notes": (row.get("メモ") or row.get("notes") or "").strip() or None,
            "assigned_to": (row.get("担当者") or row.get("assigned_to") or "").strip() or None,
            "deal_id": None,
        })
    return results


def import_leads(con, csv_text: str) -> tuple[int, int]:
    """CSVを取り込み、(成功件数, スキップ件数) を返す。"""
    themes = sfa_db.list_pitch_themes(con)
    themes_by_name = {t["name"]: t["id"] for t in themes}
    rows = parse_leads_csv(csv_text, themes_by_name)
    ok = skip = 0
    for r in rows:
        try:
            sfa_db.upsert_lead(con, **r)
            ok += 1
        except Exception:
            skip += 1
    return ok, skip
