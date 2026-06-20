"""営業情報DBの商談(deal) → テーマDB(todos/theme) への連携。

商談を保存したら、対応するSalesテーマをテーマDBへ UPSERT する（docs/00 §7）。
- deal.theme_id があれば そのテーマを UPDATE
- なければ 新規テーマを INSERT し、採番されたidを deal.theme_id に書き戻す（冪等）
"""

from __future__ import annotations

import json

from . import sfa_db
from .mapping import THEME_COLUMNS, build_insert_params, build_insert_sql, build_update_params, build_update_sql
from .sync import DEFAULT_USER_ID
from .theme_db import ThemeDBClient


def _meeting_dates_from_activities(con, deal_id: int) -> str | None:
    acts = sfa_db.list_activities(con, deal_id)
    dates = sorted({a["occurred_on"] for a in acts if a.get("type") == "面談" and a.get("occurred_on")})
    return json.dumps(dates) if dates else None


def deal_to_theme_fields(con, deal: dict) -> dict:
    """商談dict → テーマDBカラム値dict（THEME_COLUMNS準拠）。"""
    account_name = deal.get("account_name")
    title = f"{account_name}/{deal['deal_name']}" if account_name else deal["deal_name"]
    fields = {c: None for c in THEME_COLUMNS}
    fields.update({
        "title": title,
        "category": "Sales",
        "importance": deal.get("importance"),
        "status": deal.get("status") or "open",
        "note": deal.get("note"),
        "goal": deal.get("goal"),
        "business_type": deal.get("business_type_l2"),
        "business_type_l1": deal.get("business_type_l1"),
        "business_type_l2": deal.get("business_type_l2"),
        "deal_stage": deal.get("stage"),
        "lead_pattern": deal.get("lead_pattern"),
        "deal_owner": deal.get("owner"),
        "deal_value_lumpsum": deal.get("value_lumpsum"),
        "deal_value_lumpsum_monthly": deal.get("value_lumpsum_monthly"),
        "deal_value_recurring": deal.get("value_recurring"),
        "approach_value": deal.get("approach_value"),
        "client_name": account_name,
        "deal_name": deal.get("deal_name"),
        "meeting_dates": _meeting_dates_from_activities(con, deal["id"]),
        "client_budget": deal.get("client_budget"),
        "industry": deal.get("industry"),
        "company_size": deal.get("company_size"),
        "milestone_date": deal.get("next_milestone_date"),
        "milestone_label": deal.get("next_milestone_label"),
        "approach_rate": deal.get("approach_rate"),
        "reduction_rate": deal.get("reduction_rate"),
        "fee_rate": deal.get("fee_rate"),
        "diagnosis_cost": deal.get("diagnosis_cost"),
        "cost_stage": deal.get("cost_stage"),
    })
    return fields


def sync_deal(client: ThemeDBClient, con, deal_id: int, user_id: str = DEFAULT_USER_ID) -> dict:
    """1商談をテーマDBへ同期。結果dict（action, theme_id）を返す。"""
    deal = sfa_db.get_deal(con, deal_id)
    if not deal:
        raise ValueError(f"deal {deal_id} not found")
    fields = deal_to_theme_fields(con, deal)

    theme_id = deal.get("theme_id")
    if theme_id:
        fields["id"] = theme_id
        client.execute(build_update_sql(), build_update_params(fields))
        return {"action": "update", "theme_id": theme_id}

    # 新規テーマ: 採番（既存max+1）
    res = client.execute("SELECT COALESCE(MAX(id),0)+1 AS next_id FROM todos", [])
    next_id = res["rows"][0]["next_id"]
    fields["id"] = next_id
    client.execute(build_insert_sql(), build_insert_params(fields, user_id))
    con.execute("UPDATE deals SET theme_id=? WHERE id=?", (next_id, deal_id))
    con.commit()
    return {"action": "insert", "theme_id": next_id}
