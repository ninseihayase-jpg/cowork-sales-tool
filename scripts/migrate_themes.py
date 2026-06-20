"""テーマDB（Salesカテゴリ）→ 営業情報DB への初期移行スクリプト。

既存 todos の Sales テーマを accounts + deals として一括インポートし、
deals.theme_id に既存 todos.id を紐づける。冪等（何度実行しても重複しない）。

使い方:
    python3 scripts/migrate_themes.py --dry-run   # 内容確認のみ
    python3 scripts/migrate_themes.py             # 本番実行
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT))
from cowork import sfa_db

THEME_API_URL = os.environ.get("THEME_API_URL", "https://hisho-ohxe.onrender.com")
THEME_API_TOKEN = os.environ.get("THEME_API_TOKEN", "")
DB_PATH = os.environ.get("COWORK_SFA_DB", sfa_db.DEFAULT_DB_PATH)

MIGRATE_CATEGORIES = {"Sales"}


def fetch_themes() -> list[dict]:
    url = f"{THEME_API_URL}/api/themes?token={THEME_API_TOKEN}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def find_or_create_account(con, name: str, industry: str | None, company_size: str | None, dry_run: bool) -> int | None:
    if not name:
        return None
    row = con.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    if dry_run:
        return None
    cur = con.execute(
        "INSERT INTO accounts (name, industry, company_size) VALUES (?,?,?)",
        (name, industry, company_size),
    )
    con.commit()
    return cur.lastrowid


def migrate(dry_run: bool) -> None:
    print(f"{'=== DRY RUN（DBには書き込みません）===' if dry_run else '=== 本番実行 ==='}")
    themes = fetch_themes()
    sales = [t for t in themes if t.get("category") in MIGRATE_CATEGORIES]
    print(f"テーマDB取得: 全{len(themes)}件 → Sales {len(sales)}件を対象")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    inserted = skipped = 0
    for t in sales:
        theme_id = t["id"]
        existing = con.execute("SELECT id FROM deals WHERE theme_id=?", (theme_id,)).fetchone()
        if existing:
            print(f"  [SKIP  theme_id={theme_id:3}] {t['title']} （既に deals.id={existing['id']} に紐づき済み）")
            skipped += 1
            continue

        client_name = (t.get("client_name") or "").strip()
        deal_name   = (t.get("deal_name")   or t.get("title") or "").strip()
        if not deal_name:
            print(f"  [SKIP  theme_id={theme_id:3}] deal_name が空のためスキップ")
            skipped += 1
            continue

        industry     = t.get("industry")
        company_size = t.get("company_size")
        owner        = t.get("deal_owner")
        stage        = t.get("deal_stage")
        l1           = t.get("business_type_l1")
        l2           = t.get("business_type_l2")
        value_ls     = t.get("deal_value_lumpsum")
        value_lsm    = t.get("deal_value_lumpsum_monthly")
        value_rec    = t.get("deal_value_recurring")
        budget       = t.get("client_budget")
        ms_date      = t.get("milestone_date")
        ms_label     = t.get("milestone_label")
        note         = t.get("note")
        goal         = t.get("goal")
        status       = t.get("status") or "open"
        lead_pattern = t.get("lead_pattern")

        print(f"  [INSERT theme_id={theme_id:3}] {client_name or '（企業名なし）'}/{deal_name}"
              f"  stage={stage}  owner={owner}")

        if not dry_run:
            account_id = find_or_create_account(con, client_name, industry, company_size, dry_run=False)
            sfa_db.upsert_deal(
                con,
                id=None,
                account_id=account_id,
                theme_id=theme_id,
                deal_name=deal_name,
                stage=stage,
                business_type_l1=l1,
                business_type_l2=l2,
                lead_pattern=lead_pattern,
                owner=owner,
                value_lumpsum=value_ls,
                value_lumpsum_monthly=value_lsm,
                value_recurring=value_rec,
                client_budget=budget,
                next_milestone_date=ms_date,
                next_milestone_label=ms_label,
                note=note,
                goal=goal,
                status=status,
            )

        inserted += 1

    print(f"\n完了: INSERT={inserted}, SKIP={skipped}")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    migrate(dry_run=args.dry_run)
