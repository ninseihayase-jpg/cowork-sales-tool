"""営業情報DB（独立）。アカウント / コンタクト / 商談 / 活動。

フェーズ2-1の正本DB。SQLite。テーマDBとは別物だが、商談(deal)は theme_id で
テーマDBのSalesテーマと対応づけ、同期できる（cowork/theme_link.py）。

設計の正本: docs/00_設計構想.md §6。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "cowork_sfa.db")

# テーマDBの選択肢に準拠（表記揺れ防止。docs/00 §3 / 秘書 db_schema_design.md）
DEAL_STAGES = ["リード", "アポ獲得", "初回アポ実施", "要件詰め", "提案", "クロージング", "受注", "失注", "保留中"]
BUSINESS_TYPE_L1 = ["コスト削減", "コンサルティング", "AI導入", "他"]
LEAD_PATTERNS = ["Connection", "Exh.", "Partner", "Advisor", "PE", "Under", "SNS", "HP", "na"]
COMPANY_SIZES = ["500億未満", "1000億未満", "5000億未満", "5000億以上"]
ACTIVITY_TYPES = ["面談", "電話", "メール", "メモ"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    industry TEXT,
    company_size TEXT,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    title TEXT,
    email TEXT,
    phone TEXT,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    theme_id INTEGER,                 -- テーマDB todos.id（同期キー。NULL=未連携）
    deal_name TEXT NOT NULL,
    stage TEXT,
    business_type_l1 TEXT,
    business_type_l2 TEXT,
    lead_pattern TEXT,
    owner TEXT,
    value_lumpsum REAL,               -- 単発総額（万円）
    value_lumpsum_monthly REAL,       -- 単発月額（万円）
    value_recurring REAL,             -- 継続月額（万円）
    client_budget TEXT,
    next_milestone_date TEXT,
    next_milestone_label TEXT,
    note TEXT,
    goal TEXT,
    status TEXT DEFAULT 'open',       -- open / closed
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    type TEXT,                        -- 面談 / 電話 / メール / メモ
    occurred_on TEXT,                 -- YYYY-MM-DD
    body TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_deals_account ON deals(account_id);
CREATE INDEX IF NOT EXISTS idx_activities_deal ON activities(deal_id);
"""


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    con = connect(db_path)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


# ---- 取得系 ----
def list_accounts(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM accounts ORDER BY name")]


def list_deals(con, status: str | None = "open") -> list[dict]:
    q = """SELECT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d LEFT JOIN accounts a ON a.id = d.account_id"""
    params: list = []
    if status:
        q += " WHERE d.status = ?"
        params.append(status)
    q += " ORDER BY d.updated_at DESC"
    return [dict(r) for r in con.execute(q, params)]


def get_deal(con, deal_id: int) -> dict | None:
    r = con.execute(
        """SELECT d.*, a.name AS account_name FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id WHERE d.id = ?""",
        (deal_id,),
    ).fetchone()
    return dict(r) if r else None


def list_activities(con, deal_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM activities WHERE deal_id = ? ORDER BY occurred_on DESC, id DESC", (deal_id,)
    )]


# ---- 更新系 ----
def upsert_account(con, *, id=None, name, industry=None, company_size=None, note=None) -> int:
    if id:
        con.execute(
            "UPDATE accounts SET name=?, industry=?, company_size=?, note=?, updated_at=datetime('now') WHERE id=?",
            (name, industry, company_size, note, id),
        )
        con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO accounts (name, industry, company_size, note) VALUES (?,?,?,?)",
        (name, industry, company_size, note),
    )
    con.commit()
    return cur.lastrowid


DEAL_FIELDS = [
    "account_id", "theme_id", "deal_name", "stage", "business_type_l1", "business_type_l2",
    "lead_pattern", "owner", "value_lumpsum", "value_lumpsum_monthly", "value_recurring",
    "client_budget", "next_milestone_date", "next_milestone_label", "note", "goal", "status",
]


def upsert_deal(con, *, id=None, **fields) -> int:
    data = {k: fields.get(k) for k in DEAL_FIELDS}
    if id:
        sets = ", ".join(f"{k}=?" for k in DEAL_FIELDS) + ", updated_at=datetime('now')"
        con.execute(f"UPDATE deals SET {sets} WHERE id=?", [data[k] for k in DEAL_FIELDS] + [id])
        con.commit()
        return int(id)
    cols = ", ".join(DEAL_FIELDS)
    ph = ", ".join("?" for _ in DEAL_FIELDS)
    cur = con.execute(f"INSERT INTO deals ({cols}) VALUES ({ph})", [data[k] for k in DEAL_FIELDS])
    con.commit()
    return cur.lastrowid


def add_activity(con, *, deal_id, type=None, occurred_on=None, body=None) -> int:
    cur = con.execute(
        "INSERT INTO activities (deal_id, type, occurred_on, body) VALUES (?,?,?,?)",
        (deal_id, type, occurred_on, body),
    )
    con.commit()
    return cur.lastrowid
