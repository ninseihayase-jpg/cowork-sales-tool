"""営業情報DB（独立）。アカウント / コンタクト / 商談 / 活動。

フェーズ2-1の正本DB。SQLite。テーマDBとは別物だが、商談(deal)は theme_id で
テーマDBのSalesテーマと対応づけ、同期できる（cowork/theme_link.py）。

設計の正本: docs/00_設計構想.md §6。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "cowork_sfa.db")

# テーマDBの選択肢に準拠（表記揺れ防止。docs/00 §3 / 秘書 db_schema_design.md）
# ※「失注」は選択肢から撤廃済み(#67)。失注は「クローズ(status=closed)＋close_reason='失注'」で
#   一元管理する。既存データにstage='失注'が残りうるため、集計側は後方互換で扱う。
DEAL_STAGES = ["初回アポ実施", "要件詰め", "提案", "クロージング", "受注", "保留中"]
# 決着済みステージ。次回MSが無くても「要フォロー(MS超過)」に出さない（受注は追うものが無い）。
# 失注は撤廃したが、移行前の残存データ(stage='失注')を保護するため一覧に残す（クローズ済みなので実害なし）。
CONCLUDED_DEAL_STAGES = ["受注", "失注"]
# パイプライン（有効商談）とみなすステージ。週次レポートの「パイプライン件数・金額」はここに限定する
# （初回アポ実施＝接触直後で実質未着手、受注＝成約済、保留中＝停止、は除外）。#27でユーザー確定。
PIPELINE_STAGES = ["要件詰め", "提案", "クロージング"]
# 次回MSの種別。Slack日次アポ通知は「アポ」（および未設定=fail-safe）のみ投稿し、「タスク」は除外する。
NEXT_MS_TYPES = ["アポ", "タスク"]
# 商談/リードの終了理由（区分）。「自社都合で撤退」を独立させ"失注"と区別する（戦略的な選別を数字で語るため）。
# 展示会ファネルの有効母数は「ニーズなし」を除いて算出する。
CLOSE_REASONS = ["ニーズなし", "キャンセル", "失注", "自社都合で撤退", "保留・時期尚早"]
BUSINESS_TYPE_L1 = ["コスト削減", "コンサルティング", "AI導入", "他"]
BUSINESS_TYPE_L2_BY_L1 = {
    "コスト削減":     ["コスト診断(無償)", "コスト診断(有償)", "コスト削減(成果報酬)"],
    "コンサルティング": ["コンサル(調達/SCM)", "コンサル(IT)", "コンサル(他)", "アンダー"],
    "AI導入":        ["AI開発(軽)", "AI開発(重)", "汎用AIエージェント(調達)", "汎用AIエージェント(SCM)", "汎用AIエージェント(IT)", "AXパートナー"],
    "他":            ["調達BPO(スポット)", "未定"],
}
# 担当者の担当領域（最大余剰工数の対象/対象外判定などに使う）。マスタ編集可。
OWNER_DOMAINS = ["営業", "コンサルタント", "エンジニア", "オペレータ", "その他"]
# 業界のターゲット領域（業界→領域の対応。業界を持つ各所で自動的に付随させる）。マスタ編集可。
TARGET_DOMAINS = ["製造", "建設", "その他"]
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

# 開発案件で使う「必要な技術シード」。#60でツリー(2階層)化: L1カテゴリ→L2シード。
# 「基礎技術」の初期L2シード（研究テーマ系L1は空で開始し、運用で埋めていく）。
TECH_SEEDS = [
    "LLM/生成AI", "RAG(社内文書検索)", "AIエージェント", "画像認識", "音声認識/文字起こし",
    "OCR/帳票読取", "需要予測/予測モデル", "最適化/数理計画", "レコメンド",
    "スクレイピング/データ収集", "業務自動化(RPA)", "データ基盤/ETL", "BI/ダッシュボード",
    "Web/業務アプリ開発", "外部API連携", "スプレッドシート連携",
]
# 技術シードのツリー既定値。L1は事業が増えると「研究テーマ(◯◯)」が増える（マスタ画面で編集）。
TECH_SEED_TREE_DEFAULT = {
    "基礎技術": TECH_SEEDS,
    "研究テーマ(SCM)": [],
    "研究テーマ(建設)": [],
    "研究テーマ(コスト削減)": [],
}

# タスク種類(分類)。マスタ編集可（管理→マスタ）。色は _task_category_color で自動割当。
TASK_CATEGORIES = ["開発", "調査・検証", "設計", "レビュー", "バグ修正", "環境・インフラ",
                   "データ整備", "ドキュメント", "打合せ準備", "営業対応", "社内・庶務", "その他"]
# 種類の表示上のツリー(L1→L2)。フィルタ/保存で使うのはL2(=leaf=TASK_CATEGORIES)のみ。
# L1は<select>のoptgroupや表示のグルーピングに使う「見せ方」だけの階層。
TASK_CATEGORY_TREE = {
    "開発・技術": ["開発", "調査・検証", "設計", "レビュー", "バグ修正", "環境・インフラ"],
    "データ・文書": ["データ整備", "ドキュメント"],
    "営業・打合せ": ["打合せ準備", "営業対応"],
    "社内・その他": ["社内・庶務", "その他"],
}
# タスクの大項目＝プロジェクト(取り組み)。期限＋状態を持つ管理対象（task_projectsテーブル）。
# 専用の管理画面(/task-projects)で追加・編集する。tasks.projectは名前で緩く参照。
TASK_PROJECT_STATUSES = ["進行中", "保留", "完了"]

# マスタ編集対象キー → デフォルト値のマッピング（技術シード・プロジェクトは専用画面で編集＝ここに含めない）
MASTER_KEYS = {
    "owners":            OWNERS,
    "deal_stages":       DEAL_STAGES,
    "business_type_l1":  BUSINESS_TYPE_L1,
    "lead_patterns":     LEAD_PATTERNS,
    "industries":        INDUSTRIES,
    "company_sizes":     COMPANY_SIZES,
    "activity_types":    ACTIVITY_TYPES,
    "task_categories":   TASK_CATEGORIES,
    "owner_domains":     OWNER_DOMAINS,
    "target_domains":    TARGET_DOMAINS,
}
MASTER_LABELS = {
    "owners":            "担当者",
    "deal_stages":       "商談ステージ",
    "business_type_l1":  "事業種別L1",
    "lead_patterns":     "リード経路（商談）",
    "industries":        "業界",
    "company_sizes":     "企業規模",
    "activity_types":    "活動種別",
    "task_categories":   "タスク種類",
    "owner_domains":     "担当領域",
    "target_domains":    "ターゲット領域",
}
COST_STAGES = ["診断中", "削減機会発見", "削減提案中", "削減実行中", "成果確定", "不発"]

# Delivery（受注後・納品）アサイン計画（#75）。デモ開発とは別系統。
DELIVERY_STATUSES = ["進行中", "完了", "保留"]
DELIVERY_VIEW_WEEKS = 16              # 全社稼働テーブルの初期表示週数（今週〜。調整可）
POINTS_PER_FTE = 20                   # デモ開発点数→FTE%換算の基準（20点≒100%FTE）※デモ負荷率自体は個人上限基準
# Delivery案件を自動起票するステージ（「提案」到達以降）。
DELIVERY_TRIGGER_STAGES = ("提案", "クロージング", "受注")
# ヒートマップ閾値(%)。100%超が常態のため150%も閾値に。定数で調整可。
DELIVERY_HEAT_THRESHOLDS = {"ok": 70, "full": 100, "over": 150}

# 開発案件（商談に紐づく開発テーマの管理）
DEV_PROJECT_STATUSES = ["開発中", "完成", "中止"]
DEV_PROJECT_STAGES = ["プロト", "PoC", "本番"]
DEV_ORDER_POTENTIALS = ["低", "中", "高"]
DEV_RESOLUTIONS = ["〇", "△", "×"]
DEV_BUDGET_CONFIRMED = ["〇", "×"]
DEV_DIFFICULTIES = ["易", "中", "難"]
DEV_HAS_BACKEND = ["有り", "無し"]
# 開発点数(工数)機能（#41）
# 点数マスタ＝「作業種別」ごとの基準点数。既存分類(プロト/PoC/本番)と難易度は「係数」として掛ける。
DEV_AUDIENCES = ["社外向け", "社内向け", "研究"]  # 提供先。研究=図面OCR等の技術シード
DEV_PRICINGS = ["無償", "有償"]                    # 課金（分類ディメンション・点数計算には未使用）
DEV_DIFFICULTY_COEF = {"易": 0.8, "中": 1.0, "難": 1.3}     # 難易度係数
DEV_STAGE_COEF = {"プロト": 1.0, "PoC": 1.3, "本番": 1.6}   # 既存分類=係数（仮値・後で調整）
DEV_BACKEND_BONUS = {"有り": 2, "無し": 0}                   # バックエンド加点（係数を掛ける前に基準点数へ加算）
# 作業種別の初期シード（仮値。マスタ画面で付け直す前提）
DEV_WORK_TYPE_SEED = [
    ("既存デモ+input改変", 1),
    ("既存デモの改変", 2),
    ("新規フロントエンド", 3),
    ("バックエンド含むデモ", 5),
    ("本番向けツール", 8),
    ("研究テーマ", 5),
]


def compute_dev_points(con, *, work_type, stage, difficulty, has_backend=None) -> float | None:
    """点数 = (作業種別の基準点数 ＋ バックエンド加点) × 既存分類係数(プロト/PoC/本番) × 難易度係数。
    マスタ未登録ならNone。係数・加点は dev_coefficient（管理画面で編集可）を優先し、無ければ定数の既定値。"""
    base = get_dev_point_base(con, work_type or "")
    if base is None:
        return None
    coefs = get_dev_coefs(con)
    bonus = coefs["backend"].get(has_backend or "", 0.0)
    coef = coefs["stage"].get(stage or "", 1.0) * coefs["difficulty"].get(difficulty or "", 1.0)
    return round((base + bonus) * coef, 1)

# 社内論点管理
DEAL_ISSUE_STATUSES = ["議論中", "議論済み", "取り消し"]
DEAL_ISSUE_MEMBERS = ["経営", "営業担当", "営業+開発担当", "開発コア"]

# タスク管理（#30）
TASK_STATUSES = ["受信箱", "未着手", "対応中", "保留", "完了"]  # カンバン列。受信箱=未整理(Triage)
TASK_OPEN_STATUSES = ["受信箱", "未着手", "対応中", "保留"]        # 完了以外
TASK_PRIORITIES = ["高", "中", "低"]

# 事務員向けタスク（is_admin=1）。各メンバーから事務員に降ってくる依頼を分離管理する専用ビュー
# (/desk-tasks) 用。カテゴリは開発系タスク(TASK_CATEGORIES)とは別体系。
ADMIN_TASK_CATEGORIES = ["書類作成", "経費・請求", "予約・手配", "データ入力", "連絡・調整", "庶務", "その他"]
# 事務タスクの事務員（複数可）。環境変数 DESK_ASSIGNEE をカンマ/読点/空白区切りで解釈。
# 先頭＝既定担当（原則この人に自動割当）、残り＝パス先候補（例: "あみ,磯部" → 既定=あみ、磯部にパス可）。
# 特定個人名をハードコードしない（owners マスタに存在する担当名を想定）。
DESK_ASSIGNEES = [x.strip() for x in
                  os.environ.get("DESK_ASSIGNEE", "").replace("、", ",").replace(" ", ",").split(",")
                  if x.strip()]
DESK_ASSIGNEE_DEFAULT = DESK_ASSIGNEES[0] if DESK_ASSIGNEES else ""
TASK_LINK_TYPES = ["dev_project", "deal", "issue", "org", "personal"]
TASK_LINK_LABELS = {"dev_project": "開発案件", "deal": "商談", "issue": "論点",
                    "org": "全社", "personal": "個人"}


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
    """開発期間（営業日）。ステージ倍率: プロト×1 / PoC×2 / 本番×2。
    ※Hisho側 dashboard.html devPeriodDays() と必ず同一係数に保つこと（INTEGRATION.md (b)）。"""
    mult = 2 if stage == "PoC" else 2 if stage == "本番" else 1
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
    client_contact TEXT,              -- 先方（顧客側）担当者名
    client_dept TEXT,                 -- 先方 部署
    exhibition_name TEXT,             -- 展示会由来(lead_pattern='Exh.')の場合、どの展示会か
    value_lumpsum REAL,               -- 単発総額（万円）
    value_lumpsum_monthly REAL,       -- 単発月額（万円）
    value_recurring REAL,             -- 継続月額（万円）
    client_budget TEXT,
    next_milestone_date TEXT,
    next_milestone_label TEXT,
    next_milestone_type TEXT,         -- 次回MSの種別: アポ / タスク。Slack日次通知は「アポ」(および未設定)のみ対象
    note TEXT,
    rich_note TEXT,                   -- OneNote風リッチメモ(#70)。サニタイズ済みHTML。noteとは別枠(自動追記で汚れないノート)
    goal TEXT,
    importance TEXT,                  -- 重要度: 高/中/低
    status TEXT DEFAULT 'open',       -- open / closed
    close_reason TEXT,                -- 終了理由: ニーズなし/キャンセル/失注/自社都合で撤退/保留・時期尚早
    cost_stage TEXT,                  -- コスト削減ステージ（L1=コスト削減のみ）
    approach_value REAL,              -- アプローチ額（億円）
    approach_rate REAL,               -- アプローチ率(%)
    reduction_rate REAL,              -- コスト削減率(%)
    fee_rate REAL,                    -- 成果報酬率(%)
    diagnosis_cost REAL,              -- 診断原価（万円）
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 次回マイルストーン（1商談:N。#48）。deals.next_milestone_* は「未完了で最も古い1件」のキャッシュ。
CREATE TABLE IF NOT EXISTS deal_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    ms_date TEXT,                     -- YYYY-MM-DD
    ms_label TEXT,
    ms_type TEXT,                     -- アポ / タスク
    done INTEGER DEFAULT 0,           -- 0=未完了 / 1=完了（集計対象は未完了のみ）
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deal_milestones_deal ON deal_milestones(deal_id);

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
    lost_reason     TEXT,                 -- lead_status=lost時の終了理由(CLOSE_REASONS)。商談化前のキャンセル等
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
    dev_audience     TEXT,                 -- 提供先: 社外向け/社内向け/研究（#41）
    work_type        TEXT,                 -- 作業種別（点数マスタのキー。例: 新規フロントエンド・本番向けツール等・#41）
    pricing          TEXT,                 -- 課金: 無償/有償（分類ディメンション・点数計算には未使用・#41）
    dev_points       REAL,                 -- 開発点数(工数)。作業種別×分類係数×難易度係数で自動付与→手動調整可（#41）
    dev_owner        TEXT,                 -- 開発担当（メンバー選択）
    tech_support     TEXT,                 -- 技術サポート（自由記述）
    dev_milestone    TEXT,                 -- 開発MS（自由記述ラベル）
    dev_milestone_date TEXT,               -- 開発MS日（YYYY-MM-DD）
    deadline         TEXT,                 -- 期限（YYYY-MM-DD）。ガント上では変更不可、SFAでのみ変更する
    dev_start_date   TEXT,                 -- 開発開始日（起票時に期限から自動計算。以後はHisho側の手動調整に委ねる）
    dev_end_date     TEXT,                 -- 開発終了日（起票時のデフォルトは期限と同値）
    dev_policy       TEXT,                 -- 開発方針（自由記述）
    tech_seeds       TEXT,                 -- 必要な技術シード（マスタ tech_seeds からの複数選択・カンマ区切り, #46）
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

-- OneNote風リッチメモ(#70)。任意のエンティティ(kind, entity_id)に複数枚のノートをぶら下げる。
-- kind='deal'|'issue'|'htmpl' 等。title 空=無題。body=サニタイズ済みHTML。
CREATE TABLE IF NOT EXISTS rich_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    entity_id  INTEGER NOT NULL,
    title      TEXT,
    body       TEXT,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rich_notes_entity ON rich_notes(kind, entity_id);

-- タスク管理（#30）。開発案件/商談/論点等にひも付け可能。受信箱=未整理(Triage)。
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,           -- タスク名（中項目）
    detail        TEXT,                    -- 文脈・補足
    project       TEXT,                    -- 大項目＝プロジェクト（task_projectsマスタ）
    next_action   TEXT,                    -- 次アクション（常時1行表示。空なら止まっている合図）
    assignee      TEXT,                    -- 担当（owner名）
    due_date      TEXT,                    -- 期限 YYYY-MM-DD
    status        TEXT DEFAULT '受信箱',    -- 受信箱/未着手/対応中/保留/完了
    priority      TEXT DEFAULT '中',        -- 高/中/低（旧・優先度。緊急度は期限から自動算出へ移行）
    pinned        INTEGER DEFAULT 0,       -- ★最優先ピン（手動・例外用）。緊急度自動化の上書き
    category      TEXT,                    -- 種類（task_categoriesマスタ・AI自動判定）
    is_admin      INTEGER DEFAULT 0,       -- 事務タスク判別（1=事務員向け /desk-tasks）
    requester     TEXT,                    -- 依頼者（事務タスクで「誰から降ってきたか」）
    summary       TEXT,                    -- 議論メモのAIサマリ（追記のたび再生成）
    summary_at    TEXT,                    -- サマリ生成時刻
    link_type     TEXT,                    -- dev_project/deal/issue/org/personal
    link_id       INTEGER,                 -- 紐付け先ID
    source        TEXT DEFAULT 'web',      -- web/slack/ai
    slack_channel TEXT,
    slack_ts      TEXT,
    slack_permalink TEXT,                  -- 起票元Slackメッセージへのpermalink（事務タスク等）
    created_by    TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    done_at       TEXT,
    remind_last_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_link ON tasks(link_type, link_id);
-- 注: idx_tasks_project は project列に依存するため、init_db()の列追加(ALTER)後に作成する
-- （既存DBではSCHEMA実行時点でproject列が無く、ここに置くと executescript が失敗する）。

-- タスクの追記式ログ（履歴が残る）。kind='progress'(進捗) / 'discussion'(議論メモ)。
-- 議論メモを追記するとタスクのAIサマリ(tasks.summary)が再生成される（論点管理の吸収, #30）。
CREATE TABLE IF NOT EXISTS task_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    author     TEXT,
    kind       TEXT DEFAULT 'progress',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_notes_task ON task_notes(task_id);

-- タスクの大項目＝プロジェクト（取り組み）。期限＋状態を持つ管理対象（#30）。
-- tasks.project は名前(name)で緩く参照する。
CREATE TABLE IF NOT EXISTS task_projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    deadline   TEXT,                        -- 期限 YYYY-MM-DD（タスク期日の逆算推奨に使う）
    status     TEXT DEFAULT '進行中',        -- 進行中/保留/完了
    sort_order INTEGER DEFAULT 0,
    summary    TEXT,                         -- PJ全体のAIサマリ（配下タスクの議論＋進捗を俯瞰要約）
    summary_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- 商談への添付ファイル（実体は保存せずSharePoint等の外部リンクのみ保持）
CREATE TABLE IF NOT EXISTS deal_attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id    INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    url        TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deal_attachments_deal ON deal_attachments(deal_id);

-- 開発案件の「追加ツールリンク」（主リンクは dev_projects.tool_url。2つ目以降をここに複数保持）。
-- 主リンクのみHishoへ同期し、追加リンクはSFA内表示専用（連携契約を崩さないための分離）。
CREATE TABLE IF NOT EXISTS dev_project_tools (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_project_id INTEGER NOT NULL REFERENCES dev_projects(id) ON DELETE CASCADE,
    label          TEXT,
    url            TEXT NOT NULL,
    login_id       TEXT,
    login_pass     TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dev_project_tools_dp ON dev_project_tools(dev_project_id);

-- Hisho同期の失敗記録（printで消えていた失敗を永続化し、後から再同期・可視化する）
CREATE TABLE IF NOT EXISTS sync_failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,   -- 'deal' | 'dev_project' | 'dev_project_delete'
    ref_id     INTEGER NOT NULL,-- 対象のSFA側ID（dev_project_delete時はhisho_id）
    error      TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(kind, ref_id)
);

-- 週次スナップショット（前週比のための時系列記録。週ごと×指標ごとに1行）
CREATE TABLE IF NOT EXISTS weekly_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start  TEXT NOT NULL,   -- その週の月曜(YYYY-MM-DD)
    metric_key  TEXT NOT NULL,   -- 例: open_deals / pipeline_lump / stage_count:提案 / leads_active ...
    metric_value REAL,
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(week_start, metric_key)
);

-- 週次営業レポートの本文（社外秘: クライアント名・金額を含むためGitには置かず、
-- 永続ディスク上のこのDBにのみ格納する。/reports で既存Basic認証越しに配信）
CREATE TABLE IF NOT EXISTS weekly_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,   -- URLスラッグ（英数と-_のみ）例: 2026-07-12
    report_date TEXT,                   -- 一覧表示用の期間文字列 例: 2026.7.6 – 7.12
    week_start  TEXT,                   -- 対象週の月曜(YYYY-MM-DD)。数字レール自動注入の基準週（#39）
    title       TEXT,                   -- 号の表題
    lead        TEXT,                   -- 一覧に出す「一言」
    cover_image TEXT,                   -- カバー画像（data: URI。一覧サムネ＋記事hero。無ければ既定の装飾）
    html_body   TEXT NOT NULL,          -- 号の本文HTML（アプリ共通ガワの中に差し込む本文fragment）
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- 開発点数マスタ（作業種別→基準点数。#41。経験曲線＝この基準値を手動で下げていく）
CREATE TABLE IF NOT EXISTS dev_point_master (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_type   TEXT NOT NULL UNIQUE,     -- 作業種別（例: 新規フロントエンド・本番向けツール）
    base_points REAL NOT NULL,            -- 基準点数（分類係数=1・難易度=中の想定）
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- 開発担当の週次キャパ（週次上限点数。#41。負荷率＝週配分点数÷上限）
-- 特定週以降で上限が変わるケースに対応（change_from_week 以降は weekly_max_points2 を使う）
CREATE TABLE IF NOT EXISTS dev_owner_capacity (
    owner              TEXT PRIMARY KEY,  -- 開発担当名（owners マスタの値）
    weekly_max_points  REAL NOT NULL,     -- 週次上限点数（基準）
    change_from_week   TEXT,              -- 上限変更週(from)。この週(を含む週)以降 points2 を使う（YYYY-MM-DD）
    weekly_max_points2 REAL,              -- 変更後の週次上限点数
    updated_at         TEXT DEFAULT (datetime('now'))
);

-- 開発点数の係数マスタ（既存分類/難易度の係数を管理画面で編集可能にする。#41-②）
CREATE TABLE IF NOT EXISTS dev_coefficient (
    coef_type  TEXT NOT NULL,             -- 'stage' | 'difficulty'
    coef_key   TEXT NOT NULL,             -- プロト/PoC/本番 | 易/中/難
    coef_value REAL NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (coef_type, coef_key)
);

-- Delivery（受注後・納品）案件（#75）。デモ開発(dev_projects)とは別系統。商談が「提案」到達で自動起票。
-- 1商談に複数可（deal_id は非UNIQUE）。見込み/確定の確度は紐づく deal.stage から都度導出（専用列は持たない）。
CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id     INTEGER NOT NULL,          -- 紐づく商談（非UNIQUE）
    title       TEXT,                      -- 納品案件名（既定=商談名）
    start_week  TEXT,                      -- 開始週の月曜(YYYY-MM-DD)
    end_week    TEXT,                      -- 終了週の月曜(YYYY-MM-DD)
    status      TEXT DEFAULT '進行中',      -- 進行中/完了/保留
    overview    TEXT,                      -- 概要・納品方針（自由記述）
    fee_mode    TEXT DEFAULT 'monthly',    -- 報酬形態: monthly=月額報酬 / total=総額報酬（どちらを入力するか）
    fee_monthly REAL,                      -- 報酬額/月額（万円）
    fee_total   REAL,                      -- 報酬額/総額（万円）。期間の月数で月額と相互換算
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (deal_id) REFERENCES deals(id) ON DELETE CASCADE
);

-- アサインブロック（#75）。入力の最小単位＝(メンバー・開始週〜終了週・FTE%)。
-- 週へは読み出し時に展開・合算する（20週手打ち回避）。特定週調整は from=to の1週ブロック。
CREATE TABLE IF NOT EXISTS delivery_assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id  INTEGER NOT NULL,
    role         TEXT,                     -- 役割（PM/エンジニア等・自由入力。体制欄から生成）
    member_kind  TEXT DEFAULT '内部',       -- 内部/外部。内部=担当者マスタ選択、外部=自由記述
    owner        TEXT NOT NULL,            -- メンバー（内部=マスタ名／外部=自由記述。未定は空文字可）
    from_week    TEXT NOT NULL,            -- 開始週の月曜(YYYY-MM-DD)
    to_week      TEXT NOT NULL,            -- 終了週の月曜(YYYY-MM-DD)
    fte_pct      REAL NOT NULL DEFAULT 0,  -- 稼働率(実想定)(%) 0〜（100超も可＝過負荷）。負荷計算はこちら
    fte_billing  REAL,                     -- 稼働率(請求)(%)。請求上の稼働。NULL=実想定と同値扱い
    note         TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE
);

-- Delivery体制（#75）。役割ごとの目標稼働率（請求/実想定）。
-- アサインメンバーの役割別合計がこの目標と一致しない場合、編集画面で稼働率欄をハイライトする。
CREATE TABLE IF NOT EXISTS delivery_roles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id  INTEGER NOT NULL,
    role         TEXT NOT NULL,            -- 役割（自由入力）
    fte_billing  REAL,                     -- 目標 稼働率(請求)%
    fte_pct      REAL,                     -- 目標 稼働率(実想定)%
    created_at   TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE
);

-- ベース最大稼働率（#75）。その人がInProcに割ける最大稼働率(%)。稼働予定の負荷率の分母。
-- 未設定は100%扱い。例: 週2の人=40%（総工数40%で負荷率100%表示）。
CREATE TABLE IF NOT EXISTS owner_base_max (
    owner       TEXT PRIMARY KEY,   -- メンバー（OWNERS）
    max_pct     REAL NOT NULL DEFAULT 100,
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- ベース最大稼働率の期間版（#75）。owner_base_max（単一）を期間で上書きできるようにする。
-- from_week/to_week は月曜(YYYY-MM-DD)。to_week 空=以降ずっと継続。過去分は基本編集しない運用。
CREATE TABLE IF NOT EXISTS owner_base_max_periods (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    from_week   TEXT,               -- 開始週(月曜)。空=下限なし
    to_week     TEXT,               -- 終了週(月曜)。空=継続
    max_pct     REAL NOT NULL DEFAULT 100,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_obmp_owner ON owner_base_max_periods(owner, from_week);

-- ベース工数（#75）。案件に紐づかない恒常稼働（人×機能×%）。例: 早瀬 営業 30%。
-- functionは自由入力。正本SFA＋Hishoからの書き戻し(POST /api/base_workload)で両方編集。
CREATE TABLE IF NOT EXISTS base_workload (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,             -- メンバー（OWNERS）
    function    TEXT NOT NULL,             -- 機能（自由入力: 営業/管理/採用 等）
    pct         REAL NOT NULL DEFAULT 0,   -- 恒常稼働率(%)
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(owner, function)
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
            ("lost_reason", "TEXT"),
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
            ("next_milestone_type", "TEXT"),
            ("close_reason", "TEXT"),
            ("client_contact", "TEXT"),
            ("client_dept", "TEXT"),
            ("exhibition_name", "TEXT"),
            ("rich_note", "TEXT"),
        ]:
            if col not in deal_cols:
                con.execute(f"ALTER TABLE deals ADD COLUMN {col} {typedef}")
        # 旧: deals.rich_note（単一メモ）→ rich_notes（タイトル付き複数メモ）へ一度だけ移行（冪等）。
        if "rich_note" in deal_cols or True:  # 列は上のALTERで必ず存在
            con.execute(
                "INSERT INTO rich_notes (kind, entity_id, title, body, sort_order, created_at, updated_at) "
                "SELECT 'deal', d.id, NULL, d.rich_note, 0, datetime('now'), datetime('now') FROM deals d "
                "WHERE d.rich_note IS NOT NULL AND trim(d.rich_note) <> '' "
                "AND NOT EXISTS (SELECT 1 FROM rich_notes r WHERE r.kind='deal' AND r.entity_id=d.id)")
        # 旧: deal_issue_memos（追記式メモ）→ rich_notes(kind='issue') へ一度だけ集約移行（冪等）。
        # 論点ごとに全メモを「旧メモ（移行）」1ノートへ時系列で連結（各行=日時付きの段落）。
        # HTMLとして描画されるため <,&,> をエスケープし、改行は <br> にする。
        con.execute(
            "INSERT INTO rich_notes (kind, entity_id, title, body, sort_order, created_at, updated_at) "
            "SELECT 'issue', issue_id, '旧メモ（移行）', group_concat(line, ''), 0, "
            "  datetime('now'), datetime('now') FROM ("
            "  SELECT m.issue_id AS issue_id, "
            "    '<p>・(' || substr(COALESCE(m.created_at,''),1,16) || ') ' || "
            "    replace(replace(replace(replace(COALESCE(m.body,''),'&','&amp;'),'<','&lt;'),'>','&gt;'),"
            "      char(10),'<br>') || '</p>' AS line "
            "  FROM deal_issue_memos m WHERE trim(COALESCE(m.body,'')) <> '' "
            "  ORDER BY m.issue_id, m.created_at, m.id"
            ") GROUP BY issue_id "
            "HAVING issue_id NOT IN "
            "  (SELECT entity_id FROM rich_notes WHERE kind='issue' AND title='旧メモ（移行）')")
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
        # 開発点数機能（#41）: 提供先/作業種別/課金/点数を後方互換追加
        for col, typedef in (("dev_audience", "TEXT"), ("work_type", "TEXT"),
                             ("pricing", "TEXT"), ("dev_points", "REAL")):
            if col not in dp_cols:
                con.execute(f"ALTER TABLE dev_projects ADD COLUMN {col} {typedef}")
        # 技術シード機能（#46）: 必要な技術シード（カンマ区切り）を後方互換追加
        if "tech_seeds" not in dp_cols:
            con.execute("ALTER TABLE dev_projects ADD COLUMN tech_seeds TEXT")
        # 失注クローズ済みだがステージが失注/受注でない既存商談を stage='失注' に補正（冪等・表示整合）。
        con.execute(
            "UPDATE deals SET stage='失注' WHERE status='closed' AND close_reason='失注' "
            "AND COALESCE(stage,'') NOT IN ('失注','受注')")
        # tasks に project(大項目)・next_action(次アクション) を後方互換追加（#30・前回デプロイ後の追加列）
        _task_cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        if _task_cols and "project" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN project TEXT")
        if _task_cols and "next_action" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN next_action TEXT")
        if _task_cols and "pinned" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN pinned INTEGER DEFAULT 0")
        if _task_cols and "summary" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN summary TEXT")
            con.execute("ALTER TABLE tasks ADD COLUMN summary_at TEXT")
        # 事務員向けタスク（/desk-tasks）用の後方互換追加。破壊的変更はしない。
        if _task_cols and "is_admin" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN is_admin INTEGER DEFAULT 0")
        if _task_cols and "requester" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN requester TEXT")
        if _task_cols and "slack_permalink" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN slack_permalink TEXT")
        # project列が存在する状態でインデックスを作る（SCHEMAではなくここで＝既存DBでも安全）
        if _task_cols:
            con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project)")
        # task_notes.kind（進捗/議論メモの区別）を後方互換追加（#30）
        _tn_cols = {r[1] for r in con.execute("PRAGMA table_info(task_notes)")}
        if _tn_cols and "kind" not in _tn_cols:
            con.execute("ALTER TABLE task_notes ADD COLUMN kind TEXT DEFAULT 'progress'")
        # task_projects.summary（PJ全体のAIサマリ）を後方互換追加（#30）
        _tp_cols = {r[1] for r in con.execute("PRAGMA table_info(task_projects)")}
        if _tp_cols and "summary" not in _tp_cols:
            con.execute("ALTER TABLE task_projects ADD COLUMN summary TEXT")
            con.execute("ALTER TABLE task_projects ADD COLUMN summary_at TEXT")
        # weekly_reports に cover_image を後方互換追加（前回デプロイ時は列が無かった）
        wr_cols = {r[1] for r in con.execute("PRAGMA table_info(weekly_reports)")}
        if wr_cols and "cover_image" not in wr_cols:
            con.execute("ALTER TABLE weekly_reports ADD COLUMN cover_image TEXT")
        if wr_cols and "week_start" not in wr_cols:
            con.execute("ALTER TABLE weekly_reports ADD COLUMN week_start TEXT")
        # dev_owner_capacity に上限変更(2段階)列を後方互換追加（#42）
        _cap_cols = {r[1] for r in con.execute("PRAGMA table_info(dev_owner_capacity)")}
        if _cap_cols and "change_from_week" not in _cap_cols:
            con.execute("ALTER TABLE dev_owner_capacity ADD COLUMN change_from_week TEXT")
        if _cap_cols and "weekly_max_points2" not in _cap_cols:
            con.execute("ALTER TABLE dev_owner_capacity ADD COLUMN weekly_max_points2 REAL")
        # Delivery アサイン: 稼働率を「実想定(fte_pct)」＋「請求(fte_billing)」の2本立てに（#75）。
        # 既存行は請求=実想定で初期化。
        _da_cols = {r[1] for r in con.execute("PRAGMA table_info(delivery_assignments)")}
        if _da_cols and "fte_billing" not in _da_cols:
            con.execute("ALTER TABLE delivery_assignments ADD COLUMN fte_billing REAL")
            con.execute("UPDATE delivery_assignments SET fte_billing=fte_pct WHERE fte_billing IS NULL")
        if _da_cols and "role" not in _da_cols:
            con.execute("ALTER TABLE delivery_assignments ADD COLUMN role TEXT")
        if _da_cols and "member_kind" not in _da_cols:
            con.execute("ALTER TABLE delivery_assignments ADD COLUMN member_kind TEXT DEFAULT '内部'")
            con.execute("UPDATE delivery_assignments SET member_kind='内部' WHERE member_kind IS NULL")
        # Delivery 報酬額（#75）: 月額 or 総額を入力し、期間の月数で相互換算して両方保持。
        _dv_cols = {r[1] for r in con.execute("PRAGMA table_info(deliveries)")}
        if _dv_cols and "fee_mode" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN fee_mode TEXT DEFAULT 'monthly'")
        if _dv_cols and "fee_monthly" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN fee_monthly REAL")
        if _dv_cols and "fee_total" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN fee_total REAL")
        # 開発点数マスタ・係数の初期シード（空のときのみ・#41）
        seed_dev_point_master(con)
        seed_dev_coefficients(con)
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


# ---- 技術シードのツリー（L1カテゴリ→L2シード, #60）。masters テーブルに JSON で保持 ----

def get_tech_seed_tree(con) -> dict:
    """{L1: [L2, ...]} を挿入順で返す。未保存ならデフォルト(TECH_SEED_TREE_DEFAULT)。"""
    row = con.execute("SELECT values_json FROM masters WHERE key='tech_seed_tree'").fetchone()
    if row:
        try:
            data = _json.loads(row[0])
            if isinstance(data, dict):
                return {str(k): [str(x) for x in (v or [])] for k, v in data.items()}
        except (ValueError, TypeError):
            print("[masters] tech_seed_tree broken, falling back to default", flush=True)
    return {k: list(v) for k, v in TECH_SEED_TREE_DEFAULT.items()}


def set_tech_seed_tree(con, tree: dict) -> None:
    """ツリーを保存。空のL1名は除外、各L1内のL2は重複除去（順序保持）。"""
    clean: dict = {}
    for k, v in tree.items():
        k = str(k).strip()
        if not k:
            continue
        seen: set = set()
        items: list = []
        for x in (v or []):
            x = str(x).strip()
            if x and x not in seen:
                seen.add(x)
                items.append(x)
        clean[k] = items
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES('tech_seed_tree',?) "
        "ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (_json.dumps(clean, ensure_ascii=False),),
    )
    con.commit()


def tech_seed_leaves(con) -> list[str]:
    """全L2シードを平坦に返す。"""
    out: list = []
    for v in get_tech_seed_tree(con).values():
        out.extend(v)
    return out


def tech_seed_l1_of(con) -> dict:
    """L2シード -> L1カテゴリ のマップ（表示グルーピング用。重複時は先勝ち）。"""
    m: dict = {}
    for l1, leaves in get_tech_seed_tree(con).items():
        for leaf in leaves:
            m.setdefault(leaf, l1)
    return m


# ---- 事業種別のツリー（L1→L2）。tech_seed_tree と同方式で masters に JSON 保持 ----
# 以前はコード内定数 BUSINESS_TYPE_L2_BY_L1 で固定だったが、マスタ画面で編集可能にした。

def get_business_type_tree(con) -> dict:
    """事業種別 {L1: [L2, ...]} を挿入順で返す。未保存なら現行L1マスタ×既定L2から構築。"""
    row = con.execute("SELECT values_json FROM masters WHERE key='business_type_tree'").fetchone()
    if row:
        try:
            data = _json.loads(row[0])
            if isinstance(data, dict):
                return {str(k): [str(x) for x in (v or [])] for k, v in data.items()}
        except (ValueError, TypeError):
            print("[masters] business_type_tree broken, falling back to default", flush=True)
    # フォールバック: 現行L1フラットマスタのキー順 × 既定L2（定数）で構築。
    l1_keys = get_master_list(con, "business_type_l1") or list(BUSINESS_TYPE_L1)
    return {l1: list(BUSINESS_TYPE_L2_BY_L1.get(l1, [])) for l1 in l1_keys}


def set_business_type_tree(con, tree: dict) -> None:
    """ツリーを保存。空L1名は除外・各L1内L2は重複除去(順序保持)。
    既存コードが参照する business_type_l1 フラットマスタも、ツリーのキーへ同期する。"""
    clean: dict = {}
    for k, v in tree.items():
        k = str(k).strip()
        if not k:
            continue
        seen: set = set()
        items: list = []
        for x in (v or []):
            x = str(x).strip()
            if x and x not in seen:
                seen.add(x)
                items.append(x)
        clean[k] = items
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES('business_type_tree',?) "
        "ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (_json.dumps(clean, ensure_ascii=False),),
    )
    # L1フラットマスタをツリーのキーに同期（get_master_list('business_type_l1') 経路を維持）。
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES('business_type_l1',?) "
        "ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (_json.dumps(list(clean.keys()), ensure_ascii=False),),
    )
    con.commit()


def business_type_l2_of(con, l1: str | None) -> list:
    """指定L1配下のL2一覧。"""
    return list(get_business_type_tree(con).get(l1 or "", []))


def get_owner_domain_map(con) -> dict:
    """担当者→担当領域 のマップ（masters key='owner_domain_map' にJSON保持）。未設定は空dict。"""
    row = con.execute("SELECT values_json FROM masters WHERE key='owner_domain_map'").fetchone()
    if row:
        try:
            data = _json.loads(row[0])
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if str(v).strip()}
        except (ValueError, TypeError):
            print("[masters] owner_domain_map broken, ignoring", flush=True)
    return {}


def set_owner_domain_map(con, mapping: dict) -> None:
    """担当者→担当領域 を保存。空の領域は除外（＝未設定扱い）。"""
    clean = {str(k).strip(): str(v).strip() for k, v in mapping.items()
             if str(k).strip() and str(v).strip()}
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES('owner_domain_map',?) "
        "ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (_json.dumps(clean, ensure_ascii=False),),
    )
    con.commit()


def owner_domain_of(con, owner: str) -> str:
    """指定担当者の担当領域（未設定は空文字）。"""
    return get_owner_domain_map(con).get(owner or "", "")


def get_industry_target_map(con) -> dict:
    """業界→ターゲット領域 のマップ（masters key='industry_target_map' にJSON保持）。未設定は空dict。"""
    row = con.execute("SELECT values_json FROM masters WHERE key='industry_target_map'").fetchone()
    if row:
        try:
            data = _json.loads(row[0])
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if str(v).strip()}
        except (ValueError, TypeError):
            print("[masters] industry_target_map broken, ignoring", flush=True)
    return {}


def set_industry_target_map(con, mapping: dict) -> None:
    """業界→ターゲット領域 を保存。空の領域は除外（＝未設定扱い）。"""
    clean = {str(k).strip(): str(v).strip() for k, v in mapping.items()
             if str(k).strip() and str(v).strip()}
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES('industry_target_map',?) "
        "ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (_json.dumps(clean, ensure_ascii=False),),
    )
    con.commit()


def target_domain_of_industry(con, industry: str) -> str:
    """指定業界のターゲット領域（未設定は空文字）。業界を持つ各所で自動付随させる用。"""
    return get_industry_target_map(con).get(industry or "", "")


def business_type_l2_all(con) -> list:
    """全L2を平坦に返す（重複除去・順序保持）。フィルタや一括編集の選択肢用。"""
    seen: set = set()
    out: list = []
    for leaves in get_business_type_tree(con).values():
        for x in leaves:
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


# ---- 取得系 ----
def list_accounts(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM accounts ORDER BY name")]


def find_duplicate_accounts(con) -> list[dict]:
    """同名（前後空白を無視）のアカウントが複数あるグループを返す。
    各グループに所属アカウントと、それぞれの商談数・コンタクト数を付ける。"""
    rows = [dict(r) for r in con.execute(
        "SELECT id, name, industry, company_size FROM accounts ORDER BY name, id")]
    groups: dict = {}
    for r in rows:
        key = (r.get("name") or "").strip()
        groups.setdefault(key, []).append(r)
    out = []
    for name, accts in groups.items():
        if len(accts) < 2:
            continue
        for a in accts:
            a["deal_count"] = con.execute(
                "SELECT COUNT(*) c FROM deals WHERE account_id=?", (a["id"],)).fetchone()["c"]
            a["contact_count"] = con.execute(
                "SELECT COUNT(*) c FROM contacts WHERE account_id=?", (a["id"],)).fetchone()["c"]
        out.append({"name": name, "accounts": accts})
    return out


def merge_accounts(con, keep_id: int, drop_ids: list[int]) -> dict:
    """drop_ids のアカウントを keep_id へ統合する。
    商談・コンタクトの account_id を付け替えてから、drop側アカウントを削除する。
    keep_id は drop_ids に含めないこと。戻り値: 付け替え件数。"""
    keep_id = int(keep_id)
    moved_deals = moved_contacts = 0
    for did in drop_ids:
        did = int(did)
        if did == keep_id:
            continue
        cur = con.execute("UPDATE deals SET account_id=? WHERE account_id=?", (keep_id, did))
        moved_deals += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur = con.execute("UPDATE contacts SET account_id=? WHERE account_id=?", (keep_id, did))
        moved_contacts += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        con.execute("DELETE FROM accounts WHERE id=?", (did,))
    con.commit()
    return {"moved_deals": moved_deals, "moved_contacts": moved_contacts,
            "dropped": len([d for d in drop_ids if int(d) != keep_id])}


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
    """指定日が次回MS日付、または活動履歴の実施日のいずれかに一致する商談を返す。
    過去の振り返り用途のためクローズ済み商談も含める（statusで絞らない）。"""
    q = """SELECT DISTINCT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE (d.next_milestone_date = ?
                  OR EXISTS (SELECT 1 FROM activities act WHERE act.deal_id = d.id AND act.occurred_on = ?))"""
    params: list = [date, date]
    if owner:
        q += " AND (d.owner = ? OR d.sub_owner = ?)"
        params.append(owner)
        params.append(owner)
    q += " ORDER BY a.name"
    return [dict(r) for r in con.execute(q, params)]


def list_deals_by_week(con, week_start: str, week_end: str, owner: str | None = None) -> list[dict]:
    """週(week_start〜week_end, 両端含む)に次回MS日付 または 活動実施日 が含まれる商談を返す。
    過去の振り返り用途のためクローズ済み商談も含める（statusで絞らない）。"""
    q = """SELECT DISTINCT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE ((d.next_milestone_date BETWEEN ? AND ?)
                  OR EXISTS (SELECT 1 FROM activities act WHERE act.deal_id = d.id
                             AND act.occurred_on BETWEEN ? AND ?))"""
    params: list = [week_start, week_end, week_start, week_end]
    if owner:
        q += " AND (d.owner = ? OR d.sub_owner = ?)"
        params.append(owner)
        params.append(owner)
    q += " ORDER BY d.next_milestone_date, a.name"
    return [dict(r) for r in con.execute(q, params)]


def list_overdue_deals(con, owner: str | None = None, today: str | None = None,
                       exclude_today: bool = False) -> list[dict]:
    """次回MSが超過した、または次回MSが未設定の進行中商談を返す（＝要フォロー一覧）。

    - 超過: next_milestone_date <= 当日
    - 未設定: next_milestone_date が NULL または空文字（次回アクション未定＝止まっている）
    - exclude_today=True: 次回MS日が「当日ちょうど」の商談を除外し、前日以前の超過のみにする
      （＝まだ当日で対応余地があるものを一覧から外す）。未設定(NULL/空)は引き続き含める。
    並び順は「超過（遅れている順に日付昇順）→ 未設定（末尾）」。
    """
    today = today or date.today().isoformat()
    # 受注/失注（決着済み）は次回MSが無くても要フォローに含めない
    _concluded_ph = ", ".join("?" for _ in CONCLUDED_DEAL_STAGES)
    _date_cmp = "<" if exclude_today else "<="
    q = f"""SELECT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE (d.status IS NULL OR d.status != 'closed')
                 AND (d.stage IS NULL OR d.stage NOT IN ({_concluded_ph}))
                 AND (d.next_milestone_date IS NULL OR d.next_milestone_date = ''
                      OR d.next_milestone_date {_date_cmp} ?)"""
    params: list = list(CONCLUDED_DEAL_STAGES) + [today]
    if owner:
        q += " AND (d.owner = ? OR d.sub_owner = ?)"
        params.append(owner)
        params.append(owner)
    # 未設定(NULL/空)は末尾へ。超過分は日付昇順（最も遅れている順）。
    q += (" ORDER BY (d.next_milestone_date IS NULL OR d.next_milestone_date = '') ASC,"
          " d.next_milestone_date ASC")
    return [dict(r) for r in con.execute(q, params)]


def list_lost_stage_deals(con) -> list[dict]:
    """ステージ='失注' の残存商談（#67移行の対象/確認用）。status問わず全件。"""
    return [dict(r) for r in con.execute(
        """SELECT d.*, a.name AS account_name FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE d.stage = '失注'
           ORDER BY (d.status = 'closed') ASC, d.id ASC""")]


def migrate_lost_stage_to_closed(con) -> dict:
    """#67: ステージ='失注' の商談を「クローズ＋close_reason='失注'」にし、対応アカウントを
    『フォロー中リード』として作成/再活性化する（正規の revert_to_lead と同じ思想）。

    - 未クローズならクローズし、close_reasonが空なら'失注'を補完（既存理由は尊重）。
    - 既にクローズ済みでもリード未整備なら整備する（例: 過去に手動でstage='失注'にして閉じた案件）。
    - リードは company（＝アカウント名）一致で重複作成を回避＝冪等。既存リードがあれば
      lead_status='following' に再活性化し、無ければアカウントから新規作成する。
    - リードには deal_id を残さない（残すと convert_lead_to_deal が「商談化済」とみなし再商談化
      できなくなるため。revert_to_lead と同じ扱い）。

    戻り値: {"deal_ids": [...処理した全商談ID...], "newly_closed": n,
             "leads_created": c, "leads_reactivated": r}
    """
    deals = [dict(r) for r in con.execute("SELECT * FROM deals WHERE stage='失注'").fetchall()]
    deal_ids: list[int] = []
    newly_closed = 0
    leads_created = 0
    leads_reactivated = 0
    for deal in deals:
        did = deal["id"]
        deal_ids.append(did)
        acct_row = con.execute("SELECT * FROM accounts WHERE id=?", (deal.get("account_id"),)).fetchone()
        acct = dict(acct_row) if acct_row else {}
        company = acct.get("name")
        # クローズ＆終了理由の補完
        if (deal.get("status") or "open") != "closed":
            newly_closed += 1
        con.execute(
            """UPDATE deals
                 SET status='closed',
                     close_reason=CASE WHEN close_reason IS NULL OR close_reason=''
                                       THEN '失注' ELSE close_reason END,
                     updated_at=datetime('now')
               WHERE id=?""", (did,))
        # フォロー中リードを作成/再活性化（company一致で重複回避＝冪等）
        existing = None
        if company:
            _row = con.execute(
                "SELECT * FROM leads WHERE company=? "
                "ORDER BY (lead_status='following') DESC, id LIMIT 1", (company,)
            ).fetchone()
            existing = dict(_row) if _row else None
        if existing:
            if (existing.get("lead_status") or "") != "following":
                con.execute(
                    "UPDATE leads SET lead_status='following', updated_at=datetime('now') WHERE id=?",
                    (existing["id"],))
                leads_reactivated += 1
        else:
            upsert_lead(
                con, name=company or "（不明）", company=company or "（不明）",
                industry=acct.get("industry"), company_size=acct.get("company_size"),
                lead_status="following",
                notes=f"商談 #{did}（{deal.get('deal_name', '')}）を失注クローズ→リード化(#67)",
                assigned_to=deal.get("owner"))
            leads_created += 1
    con.commit()
    return {"deal_ids": deal_ids, "newly_closed": newly_closed,
            "leads_created": leads_created, "leads_reactivated": leads_reactivated}


def list_untyped_milestone_deals(con) -> list[dict]:
    """次回MSがあるのに種別(next_milestone_type)未設定の進行中商談（種別バックフィル対象）。"""
    return [dict(r) for r in con.execute(
        """SELECT d.*, a.name AS account_name FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE (d.status IS NULL OR d.status != 'closed')
                 AND d.next_milestone_date IS NOT NULL
                 AND (d.next_milestone_type IS NULL OR d.next_milestone_type = '')
           ORDER BY d.next_milestone_date ASC""")]


def bulk_tag_appt_by_label(con, *, after_date: str, label_like: str = "初回アポ") -> int:
    """次回MSが after_date より後 かつ ラベルに label_like を含む 未タグの進行中商談を、
    まとめて種別「アポ」にする。更新件数を返す（一括バックフィル用）。"""
    cur = con.execute(
        "UPDATE deals SET next_milestone_type='アポ', updated_at=datetime('now') "
        "WHERE (status IS NULL OR status != 'closed') "
        "AND (next_milestone_type IS NULL OR next_milestone_type = '') "
        "AND next_milestone_date IS NOT NULL AND next_milestone_date > ? "
        "AND next_milestone_label LIKE ?",
        (after_date, f"%{label_like}%"))
    con.commit()
    return cur.rowcount


def list_unclassified_closed_deals(con) -> list[dict]:
    """終了理由(close_reason)未設定のクローズ済み商談（終了理由バックフィル対象）。noteを添える。"""
    return [dict(r) for r in con.execute(
        """SELECT d.*, a.name AS account_name FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE d.status = 'closed' AND (d.close_reason IS NULL OR d.close_reason = '')
           ORDER BY d.updated_at DESC""")]


def list_unclassified_lost_leads(con) -> list[dict]:
    """終了理由(lost_reason)未設定の lost リード（商談化前キャンセル等のバックフィル対象）。"""
    return [dict(r) for r in con.execute(
        """SELECT * FROM leads
           WHERE lead_status = 'lost' AND (lost_reason IS NULL OR lost_reason = '')
           ORDER BY updated_at DESC""")]


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


def reopen_deal(con, deal_id: int) -> dict:
    """クローズ済み商談を『商談に戻す（再開）』。status='open'・close_reasonを解除。
    リードに戻していた場合は、同一会社の未紐付フォロー中リードを商談へ再紐付け(converted)する。
    revert_to_lead（商談→リード）の逆操作。"""
    deal = get_deal(con, deal_id)
    if not deal:
        return {"reopened": False}
    # 失注クローズ時にstage='失注'にしていた場合、再開時は進行中ステージ(提案)へ戻す（受注は保持）。
    _stg_fix = ", stage='提案'" if (deal.get("stage") == "失注") else ""
    con.execute(
        f"UPDATE deals SET status='open', close_reason=NULL{_stg_fix}, updated_at=datetime('now') WHERE id=?",
        (int(deal_id),))
    relinked = None
    accname = deal.get("account_name")
    if accname:
        row = con.execute(
            "SELECT id, name FROM leads WHERE company=? AND deal_id IS NULL "
            "AND lead_status NOT IN ('converted') ORDER BY updated_at DESC LIMIT 1",
            (accname,)).fetchone()
        if row:
            con.execute(
                "UPDATE leads SET deal_id=?, lead_status='converted', updated_at=datetime('now') WHERE id=?",
                (int(deal_id), row["id"]))
            relinked = row["id"]
    _note = (f"リード「{row['name'] or ''}」から商談に戻す（再開）。" if relinked
             else "商談に戻す（再開）。")
    con.execute("INSERT INTO activities (deal_id, type, occurred_on, body) VALUES (?,?,date('now'),?)",
                (int(deal_id), "メモ", _note))
    con.commit()
    return {"reopened": True, "relinked_lead_id": relinked}


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
    "lead_pattern", "owner", "sub_owner", "client_contact", "client_dept",
    "value_lumpsum", "value_lumpsum_monthly", "value_recurring",
    "client_budget", "next_milestone_date", "next_milestone_label", "next_milestone_type", "note", "goal",
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


# ---- 次回マイルストーン（1商談:N。#48） ----
# deals.next_milestone_date/label/type は「未完了で最も日付の早いMS」のキャッシュ（ミラー）。
# 集計（MS超過・Slack通知・Hisho同期）は従来どおりキャッシュ列を読むため影響範囲が最小。

def list_deal_milestones(con, deal_id: int) -> list[dict]:
    """商談のMSを 未完了→完了 / 日付昇順(未設定は末尾) で返す。"""
    return [dict(r) for r in con.execute(
        "SELECT * FROM deal_milestones WHERE deal_id=? "
        "ORDER BY done ASC, (ms_date IS NULL OR ms_date='') ASC, ms_date ASC, id ASC",
        (int(deal_id),))]


def count_open_milestones(con, deal_ids: list[int]) -> dict:
    """deal_id -> 未完了MS件数。一覧の「ほかN件」バッジ用（0件=レガシー扱い）。"""
    ids = [int(x) for x in deal_ids if x is not None]
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    return {r["deal_id"]: r["c"] for r in con.execute(
        f"SELECT deal_id, COUNT(*) c FROM deal_milestones WHERE done=0 AND deal_id IN ({ph}) "
        f"GROUP BY deal_id", ids)}


def recompute_deal_next_milestone(con, deal_id: int, commit: bool = True) -> None:
    """未完了で最も日付の早いMSを deals.next_milestone_* に反映（キャッシュ更新）。
    未完了で日付ありのMSが無ければキャッシュを空にする。"""
    row = con.execute(
        "SELECT ms_date, ms_label, ms_type FROM deal_milestones "
        "WHERE deal_id=? AND done=0 AND ms_date IS NOT NULL AND ms_date!='' "
        "ORDER BY ms_date ASC, id ASC LIMIT 1", (int(deal_id),)).fetchone()
    if row:
        con.execute("UPDATE deals SET next_milestone_date=?, next_milestone_label=?, "
                    "next_milestone_type=?, updated_at=datetime('now') WHERE id=?",
                    (row["ms_date"], row["ms_label"], row["ms_type"], int(deal_id)))
    else:
        con.execute("UPDATE deals SET next_milestone_date=NULL, next_milestone_label=NULL, "
                    "next_milestone_type=NULL, updated_at=datetime('now') WHERE id=?", (int(deal_id),))
    if commit:
        con.commit()


def set_deal_milestones(con, deal_id: int, items: list[dict], commit: bool = True) -> None:
    """商談のMSを与えられたリストで置き換え（全削除→挿入）→キャッシュ再計算。
    items各要素: {date,label,type,done}。日付もラベルも空の行はスキップ。"""
    con.execute("DELETE FROM deal_milestones WHERE deal_id=?", (int(deal_id),))
    for it in items:
        d = (it.get("date") or "").strip()
        lb = (it.get("label") or "").strip()
        tp = (it.get("type") or "").strip()
        dn = 1 if it.get("done") else 0
        if not d and not lb:
            continue
        con.execute(
            "INSERT INTO deal_milestones (deal_id, ms_date, ms_label, ms_type, done) VALUES (?,?,?,?,?)",
            (int(deal_id), d or None, lb or None, tp or None, dn))
    recompute_deal_next_milestone(con, deal_id, commit=False)
    if commit:
        con.commit()


def set_earliest_milestone_field(con, deal_id: int, field: str, value, commit: bool = True) -> dict:
    """一覧インライン編集用: 未完了で最古のMSの1項目を更新（行が無ければキャッシュ現値を継いで新規作成）。
    field は 'date'|'label'|'type'。キャッシュ再計算後、新キャッシュ値dictを返す。"""
    col = {"date": "ms_date", "label": "ms_label", "type": "ms_type"}[field]
    v = (str(value).strip() or None) if value is not None else None
    row = con.execute(
        "SELECT id FROM deal_milestones WHERE deal_id=? AND done=0 "
        "ORDER BY (ms_date IS NULL OR ms_date='') ASC, ms_date ASC, id ASC LIMIT 1",
        (int(deal_id),)).fetchone()
    if row:
        con.execute(f"UPDATE deal_milestones SET {col}=? WHERE id=?", (v, row["id"]))
    else:
        cur = con.execute("SELECT next_milestone_date, next_milestone_label, next_milestone_type "
                          "FROM deals WHERE id=?", (int(deal_id),)).fetchone()
        base = {"ms_date": cur["next_milestone_date"] if cur else None,
                "ms_label": cur["next_milestone_label"] if cur else None,
                "ms_type": cur["next_milestone_type"] if cur else None}
        base[col] = v
        con.execute("INSERT INTO deal_milestones (deal_id, ms_date, ms_label, ms_type, done) "
                    "VALUES (?,?,?,?,0)",
                    (int(deal_id), base["ms_date"], base["ms_label"], base["ms_type"]))
    recompute_deal_next_milestone(con, deal_id, commit=False)
    if commit:
        con.commit()
    r = con.execute("SELECT next_milestone_date, next_milestone_label, next_milestone_type "
                    "FROM deals WHERE id=?", (int(deal_id),)).fetchone()
    return dict(r) if r else {}


def get_deal_milestone(con, ms_id: int) -> dict | None:
    r = con.execute("SELECT * FROM deal_milestones WHERE id=?", (int(ms_id),)).fetchone()
    return dict(r) if r else None


def add_deal_milestone(con, deal_id: int, *, date=None, label=None, ms_type=None,
                       done: bool = False, commit: bool = True) -> int:
    """MSを1件追加→キャッシュ再計算。追加行のidを返す。"""
    cur = con.execute(
        "INSERT INTO deal_milestones (deal_id, ms_date, ms_label, ms_type, done) VALUES (?,?,?,?,?)",
        (int(deal_id), (date or "").strip() or None, (label or "").strip() or None,
         (ms_type or "").strip() or None, 1 if done else 0))
    recompute_deal_next_milestone(con, deal_id, commit=False)
    if commit:
        con.commit()
    return cur.lastrowid


def update_deal_milestone(con, ms_id: int, field: str, value, commit: bool = True) -> int | None:
    """MS1件の1項目(date|label|type|done)を更新→キャッシュ再計算。所属deal_idを返す（無ければNone）。"""
    col = {"date": "ms_date", "label": "ms_label", "type": "ms_type", "done": "done"}.get(field)
    if not col:
        return None
    r = con.execute("SELECT deal_id FROM deal_milestones WHERE id=?", (int(ms_id),)).fetchone()
    if not r:
        return None
    if field == "done":
        v = 1 if str(value) in ("1", "true", "True", "on") else 0
    else:
        v = (str(value).strip() or None) if value is not None else None
    con.execute(f"UPDATE deal_milestones SET {col}=? WHERE id=?", (v, int(ms_id)))
    recompute_deal_next_milestone(con, r["deal_id"], commit=False)
    if commit:
        con.commit()
    return r["deal_id"]


def delete_deal_milestone(con, ms_id: int, commit: bool = True) -> int | None:
    """MS1件を削除→キャッシュ再計算。所属deal_idを返す（無ければNone）。"""
    r = con.execute("SELECT deal_id FROM deal_milestones WHERE id=?", (int(ms_id),)).fetchone()
    if not r:
        return None
    con.execute("DELETE FROM deal_milestones WHERE id=?", (int(ms_id),))
    recompute_deal_next_milestone(con, r["deal_id"], commit=False)
    if commit:
        con.commit()
    return r["deal_id"]


def ensure_milestones_materialized(con, deal_id: int, commit: bool = True) -> None:
    """MS行が無く、キャッシュ(next_milestone_*)に値があれば1行として実体化する。
    一覧のMSパネル初回表示で、レガシー/単一入力の商談にも行を用意するため。"""
    n = con.execute("SELECT COUNT(*) c FROM deal_milestones WHERE deal_id=?", (int(deal_id),)).fetchone()["c"]
    if n:
        return
    d = con.execute("SELECT next_milestone_date, next_milestone_label, next_milestone_type "
                    "FROM deals WHERE id=?", (int(deal_id),)).fetchone()
    if d and (d["next_milestone_date"] or d["next_milestone_label"]):
        con.execute("INSERT INTO deal_milestones (deal_id, ms_date, ms_label, ms_type, done) "
                    "VALUES (?,?,?,?,0)",
                    (int(deal_id), d["next_milestone_date"], d["next_milestone_label"],
                     d["next_milestone_type"]))
        if commit:
            con.commit()


def upsert_earliest_milestone(con, deal_id: int, *, date, label, ms_type, commit: bool = True) -> None:
    """「現状更新」など単一MS入力用: 未完了で最古のMSを3値で更新（無ければ作成）→キャッシュ再計算。
    3値すべて空なら何もしない（誤クリア防止）。"""
    date = (date or "").strip()
    label = (label or "").strip()
    ms_type = (ms_type or "").strip()
    row = con.execute(
        "SELECT id FROM deal_milestones WHERE deal_id=? AND done=0 "
        "ORDER BY (ms_date IS NULL OR ms_date='') ASC, ms_date ASC, id ASC LIMIT 1",
        (int(deal_id),)).fetchone()
    if not row and not (date or label or ms_type):
        return
    if row:
        con.execute("UPDATE deal_milestones SET ms_date=?, ms_label=?, ms_type=? WHERE id=?",
                    (date or None, label or None, ms_type or None, row["id"]))
    else:
        con.execute("INSERT INTO deal_milestones (deal_id, ms_date, ms_label, ms_type, done) "
                    "VALUES (?,?,?,?,0)", (int(deal_id), date or None, label or None, ms_type or None))
    recompute_deal_next_milestone(con, deal_id, commit=False)
    if commit:
        con.commit()


def add_activity(con, *, deal_id, type=None, occurred_on=None, contact_name=None, body=None) -> int:
    cur = con.execute(
        "INSERT INTO activities (deal_id, type, occurred_on, contact_name, body) VALUES (?,?,?,?,?)",
        (deal_id, type, occurred_on, contact_name, body),
    )
    con.commit()
    return cur.lastrowid


ACTIVITY_EDIT_FIELDS = {"type", "occurred_on", "contact_name", "body"}


def get_activity(con, activity_id: int) -> dict | None:
    r = con.execute("SELECT * FROM activities WHERE id=?", (int(activity_id),)).fetchone()
    return dict(r) if r else None


def update_activity_field(con, activity_id: int, field: str, value) -> None:
    """活動履歴の1項目を更新（whitelistのみ）。"""
    if field not in ACTIVITY_EDIT_FIELDS:
        raise ValueError(f"invalid activity field: {field}")
    con.execute(f"UPDATE activities SET {field}=? WHERE id=?", (value or None, int(activity_id)))
    con.commit()


def delete_activity(con, activity_id: int) -> None:
    con.execute("DELETE FROM activities WHERE id=?", (int(activity_id),))
    con.commit()


def list_exhibition_names(con) -> list[str]:
    """既存の展示会名（distinct・非空）。タグ付け入力のdatalist候補に使う。"""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT exhibition_name FROM deals "
        "WHERE exhibition_name IS NOT NULL AND exhibition_name != '' ORDER BY exhibition_name")]


def list_undated_activities(con) -> list[dict]:
    """日付(occurred_on)が未入力の活動履歴（データ整備・クリーンアップ対象）。
    商談・アカウント名と内容の文脈つきで返す。"""
    return [dict(r) for r in con.execute(
        "SELECT a.id, a.type, a.contact_name, a.body, a.deal_id, "
        "d.deal_name, acc.name AS account_name "
        "FROM activities a "
        "LEFT JOIN deals d ON d.id=a.deal_id "
        "LEFT JOIN accounts acc ON acc.id=d.account_id "
        "WHERE a.occurred_on IS NULL OR a.occurred_on='' "
        "ORDER BY a.deal_id, a.id")]


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
    "resolution", "budget_confirmed", "difficulty", "has_backend",
    "dev_audience", "work_type", "pricing", "dev_points", "dev_owner",
    "tech_support", "dev_milestone", "dev_milestone_date", "deadline", "dev_start_date",
    "dev_end_date", "dev_policy", "tech_seeds", "tool_url", "tool_login_id", "tool_login_pass",
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
    # 点数の自動付与: 明示値が無いときのみ 作業種別×分類係数×難易度係数 で算出（＝手動調整を尊重）
    if data.get("dev_points") in (None, ""):
        _pts = compute_dev_points(
            con, work_type=data.get("work_type"), stage=data.get("stage"),
            difficulty=data.get("difficulty"), has_backend=data.get("has_backend"))
        data["dev_points"] = _pts
    else:
        try:
            data["dev_points"] = float(data["dev_points"])
        except (TypeError, ValueError):
            data["dev_points"] = None
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


# ---- 開発点数マスタ / 担当キャパ（#41） ----

def seed_dev_point_master(con) -> None:
    """点数マスタ（作業種別→基準点数）が空なら既定の作業種別を投入（初回のみ・仮値）。"""
    if con.execute("SELECT COUNT(*) c FROM dev_point_master").fetchone()["c"]:
        return
    for work_type, base in DEV_WORK_TYPE_SEED:
        con.execute(
            "INSERT OR IGNORE INTO dev_point_master (work_type, base_points) VALUES (?,?)",
            (work_type, base))
    con.commit()


def get_dev_point_base(con, work_type: str) -> float | None:
    """作業種別の基準点数を引く（無ければNone）。"""
    r = con.execute("SELECT base_points FROM dev_point_master WHERE work_type=?",
                    (work_type or "",)).fetchone()
    return float(r["base_points"]) if r else None


def list_dev_point_master(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, work_type, base_points FROM dev_point_master ORDER BY base_points, work_type")]


def update_dev_point_master(con, id: int, work_type: str, base_points: float) -> None:
    """既存の作業種別行を id 指定で更新（名称の変更も可）。"""
    con.execute(
        "UPDATE dev_point_master SET work_type=?, base_points=?, updated_at=datetime('now') WHERE id=?",
        (work_type, float(base_points), int(id)))
    con.commit()


def delete_dev_point_master_by_id(con, id: int) -> None:
    con.execute("DELETE FROM dev_point_master WHERE id=?", (int(id),))
    con.commit()


def dev_work_types(con) -> list[str]:
    """マスタに登録済みの作業種別一覧（フォームのセレクト用）。"""
    return [r["work_type"] for r in con.execute(
        "SELECT work_type FROM dev_point_master ORDER BY base_points, work_type")]


def upsert_dev_point_master(con, work_type: str, base_points: float) -> None:
    con.execute(
        "INSERT INTO dev_point_master (work_type, base_points, updated_at) "
        "VALUES (?,?,datetime('now')) "
        "ON CONFLICT(work_type) DO UPDATE SET "
        "base_points=excluded.base_points, updated_at=datetime('now')",
        (work_type, float(base_points)))
    con.commit()


def delete_dev_point_master(con, work_type: str) -> None:
    con.execute("DELETE FROM dev_point_master WHERE work_type=?", (work_type,))
    con.commit()


def get_owner_capacities(con) -> dict:
    """{担当名: {'base':週次上限, 'from':上限変更週(YYYY-MM-DD/None), 'base2':変更後上限/None}} を返す。"""
    out = {}
    for r in con.execute(
            "SELECT owner, weekly_max_points, change_from_week, weekly_max_points2 "
            "FROM dev_owner_capacity"):
        out[r["owner"]] = {
            "base": float(r["weekly_max_points"]),
            "from": r["change_from_week"] or None,
            "base2": (float(r["weekly_max_points2"]) if r["weekly_max_points2"] is not None else None),
        }
    return out


def owner_cap_for_week(cap: dict | None, week_start: str) -> float | None:
    """担当キャパ辞書(get_owner_capacitiesの1要素)と週(YYYY-MM-DDの月曜)から、その週の上限を返す。"""
    if not cap:
        return None
    if cap.get("from") and cap.get("base2") is not None and week_start >= cap["from"]:
        return cap["base2"]
    return cap.get("base")


def set_owner_capacity(con, owner: str, weekly_max_points: float,
                       change_from_week: str | None = None,
                       weekly_max_points2: float | None = None) -> None:
    con.execute(
        "INSERT INTO dev_owner_capacity (owner, weekly_max_points, change_from_week, weekly_max_points2, updated_at) "
        "VALUES (?,?,?,?,datetime('now')) "
        "ON CONFLICT(owner) DO UPDATE SET "
        "weekly_max_points=excluded.weekly_max_points, change_from_week=excluded.change_from_week, "
        "weekly_max_points2=excluded.weekly_max_points2, updated_at=datetime('now')",
        (owner, float(weekly_max_points), change_from_week or None,
         (float(weekly_max_points2) if weekly_max_points2 not in (None, "") else None)))
    con.commit()


def seed_dev_coefficients(con) -> None:
    """係数マスタが空なら定数の既定値を投入（初回のみ）。"""
    if con.execute("SELECT COUNT(*) c FROM dev_coefficient").fetchone()["c"]:
        return
    for k, v in DEV_STAGE_COEF.items():
        con.execute("INSERT OR IGNORE INTO dev_coefficient (coef_type, coef_key, coef_value) "
                    "VALUES ('stage',?,?)", (k, v))
    for k, v in DEV_DIFFICULTY_COEF.items():
        con.execute("INSERT OR IGNORE INTO dev_coefficient (coef_type, coef_key, coef_value) "
                    "VALUES ('difficulty',?,?)", (k, v))
    for k, v in DEV_BACKEND_BONUS.items():
        con.execute("INSERT OR IGNORE INTO dev_coefficient (coef_type, coef_key, coef_value) "
                    "VALUES ('backend',?,?)", (k, v))
    con.commit()


def get_dev_coefs(con) -> dict:
    """{'stage':{k:v}, 'difficulty':{k:v}, 'backend':{k:加点}} を返す。DB値を定数の既定に上書き。"""
    coefs = {"stage": dict(DEV_STAGE_COEF), "difficulty": dict(DEV_DIFFICULTY_COEF),
             "backend": dict(DEV_BACKEND_BONUS)}
    for r in con.execute("SELECT coef_type, coef_key, coef_value FROM dev_coefficient"):
        if r["coef_type"] in coefs:
            coefs[r["coef_type"]][r["coef_key"]] = float(r["coef_value"])
    return coefs


def set_dev_coef(con, coef_type: str, coef_key: str, coef_value: float) -> None:
    con.execute(
        "INSERT INTO dev_coefficient (coef_type, coef_key, coef_value, updated_at) "
        "VALUES (?,?,?,datetime('now')) "
        "ON CONFLICT(coef_type, coef_key) DO UPDATE SET "
        "coef_value=excluded.coef_value, updated_at=datetime('now')",
        (coef_type, coef_key, float(coef_value)))
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


# ---- タスク管理（#30） ----

TASK_FIELDS = [
    "title", "detail", "project", "next_action", "assignee", "due_date", "status",
    "priority", "category", "is_admin", "requester", "link_type", "link_id", "source",
    "slack_channel", "slack_ts", "slack_permalink", "created_by",
]


def upsert_task(con, *, id=None, commit: bool = True, **fields) -> int:
    """タスクを作成/更新。status='完了'になったら done_at をセット、外れたらクリア。"""
    data = {k: fields.get(k) for k in TASK_FIELDS}
    if id is not None:
        sets = ", ".join(f"{k}=?" for k in TASK_FIELDS) + ", updated_at=datetime('now')"
        # 完了への遷移/解除で done_at を調整
        done_sql = (", done_at=CASE WHEN ?='完了' AND (done_at IS NULL OR done_at='') "
                    "THEN datetime('now') WHEN ?!='完了' THEN NULL ELSE done_at END")
        con.execute(f"UPDATE tasks SET {sets}{done_sql} WHERE id=?",
                    [data[k] for k in TASK_FIELDS] + [data["status"], data["status"], int(id)])
        if commit:
            con.commit()
        return int(id)
    cols = ", ".join(TASK_FIELDS)
    ph = ", ".join("?" for _ in TASK_FIELDS)
    cur = con.execute(f"INSERT INTO tasks ({cols}) VALUES ({ph})",
                      [data[k] for k in TASK_FIELDS])
    if data.get("status") == "完了":
        con.execute("UPDATE tasks SET done_at=datetime('now') WHERE id=?", (cur.lastrowid,))
    if commit:
        con.commit()
    return cur.lastrowid


def list_tasks(con, *, status: str | None = None, assignee: str | None = None,
               category: str | None = None, project: str | None = None,
               link_type: str | None = None,
               link_id: int | None = None, exclude_done: bool = False,
               admin: bool | None = None) -> list[dict]:
    """admin=True で事務タスク(is_admin=1)のみ、admin=False で通常タスク(is_admin=0/NULL)のみ、
    admin=None（既定）で両方。既存呼び出しは admin=None のため挙動不変。"""
    q = "SELECT * FROM tasks"
    conds: list = []
    params: list = []
    if admin is True:
        conds.append("COALESCE(is_admin,0) = 1")
    elif admin is False:
        conds.append("COALESCE(is_admin,0) = 0")
    if status:
        conds.append("status = ?"); params.append(status)
    if exclude_done:
        conds.append("status != '完了'")
    if assignee:
        conds.append("assignee = ?"); params.append(assignee)
    if category:
        conds.append("category = ?"); params.append(category)
    if project:
        conds.append("project = ?"); params.append(project)
    if link_type:
        conds.append("link_type = ?"); params.append(link_type)
    if link_id is not None:
        conds.append("link_id = ?"); params.append(int(link_id))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    # ★ピン最優先→期限昇順(未設定は末尾)→id
    q += (" ORDER BY COALESCE(pinned,0) DESC, (due_date IS NULL OR due_date='') ASC, "
          "due_date ASC, id DESC")
    return [dict(r) for r in con.execute(q, params)]


def get_task(con, id: int) -> dict | None:
    r = con.execute("SELECT * FROM tasks WHERE id=?", (int(id),)).fetchone()
    return dict(r) if r else None


def set_task_status(con, id: int, status: str, commit: bool = True) -> None:
    if status == "完了":
        con.execute("UPDATE tasks SET status=?, done_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                    (status, int(id)))
    else:
        con.execute("UPDATE tasks SET status=?, done_at=NULL, updated_at=datetime('now') WHERE id=?",
                    (status, int(id)))
    if commit:
        con.commit()


def delete_task(con, id: int) -> None:
    con.execute("DELETE FROM tasks WHERE id=?", (int(id),))
    con.commit()


def add_task_note(con, task_id: int, body: str, author: str | None = None,
                  kind: str = "progress", commit: bool = True) -> int:
    """追記ログを1件追加し、タスクのupdated_atを更新する。kind='progress'(進捗)/'discussion'(議論メモ)。"""
    cur = con.execute("INSERT INTO task_notes (task_id, body, author, kind) VALUES (?,?,?,?)",
                      (int(task_id), body, author, kind))
    con.execute("UPDATE tasks SET updated_at=datetime('now') WHERE id=?", (int(task_id),))
    if commit:
        con.commit()
    return cur.lastrowid


def list_task_notes(con, task_id: int, kind: str | None = None) -> list[dict]:
    """追記ログを新しい順に返す。kind指定でその種別のみ。"""
    q = "SELECT * FROM task_notes WHERE task_id=?"
    params: list = [int(task_id)]
    if kind:
        q += " AND kind=?"
        params.append(kind)
    q += " ORDER BY created_at DESC, id DESC"
    return [dict(r) for r in con.execute(q, params)]


def set_task_summary(con, task_id: int, summary: str) -> None:
    con.execute("UPDATE tasks SET summary=?, summary_at=datetime('now') WHERE id=?",
                (summary, int(task_id)))
    con.commit()


def set_project_summary(con, name: str, summary: str) -> None:
    con.execute("UPDATE task_projects SET summary=?, summary_at=datetime('now') WHERE name=?",
                (summary, name))
    con.commit()


def latest_task_note(con, task_id: int) -> dict | None:
    r = con.execute(
        "SELECT * FROM task_notes WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (int(task_id),)).fetchone()
    return dict(r) if r else None


def delete_task_note(con, note_id: int) -> None:
    con.execute("DELETE FROM task_notes WHERE id=?", (int(note_id),))
    con.commit()


# ---- タスクのプロジェクト（大項目・期限＋状態を持つ管理対象）(#30) ----

def list_task_projects(con, include_done: bool = True) -> list[dict]:
    """プロジェクト一覧（sort_order→期限→名前）。include_done=Falseで完了を除く。"""
    q = "SELECT * FROM task_projects"
    if not include_done:
        q += " WHERE status != '完了'"
    q += (" ORDER BY sort_order ASC, "
          "(deadline IS NULL OR deadline='') ASC, deadline ASC, name ASC")
    return [dict(r) for r in con.execute(q)]


def get_task_project(con, name: str) -> dict | None:
    r = con.execute("SELECT * FROM task_projects WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def upsert_task_project(con, *, id=None, name: str, deadline: str | None = None,
                        status: str = "進行中", sort_order: int = 0) -> int:
    """プロジェクトを作成/更新。id指定で更新（改名可）、無ければname一致で更新or新規。"""
    name = (name or "").strip()
    if id is not None:
        con.execute("UPDATE task_projects SET name=?, deadline=?, status=?, sort_order=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (name, deadline or None, status, sort_order, int(id)))
        con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO task_projects (name, deadline, status, sort_order) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET deadline=excluded.deadline, status=excluded.status, "
        "sort_order=excluded.sort_order, updated_at=datetime('now')",
        (name, deadline or None, status, sort_order))
    con.commit()
    r = con.execute("SELECT id FROM task_projects WHERE name=?", (name,)).fetchone()
    return r["id"] if r else cur.lastrowid


def delete_task_project(con, id: int) -> None:
    """プロジェクト定義を削除（タスクのproject名文字列はそのまま残る＝孤立表示になるだけ）。"""
    con.execute("DELETE FROM task_projects WHERE id=?", (int(id),))
    con.commit()


def task_counts_by_project_status(con) -> dict:
    """プロジェクト名→{status: 件数} の集計（看板上部のプロジェクト一覧用）。"""
    out: dict = {}
    for r in con.execute(
        "SELECT COALESCE(project,'') p, status, COUNT(*) n FROM tasks "
        "WHERE project IS NOT NULL AND project!='' GROUP BY project, status"):
        out.setdefault(r["p"], {})[r["status"] or "受信箱"] = r["n"]
    return out


# テストデータ（検証用・source='test'で明示。ワンクリック投入/削除できる）。
# タイトルは必ず「【テスト】」で始め、本番タスクと視覚的に区別する。
def seed_sample_tasks(con, assignee: str = "早瀬", today: str | None = None) -> int:
    """検証用のサンプルタスクを投入する（既存のtestデータは一旦消してから入れ直す）。件数を返す。"""
    from datetime import date as _date, timedelta as _td
    base = _date.fromisoformat(today) if today else _date.today()
    past = (base - _td(days=6)).isoformat()
    thisweek = (base + _td(days=2)).isoformat()
    tod = base.isoformat()
    future = (base + _td(days=20)).isoformat()
    delete_test_tasks(con)  # 二重投入を防ぐ
    # テスト用プロジェクト（期限付き＝看板ストリップ・期限逆算の推奨を体験できる）
    upsert_task_project(con, name="【テスト】図面OCR研究開発",
                        deadline=add_business_days(base, 30).isoformat())
    upsert_task_project(con, name="【テスト】セキュリティISO取得",
                        deadline=add_business_days(base, 15).isoformat())
    samples = [
        dict(title="【テスト】デモ環境を用意する", project="【テスト】図面OCR研究開発",
             next_action="サンプルデータを△△さんに依頼する", assignee=assignee,
             due_date=past, status="対応中", priority="高", category="開発"),
        dict(title="【テスト】ISO内部監査の資料レビュー", project="【テスト】セキュリティISO取得",
             assignee=assignee, due_date=thisweek, status="未着手", priority="中", category="ドキュメント"),
        dict(title="【テスト】提案書の骨子を作る", next_action="競合比較表を1枚にまとめる",
             assignee=assignee, due_date=tod, status="未着手", priority="高", category="設計"),
        dict(title="【テスト】Slackで来た依頼を整理する", assignee=assignee,
             status="受信箱", priority="中"),
        dict(title="【テスト】先の対応（期限なし）", project="【テスト】図面OCR研究開発",
             next_action="要件が固まったら着手", assignee=assignee, status="保留", priority="低"),
        dict(title="【テスト】完了済みサンプル", assignee=assignee, due_date=past,
             status="完了", priority="中", category="開発"),
    ]
    n = 0
    first_id = None
    for s in samples:
        tid = upsert_task(con, source="test", commit=False, **s)
        if first_id is None:
            first_id = tid
        n += 1
    con.commit()
    if first_id:
        add_task_note(con, first_id, "【テスト】環境構築に着手。あと少しで完了予定。")
        add_task_note(con, first_id, "【テスト】DBスキーマを確定した。")
    return n


def delete_test_tasks(con) -> int:
    """source='test' のテストタスク（と紐づく進捗ログ）を全削除。件数を返す。"""
    ids = [r[0] for r in con.execute("SELECT id FROM tasks WHERE source='test'")]
    for tid in ids:
        con.execute("DELETE FROM task_notes WHERE task_id=?", (tid,))
    con.execute("DELETE FROM tasks WHERE source='test'")
    con.execute("DELETE FROM task_projects WHERE name LIKE '【テスト】%'")
    con.commit()
    return len(ids)


def delete_admin_tasks(con) -> int:
    """事務タスク(is_admin=1)と紐づく進捗/議論メモを全削除。件数を返す。
    立ち上げ直後の受付テスト分を一括で片付ける用途（受付=事務タスクのみを消し、通常タスクは残す）。"""
    ids = [r[0] for r in con.execute("SELECT id FROM tasks WHERE COALESCE(is_admin,0)=1")]
    for tid in ids:
        con.execute("DELETE FROM task_notes WHERE task_id=?", (tid,))
    con.execute("DELETE FROM tasks WHERE COALESCE(is_admin,0)=1")
    con.commit()
    return len(ids)


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


def deal_delete_impact(con, deal_id: int) -> dict:
    """商談を完全削除したときに連鎖削除される子データ件数と、Hisho連携ID群を返す。
    削除前の確認メッセージ・完了フラッシュ・Hisho側クリーンアップに使う。
    子テーブルはFKの ON DELETE CASCADE で自動削除される（connect()で foreign_keys=ON）。"""
    did = int(deal_id)
    c = lambda sql: con.execute(sql, (did,)).fetchone()[0]
    d = get_deal(con, did)
    dev_hisho_ids = [r[0] for r in con.execute(
        "SELECT hisho_id FROM dev_projects WHERE deal_id=? AND hisho_id IS NOT NULL", (did,))]
    return {
        "activities": c("SELECT COUNT(*) FROM activities WHERE deal_id=?"),
        "issues": c("SELECT COUNT(*) FROM deal_issues WHERE deal_id=?"),
        "milestones": c("SELECT COUNT(*) FROM deal_milestones WHERE deal_id=?"),
        "dev_projects": c("SELECT COUNT(*) FROM dev_projects WHERE deal_id=?"),
        "deliveries": c("SELECT COUNT(*) FROM deliveries WHERE deal_id=?"),
        "attachments": c("SELECT COUNT(*) FROM deal_attachments WHERE deal_id=?"),
        "leads_detached": c("SELECT COUNT(*) FROM leads WHERE deal_id=?"),
        "theme_id": (d or {}).get("theme_id"),
        "dev_hisho_ids": dev_hisho_ids,
    }


def delete_deal(con, deal_id: int) -> None:
    """商談を完全削除（物理削除）。クローズ（リード化）ではなく行そのものを消す。
    子データ（deal_milestones/activities/hearing_results/dev_projects(+tools)/deal_issues(+memos)/
    deal_attachments/deliveries(+assignments)）は ON DELETE CASCADE で連鎖削除、leads.deal_id は
    SET NULL（＝そのリードは「未商談化」に戻る）。foreign_keys=ON 前提（connect()で常時ON）。
    slack_threads.deal_id はFK未設定のため孤児として残るが害はない（Botのスレッド状態のみ）。
    Hisho側（todos/dev_projects）のクリーンアップは呼び出し側でtheme_client経由の best-effort。"""
    con.execute("DELETE FROM deals WHERE id=?", (int(deal_id),))
    con.commit()


# ---- OneNote風リッチメモ（rich_notes・#70）: (kind, entity_id)に複数ノート ----

RICH_NOTE_KINDS = ("deal", "issue", "htmpl")  # 商談 / 論点 / ヒアリングテンプレ


def list_rich_notes(con, kind: str, entity_id: int) -> list[dict]:
    """指定エンティティのノート一覧（sort_order→id昇順）。"""
    return [dict(r) for r in con.execute(
        "SELECT * FROM rich_notes WHERE kind=? AND entity_id=? ORDER BY sort_order ASC, id ASC",
        (kind, int(entity_id)))]


def get_rich_note(con, note_id: int) -> dict | None:
    r = con.execute("SELECT * FROM rich_notes WHERE id=?", (int(note_id),)).fetchone()
    return dict(r) if r else None


def create_rich_note(con, *, kind: str, entity_id: int, title: str | None = None,
                     body: str | None = None) -> int:
    """新規ノートを作成し idを返す。sort_orderは末尾。"""
    nx = con.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM rich_notes WHERE kind=? AND entity_id=?",
                     (kind, int(entity_id))).fetchone()[0]
    cur = con.execute(
        "INSERT INTO rich_notes (kind, entity_id, title, body, sort_order) VALUES (?,?,?,?,?)",
        (kind, int(entity_id), (title or None), (body or None), nx))
    con.commit()
    return cur.lastrowid


def update_rich_note(con, note_id: int, *, title: str | None, body: str | None) -> None:
    con.execute("UPDATE rich_notes SET title=?, body=?, updated_at=datetime('now') WHERE id=?",
                ((title or None), (body or None), int(note_id)))
    con.commit()


def delete_rich_note(con, note_id: int) -> None:
    con.execute("DELETE FROM rich_notes WHERE id=?", (int(note_id),))
    con.commit()


def rich_note_entity_ids(con, kind: str) -> set:
    """そのkindでノートが1件以上あるentity_idの集合（一覧の📝バッジ点灯用）。"""
    return {r[0] for r in con.execute(
        "SELECT DISTINCT entity_id FROM rich_notes WHERE kind=?", (kind,))}


# ---- 開発案件の追加ツールリンク（dev_project_tools） ----

def list_dev_project_tools(con, dev_project_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM dev_project_tools WHERE dev_project_id=? ORDER BY created_at ASC, id ASC",
        (int(dev_project_id),))]


def list_dev_project_tools_for(con, dev_project_ids: list[int]) -> dict:
    """複数開発案件の追加リンクを一括取得し {dev_project_id: [rows]} で返す（一覧のN+1回避）。"""
    out: dict = {}
    ids = [int(i) for i in dev_project_ids if i is not None]
    if not ids:
        return out
    ph = ",".join("?" for _ in ids)
    for r in con.execute(
        f"SELECT * FROM dev_project_tools WHERE dev_project_id IN ({ph}) ORDER BY created_at ASC, id ASC", ids):
        out.setdefault(r["dev_project_id"], []).append(dict(r))
    return out


def add_dev_project_tool(con, *, dev_project_id: int, url: str,
                         label: str | None = None, login_id: str | None = None,
                         login_pass: str | None = None) -> int:
    cur = con.execute(
        "INSERT INTO dev_project_tools (dev_project_id, label, url, login_id, login_pass) VALUES (?,?,?,?,?)",
        (int(dev_project_id), label or None, url, login_id or None, login_pass or None))
    con.commit()
    return cur.lastrowid


def delete_dev_project_tool(con, tool_id: int) -> None:
    con.execute("DELETE FROM dev_project_tools WHERE id=?", (int(tool_id),))
    con.commit()


# ---- Delivery（受注後・納品）アサイン計画（#75） ----

def _monday_of(d: date) -> str:
    """dを含む週の月曜(YYYY-MM-DD文字列)。"""
    return (d - timedelta(days=d.weekday())).isoformat()


def _weeks_from(start_monday: str, n: int) -> list[str]:
    """start_monday(月曜)からn週分の月曜日付リスト。"""
    d0 = date.fromisoformat(start_monday)
    return [(d0 + timedelta(days=7 * i)).isoformat() for i in range(max(0, n))]


def create_delivery(con, *, deal_id: int, title: str = "", start_week: str | None = None,
                    end_week: str | None = None, status: str = "進行中",
                    overview: str = "") -> int:
    cur = con.execute(
        "INSERT INTO deliveries (deal_id, title, start_week, end_week, status, overview) "
        "VALUES (?,?,?,?,?,?)",
        (int(deal_id), title or None, start_week or None, end_week or None,
         status or "進行中", overview or None))
    con.commit()
    return cur.lastrowid


def get_delivery(con, delivery_id: int) -> dict | None:
    r = con.execute(
        "SELECT dv.*, d.deal_name, d.stage AS deal_stage, d.status AS deal_status, "
        "acc.name AS account_name "
        "FROM deliveries dv JOIN deals d ON d.id=dv.deal_id "
        "LEFT JOIN accounts acc ON acc.id=d.account_id WHERE dv.id=?",
        (int(delivery_id),)).fetchone()
    return dict(r) if r else None


def list_deliveries(con, *, deal_id: int | None = None) -> list[dict]:
    """Delivery一覧（deal名・stage・status付き）。deal_id指定でその商談分のみ。"""
    sql = ("SELECT dv.*, d.deal_name, d.stage AS deal_stage, d.status AS deal_status, "
           "acc.name AS account_name "
           "FROM deliveries dv JOIN deals d ON d.id=dv.deal_id "
           "LEFT JOIN accounts acc ON acc.id=d.account_id ")
    args: list = []
    if deal_id is not None:
        sql += "WHERE dv.deal_id=? "
        args.append(int(deal_id))
    sql += "ORDER BY dv.created_at DESC, dv.id DESC"
    return [dict(r) for r in con.execute(sql, args)]


def delivery_month_count(start_week: str | None, end_week: str | None) -> float:
    """月額↔総額換算・月額換算に使う『月数』。全て『合計週数 ÷ 4週(≒1ヶ月)』で統一。
    例: 8週 → 2.0ヶ月 / 11週 → 2.75ヶ月。不正/未設定は1。"""
    try:
        s = date.fromisoformat(str(start_week)[:10])
        e = date.fromisoformat(str(end_week)[:10])
    except (TypeError, ValueError):
        return 1.0
    weeks = (e - s).days // 7 + 1
    if weeks < 1:
        return 1.0
    return round(weeks / 4.0, 4)


def delivery_display_fees(dv: dict) -> tuple:
    """一覧・出力の表示用 (fee_monthly, fee_total)。fee_modeの入力値を正とし、現在の月数
    （合計週数÷4）でもう一方を都度再計算する。個別編集画面のライブ換算と一致させ、
    保存済み派生値（旧ロジックや週変更で古くなった値）とのズレを防ぐ。"""
    months = delivery_month_count(dv.get("start_week"), dv.get("end_week"))
    if (dv.get("fee_mode") or "monthly") == "total":
        return compute_delivery_fee("total", None, dv.get("fee_total"), months)
    return compute_delivery_fee("monthly", dv.get("fee_monthly"), None, months)


def compute_delivery_fee(mode: str | None, monthly, total, months) -> tuple:
    """(fee_monthly, fee_total) を返す。mode='monthly'なら月額を正とし総額=月額×月数、
    mode='total'なら総額を正とし月額=総額÷月数。月数は合計週数÷4（小数可）。空/不正は None。"""
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    try:
        m = float(months)
    except (TypeError, ValueError):
        m = 0.0
    if m <= 0:
        m = 1.0
    mo, to = _f(monthly), _f(total)
    if (mode or "monthly") == "total":
        return (round(to / m, 2) if to is not None else None, to)
    return (mo, round(mo * m, 2) if mo is not None else None)


def update_delivery(con, delivery_id: int, **fields) -> None:
    allowed = {"title", "start_week", "end_week", "status", "overview",
               "fee_mode", "fee_monthly", "fee_total"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v if v != "" else None)
    if not sets:
        return
    args.append(int(delivery_id))
    con.execute(f"UPDATE deliveries SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?", args)
    con.commit()


def delete_delivery(con, delivery_id: int) -> None:
    # delivery_assignments は ON DELETE CASCADE。念のためFK ON前提でなくても消す。
    con.execute("DELETE FROM delivery_assignments WHERE delivery_id=?", (int(delivery_id),))
    con.execute("DELETE FROM deliveries WHERE id=?", (int(delivery_id),))
    con.commit()


def ensure_delivery_on_stage(con, deal_id: int, stage: str | None) -> int | None:
    """商談が「提案」以降に到達し、まだDeliveryが無ければ1件自動起票（#75）。作成したidを返す。"""
    if (stage or "") not in DELIVERY_TRIGGER_STAGES:
        return None
    n = con.execute("SELECT COUNT(*) c FROM deliveries WHERE deal_id=?", (int(deal_id),)).fetchone()["c"]
    if n:
        return None
    deal = get_deal(con, deal_id)
    if not deal:
        return None
    return create_delivery(con, deal_id=deal_id, title=(deal.get("deal_name") or ""))


def list_delivery_assignments(con, delivery_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM delivery_assignments WHERE delivery_id=? ORDER BY owner, from_week",
        (int(delivery_id),))]


def _week_delta(old_w: str | None, new_w: str | None) -> int:
    """old→new の週差（週数・整数）。どちらか欠け/不正なら0。"""
    try:
        o = date.fromisoformat(str(old_w)[:10])
        n = date.fromisoformat(str(new_w)[:10])
        return (n - o).days // 7
    except (TypeError, ValueError):
        return 0


def _shift_week(w: str | None, delta_weeks: int) -> str | None:
    try:
        return (date.fromisoformat(str(w)[:10]) + timedelta(weeks=delta_weeks)).isoformat()
    except (TypeError, ValueError):
        return None


def reschedule_delivery_assignments(con, delivery_id: int, old_start, old_end,
                                    new_start, new_end) -> int:
    """デリバリー期間の変更に、各アサインの週を連動スライドさせる（#75）。
    from_week は「開始週の移動量(Δstart)」だけ、to_week は「終了週の移動量(Δend)」だけずらす。
    ＝スライド(開始=終了が同量移動)なら全員まるごと移動／週数延長(終了のみ移動)なら全員の終了だけ延びる。
    変更した行数を返す。両Δが0なら何もしない。"""
    ds = _week_delta(old_start, new_start) if (old_start and new_start) else 0
    de = _week_delta(old_end, new_end) if (old_end and new_end) else 0
    if not ds and not de:
        return 0
    n = 0
    for a in list_delivery_assignments(con, delivery_id):
        nf = _shift_week(a.get("from_week"), ds)
        nt = _shift_week(a.get("to_week"), de)
        if not nf or not nt:
            continue
        if nt < nf:
            nt = nf  # 逆転防止
        con.execute("UPDATE delivery_assignments SET from_week=?, to_week=? WHERE id=?",
                    (nf, nt, a["id"]))
        n += 1
    con.commit()
    return n


def _assignment_weeks(from_week: str | None, to_week: str | None) -> int:
    """アサインの週数（from〜to の週数・両端含む）。月曜スナップ前提だが日付差で算出。"""
    try:
        s = date.fromisoformat(str(from_week)[:10])
        e = date.fromisoformat(str(to_week)[:10])
    except (TypeError, ValueError):
        return 0
    d = (e - s).days
    return (d // 7) + 1 if d >= 0 else 0


def delivery_total_assign_effort(con, delivery_id: int, use_billing: bool = False) -> float:
    """Deliveryの『総アサイン工数(%/月)』＝期間を通じた平均の合計稼働率。
    Σ(各アサインの 週数×稼働率) ÷ 総期間週数。
    use_billing=False: 実想定(fte_pct)ベース（稼働負荷用）。
    use_billing=True : 請求(fte_billing)ベース＝純粋な請求のみ（未入力0/Noneは0＝稼働コミットなし）。
      ※フォールバックはしない。全行請求0なら0（＝成果物ベース）。平均単価側でどちらを使うか判断する。
    例: 高橋20%×11週+杉山80%×11週=1100÷11週=100（実想定）／請求40%+80%なら1320÷11=120。
    総期間週数はデリバリー期間(start_week〜end_week)。未設定ならアサインの最小from〜最大to。"""
    asgs = list_delivery_assignments(con, delivery_id)
    if not asgs:
        return 0.0
    dv = get_delivery(con, delivery_id)
    total_weeks = 0
    if dv and dv.get("start_week") and dv.get("end_week"):
        total_weeks = _assignment_weeks(dv["start_week"], dv["end_week"])
    if total_weeks <= 0:
        froms = [a["from_week"] for a in asgs if a.get("from_week")]
        tos = [a["to_week"] for a in asgs if a.get("to_week")]
        if froms and tos:
            total_weeks = _assignment_weeks(min(froms), max(tos))
    if total_weeks <= 0:
        return 0.0

    def _rate(a) -> float:
        if use_billing:
            return float(a.get("fte_billing") or 0)  # 純粋な請求（0/None=稼働コミットなし）。フォールバックなし
        return float(a.get("fte_pct") or 0)

    total = sum(_assignment_weeks(a.get("from_week"), a.get("to_week")) * _rate(a) for a in asgs)
    return round(total / total_weeks, 1)


def _billing_of(b: dict) -> float:
    """アサイン行の請求稼働率。fte_billingがNULLなら実想定(fte_pct)にフォールバック。"""
    v = b.get("fte_billing")
    return float(v) if v is not None else float(b.get("fte_pct") or 0)


def add_delivery_assignment(con, *, delivery_id: int, owner: str, from_week: str,
                            to_week: str, fte_pct: float, fte_billing: float | None = None,
                            note: str = "", role: str = "", member_kind: str = "内部") -> int:
    """fte_pct=実想定(負荷計算に使う), fte_billing=請求(NoneならSQL側はNULL=実想定と同値扱い)。
    role=役割, member_kind=内部/外部。owner未定は空文字可（体制欄から役割だけ先に作る場合）。"""
    cur = con.execute(
        "INSERT INTO delivery_assignments "
        "(delivery_id, role, member_kind, owner, from_week, to_week, fte_pct, fte_billing, note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (int(delivery_id), role or None, member_kind or "内部", owner or "", from_week, to_week,
         float(fte_pct or 0), (float(fte_billing) if fte_billing is not None else None), note or None))
    con.commit()
    return cur.lastrowid


def update_delivery_assignment(con, assignment_id: int, *, owner: str, from_week: str,
                               to_week: str, fte_pct: float, fte_billing: float | None = None,
                               note: str = "", role: str = "", member_kind: str = "内部") -> None:
    con.execute(
        "UPDATE delivery_assignments SET role=?, member_kind=?, owner=?, from_week=?, to_week=?, "
        "fte_pct=?, fte_billing=?, note=? WHERE id=?",
        (role or None, member_kind or "内部", owner or "", from_week, to_week, float(fte_pct or 0),
         (float(fte_billing) if fte_billing is not None else None), note or None, int(assignment_id)))
    con.commit()


def delete_delivery_assignment(con, assignment_id: int) -> None:
    con.execute("DELETE FROM delivery_assignments WHERE id=?", (int(assignment_id),))
    con.commit()


def delivery_grid(con, delivery_id: int) -> dict:
    """1Deliveryのアサインをメンバー×週に展開したプレビュー用グリッド。
    週の範囲はブロックの最小from〜最大to（無ければ空）。cell={actual, billing}の合算。"""
    blocks = list_delivery_assignments(con, delivery_id)
    # from/to が未入力(空)の行（役割追加直後などは開始/終了週が空）は週展開できないので除外。
    # これを弾かないと date.fromisoformat('') で ValueError → 画面が500/502になる。
    dated = [b for b in blocks if (b.get("from_week") or "").strip() and (b.get("to_week") or "").strip()]
    if not dated:
        return {"weeks": [], "owners": [], "cells": {}}
    try:
        fmin = min(b["from_week"] for b in dated)
        tmax = max(b["to_week"] for b in dated)
        d0, d1 = date.fromisoformat(fmin), date.fromisoformat(tmax)
    except (TypeError, ValueError):
        return {"weeks": [], "owners": [], "cells": {}}
    n = (d1 - d0).days // 7 + 1
    weeks = _weeks_from(_monday_of(d0), n)
    cells: dict = {}
    owners: list = []
    for b in dated:
        if b["owner"] not in owners:
            owners.append(b["owner"])
        for wk in weeks:
            if b["from_week"] <= wk <= b["to_week"]:
                c = cells.setdefault(b["owner"], {}).setdefault(wk, {"actual": 0.0, "billing": 0.0})
                c["actual"] += b["fte_pct"] or 0
                c["billing"] += _billing_of(b)
    return {"weeks": weeks, "owners": owners, "cells": cells}


def list_delivery_roles(con, delivery_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM delivery_roles WHERE delivery_id=? ORDER BY id", (int(delivery_id),))]


def add_delivery_role(con, *, delivery_id: int, role: str, fte_billing: float | None = None,
                      fte_pct: float | None = None) -> int:
    cur = con.execute(
        "INSERT INTO delivery_roles (delivery_id, role, fte_billing, fte_pct) VALUES (?,?,?,?)",
        (int(delivery_id), role,
         (float(fte_billing) if fte_billing is not None else None),
         (float(fte_pct) if fte_pct is not None else None)))
    con.commit()
    return cur.lastrowid


def update_delivery_role(con, role_id: int, *, role: str, fte_billing: float | None = None,
                         fte_pct: float | None = None) -> None:
    con.execute(
        "UPDATE delivery_roles SET role=?, fte_billing=?, fte_pct=? WHERE id=?",
        (role, (float(fte_billing) if fte_billing is not None else None),
         (float(fte_pct) if fte_pct is not None else None), int(role_id)))
    con.commit()


def delete_delivery_role(con, role_id: int) -> None:
    con.execute("DELETE FROM delivery_roles WHERE id=?", (int(role_id),))
    con.commit()


def list_base_workload(con, owner: str | None = None) -> list[dict]:
    if owner:
        return [dict(r) for r in con.execute(
            "SELECT * FROM base_workload WHERE owner=? ORDER BY function", (owner,))]
    return [dict(r) for r in con.execute(
        "SELECT * FROM base_workload ORDER BY owner, function")]


def base_workload_by_owner(con) -> dict:
    """{owner: 合算pct} を返す（総工数計算用）。"""
    return {r["owner"]: r["p"] for r in con.execute(
        "SELECT owner, COALESCE(SUM(pct),0) p FROM base_workload GROUP BY owner")}


def upsert_base_workload(con, owner: str, function: str, pct: float) -> None:
    con.execute(
        "INSERT INTO base_workload (owner, function, pct, updated_at) VALUES (?,?,?,datetime('now')) "
        "ON CONFLICT(owner, function) DO UPDATE SET pct=excluded.pct, updated_at=datetime('now')",
        (owner, function, float(pct or 0)))
    con.commit()


def get_owner_base_max(con) -> dict:
    """{owner: 最大稼働率%}。未設定は含まない（呼び出し側で100%既定）。"""
    return {r["owner"]: r["max_pct"] for r in con.execute("SELECT owner, max_pct FROM owner_base_max")}


def set_owner_base_max(con, owner: str, max_pct) -> None:
    con.execute(
        "INSERT INTO owner_base_max (owner, max_pct, updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(owner) DO UPDATE SET max_pct=excluded.max_pct, updated_at=datetime('now')",
        (owner, float(max_pct if max_pct is not None else 100)))
    con.commit()


# ---- ベース最大稼働率の期間版（#75）----

def list_base_max_periods(con) -> dict:
    """{owner: [{id, from_week, to_week, max_pct}, ...]}。from_week昇順（空=先頭）→id。"""
    out: dict = {}
    for r in con.execute(
        "SELECT id, owner, from_week, to_week, max_pct FROM owner_base_max_periods "
        "ORDER BY owner, CASE WHEN from_week IS NULL OR from_week='' THEN 0 ELSE 1 END, from_week, id"):
        out.setdefault(r["owner"], []).append(
            {"id": r["id"], "from_week": r["from_week"] or "",
             "to_week": r["to_week"] or "", "max_pct": r["max_pct"]})
    return out


def replace_base_max_periods(con, owner: str, periods: list) -> None:
    """指定メンバーのベース最大稼働率の期間を総入れ替え。periods=[{from_week,to_week,max_pct}]。
    from/to が両方空 かつ 既定値のみの空行は無視。保存後、owner_base_max（単一・互換用）を
    「今週を含む期間の値」または最新の開いた期間の値で更新する。"""
    con.execute("DELETE FROM owner_base_max_periods WHERE owner=?", (owner,))
    kept = []
    for p in periods:
        fw = (p.get("from_week") or "").strip()
        tw = (p.get("to_week") or "").strip()
        mx = p.get("max_pct")
        # 全項目空（週なし＆max未入力）はスキップ
        if not fw and not tw and (mx is None or mx == ""):
            continue
        mxv = float(mx) if (mx is not None and mx != "") else 100.0
        con.execute(
            "INSERT INTO owner_base_max_periods (owner, from_week, to_week, max_pct) VALUES (?,?,?,?)",
            (owner, fw or None, tw or None, mxv))
        kept.append({"from_week": fw, "to_week": tw, "max_pct": mxv})
    con.commit()
    # 互換: 単一 owner_base_max を「今週の実効値」に更新（無ければ既存維持/100）
    today_mon = _monday_of(date.today())
    eff = _base_max_pick(kept, today_mon)
    if eff is not None:
        set_owner_base_max(con, owner, eff)


def _base_max_pick(periods: list, week: str):
    """periods（[{from_week,to_week,max_pct}]）から week を含む期間の max_pct を返す。無ければNone。
    from空=下限なし / to空=以降継続。複数該当時は最も限定的（from が新しい）を優先。"""
    best = None
    best_from = None
    for p in periods:
        fw = (p.get("from_week") or "").strip()
        tw = (p.get("to_week") or "").strip()
        if fw and week < fw:
            continue
        if tw and week > tw:
            continue
        # 該当。from がより新しい（大きい）ものを優先
        if best is None or (fw or "") >= (best_from or ""):
            best = p.get("max_pct")
            best_from = fw
    return best


def base_max_at(con, owner: str, week: str):
    """owner の week 時点のベース最大稼働率。期間があれば期間優先、無ければ単一値、無ければ100。"""
    periods = list_base_max_periods(con).get(owner, [])
    v = _base_max_pick(periods, week)
    if v is not None:
        return v
    single = get_owner_base_max(con).get(owner)
    return single if single is not None else 100.0


def replace_base_workload_for_owner(con, owner: str, items: list) -> None:
    """指定メンバーのベース工数を items=[(function, pct), ...] で総入れ替え（空機能は無視）。
    5スロット固定UIの自動保存用（#75）。同一機能は最後の値で上書き。"""
    con.execute("DELETE FROM base_workload WHERE owner=?", (owner,))
    for fn, pc in items:
        fn = (fn or "").strip()
        if not fn:
            continue
        con.execute(
            "INSERT OR REPLACE INTO base_workload (owner, function, pct, updated_at) "
            "VALUES (?,?,?,datetime('now'))", (owner, fn, float(pc or 0)))
    con.commit()


def update_base_workload(con, base_id: int, *, function: str, pct: float) -> None:
    con.execute(
        "UPDATE base_workload SET function=?, pct=?, updated_at=datetime('now') WHERE id=?",
        (function, float(pct or 0), int(base_id)))
    con.commit()


def delete_base_workload(con, base_id: int) -> None:
    con.execute("DELETE FROM base_workload WHERE id=?", (int(base_id),))
    con.commit()


def compute_delivery_load(con, *, start_week: str | None = None,
                          n_weeks: int = DELIVERY_VIEW_WEEKS,
                          internal_only: bool = False) -> dict:
    """全社の Delivery 稼働(FTE%)を メンバー×週 に展開・合算（#75）。
    確度は紐づく deal.stage から導出: 受注=確定(committed)/提案・クロージング=見込み(forecast)。
    クローズ済みかつ非受注（失注・リード戻し等）は集計から除外。
    internal_only=True で 外部メンバー(member_kind='外部') を除外（Hisho稼働予定はこちら）。
    items にはクリック明細用の各アサイン（案件名・役割・期間・稼働率・確度）を返す。
    デモ開発負荷は別系統(Hisho側で加算)。ここでは delivery と base のみ返す。"""
    start_week = start_week or _monday_of(date.today())
    weeks = _weeks_from(start_week, n_weeks)
    week_end = weeks[-1] if weeks else start_week
    # owner -> week -> {actual:{committed,forecast}, billing:{committed,forecast}}
    cells: dict = {}
    items: list = []

    def _blank():
        return {"actual": {"committed": 0.0, "forecast": 0.0},
                "billing": {"committed": 0.0, "forecast": 0.0}}

    # 案件ごとの体制(役割)並び順（delivery_form と同じ id 昇順）。クリック内訳をこの順で並べる用。
    _role_order: dict = {}
    for rr in con.execute("SELECT delivery_id, role FROM delivery_roles ORDER BY delivery_id, id"):
        _m = _role_order.setdefault(rr["delivery_id"], {})
        if rr["role"] not in _m:
            _m[rr["role"]] = len(_m)

    for r in con.execute(
        "SELECT da.owner, da.role, da.member_kind, da.from_week, da.to_week, da.fte_pct, da.fte_billing, "
        "d.stage, d.status, d.deal_name, dv.id AS delivery_id, dv.title AS delivery_title, "
        "dv.start_week AS delivery_start, acc.name AS account_name "
        "FROM delivery_assignments da "
        "JOIN deliveries dv ON dv.id=da.delivery_id "
        "JOIN deals d ON d.id=dv.deal_id "
        "LEFT JOIN accounts acc ON acc.id=d.account_id"):
        stage = r["stage"] or ""
        status = r["status"] or "open"
        if status == "closed" and stage != "受注":
            continue  # 提案でクローズ（失注/リード戻し）は見込みから除外
        if internal_only and (r["member_kind"] or "内部") == "外部":
            continue
        key = "committed" if stage == "受注" else "forecast"
        actual = r["fte_pct"] or 0
        billing = float(r["fte_billing"]) if r["fte_billing"] is not None else actual
        for wk in weeks:
            if r["from_week"] <= wk <= r["to_week"]:
                c = cells.setdefault(r["owner"], {}).setdefault(wk, _blank())
                c["actual"][key] += actual
                c["billing"][key] += billing
        # 表示窓に期間が重なるアサインだけ明細に含める
        if r["from_week"] <= week_end and r["to_week"] >= start_week:
            items.append({
                "owner": r["owner"], "role": r["role"] or "", "member_kind": r["member_kind"] or "内部",
                "from_week": r["from_week"], "to_week": r["to_week"],
                "actual": actual, "billing": billing, "committed": (stage == "受注"),
                "deal_name": r["deal_name"] or "", "delivery_title": r["delivery_title"] or "",
                "account_name": r["account_name"] or "",
                "delivery_id": r["delivery_id"], "delivery_start": r["delivery_start"] or "",
                "role_order": _role_order.get(r["delivery_id"], {}).get(r["role"] or "", 9999),
            })
    base = base_workload_by_owner(con)
    owners = sorted(set(list(base.keys()) + list(cells.keys())),
                    key=lambda o: (OWNERS.index(o) if o in OWNERS else 999, o))
    return {"start_week": start_week, "weeks": weeks, "owners": owners,
            "base": base, "cells": cells, "items": items}


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


# ---- 週次スナップショット（前週比） ----

def week_start_of(d: date | None = None) -> str:
    """指定日（省略時は今日）が属する週の月曜をYYYY-MM-DDで返す。"""
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def save_weekly_snapshot(con, week_start: str, metrics: dict) -> None:
    """week_start週の各指標をupsertする（同週の再実行は上書き＝最新状態を保持）。"""
    for key, val in metrics.items():
        con.execute(
            "INSERT INTO weekly_snapshots (week_start, metric_key, metric_value, updated_at) "
            "VALUES (?,?,?,datetime('now')) "
            "ON CONFLICT(week_start, metric_key) DO UPDATE SET "
            "metric_value=excluded.metric_value, updated_at=datetime('now')",
            (week_start, key, None if val is None else float(val)),
        )
    con.commit()


def get_weekly_snapshot(con, week_start: str) -> dict:
    """week_start週のスナップショットを {metric_key: metric_value} で返す（無ければ空dict）。"""
    return {r["metric_key"]: r["metric_value"] for r in con.execute(
        "SELECT metric_key, metric_value FROM weekly_snapshots WHERE week_start=?", (week_start,))}


def list_snapshot_weeks(con) -> list[str]:
    """記録済みの週(week_start)を新しい順で返す。"""
    return [r["week_start"] for r in con.execute(
        "SELECT DISTINCT week_start FROM weekly_snapshots ORDER BY week_start DESC")]


# ---- 週次営業レポート（本文はDBのみに格納。Git(public)には置かない） ----

def list_weekly_reports(con) -> list[dict]:
    """レポートの号一覧（本文は除きメタ＋カバー画像）を新しい順(slug降順)で返す。"""
    return [dict(r) for r in con.execute(
        "SELECT slug, report_date, week_start, title, lead, cover_image, updated_at FROM weekly_reports "
        "ORDER BY slug DESC")]


def get_weekly_report(con, slug: str) -> dict | None:
    """slugの号を返す（本文html_body・カバー画像含む）。無ければNone。"""
    r = con.execute(
        "SELECT slug, report_date, week_start, title, lead, cover_image, html_body, created_at, updated_at "
        "FROM weekly_reports WHERE slug=?", (slug,)).fetchone()
    return dict(r) if r else None


def upsert_weekly_report(con, slug: str, report_date: str, title: str,
                         lead: str, html_body: str, cover_image: str = "",
                         week_start: str = "") -> None:
    """号を作成/更新（slug一致で上書き）。cover_imageはdata: URI（任意）。
    week_start=対象週の月曜(YYYY-MM-DD)。数字レール自動注入の基準（#39）。"""
    con.execute(
        "INSERT INTO weekly_reports (slug, report_date, week_start, title, lead, cover_image, html_body, updated_at) "
        "VALUES (?,?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "report_date=excluded.report_date, week_start=excluded.week_start, title=excluded.title, "
        "lead=excluded.lead, cover_image=excluded.cover_image, html_body=excluded.html_body, "
        "updated_at=datetime('now')",
        (slug, report_date, week_start or None, title, lead, cover_image or "", html_body),
    )
    con.commit()


def delete_weekly_report(con, slug: str) -> None:
    con.execute("DELETE FROM weekly_reports WHERE slug=?", (slug,))
    con.commit()
