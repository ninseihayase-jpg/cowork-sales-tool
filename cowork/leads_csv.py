"""リード CSV一括取込。

引き継ぎ元: CRM cowork_CRM/crm-app/src/lib/storage.ts parseContactsCsv
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.request

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


def _estimate_fields(rows: list[dict], industries: list[str], company_sizes: list[str]) -> list[dict]:
    """業界・企業規模が未入力の行をClaude APIで推定補完する。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return rows

    # 推定が必要な行のインデックスと会社名を収集
    need = [(i, r["company"]) for i, r in enumerate(rows)
            if not r.get("industry") or not r.get("company_size")]
    if not need:
        return rows

    companies = list({company for _, company in need})
    prompt = (
        f"以下の会社名のリストについて、業界と企業規模を推定してください。\n"
        f"業界の選択肢: {industries}\n"
        f"企業規模の選択肢: {company_sizes}\n"
        f"会社名リスト: {companies}\n\n"
        f"回答はJSON形式で、会社名をキーとし、値は {{\"industry\": \"...\", \"company_size\": \"...\"}} の形式にしてください。"
        f"選択肢にない場合はnullにしてください。JSONのみ返してください。"
    )

    payload = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = result["content"][0]["text"].strip()
        # JSONブロックを抽出（```json ... ``` の場合に対応）
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        estimates: dict = json.loads(text.strip())
    except Exception:
        return rows

    valid_industries = set(industries)
    valid_sizes = set(company_sizes)
    for i, company in need:
        est = estimates.get(company, {})
        if not rows[i].get("industry"):
            ind = est.get("industry")
            if ind in valid_industries:
                rows[i]["industry"] = ind
        if not rows[i].get("company_size"):
            sz = est.get("company_size")
            if sz in valid_sizes:
                rows[i]["company_size"] = sz

    return rows


def import_leads(con, csv_text: str, industries=None, company_sizes=None) -> tuple[int, int]:
    """CSVを取り込み、(成功件数, スキップ件数) を返す。"""
    themes = sfa_db.list_pitch_themes(con)
    themes_by_name = {t["name"]: t["id"] for t in themes}
    rows = parse_leads_csv(csv_text, themes_by_name)
    if industries and company_sizes and os.environ.get("ANTHROPIC_API_KEY"):
        rows = _estimate_fields(rows, industries, company_sizes)
    ok = skip = 0
    for r in rows:
        try:
            sfa_db.upsert_lead(con, **r)
            ok += 1
        except Exception:
            skip += 1
    return ok, skip
