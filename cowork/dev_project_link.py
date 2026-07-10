"""営業情報DBの開発案件(dev_project) → Hisho DB(dev_projects) への連携。

開発案件を保存したら、対応するレコードをHisho側テーブルへ UPSERT する（theme_link.pyのdeal版）。
- dev_project.hisho_id があれば そのレコードを UPDATE
- なければ 新規レコードを INSERT し、採番されたidを hisho_id に書き戻す（冪等）

Hisho側の dev_projects テーブルは新規追加（src/hisho/db.py の SCHEMA）。
"""

from __future__ import annotations

from . import sfa_db
from .theme_db import ThemeDBClient

DEV_PROJECT_COLUMNS = [
    "sfa_id", "deal_id", "client_name", "deal_name", "theme", "theme_detail",
    "status", "stage", "order_potential", "resolution", "budget_confirmed",
    "difficulty", "has_backend", "dev_owner", "tech_support", "sales_owner",
    "sales_sub_owner", "dev_milestone", "dev_milestone_date", "deadline",
    "dev_start_date", "dev_end_date", "dev_policy",
    "tool_url",
]


def _fields(p: dict) -> dict:
    return {
        "sfa_id": p["id"],
        "deal_id": p.get("deal_id"),
        "client_name": p.get("account_name"),
        "deal_name": p.get("deal_name"),
        "theme": p.get("theme"),
        "theme_detail": p.get("theme_detail"),
        "status": p.get("status"),
        "stage": p.get("stage"),
        "order_potential": p.get("order_potential"),
        "resolution": p.get("resolution"),
        "budget_confirmed": p.get("budget_confirmed"),
        "difficulty": p.get("difficulty"),
        "has_backend": p.get("has_backend"),
        "dev_owner": p.get("dev_owner"),
        "tech_support": p.get("tech_support"),
        "sales_owner": p.get("sales_owner"),
        "sales_sub_owner": p.get("sales_sub_owner"),
        "dev_milestone": p.get("dev_milestone"),
        "dev_milestone_date": p.get("dev_milestone_date"),
        "deadline": p.get("deadline"),
        "dev_start_date": p.get("dev_start_date"),
        "dev_end_date": p.get("dev_end_date"),
        "dev_policy": p.get("dev_policy"),
        "tool_url": p.get("tool_url"),
    }


def sync_dev_project(client: ThemeDBClient, con, dev_project_id: int) -> dict:
    """1開発案件をHisho DBへ同期。結果dict（action, hisho_id）を返す。"""
    p = sfa_db.get_dev_project(con, dev_project_id)
    if not p:
        raise ValueError(f"dev_project {dev_project_id} not found")
    fields = _fields(p)

    hisho_id = p.get("hisho_id")
    if not hisho_id:
        # ローカルにhisho_idが無くても、過去のINSERTがACK喪失で書き戻せていない可能性がある。
        # sfa_idでHisho側を検索し、既存行があればそのidを回復してUPDATEに回す。
        # （これをしないと再INSERTがsfa_id UNIQUE制約で永久に失敗し「詰み」になる）
        try:
            found = client.execute("SELECT id FROM dev_projects WHERE sfa_id=?", [p["id"]])
            rows = found.get("rows") or []
            if rows:
                hisho_id = rows[0]["id"]
                con.execute("UPDATE dev_projects SET hisho_id=? WHERE id=?", (hisho_id, dev_project_id))
                con.commit()
        except Exception as exc:  # noqa: BLE001 — 回復検索の失敗は握りつぶし通常INSERTへ
            print(f"[dev_project_link] sfa_id recovery lookup failed: {exc}", flush=True)

    if hisho_id:
        sets = ", ".join(f"{k}=?" for k in DEV_PROJECT_COLUMNS) + ", updated_at=datetime('now')"
        client.execute(f"UPDATE dev_projects SET {sets} WHERE id=?",
                        [fields[k] for k in DEV_PROJECT_COLUMNS] + [hisho_id])
        return {"action": "update", "hisho_id": hisho_id}

    cols = ", ".join(DEV_PROJECT_COLUMNS) + ", created_at, updated_at"
    ph = ", ".join("?" for _ in DEV_PROJECT_COLUMNS) + ", datetime('now'), datetime('now')"
    result = client.execute(f"INSERT INTO dev_projects ({cols}) VALUES ({ph})",
                             [fields[k] for k in DEV_PROJECT_COLUMNS])
    new_id = result.get("lastrowid")
    con.execute("UPDATE dev_projects SET hisho_id=? WHERE id=?", (new_id, dev_project_id))
    con.commit()
    return {"action": "insert", "hisho_id": new_id}


def delete_dev_project_remote(client: ThemeDBClient, hisho_id: int | None) -> None:
    """Hisho側の対応レコードを削除する。未同期（hisho_id未設定）なら何もしない。"""
    if not hisho_id:
        return
    client.execute("DELETE FROM dev_projects WHERE id=?", [hisho_id])
