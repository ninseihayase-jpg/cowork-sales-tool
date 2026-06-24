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


def estimate_companies(companies: list[str], industries: list[str], company_sizes: list[str]) -> dict:
    """会社名リストの業界・企業規模をClaude APIで一括推定。
    Returns {company_name: {"industry": ..., "company_size": ...}}（選択肢外はNone）。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not companies:
        return {}

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
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        raw: dict = json.loads(text.strip())
    except Exception:
        return {}

    valid_industries = set(industries)
    valid_sizes = set(company_sizes)
    out = {}
    for company, est in raw.items():
        if not isinstance(est, dict):
            continue
        ind = est.get("industry")
        sz = est.get("company_size")
        out[company] = {
            "industry": ind if ind in valid_industries else None,
            "company_size": sz if sz in valid_sizes else None,
        }
    return out


def _estimate_fields(rows: list[dict], industries: list[str], company_sizes: list[str]) -> list[dict]:
    """業界・企業規模が未入力の行をClaude APIで推定補完する。"""
    need_companies = list({r["company"] for r in rows if not r.get("industry") or not r.get("company_size")})
    if not need_companies:
        return rows
    estimates = estimate_companies(need_companies, industries, company_sizes)
    for r in rows:
        est = estimates.get(r["company"], {})
        if not r.get("industry") and est.get("industry"):
            r["industry"] = est["industry"]
        if not r.get("company_size") and est.get("company_size"):
            r["company_size"] = est["company_size"]
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
            # アカウント自動追加・補完
            company = r.get("company", "")
            if company:
                existing = con.execute(
                    "SELECT id, industry, company_size FROM accounts WHERE name=?",
                    (company,)
                ).fetchone()
                if existing is None:
                    sfa_db.upsert_account(
                        con, name=company,
                        industry=r.get("industry"),
                        company_size=r.get("company_size"),
                    )
                else:
                    acc_row = dict(existing)
                    updates = {}
                    if r.get("industry") and not acc_row.get("industry"):
                        updates["industry"] = r["industry"]
                    if r.get("company_size") and not acc_row.get("company_size"):
                        updates["company_size"] = r["company_size"]
                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        con.execute(
                            f"UPDATE accounts SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                            (*updates.values(), acc_row["id"]),
                        )
                        con.commit()
            ok += 1
        except Exception:
            skip += 1
    return ok, skip
