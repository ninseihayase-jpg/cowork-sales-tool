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
DEAL_STAGES = ["初回アポ実施", "要件詰め", "提案", "クロージング", "受注", "失注", "保留中"]
BUSINESS_TYPE_L1 = ["コスト削減", "コンサルティング", "AI導入", "他"]
BUSINESS_TYPE_L2_BY_L1 = {
    "コスト削減":     ["コスト診断(無償)", "コスト診断(有償)", "コスト削減(成果報酬)"],
    "コンサルティング": ["コンサル(調達/SCM)", "コンサル(IT)", "コンサル(他)", "アンダー"],
    "AI導入":        ["AI開発(軽)", "AI開発(重)", "汎用AIエージェント(調達)", "汎用AIエージェント(SCM)", "汎用AIエージェント(IT)", "AXパートナー"],
    "他":            ["調達BPO(スポット)", "未定"],
}
LEAD_PATTERNS = ["Connection", "Exh.", "Partner", "Advisor", "PE", "Under", "SNS", "HP", "na"]
COMPANY_SIZES = ["500億未満", "1000億未満", "5000億未満", "5000億以上"]
ACTIVITY_TYPES = ["面談", "電話", "メール", "メモ"]
IMPORTANCE_OPTIONS = ["高", "中", "低"]
OWNERS = ["吉江", "中島", "早瀬", "岩崎", "高橋", "土屋", "戸田", "片山", "杉山", "山端", "堀籠"]
INDUSTRIES = [
    "製造業(自動車・モビリティ)", "製造業(電機・電子・精密)", "製造業(重工・鉄鋼)",
    "製造業(化学・素材)", "製造業(食品・消費財)", "製造業(医療機器)", "製造業(その他)",
    "ヘルスケア・医療・製薬", "エネルギー・インフラ", "金融・証券・保険",
    "不動産・建設", "物流・運輸・倉庫", "商社・卸売", "流通・小売・EC",
    "外食・飲食サービス", "ラグジュアリー・ファッション", "エンタメ・ゲーム・スポーツ",
    "ITサービス・テクノロジー", "通信・メディア・広告", "教育・人材・HR",
    "官公庁・公共・非営利", "コンサル・専門サービス", "ファンド", "その他",
]

# マスタ編集対象キー → デフォルト値のマッピング
MASTER_KEYS = {
    "owners":            OWNERS,
    "deal_stages":       DEAL_STAGES,
    "business_type_l1":  BUSINESS_TYPE_L1,
    "lead_patterns":     LEAD_PATTERNS,
    "industries":        INDUSTRIES,
    "company_sizes":     COMPANY_SIZES,
    "activity_types":    ACTIVITY_TYPES,
}
MASTER_LABELS = {
    "owners":            "担当者",
    "deal_stages":       "商談ステージ",
    "business_type_l1":  "事業種別L1",
    "lead_patterns":     "リード経路（商談）",
    "industries":        "業界",
    "company_sizes":     "企業規模",
    "activity_types":    "活動種別",
}
COST_STAGES = ["診断中", "削減機会発見", "削減提案中", "削減実行中", "成果確定", "不発"]

# CRM吸収: リード/ピッチテーマ用定数
PITCH_THEME_COLORS = ['#6366f1', '#8b5cf6', '#ec4899', '#f97316', '#eab308', '#22c55e', '#14b8a6', '#3b82f6']
LEAD_STATUSES = ["new", "following", "appointed", "converted", "lost"]
LEAD_STATUS_LABELS = {"new": "新規", "following": "フォロー中", "appointed": "アポ獲得",
                      "converted": "商談化済", "lost": "見込みなし"}
LEAD_SOURCES = ["exhibition", "referral", "inbound", "other"]
LEAD_SOURCE_LABELS = {"exhibition": "展示会", "referral": "紹介・知人",
                      "inbound": "インバウンド", "other": "その他"}
LEAD_ACTIVITY_TYPES = ["note", "email", "call", "meeting"]
LEAD_ACTIVITY_LABELS = {"note": "メモ", "email": "メール", "call": "電話", "meeting": "面談"}

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
    importance TEXT,                  -- 重要度: 高/中/低
    status TEXT DEFAULT 'open',       -- open / closed
    cost_stage TEXT,                  -- コスト削減ステージ（L1=コスト削減のみ）
    approach_value REAL,              -- アプローチ額（億円）
    approach_rate REAL,               -- アプローチ率(%)
    reduction_rate REAL,              -- コスト削減率(%)
    fee_rate REAL,                    -- 成果報酬率(%)
    diagnosis_cost REAL,              -- 診断原価（万円）
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    type TEXT,                        -- 面談 / 電話 / メール / メモ
    occurred_on TEXT,                 -- YYYY-MM-DD
    contact_name TEXT,                -- 相手（誰と話したか）
    body TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_deals_account ON deals(account_id);
CREATE INDEX IF NOT EXISTS idx_activities_deal ON activities(deal_id);

-- CRM吸収: ピッチテーマ
CREATE TABLE IF NOT EXISTS pitch_themes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    color       TEXT DEFAULT '#6366f1',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- CRM吸収: リード（アカウント紐付け前の人）
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT,
    email           TEXT,
    phone           TEXT,
    source          TEXT DEFAULT 'other',
    pitch_theme_id  INTEGER REFERENCES pitch_themes(id) ON DELETE SET NULL,
    lead_status     TEXT DEFAULT 'new',
    notes           TEXT,
    assigned_to     TEXT,
    deal_id         INTEGER REFERENCES deals(id) ON DELETE SET NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(lead_status);
CREATE INDEX IF NOT EXISTS idx_leads_theme  ON leads(pitch_theme_id);

-- CRM吸収: リード活動ログ
CREATE TABLE IF NOT EXISTS lead_activities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    type       TEXT DEFAULT 'note',
    content    TEXT NOT NULL,
    author     TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lead_activities_lead ON lead_activities(lead_id);

-- 入力マスタ（編集可能な選択肢）
CREATE TABLE IF NOT EXISTS masters (
    key   TEXT PRIMARY KEY,
    values_json TEXT NOT NULL
);
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
        # カラム追加マイグレーション（既存DBへの後付け対応）
        cols = {r[1] for r in con.execute("PRAGMA table_info(activities)")}
        if "contact_name" not in cols:
            con.execute("ALTER TABLE activities ADD COLUMN contact_name TEXT")
        lead_cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
        for col, typedef in [
            ("industry", "TEXT"),
            ("company_size", "TEXT"),
        ]:
            if col not in lead_cols:
                con.execute(f"ALTER TABLE leads ADD COLUMN {col} {typedef}")
        deal_cols = {r[1] for r in con.execute("PRAGMA table_info(deals)")}
        for col, typedef in [
            ("importance", "TEXT"),
            ("cost_stage", "TEXT"),
            ("approach_value", "REAL"),
            ("approach_rate", "REAL"),
            ("reduction_rate", "REAL"),
            ("fee_rate", "REAL"),
            ("diagnosis_cost", "REAL"),
        ]:
            if col not in deal_cols:
                con.execute(f"ALTER TABLE deals ADD COLUMN {col} {typedef}")
        con.commit()
    finally:
        con.close()


# ---- マスタ ----
import json as _json


def get_master_list(con, key: str) -> list[str]:
    """DB保存値があればそれを返す。なければデフォルト定数を返す。"""
    row = con.execute("SELECT values_json FROM masters WHERE key=?", (key,)).fetchone()
    if row:
        return _json.loads(row[0])
    return list(MASTER_KEYS.get(key, []))


def set_master_list(con, key: str, values: list[str]) -> None:
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (key, _json.dumps(values, ensure_ascii=False)),
    )
    con.commit()


# ---- 取得系 ----
def list_accounts(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM accounts ORDER BY name")]


def list_deals(con, status: str | None = "open", owner: str | None = None) -> list[dict]:
    q = """SELECT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d LEFT JOIN accounts a ON a.id = d.account_id WHERE 1=1"""
    params: list = []
    if status:
        q += " AND d.status = ?"
        params.append(status)
    if owner:
        q += " AND d.owner = ?"
        params.append(owner)
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
    "client_budget", "next_milestone_date", "next_milestone_label", "note", "goal",
    "importance", "status",
    "cost_stage", "approach_value", "approach_rate", "reduction_rate", "fee_rate", "diagnosis_cost",
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


def add_activity(con, *, deal_id, type=None, occurred_on=None, contact_name=None, body=None) -> int:
    cur = con.execute(
        "INSERT INTO activities (deal_id, type, occurred_on, contact_name, body) VALUES (?,?,?,?,?)",
        (deal_id, type, occurred_on, contact_name, body),
    )
    con.commit()
    return cur.lastrowid


# ---- ピッチテーマ ----

def list_pitch_themes(con, active_only: bool = False) -> list[dict]:
    q = ("SELECT *, "
         "(SELECT count(*) FROM leads WHERE pitch_theme_id=pitch_themes.id) AS lead_count, "
         "(SELECT count(*) FROM leads WHERE pitch_theme_id=pitch_themes.id AND lead_status='won') AS won_count "
         "FROM pitch_themes")
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY name"
    return [dict(r) for r in con.execute(q)]


def upsert_pitch_theme(con, *, id=None, name, description=None, color='#6366f1', is_active=1) -> int:
    if id:
        con.execute(
            "UPDATE pitch_themes SET name=?,description=?,color=?,is_active=? WHERE id=?",
            (name, description, color, int(is_active), id),
        )
        con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO pitch_themes (name,description,color,is_active) VALUES (?,?,?,?)",
        (name, description, color, int(is_active)),
    )
    con.commit()
    return cur.lastrowid


def toggle_pitch_theme(con, theme_id: int) -> None:
    con.execute(
        "UPDATE pitch_themes SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
        (theme_id,),
    )
    con.commit()


# ---- リード ----

LEAD_FIELDS = [
    "name", "company", "industry", "company_size", "title", "email", "phone", "source",
    "lead_status", "notes", "assigned_to", "deal_id",
]


def list_leads(con, *, status=None, source=None, theme_id=None, q=None) -> list[dict]:
    sql = ("SELECT l.*, pt.name AS theme_name, pt.color AS theme_color "
           "FROM leads l LEFT JOIN pitch_themes pt ON pt.id = l.pitch_theme_id WHERE 1=1")
    params: list = []
    if status:
        sql += " AND l.lead_status=?"
        params.append(status)
    if source:
        sql += " AND l.source=?"
        params.append(source)
    if theme_id:
        sql += " AND l.pitch_theme_id=?"
        params.append(int(theme_id))
    if q:
        sql += " AND (l.name LIKE ? OR l.company LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY l.updated_at DESC"
    return [dict(r) for r in con.execute(sql, params)]


def get_lead(con, lead_id: int) -> dict | None:
    r = con.execute(
        "SELECT l.*, pt.name AS theme_name, pt.color AS theme_color "
        "FROM leads l LEFT JOIN pitch_themes pt ON pt.id = l.pitch_theme_id WHERE l.id=?",
        (lead_id,),
    ).fetchone()
    return dict(r) if r else None


def upsert_lead(con, *, id=None, **fields) -> int:
    data = {k: fields.get(k) for k in LEAD_FIELDS}
    if id:
        sets = ", ".join(f"{k}=?" for k in LEAD_FIELDS) + ", updated_at=datetime('now')"
        con.execute(f"UPDATE leads SET {sets} WHERE id=?", [data[k] for k in LEAD_FIELDS] + [id])
        con.commit()
        return int(id)
    cols = ", ".join(LEAD_FIELDS)
    ph = ", ".join("?" for _ in LEAD_FIELDS)
    cur = con.execute(f"INSERT INTO leads ({cols}) VALUES ({ph})", [data[k] for k in LEAD_FIELDS])
    con.commit()
    return cur.lastrowid


def list_lead_activities(con, lead_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM lead_activities WHERE lead_id=? ORDER BY created_at DESC", (lead_id,)
    )]


def create_lead_activity(con, *, lead_id, type="note", content, author=None) -> int:
    cur = con.execute(
        "INSERT INTO lead_activities (lead_id,type,content,author) VALUES (?,?,?,?)",
        (lead_id, type, content, author),
    )
    con.commit()
    return cur.lastrowid
