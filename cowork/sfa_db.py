"""営業情報DB（独立）。アカウント / コンタクト / 商談 / 活動。

フェーズ2-1の正本DB。SQLite。テーマDBとは別物だが、商談(deal)は theme_id で
テーマDBのSalesテーマと対応づけ、同期できる（cowork/theme_link.py）。

設計の正本: docs/00_設計構想.md §6。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
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
COMPANY_SIZES = ["500億未満", "1000億未満", "3000億未満", "5000億未満", "5000億以上"]
ACTIVITY_TYPES = ["面談", "電話", "メール", "メモ"]
IMPORTANCE_OPTIONS = ["高", "中", "低"]
OWNERS = ["吉江", "中島", "早瀬", "岩崎", "高橋", "土屋", "戸田", "片山", "杉山", "山端", "堀籠", "Shreyas"]
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

# 開発案件（商談に紐づく開発テーマの管理）
DEV_PROJECT_STATUSES = ["開発中", "完成", "中止"]
DEV_PROJECT_STAGES = ["プロト", "PoC", "本番"]
DEV_ORDER_POTENTIALS = ["低", "中", "高"]
DEV_RESOLUTIONS = ["〇", "△", "×"]
DEV_BUDGET_CONFIRMED = ["〇", "×"]
DEV_DIFFICULTIES = ["易", "中", "難"]
DEV_HAS_BACKEND = ["有り", "無し"]

# 社内論点管理
DEAL_ISSUE_STATUSES = ["議論中", "議論済み", "取り消し"]
DEAL_ISSUE_MEMBERS = ["経営", "営業担当", "営業+開発担当", "開発コア"]


def compute_dev_order_potential(*, budget_confirmed: str | None, resolution: str | None,
                                 difficulty: str | None) -> str:
    """受注余地を解像度・予算確認・実現難易度から自動判定する。

    a) 予算確認×なら低
    b) 予算確認〇 かつ 解像度〇 かつ 難易度が易/中 なら高
    c) それ以外はすべて中
    """
    if budget_confirmed == "×":
        return "低"
    if budget_confirmed == "〇" and resolution == "〇" and difficulty in ("易", "中"):
        return "高"
    return "中"


# ---- 開発スケジュール自動計算（営業日・日本の祝日考慮） ----
# Hisho側dashboard.htmlの同名JSロジックをPython移植したもの。両者は常に同じ結果になるよう保つこと。

def _jp_equinox_day(year: int, is_spring: bool) -> int:
    base = 20.8431 if is_spring else 23.2488
    leap = (year - 1980) // 4
    return int(base + 0.242194 * (year - 1980) - leap)


def _nth_monday_of_month(year: int, month: int, n: int) -> int:
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() == 0:  # Monday
            count += 1
            if count == n:
                return d.day
        d += timedelta(days=1)


def _jp_holidays_for_year(year: int) -> set:
    holidays: set = set()

    def add(m, d):
        holidays.add(date(year, m, d))

    add(1, 1)                                    # 元日
    add(1, _nth_monday_of_month(year, 1, 2))      # 成人の日
    add(2, 11)                                    # 建国記念の日
    add(2, 23)                                    # 天皇誕生日
    add(3, _jp_equinox_day(year, True))           # 春分の日
    add(4, 29)                                    # 昭和の日
    add(5, 3); add(5, 4); add(5, 5)               # 憲法記念日/みどりの日/こどもの日
    add(7, _nth_monday_of_month(year, 7, 3))      # 海の日
    add(8, 11)                                    # 山の日
    add(9, _nth_monday_of_month(year, 9, 3))      # 敬老の日
    add(9, _jp_equinox_day(year, False))          # 秋分の日
    add(10, _nth_monday_of_month(year, 10, 2))    # スポーツの日
    add(11, 3); add(11, 23)                       # 文化の日/勤労感謝の日
    # 振替休日: 日曜の祝日→直後の非祝日
    for d in list(holidays):
        if d.weekday() == 6:
            nd = d + timedelta(days=1)
            while nd in holidays:
                nd += timedelta(days=1)
            holidays.add(nd)
    return holidays


_JP_HOLIDAY_CACHE: dict = {}


def _jp_holidays(year: int) -> set:
    if year not in _JP_HOLIDAY_CACHE:
        _JP_HOLIDAY_CACHE[year] = _jp_holidays_for_year(year)
    return _JP_HOLIDAY_CACHE[year]


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _jp_holidays(d.year)


def add_business_days(d: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining > 0:
        d += timedelta(days=step)
        if is_business_day(d):
            remaining -= 1
    return d


def dev_period_days(stage: str | None, has_backend: str | None, difficulty: str | None) -> int:
    """開発期間（営業日）。"""
    mult = 4 if stage == "PoC" else 2 if stage == "本番" else 1
    days = 2
    if has_backend == "有り":
        days += 3
    if difficulty == "中":
        days += 2
    elif difficulty == "難":
        days += 5
    return days * mult


def compute_dev_schedule(deadline: str | None, stage: str | None, has_backend: str | None,
                          difficulty: str | None) -> tuple:
    """期限から開発期間（営業日）を逆算し、(開始日, 終了日) を返す。
    終了日はデフォルトで期限と同値。期限未設定なら (None, None)。
    開発案件の起票（新規作成）時にのみ呼び出し、以後はHisho側での手動調整に委ねる。"""
    if not deadline:
        return None, None
    try:
        end = date.fromisoformat(deadline)
    except ValueError:
        return None, None
    days = dev_period_days(stage, has_backend, difficulty)
    start = add_business_days(end, -days)
    return start.isoformat(), end.isoformat()


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
    sub_owner TEXT,                   -- サブ担当
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
CREATE INDEX IF NOT EXISTS idx_deals_status_updated ON deals(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(owner);
CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage);
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

-- メールパターン（一斉ドラフト用テンプレート）
CREATE TABLE IF NOT EXISTS email_patterns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    subject      TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    from_address TEXT,
    cc_addresses TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

-- Slack bot スレッド状態管理
CREATE TABLE IF NOT EXISTS slack_threads (
    thread_ts      TEXT PRIMARY KEY,
    channel_id     TEXT NOT NULL,
    deal_id        INTEGER,
    bot_message_ts TEXT,
    state          TEXT DEFAULT 'pending',
    meta           TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);

-- ダッシュボードメモ（Hishoダッシュボードから投稿）
CREATE TABLE IF NOT EXISTS meeting_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id   INTEGER,
    note_date  TEXT,
    body       TEXT,
    task       TEXT,
    task_owner TEXT,
    task_due   TEXT,
    task_done  INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 初回ヒアリング: テンプレート（質問項目集）
CREATE TABLE IF NOT EXISTS hearing_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    items_json  TEXT NOT NULL DEFAULT '[]',  -- [{label,type:'text'|'choice',multi:bool,required:bool,options:[...]}]
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 初回ヒアリング: 結果（商談#をキーに蓄積。活動履歴とは別管理）
CREATE TABLE IF NOT EXISTS hearing_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id       INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    template_id   INTEGER,
    template_name TEXT,                       -- テンプレ名スナップショット
    conducted_on  TEXT,                        -- ヒアリング日（バージョン識別）
    answers_json  TEXT NOT NULL DEFAULT '[]',  -- [{label,type,answer:str|[str]}]
    activity_id   INTEGER,                     -- 紐づく活動履歴
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_hearing_results_deal ON hearing_results(deal_id);

-- 初回ヒアリング: 自動保存下書き（30秒ごとに上書き保存し、確定保存時に削除する）
CREATE TABLE IF NOT EXISTS hearing_drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type   TEXT NOT NULL,
    target_id     INTEGER NOT NULL,
    template_id   INTEGER NOT NULL,
    form_json     TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(target_type, target_id, template_id)
);

-- 開発案件（商談に紐づく開発テーマ。1商談:N開発案件）
CREATE TABLE IF NOT EXISTS dev_projects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id          INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    theme            TEXT NOT NULL,        -- 開発テーマ
    theme_detail     TEXT,                 -- 開発テーマ詳細
    status           TEXT,                 -- 開発中/完成/中止
    stage            TEXT,                 -- プロト/PoC/本番
    order_potential  TEXT,                 -- 受注余地: 低/中/高（自動判定）
    resolution       TEXT,                 -- 解像度: 〇/△/×
    budget_confirmed TEXT,                 -- 予算確認: 〇/×
    difficulty       TEXT,                 -- 実現難易度: 易/中/難
    has_backend      TEXT,                 -- バックエンド有無: 有り/無し
    dev_owner        TEXT,                 -- 開発担当（メンバー選択）
    tech_support     TEXT,                 -- 技術サポート（自由記述）
    dev_milestone    TEXT,                 -- 開発MS（自由記述ラベル）
    dev_milestone_date TEXT,               -- 開発MS日（YYYY-MM-DD）
    deadline         TEXT,                 -- 期限（YYYY-MM-DD）。ガント上では変更不可、SFAでのみ変更する
    dev_start_date   TEXT,                 -- 開発開始日（起票時に期限から自動計算。以後はHisho側の手動調整に委ねる）
    dev_end_date     TEXT,                 -- 開発終了日（起票時のデフォルトは期限と同値）
    dev_policy       TEXT,                 -- 開発方針（自由記述）
    tool_url         TEXT,                 -- 制作したツールのリンク
    tool_login_id    TEXT,                 -- 制作したツールのログインID（必要な場合のみ）
    tool_login_pass  TEXT,                 -- 制作したツールのログインパスワード（必要な場合のみ）
    hisho_id         INTEGER,              -- Hisho側 dev_projects.id（同期キー。NULL=未連携）
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dev_projects_deal ON dev_projects(deal_id);

-- 社内論点（商談に紐づく議論すべき論点。1商談:N論点）
CREATE TABLE IF NOT EXISTS deal_issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id     INTEGER REFERENCES deals(id) ON DELETE CASCADE,  -- NULL=商談に紐づかない共通論点
    issue       TEXT NOT NULL,        -- 論点
    members     TEXT,                 -- 議論メンバー（複数選択、カンマ区切り）
    status      TEXT DEFAULT '議論中', -- 議論中/議論済み/取り消し
    due_date    TEXT,                 -- 解消期限（YYYY-MM-DD）
    ai_summary  TEXT,                 -- メモ全履歴からAI自動生成したサマリー
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deal_issues_deal ON deal_issues(deal_id);

-- 論点メモ（論点ごとの追記型ディスカッションログ）
CREATE TABLE IF NOT EXISTS deal_issue_memos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES deal_issues(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    author     TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deal_issue_memos_issue ON deal_issue_memos(issue_id);

-- 商談への添付ファイル（実体は保存せずSharePoint等の外部リンクのみ保持）
CREATE TABLE IF NOT EXISTS deal_attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id    INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    url        TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deal_attachments_deal ON deal_attachments(deal_id);

-- Hisho同期の失敗記録（printで消えていた失敗を永続化し、後から再同期・可視化する）
CREATE TABLE IF NOT EXISTS sync_failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,   -- 'deal' | 'dev_project' | 'dev_project_delete'
    ref_id     INTEGER NOT NULL,-- 対象のSFA側ID（dev_project_delete時はhisho_id）
    error      TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(kind, ref_id)
);
"""


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")  # 並行読み書きを許可
    return con


def backup_db(db_path: str = DEFAULT_DB_PATH, keep: int = 14) -> str | None:
    """DBのバックアップを backups/ に取得する（1日1世代、最新keep世代を保持）。

    init_db()のマイグレーション実行前に必ず呼ばれる（スキーマ変更事故からの復元手段）。
    同日分が既に存在する場合はスキップ。バックアップ失敗は呼び出し側で握りつぶし、
    起動自体は止めない。
    """
    p = Path(db_path)
    if not p.exists():
        return None
    backup_dir = p.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    dest = backup_dir / f"{p.stem}_{date.today().isoformat()}.db"
    if dest.exists():
        return str(dest)
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)  # WAL中でも一貫したスナップショットが取れる公式API
        finally:
            dst.close()
    finally:
        src.close()
    for old in sorted(backup_dir.glob(f"{p.stem}_*.db"))[:-keep]:
        old.unlink()
    return str(dest)


def _backup_dir(db_path: str = DEFAULT_DB_PATH) -> Path:
    return Path(db_path).parent / "backups"


def backup_now(db_path: str = DEFAULT_DB_PATH, tag: str = "manual") -> str | None:
    """任意タイミングのバックアップ（復元直前の退避や手動DL用）。日次と違い毎回作る。
    ファイル名に時刻とタグを含めるため同名衝突しない。"""
    p = Path(db_path)
    if not p.exists():
        return None
    bdir = _backup_dir(db_path)
    bdir.mkdir(exist_ok=True)
    # datetime系はサンドボックスで使えないため sqlite の strftime で時刻文字列を得る
    src = sqlite3.connect(db_path)
    try:
        stamp = src.execute("SELECT strftime('%Y%m%d_%H%M%S','now','localtime')").fetchone()[0]
        dest = bdir / f"{p.stem}_{tag}_{stamp}.db"
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return str(dest)


def list_backups(db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """backups/内のバックアップ一覧を新しい順で返す（name, size, mtime）。"""
    bdir = _backup_dir(db_path)
    if not bdir.exists():
        return []
    out = []
    for f in bdir.glob(f"{Path(db_path).stem}_*.db"):
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def restore_backup(db_path: str, backup_name: str) -> str:
    """指定バックアップで現DBを置き換える。復元前に現DBを backup_now(tag=prerestore) で退避。
    backup_name は list_backups が返す name のみ許可（パストラバーサル防止）。
    戻り値: 退避した現DBのバックアップパス。"""
    bdir = _backup_dir(db_path)
    src = bdir / backup_name
    # パストラバーサル防止: 正規化後に backups/ 直下であることを厳密確認
    if src.resolve().parent != bdir.resolve() or not src.exists():
        raise ValueError(f"不正なバックアップ名: {backup_name}")
    # SQLiteファイルであることを検証（壊れたファイルでの上書き防止）
    with open(src, "rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            raise ValueError("バックアップがSQLiteファイルではありません")
    prerestore = backup_now(db_path, tag="prerestore")
    # WALの取り残しを避けるため、生ファイルコピーではなく sqlite backup API で上書き
    dst = sqlite3.connect(db_path)
    try:
        s = sqlite3.connect(str(src))
        try:
            s.backup(dst)
        finally:
            s.close()
    finally:
        dst.close()
    return prerestore or ""


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    try:
        backup_db(db_path)
    except Exception as exc:  # noqa: BLE001 — バックアップ失敗で起動を止めない
        print(f"[backup] DB backup failed (continuing): {exc}", flush=True)
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
            ("email_pattern_id", "INTEGER REFERENCES email_patterns(id) ON DELETE SET NULL"),
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
            ("sub_owner", "TEXT"),
            ("slack_notified_date", "TEXT"),
        ]:
            if col not in deal_cols:
                con.execute(f"ALTER TABLE deals ADD COLUMN {col} {typedef}")
        thread_cols = {r[1] for r in con.execute("PRAGMA table_info(slack_threads)")}
        if "meta" not in thread_cols:
            con.execute("ALTER TABLE slack_threads ADD COLUMN meta TEXT")
        note_cols = {r[1] for r in con.execute("PRAGMA table_info(meeting_notes)")}
        if "theme_id" not in note_cols:
            con.execute("ALTER TABLE meeting_notes ADD COLUMN theme_id INTEGER")
            con.execute("CREATE INDEX IF NOT EXISTS idx_meeting_notes_theme ON meeting_notes(theme_id)")
        if "task_done" not in note_cols:
            con.execute("ALTER TABLE meeting_notes ADD COLUMN task_done INTEGER DEFAULT 0")
        dp_cols = {r[1] for r in con.execute("PRAGMA table_info(dev_projects)")}
        if "tool_url" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN tool_url TEXT")
        if "dev_milestone_date" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN dev_milestone_date TEXT")
        if "dev_start_date" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN dev_start_date TEXT")
        if "dev_end_date" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN dev_end_date TEXT")
        if "tool_login_id" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN tool_login_id TEXT")
        if "tool_login_pass" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN tool_login_pass TEXT")
        # deal_issues.deal_id を NOT NULL → NULL可に変更（商談共通の論点に対応）。
        # SQLiteはNOT NULL制約を直接ALTERできないため、テーブルを作り直す。
        issue_deal_id_col = next(
            (c for c in con.execute("PRAGMA table_info(deal_issues)") if c[1] == "deal_id"), None
        )
        if issue_deal_id_col is not None and issue_deal_id_col[3] == 1:
            # foreign_keys=ONのままDROP TABLEすると、SQLiteはdeal_issue_memosの
            # ON DELETE CASCADEを全行に適用してしまい、既存メモが全消失する。
            # 一時的にOFFにしてから作り直す。
            con.commit()
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("""
                CREATE TABLE deal_issues_new (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id     INTEGER REFERENCES deals(id) ON DELETE CASCADE,
                    issue       TEXT NOT NULL,
                    members     TEXT,
                    status      TEXT DEFAULT '議論中',
                    due_date    TEXT,
                    ai_summary  TEXT,
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now'))
                )
            """)
            con.execute(
                "INSERT INTO deal_issues_new (id, deal_id, issue, members, status, due_date, "
                "ai_summary, created_at, updated_at) "
                "SELECT id, deal_id, issue, members, status, due_date, ai_summary, created_at, updated_at "
                "FROM deal_issues"
            )
            con.execute("DROP TABLE deal_issues")
            con.execute("ALTER TABLE deal_issues_new RENAME TO deal_issues")
            con.execute("CREATE INDEX IF NOT EXISTS idx_deal_issues_deal ON deal_issues(deal_id)")
            con.commit()
            con.execute("PRAGMA foreign_keys = ON")
        con.commit()
    finally:
        con.close()


# ---- マスタ ----
import json as _json


def get_master_list(con, key: str) -> list[str]:
    """DB保存値があればそれを返す。なければデフォルト定数を返す。"""
    row = con.execute("SELECT values_json FROM masters WHERE key=?", (key,)).fetchone()
    if row:
        try:
            return _json.loads(row[0])
        except (ValueError, TypeError):
            # 保存値が破損している場合はデフォルトにフォールバック（呼び出し側のクラッシュ防止）
            print(f"[masters] values_json broken for key={key}, falling back to default", flush=True)
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


def list_deals(con, status: str | None = "open", owner: str | None = None,
               stage: str | None = None) -> list[dict]:
    q = """SELECT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d LEFT JOIN accounts a ON a.id = d.account_id WHERE 1=1"""
    params: list = []
    if status == "open":
        q += " AND (d.status = 'open' OR d.status IS NULL)"
    elif status:
        q += " AND d.status = ?"
        params.append(status)
    if owner:
        q += " AND (d.owner = ? OR d.sub_owner = ?)"
        params.append(owner)
        params.append(owner)
    if stage:
        q += " AND d.stage = ?"
        params.append(stage)
    q += " ORDER BY d.updated_at DESC"
    return [dict(r) for r in con.execute(q, params)]


def list_deals_by_date(con, date: str, owner: str | None = None) -> list[dict]:
    """指定日が次回MS日付、または活動履歴の実施日のいずれかに一致する商談を返す。"""
    q = """SELECT DISTINCT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE (d.status IS NULL OR d.status != 'closed')
                 AND (d.next_milestone_date = ?
                      OR EXISTS (SELECT 1 FROM activities act WHERE act.deal_id = d.id AND act.occurred_on = ?))"""
    params: list = [date, date]
    if owner:
        q += " AND (d.owner = ? OR d.sub_owner = ?)"
        params.append(owner)
        params.append(owner)
    q += " ORDER BY a.name"
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
def upsert_account(con, *, id=None, name, industry=None, company_size=None, note=None, commit: bool = True) -> int:
    if id is not None:
        con.execute(
            "UPDATE accounts SET name=?, industry=?, company_size=?, note=?, updated_at=datetime('now') WHERE id=?",
            (name, industry, company_size, note, id),
        )
        if commit:
            con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO accounts (name, industry, company_size, note) VALUES (?,?,?,?)",
        (name, industry, company_size, note),
    )
    if commit:
        con.commit()
    return cur.lastrowid


def upsert_account_merge(con, *, name: str, industry=None, company_size=None, commit: bool = True) -> int:
    """アカウントを名前で検索し、無ければ新規作成、あれば空欄項目のみ埋める。

    リード/商談のCSV一括取込で、同名アカウントの重複作成を防ぐために使う。
    commit=False指定時は呼び出し側が後でまとめてcommitする（大量件数の一括取込を高速化するため）。
    """
    existing = con.execute(
        "SELECT id, industry, company_size FROM accounts WHERE name=?", (name,)
    ).fetchone()
    if existing is None:
        return upsert_account(con, name=name, industry=industry, company_size=company_size, commit=commit)
    acc_row = dict(existing)
    updates = {}
    if industry and not acc_row.get("industry"):
        updates["industry"] = industry
    if company_size and not acc_row.get("company_size"):
        updates["company_size"] = company_size
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        con.execute(
            f"UPDATE accounts SET {set_clause}, updated_at=datetime('now') WHERE id=?",
            (*updates.values(), acc_row["id"]),
        )
        if commit:
            con.commit()
    return acc_row["id"]


DEAL_FIELDS = [
    "account_id", "theme_id", "deal_name", "stage", "business_type_l1", "business_type_l2",
    "lead_pattern", "owner", "sub_owner", "value_lumpsum", "value_lumpsum_monthly", "value_recurring",
    "client_budget", "next_milestone_date", "next_milestone_label", "note", "goal",
    "importance", "status",
    "cost_stage", "approach_value", "approach_rate", "reduction_rate", "fee_rate", "diagnosis_cost",
]


def upsert_deal(con, *, id=None, commit: bool = True, **fields) -> int:
    data = {k: fields.get(k) for k in DEAL_FIELDS}
    if id is not None:
        # theme_id は sync_deal が直接 SQL で管理するため、NULL で上書きしない
        update_keys = [k for k in DEAL_FIELDS if not (k == "theme_id" and data[k] is None)]
        sets = ", ".join(f"{k}=?" for k in update_keys) + ", updated_at=datetime('now')"
        con.execute(f"UPDATE deals SET {sets} WHERE id=?", [data[k] for k in update_keys] + [id])
        if commit:
            con.commit()
        return int(id)
    cols = ", ".join(DEAL_FIELDS)
    ph = ", ".join("?" for _ in DEAL_FIELDS)
    cur = con.execute(f"INSERT INTO deals ({cols}) VALUES ({ph})", [data[k] for k in DEAL_FIELDS])
    if commit:
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
    if id is not None:
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
    "pitch_theme_id", "lead_status", "notes", "assigned_to", "deal_id",
]


def list_leads(con, *, status=None, source=None, theme_id=None, q=None) -> list[dict]:
    sql = ("SELECT l.*, pt.name AS theme_name, pt.color AS theme_color, "
           "ep.name AS email_pattern_name "
           "FROM leads l "
           "LEFT JOIN pitch_themes pt ON pt.id = l.pitch_theme_id "
           "LEFT JOIN email_patterns ep ON ep.id = l.email_pattern_id "
           "WHERE 1=1")
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


def upsert_lead(con, *, id=None, commit: bool = True, **fields) -> int:
    data = {k: fields.get(k) for k in LEAD_FIELDS}
    if id is not None:
        sets = ", ".join(f"{k}=?" for k in LEAD_FIELDS) + ", updated_at=datetime('now')"
        con.execute(f"UPDATE leads SET {sets} WHERE id=?", [data[k] for k in LEAD_FIELDS] + [id])
        if commit:
            con.commit()
        return int(id)
    cols = ", ".join(LEAD_FIELDS)
    ph = ", ".join("?" for _ in LEAD_FIELDS)
    cur = con.execute(f"INSERT INTO leads ({cols}) VALUES ({ph})", [data[k] for k in LEAD_FIELDS])
    if commit:
        con.commit()
    return cur.lastrowid


def list_email_patterns(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM email_patterns ORDER BY created_at ASC"
    )]


def get_email_pattern(con, id: int) -> dict | None:
    r = con.execute("SELECT * FROM email_patterns WHERE id=?", (int(id),)).fetchone()
    return dict(r) if r else None


def save_email_pattern(con, *, id=None, name, subject, body,
                       from_address=None, cc_addresses=None) -> int:
    if id is not None:
        con.execute(
            "UPDATE email_patterns SET name=?, subject=?, body=?, from_address=?, cc_addresses=? WHERE id=?",
            (name, subject, body, from_address or None, cc_addresses or None, int(id)),
        )
        con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO email_patterns(name, subject, body, from_address, cc_addresses) VALUES(?,?,?,?,?)",
        (name, subject, body, from_address or None, cc_addresses or None),
    )
    con.commit()
    return cur.lastrowid


def delete_email_pattern(con, id: int):
    con.execute("UPDATE leads SET email_pattern_id=NULL WHERE email_pattern_id=?", (int(id),))
    con.execute("DELETE FROM email_patterns WHERE id=?", (int(id),))
    con.commit()


def set_lead_email_pattern(con, lead_id: int, pattern_id: int | None):
    con.execute(
        "UPDATE leads SET email_pattern_id=?, updated_at=datetime('now') WHERE id=?",
        (int(pattern_id) if pattern_id else None, int(lead_id)),
    )
    con.commit()


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


# ---- 初回ヒアリング: テンプレート ----

def _parse_items(items_json: str | None) -> list[dict]:
    """items_json をパースして項目リストを返す（壊れていれば空）。"""
    try:
        items = _json.loads(items_json or "[]")
        return items if isinstance(items, list) else []
    except (ValueError, TypeError):
        return []


def list_hearing_templates(con) -> list[dict]:
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM hearing_templates ORDER BY created_at ASC"
    )]
    for r in rows:
        r["items"] = _parse_items(r.get("items_json"))
    return rows


def get_hearing_template(con, id: int) -> dict | None:
    r = con.execute("SELECT * FROM hearing_templates WHERE id=?", (int(id),)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["items"] = _parse_items(d.get("items_json"))
    return d


def save_hearing_template(con, *, id=None, name, description=None, items) -> int:
    items_json = _json.dumps(items if items is not None else [], ensure_ascii=False)
    if id is not None:
        con.execute(
            "UPDATE hearing_templates SET name=?, description=?, items_json=? WHERE id=?",
            (name, description or None, items_json, int(id)),
        )
        con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO hearing_templates(name, description, items_json) VALUES(?,?,?)",
        (name, description or None, items_json),
    )
    con.commit()
    return cur.lastrowid


def delete_hearing_template(con, id: int) -> None:
    con.execute("DELETE FROM hearing_templates WHERE id=?", (int(id),))
    con.commit()


# ---- 初回ヒアリング: 結果 ----

def add_hearing_result(con, *, deal_id, template_id=None, template_name=None,
                       conducted_on=None, answers: list[dict], activity_id=None) -> int:
    cur = con.execute(
        "INSERT INTO hearing_results "
        "(deal_id, template_id, template_name, conducted_on, answers_json, activity_id) "
        "VALUES (?,?,?,?,?,?)",
        (int(deal_id), int(template_id) if template_id else None, template_name,
         conducted_on, _json.dumps(answers or [], ensure_ascii=False),
         int(activity_id) if activity_id else None),
    )
    con.commit()
    return cur.lastrowid


def save_hearing_draft(con, *, target_type: str, target_id: int, template_id: int, form_data: dict) -> None:
    """ヒアリング入力中の自動保存下書きを保存（同一対象・同一テンプレなら上書き）。"""
    con.execute(
        "INSERT INTO hearing_drafts (target_type, target_id, template_id, form_json, updated_at) "
        "VALUES (?,?,?,?,datetime('now')) "
        "ON CONFLICT(target_type, target_id, template_id) DO UPDATE SET "
        "form_json=excluded.form_json, updated_at=excluded.updated_at",
        (target_type, int(target_id), int(template_id),
         _json.dumps(form_data or {}, ensure_ascii=False)),
    )
    con.commit()


def get_hearing_draft(con, *, target_type: str, target_id: int, template_id: int) -> dict | None:
    r = con.execute(
        "SELECT * FROM hearing_drafts WHERE target_type=? AND target_id=? AND template_id=?",
        (target_type, int(target_id), int(template_id)),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["form_data"] = _json.loads(d.get("form_json") or "{}")
    except (ValueError, TypeError):
        d["form_data"] = {}
    return d


def delete_hearing_draft(con, *, target_type: str, target_id: int, template_id: int) -> None:
    con.execute(
        "DELETE FROM hearing_drafts WHERE target_type=? AND target_id=? AND template_id=?",
        (target_type, int(target_id), int(template_id)),
    )
    con.commit()


def _hydrate_hearing_result(r: dict) -> dict:
    try:
        r["answers"] = _json.loads(r.get("answers_json") or "[]")
    except (ValueError, TypeError):
        r["answers"] = []
    return r


def list_hearing_results(con, deal_id: int) -> list[dict]:
    """1商談のヒアリング結果（新しい順）。"""
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM hearing_results WHERE deal_id=? "
        "ORDER BY conducted_on DESC, id DESC", (int(deal_id),)
    )]
    return [_hydrate_hearing_result(r) for r in rows]


def get_hearing_result(con, id: int) -> dict | None:
    r = con.execute(
        "SELECT hr.*, d.deal_name, a.name AS account_name "
        "FROM hearing_results hr "
        "LEFT JOIN deals d ON d.id = hr.deal_id "
        "LEFT JOIN accounts a ON a.id = d.account_id WHERE hr.id=?",
        (int(id),),
    ).fetchone()
    return _hydrate_hearing_result(dict(r)) if r else None


def update_hearing_result(con, id: int, *, conducted_on=None, answers: list[dict]) -> None:
    """既存ヒアリング結果の内容を修正する（新規行は作らず上書き）。"""
    con.execute(
        "UPDATE hearing_results SET conducted_on=?, answers_json=? WHERE id=?",
        (conducted_on, _json.dumps(answers or [], ensure_ascii=False), int(id)),
    )
    con.commit()


def delete_hearing_result(con, id: int) -> None:
    con.execute("DELETE FROM hearing_results WHERE id=?", (int(id),))
    con.commit()


def list_all_hearing_results(con, template_id: int | None = None) -> list[dict]:
    """全ヒアリング結果（商談・アカウント名つき）。一覧/エクスポート用。

    template_id指定時はそのテンプレートの結果のみに絞り込む。
    """
    sql = ("SELECT hr.*, d.deal_name, a.name AS account_name "
           "FROM hearing_results hr "
           "LEFT JOIN deals d ON d.id = hr.deal_id "
           "LEFT JOIN accounts a ON a.id = d.account_id ")
    params: list = []
    if template_id:
        sql += "WHERE hr.template_id=? "
        params.append(int(template_id))
    sql += "ORDER BY hr.conducted_on DESC, hr.id DESC"
    rows = [dict(r) for r in con.execute(sql, params)]
    return [_hydrate_hearing_result(r) for r in rows]


def count_hearing_results_by_template(con) -> dict:
    """テンプレートID→実施件数 のマッピングを返す。"""
    rows = con.execute(
        "SELECT template_id, COUNT(*) AS cnt FROM hearing_results "
        "WHERE template_id IS NOT NULL GROUP BY template_id"
    ).fetchall()
    return {r["template_id"]: r["cnt"] for r in rows}


def count_hearing_results(con, deal_id: int) -> int:
    r = con.execute(
        "SELECT count(*) FROM hearing_results WHERE deal_id=?", (int(deal_id),)
    ).fetchone()
    return int(r[0]) if r else 0


# ---- 開発案件（deal_id:N の開発テーマ管理） ----

DEV_PROJECT_FIELDS = [
    "deal_id", "theme", "theme_detail", "status", "stage", "order_potential",
    "resolution", "budget_confirmed", "difficulty", "has_backend", "dev_owner",
    "tech_support", "dev_milestone", "dev_milestone_date", "deadline", "dev_start_date",
    "dev_end_date", "dev_policy", "tool_url", "tool_login_id", "tool_login_pass",
]

_DEV_PROJECT_SELECT = (
    "SELECT p.*, d.deal_name, d.owner AS sales_owner, d.sub_owner AS sales_sub_owner, "
    "a.name AS account_name FROM dev_projects p "
    "LEFT JOIN deals d ON d.id = p.deal_id LEFT JOIN accounts a ON a.id = d.account_id"
)


def list_dev_projects(con, *, deal_id: int | None = None, dev_owner: str | None = None,
                       sales_owner: str | None = None, status: str | None = None,
                       stage: str | None = None, order_potential: str | None = None,
                       deadline_from: str | None = None, deadline_to: str | None = None) -> list[dict]:
    """開発案件一覧。deal_id以外はすべて一覧画面の絞り込み用。
    sales_ownerは主担当・サブ担当のいずれかに一致する案件を返す。"""
    q = _DEV_PROJECT_SELECT
    conds: list = []
    params: list = []
    if deal_id:
        conds.append("p.deal_id = ?")
        params.append(int(deal_id))
    if dev_owner:
        conds.append("p.dev_owner = ?")
        params.append(dev_owner)
    if sales_owner:
        conds.append("(d.owner = ? OR d.sub_owner = ?)")
        params.append(sales_owner)
        params.append(sales_owner)
    if status:
        conds.append("p.status = ?")
        params.append(status)
    if stage:
        conds.append("p.stage = ?")
        params.append(stage)
    if order_potential:
        conds.append("p.order_potential = ?")
        params.append(order_potential)
    if deadline_from:
        conds.append("p.deadline >= ?")
        params.append(deadline_from)
    if deadline_to:
        conds.append("p.deadline <= ?")
        params.append(deadline_to)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY p.updated_at DESC"
    return [dict(r) for r in con.execute(q, params)]


def get_dev_project(con, id: int) -> dict | None:
    r = con.execute(_DEV_PROJECT_SELECT + " WHERE p.id = ?", (int(id),)).fetchone()
    return dict(r) if r else None


def upsert_dev_project(con, *, id=None, commit: bool = True, **fields) -> int:
    data = {k: fields.get(k) for k in DEV_PROJECT_FIELDS}
    data["order_potential"] = compute_dev_order_potential(
        budget_confirmed=data.get("budget_confirmed"),
        resolution=data.get("resolution"),
        difficulty=data.get("difficulty"),
    )
    if id is not None:
        sets = ", ".join(f"{k}=?" for k in DEV_PROJECT_FIELDS) + ", updated_at=datetime('now')"
        con.execute(f"UPDATE dev_projects SET {sets} WHERE id=?",
                    [data[k] for k in DEV_PROJECT_FIELDS] + [int(id)])
        if commit:
            con.commit()
        return int(id)
    cols = ", ".join(DEV_PROJECT_FIELDS)
    ph = ", ".join("?" for _ in DEV_PROJECT_FIELDS)
    cur = con.execute(f"INSERT INTO dev_projects ({cols}) VALUES ({ph})",
                       [data[k] for k in DEV_PROJECT_FIELDS])
    if commit:
        con.commit()
    return cur.lastrowid


def delete_dev_project(con, id: int) -> None:
    con.execute("DELETE FROM dev_projects WHERE id=?", (int(id),))
    con.commit()


# ---- 社内論点（deal_id:N の議論ポイント管理） ----

DEAL_ISSUE_FIELDS = ["deal_id", "issue", "members", "status", "due_date"]

_DEAL_ISSUE_SELECT = (
    "SELECT i.*, d.deal_name, d.owner AS sales_owner, d.sub_owner AS sales_sub_owner, "
    "a.name AS account_name FROM deal_issues i "
    "LEFT JOIN deals d ON d.id = i.deal_id LEFT JOIN accounts a ON a.id = d.account_id"
)

DEAL_ISSUE_SORTS = ["due_date", "status", "updated_at"]


def list_deal_issues(con, *, deal_id: int | None = None, status: str | None = None,
                      member: str | None = None, q: str | None = None,
                      sort: str = "due_date") -> list[dict]:
    """論点一覧。deal_id以外はすべて一覧画面の絞り込み用。
    member指定時は議論メンバー（カンマ区切り複数選択）にその値を含む論点のみ返す。
    q指定時はアカウント名・商談名の部分一致で絞り込む。"""
    q_sql = _DEAL_ISSUE_SELECT
    conds: list = []
    params: list = []
    if deal_id:
        conds.append("i.deal_id = ?")
        params.append(int(deal_id))
    if status:
        conds.append("i.status = ?")
        params.append(status)
    if member:
        conds.append("i.members LIKE ?")
        params.append(f"%{member}%")
    if q:
        conds.append("(a.name LIKE ? OR d.deal_name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if conds:
        q_sql += " WHERE " + " AND ".join(conds)
    order = {
        "due_date": "i.due_date IS NULL, i.due_date ASC",
        "status": "i.status ASC, i.due_date IS NULL, i.due_date ASC",
        "updated_at": "i.updated_at DESC",
    }.get(sort, "i.due_date IS NULL, i.due_date ASC")
    q_sql += f" ORDER BY {order}"
    return [dict(r) for r in con.execute(q_sql, params)]


def get_deal_issue(con, id: int) -> dict | None:
    r = con.execute(_DEAL_ISSUE_SELECT + " WHERE i.id = ?", (int(id),)).fetchone()
    return dict(r) if r else None


def upsert_deal_issue(con, *, id=None, commit: bool = True, **fields) -> int:
    data = {k: fields.get(k) for k in DEAL_ISSUE_FIELDS}
    if id is not None:
        sets = ", ".join(f"{k}=?" for k in DEAL_ISSUE_FIELDS) + ", updated_at=datetime('now')"
        con.execute(f"UPDATE deal_issues SET {sets} WHERE id=?",
                    [data[k] for k in DEAL_ISSUE_FIELDS] + [int(id)])
        if commit:
            con.commit()
        return int(id)
    cols = ", ".join(DEAL_ISSUE_FIELDS)
    ph = ", ".join("?" for _ in DEAL_ISSUE_FIELDS)
    cur = con.execute(f"INSERT INTO deal_issues ({cols}) VALUES ({ph})",
                       [data[k] for k in DEAL_ISSUE_FIELDS])
    if commit:
        con.commit()
    return cur.lastrowid


def delete_deal_issue(con, id: int) -> None:
    con.execute("DELETE FROM deal_issues WHERE id=?", (int(id),))
    con.commit()


def list_deal_issue_memos(con, issue_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM deal_issue_memos WHERE issue_id=? ORDER BY created_at ASC", (issue_id,)
    )]


def add_deal_issue_memo(con, *, issue_id: int, body: str, author: str | None = None) -> int:
    cur = con.execute(
        "INSERT INTO deal_issue_memos (issue_id, body, author) VALUES (?,?,?)",
        (int(issue_id), body, author),
    )
    con.commit()
    return cur.lastrowid


def get_deal_issue_memo(con, memo_id: int) -> dict | None:
    r = con.execute("SELECT * FROM deal_issue_memos WHERE id=?", (int(memo_id),)).fetchone()
    return dict(r) if r else None


def delete_deal_issue_memo(con, memo_id: int) -> None:
    con.execute("DELETE FROM deal_issue_memos WHERE id=?", (int(memo_id),))
    con.commit()


def list_deal_attachments(con, deal_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM deal_attachments WHERE deal_id=? ORDER BY created_at DESC", (deal_id,)
    )]


def add_deal_attachment(con, *, deal_id: int, label: str, url: str) -> int:
    cur = con.execute(
        "INSERT INTO deal_attachments (deal_id, label, url) VALUES (?,?,?)",
        (int(deal_id), label, url),
    )
    con.commit()
    return cur.lastrowid


def get_deal_attachment(con, attachment_id: int) -> dict | None:
    r = con.execute("SELECT * FROM deal_attachments WHERE id=?", (int(attachment_id),)).fetchone()
    return dict(r) if r else None


def delete_deal_attachment(con, attachment_id: int) -> None:
    con.execute("DELETE FROM deal_attachments WHERE id=?", (int(attachment_id),))
    con.commit()


def set_deal_issue_ai_summary(con, issue_id: int, summary: str) -> None:
    con.execute(
        "UPDATE deal_issues SET ai_summary=?, updated_at=datetime('now') WHERE id=?",
        (summary, int(issue_id)),
    )
    con.commit()


# ---- Hisho同期の失敗記録 ----

def record_sync_failure(con, kind: str, ref_id: int, error: str) -> None:
    """同期失敗を記録（同一(kind,ref_id)は最新エラーで上書き）。記録自体の失敗は握りつぶす。"""
    try:
        con.execute(
            "INSERT INTO sync_failures (kind, ref_id, error) VALUES (?,?,?) "
            "ON CONFLICT(kind, ref_id) DO UPDATE SET error=excluded.error, created_at=datetime('now')",
            (kind, int(ref_id), (error or "")[:2000]),
        )
        con.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_failures] record failed: {exc}", flush=True)


def clear_sync_failure(con, kind: str, ref_id: int) -> None:
    """同期成功時に該当の失敗記録を消す。"""
    try:
        con.execute("DELETE FROM sync_failures WHERE kind=? AND ref_id=?", (kind, int(ref_id)))
        con.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_failures] clear failed: {exc}", flush=True)


def list_sync_failures(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM sync_failures ORDER BY created_at ASC")]


def count_sync_failures(con) -> int:
    return con.execute("SELECT COUNT(*) c FROM sync_failures").fetchone()["c"]
