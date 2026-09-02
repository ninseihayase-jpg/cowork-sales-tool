"""営業情報DB（独立）。アカウント / コンタクト / 商談 / 活動。

フェーズ2-1の正本DB。SQLite。テーマDBとは別物だが、商談(deal)は theme_id で
テーマDBのSalesテーマと対応づけ、同期できる（cowork/theme_link.py）。

設計の正本: docs/00_設計構想.md §6。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
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
# Delivery請求関連マスタ（2026-08-28）。請求方法/請求期日はマスタで編集可能にし、
# 経費請求有無は固定enum（ドロップダウン+自由記述の値ドロップダウン側のみ）。
DELIVERY_BILLING_METHODS = ["請求書送付(オペレータ)", "顧客PF入力(アサイン者)", "顧客PF入力(オペレータ)"]
DELIVERY_BILLING_DUE_OPTIONS = ["当月末日", "翌月1日", "翌月2日", "翌月3日"]
DELIVERY_BILLING_DUE_DEFAULT = "当月末日"
DELIVERY_EXPENSE_BILLING_OPTIONS = ["有", "無", "不明(要確認)"]
DELIVERY_PERFORMANCE_FEE_OPTIONS = ["有", "無"]  # 成果報酬有無（2026-08-30）。「有」の場合のみ比率入力が必須。
# 論点(deal_issues)の会社機能（#147）。商談に紐づかない論点（deal_id IS NULL＝商談共通）の
# 場合に、社内のどの機能に紐づくかを選択する。マスタ画面で編集可能。
COMPANY_FUNCTIONS = ["経営企画", "総務", "法務", "人事", "財務", "経理"]

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
    "delivery_billing_methods": DELIVERY_BILLING_METHODS,
    "delivery_billing_due":     DELIVERY_BILLING_DUE_OPTIONS,
    "company_functions":        COMPANY_FUNCTIONS,
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
    "delivery_billing_methods": "Delivery請求方法",
    "delivery_billing_due":     "Delivery請求期日",
    "company_functions":        "論点の会社機能（商談共通論点向け）",
}
COST_STAGES = ["診断中", "削減機会発見", "削減提案中", "削減実行中", "成果確定", "不発"]

# Delivery（受注後・納品）アサイン計画（#75）。デモ開発とは別系統。
DELIVERY_STATUSES = ["進行中", "完了", "保留", "中止"]
DELIVERY_VIEW_WEEKS = 16              # 全社稼働テーブルの初期表示週数（今週〜。調整可）
POINTS_PER_FTE = 20                   # デモ開発点数→FTE%換算の基準（20点≒100%FTE）※デモ負荷率自体は個人上限基準
# Delivery案件を自動起票するステージ（「提案」到達以降）。
DELIVERY_TRIGGER_STAGES = ("提案", "クロージング", "受注")
# ヒートマップ閾値(%)。100%超が常態のため150%も閾値に。定数で調整可。
DELIVERY_HEAT_THRESHOLDS = {"ok": 70, "full": 100, "over": 150}
# Deliveryの確度（2026-08-18: 見込みを提案中/クロージングの2段階に分割）。
# 自動導出=商談のstage/statusから毎回算出。deliveries.confidence_overrideがあればそれを優先（人間の手修正）。
DELIVERY_CONFIDENCE_LEVELS = ["見込み(提案中)", "見込み(クロージング)", "確定", "無効(終了)"]


def delivery_confidence_auto(deal_stage: str | None, deal_status: str | None) -> str:
    """Deliveryの確度を商談のstage/statusから自動導出する（DELIVERY_CONFIDENCE_LEVELSのいずれか）。
    confidence_overrideの手修正は考慮しない（呼び出し側でdelivery_confidence_effectiveを使うこと）。"""
    stage = deal_stage or ""
    status = deal_status or "open"
    if status == "closed" and stage != "受注":
        return "無効(終了)"
    if stage == "受注":
        return "確定"
    if stage == "クロージング":
        return "見込み(クロージング)"
    return "見込み(提案中)"


def delivery_confidence_effective(dv: dict) -> str:
    """confidence_override（人間の手修正）があれば優先、無ければ自動導出。
    dv は deal_stage/deal_status/confidence_override を含む辞書（get_delivery/list_deliveriesの戻り値）。"""
    override = dv.get("confidence_override")
    if override in DELIVERY_CONFIDENCE_LEVELS:
        return override
    return delivery_confidence_auto(dv.get("deal_stage"), dv.get("deal_status"))


def delivery_is_active(dv: dict) -> bool:
    """状態=進行中、かつ確度≠無効(終了)のDeliveryのみ「稼働中」として稼働計算・KPI集計の対象にする
    （完了/保留/中止、および確度が無効(終了)＝手修正含む、はいずれも対象外）。"""
    if (dv.get("status") or "進行中") != "進行中":
        return False
    return delivery_confidence_effective(dv) != "無効(終了)"

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

# コンサルタスクのガントチャート用「工数感」（ユーザー確定・2026-08-19）。
# 期日から工数感の営業日数ぶん逆算した日を開始日にする（当日/作成日より前でもよい）。
TASK_EFFORT_LEVELS = ["軽", "中", "重", "超重"]
TASK_EFFORT_DAYS = {"軽": 1, "中": 2, "重": 5, "超重": 10}
# 工数感→作業時間(h)の既定換算（2026-08-24: tasks.effort_hours未入力時のフォールバック用）。
# effort_hoursを明示入力した場合は常にそちらを優先する（delivery報酬額のcompute_delivery_feeと同思想）。
TASK_EFFORT_HOURS = {"軽": 2.0, "中": 4.0, "重": 10.0, "超重": 20.0}
TASK_EFFORT_HOURS_DEFAULT = TASK_EFFORT_HOURS["中"]


def task_gantt_range(due_date: str | None, effort_level: str | None) -> tuple[str | None, str | None]:
    """(開始日, 終了日)を返す。終了日=期日。開始日=期日から工数感の営業日数ぶん逆算。
    期日または工数感が無ければ開始日は算出不能(None)（終了日だけは期日があれば返す）。"""
    due = (due_date or "").strip()
    if not due:
        return None, None
    days = TASK_EFFORT_DAYS.get(effort_level or "")
    if days is None:
        return None, due
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return None, None
    start = add_business_days(d, -days)
    return start.isoformat(), due


def effective_effort_hours(task: dict) -> float:
    """タスクの所要作業時間(h)。effort_hours明示入力があれば常に優先、無ければ工数感から
    TASK_EFFORT_HOURSで補完（さらに無ければ既定=中）。ユーザー要望2026-08-24。"""
    hours = task.get("effort_hours")
    if hours is not None:
        try:
            return float(hours)
        except (TypeError, ValueError):
            pass
    return TASK_EFFORT_HOURS.get(task.get("effort_level") or "", TASK_EFFORT_HOURS_DEFAULT)

# 繰り返し発生（定期複製）。事務タスク等のテンプレカードに付与し、複製タイミングが来たら
# 期間分の新規カードを複製生成する（→ duplicate_due_recurring_tasks）。
TASK_RECUR_FREQS = ["monthly", "weekly"]         # 頻度: 毎月 / 毎週
RECUR_WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]  # 0=月 .. 6=日（date.weekday()準拠）

# 事務員向けタスク（is_admin=1）。各メンバーから事務員に降ってくる依頼を分離管理する専用ビュー
# (/desk-tasks) 用。カテゴリは開発系タスク(TASK_CATEGORIES)とは別体系。
ADMIN_TASK_CATEGORIES = ["書類作成", "経費・請求", "予約・手配", "データ入力", "連絡・調整", "庶務", "その他"]
# 請求関連（請求書作成・請求に対する支払い）は業務上の重要度が高く、AI判定の揺れで
# 「経費・請求」に分類されない漏れが多発したため、キーワードでの確実な強制判定を用意する。
BILLING_KEYWORDS = ["請求書", "請求", "インボイス", "振込", "支払い", "支払"]


def is_billing_task(title: str, detail: str = "") -> bool:
    text = f"{title or ''} {detail or ''}"
    return any(kw in text for kw in BILLING_KEYWORDS)
# 事務タスクの事務員（複数可）。環境変数 DESK_ASSIGNEE をカンマ/読点/空白区切りで解釈。
# 先頭＝既定担当（原則この人に自動割当）、残り＝パス先候補（例: "あみ,磯部" → 既定=あみ、磯部にパス可）。
# 特定個人名をハードコードしない（owners マスタに存在する担当名を想定）。
DESK_ASSIGNEES = [x.strip() for x in
                  os.environ.get("DESK_ASSIGNEE", "").replace("、", ",").replace(" ", ",").split(",")
                  if x.strip()]
DESK_ASSIGNEE_DEFAULT = DESK_ASSIGNEES[0] if DESK_ASSIGNEES else ""
TASK_LINK_TYPES = ["dev_project", "deal", "issue", "delivery", "org", "personal"]
TASK_LINK_LABELS = {"dev_project": "開発案件", "deal": "商談", "issue": "論点", "delivery": "Delivery",
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
    intake_transcript_id INTEGER,     -- 取り込み(intake_transcripts.id)経由で作成された場合の出典（無ければNULL）
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
    first_seen_at  TEXT,      -- このスレッドを最初に検知した時刻（INSERT OR REPLACEでも不変。放置検知用）
    reminded_at    TEXT,      -- 放置リマインドを送った時刻（state変更のたびNULLに戻り再送可能になる）
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
    intake_transcript_id INTEGER,              -- 取り込み(intake_transcripts.id)経由の場合の出典
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

-- ヒアリングAI(#29): 面談セッション。文字起こし(ソース非依存)→AI整形→人の確認→確定 の作業台。
-- source は取り込み元('paste'|'jamie'|...)。structured_json はAI整形結果(項目別/全体像/NextStep/メール素案)。
-- status: 'imported'(取込済) → 'structured'(AI整形済) → 'confirmed'(確定=hearing_resultsへ反映)。
CREATE TABLE IF NOT EXISTS hearing_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id        INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    template_id    INTEGER,
    source         TEXT DEFAULT 'paste',       -- 'paste'|'jamie'|...（TranscriptSource）
    external_id    TEXT,                        -- Jamie meeting id 等（自動取込の冪等キー）
    conducted_on   TEXT,
    transcript     TEXT,                        -- 生文字起こし
    structured_json TEXT NOT NULL DEFAULT '{}', -- {items:[{label,answer}],overview,nextsteps:[..],email_draft}
    status         TEXT DEFAULT 'imported',
    result_id      INTEGER,                     -- 確定時に紐づく hearing_results.id
    intake_transcript_id INTEGER,               -- 元になったintake_transcripts.id（structure時に確定・不変）
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_hearing_sessions_deal ON hearing_sessions(deal_id);

-- AI整形の取り込み原本（文字起こしローデータ＋アップロード原本ファイル）。整形メモとは別に保存・参照する。
CREATE TABLE IF NOT EXISTS intake_transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,               -- 'issue'|'deal'|'inbox'（inbox=自動受信の未割り当て）
    entity_id   INTEGER NOT NULL,            -- 論点id/商談id（inboxは0）
    source      TEXT DEFAULT 'paste',        -- 'paste'|'file'|'jamie'|'zoom'
    filename    TEXT,                        -- アップロード時の元ファイル名
    transcript  TEXT,                        -- 抽出した文字起こし本文（生データ）
    file_blob   BLOB,                        -- 原本バイト（ファイルアップロード時のみ）
    external_source TEXT,                     -- 自動連携元 'jamie'|'zoom'
    external_id TEXT,                         -- 会議ID（冪等キー: external_source+external_id）
    title       TEXT,                         -- 会議タイトル
    occurred_on TEXT,                         -- 会議日 YYYY-MM-DD
    attendees_json TEXT,                      -- 参加者/招待者 [{name,email}] のJSON
    raw_summary TEXT,                         -- ソース側の要約（あれば）
    status      TEXT,                         -- NULL/'saved'=割当済, 'inbox'=未割当, 'assigned'=割当済(自動由来)
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_intake_transcripts_ent ON intake_transcripts(kind, entity_id);
-- 注: external_source/external_id はマイグレーションで追加する列のため、その複合インデックスは
-- SCHEMA(executescript)では作らない（既存DBは列未追加でここが失敗する）。init_db()のALTER後に作成する。

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
    members     TEXT,                 -- 議論メンバー（社員名の複数選択、カンマ区切り。OWNERS準拠）
    responsible TEXT,                 -- 責任者（社員1名、OWNERS）
    status      TEXT DEFAULT '議論中', -- 議論中/議論済み/取り消し
    due_date    TEXT,                 -- 解消期限（YYYY-MM-DD）
    ai_summary  TEXT,                 -- メモ全履歴からAI自動生成したサマリー
    company_function TEXT,            -- 会社機能（#147。deal_id IS NULLの商談共通論点向け。
                                       -- 経営企画/総務/法務/人事/財務/経理等。company_functionsマスタ準拠）
    note_lock_salt TEXT,              -- 論点メモのパスワードロック（2026-08）。NULL=ロック無し
    note_lock_hash TEXT,              -- sha256(salt+password)。NULL=ロック無し
    note_lock_recovery_email TEXT,    -- パスワード忘れ時の連絡先（本人確認クリック用リンクの送付先）
    note_lock_reset_token TEXT,       -- パスワード忘れリセット用トークン（発行後24h有効）
    note_lock_reset_expires TEXT,     -- 上記トークンの有効期限(YYYY-MM-DD HH:MM:SS)
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_deal_issues_deal ON deal_issues(deal_id);

-- 論点の検討材料（社内資料の体系化・層1、2026-08-28）。論点メモ(人が書く)とは別の、
-- 調査結果・AIレポート等を雑に投げ込むだけの置き場。層2(検討資料)生成時にAIがまとめて読む。
CREATE TABLE IF NOT EXISTS issue_materials (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES deal_issues(id) ON DELETE CASCADE,
    title      TEXT,                 -- 表示名（未入力ならcontent先頭から自動生成）
    content    TEXT NOT NULL,        -- 生の中身（markdown/プレーンテキスト想定）
    source_url TEXT,                 -- 元になったAIチャット/ドキュメントへのリンク（任意）
    added_by   TEXT,                 -- 投げ込んだ人
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_issue_materials_issue ON issue_materials(issue_id);

-- 社内資料の体系化・層2/3「検討資料」「社内報告ペーパー」の格納庫（2026-08-28）。
-- 週次レポート(weekly_reports)とは別立て。ナビ枠なしのシンプルHTMLページとして配信する。
CREATE TABLE IF NOT EXISTS docs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,        -- '検討資料' / '報告ペーパー' / 'その他'
    template   TEXT,                 -- テンプレ種別キー（例: 'process_change'）。手動アップロード分はNULL
    title      TEXT NOT NULL,
    issue_id   INTEGER REFERENCES deal_issues(id) ON DELETE SET NULL,  -- 紐づく論点（任意）
    body_html  TEXT NOT NULL,        -- 本文HTMLフラグメント（表示時に共通レイアウトで包む）
    created_by TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_docs_issue ON docs(issue_id);

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
    intake_transcript_id INTEGER,  -- 取り込み(intake_transcripts.id)経由で作成された場合の出典
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
    remind_last_at TEXT,
    is_recurring  INTEGER DEFAULT 0,       -- 繰り返し発生テンプレ（1=このカードを定期複製する）
    recur_freq    TEXT,                    -- 'monthly'（毎月）/ 'weekly'（毎週）
    recur_dup_day INTEGER,                 -- 複製タイミング。毎月=月の日(1-31) / 毎週=曜日(0=月..6=日)
    recur_last_period TEXT                 -- 最後に複製した期間キー 'YYYY-MM' / 'YYYY-Www'（冪等ガード）
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

-- タスクと商談/論点/Delivery/開発案件の多対多関連付け（#146、2026-09-02）。
-- tasks.link_type/link_idは単一紐づけ時代の名残の列で、以後は新規に書き込まない
-- （非破壊のため列自体は残置）。読み出しは両方をマージする(sfa_db.get_task_links等)ため、
-- 旧データのバックフィルは不要——新UIでその タスクの関連付けを一度編集すれば、
-- この表へ完全移行する(set_task_links)。
CREATE TABLE IF NOT EXISTS task_entity_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    link_type  TEXT NOT NULL,      -- deal/issue/delivery/dev_project
    link_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, link_type, link_id)
);
CREATE INDEX IF NOT EXISTS idx_task_entity_links_task ON task_entity_links(task_id);
CREATE INDEX IF NOT EXISTS idx_task_entity_links_entity ON task_entity_links(link_type, link_id);

-- タスクに貼る関連リンク（名前付き。ガントのフローティング編集等から追加、2026-08-28）。
CREATE TABLE IF NOT EXISTS task_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    label      TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_links_task ON task_links(task_id);

-- Slack起票(@メンション/リアクション)がAIで複数タスクに分割できると判定した際の、
-- 「分割するか/1件のまま登録するか」ユーザー確認待ちの一時データ（#151、2026-09-02）。
-- 確認ボタンが押されるまでタスクは作成しない。行はボタン押下後（または一定時間放置後の
-- クリーンアップ）に削除する使い捨てデータ。
CREATE TABLE IF NOT EXISTS pending_task_splits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    thread_ts     TEXT NOT NULL,
    user_id       TEXT,
    text          TEXT NOT NULL,
    is_admin      INTEGER DEFAULT 0,
    token         TEXT,
    prefills_json TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now'))
);

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
-- 1商談に複数可（deal_id は非UNIQUE）。確度（見込み(提案中)/見込み(クロージング)/確定/無効(終了)）は
-- 紐づく deal.stage/status から自動導出するのが既定だが、confidence_override で人間が上書きできる。
CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id     INTEGER NOT NULL,          -- 紐づく商談（非UNIQUE）
    title       TEXT,                      -- 納品案件名（既定=商談名）
    start_week  TEXT,                      -- 開始週の月曜(YYYY-MM-DD)
    end_week    TEXT,                      -- 終了週の月曜(YYYY-MM-DD)
    status      TEXT DEFAULT '進行中',      -- 進行中/完了/保留/中止
    overview    TEXT,                      -- 概要・納品方針（自由記述）
    fee_mode    TEXT DEFAULT 'monthly',    -- 報酬形態: monthly=月額報酬 / total=総額報酬（どちらを入力するか）
    fee_monthly REAL,                      -- 報酬額/月額（万円）
    fee_total   REAL,                      -- 報酬額/総額（万円）。期間の月数で月額と相互換算
    confidence_override TEXT,              -- 確度の手修正。NULL=自動導出。値ありならDELIVERY_CONFIDENCE_LEVELSのいずれか
    cost_mode   TEXT DEFAULT 'monthly',    -- 外注費の入力形態: monthly=月額 / total=総額（fee_modeと同じ仕組み）
    cost_monthly REAL,                     -- 外注費/月額（万円）
    cost_total   REAL,                     -- 外注費/総額（万円）。期間の月数でcost_monthlyと相互換算
    cost_vendor  TEXT,                     -- 外注先名（自由記述）
    payment_cycle_months INTEGER DEFAULT 1, -- 支払いサイクル: 検収月から何ヶ月後に入金されるか（既定=翌月）
    business_type_l1_override TEXT,        -- 事業種別L1の手修正。NULL=紐づく商談のL1を継承
    business_type_l2_override TEXT,        -- 事業種別L2の手修正。NULL=紐づく商談のL2を継承
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (deal_id) REFERENCES deals(id) ON DELETE CASCADE
);

-- Delivery月別検収額（2026-08）。fee_monthly/fee_totalの均した換算では実際の入金月がズレるため、
-- 月ごとの検収額を実額で入力し、payment_cycle_months分ズラして月別入金額を算出する。
CREATE TABLE IF NOT EXISTS delivery_receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL,
    month       TEXT NOT NULL,              -- 検収月(YYYY-MM)
    amount      REAL NOT NULL,              -- 検収金額（万円）
    UNIQUE(delivery_id, month),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE
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

-- コンサルタスクの容量管理（2026-08-24）。日別の実測/手修正の作業可能時間(h)。
-- 「毎朝手修正する」対象の値そのもの。未設定の日はowner_daily_capacity_default（masters）→
-- グローバル既定(TASK_DAILY_CAPACITY_DEFAULT_HOURS)にフォールバックする（capacity_at参照）。
CREATE TABLE IF NOT EXISTS owner_daily_capacity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    day         TEXT NOT NULL,               -- YYYY-MM-DD
    hours       REAL NOT NULL,
    source      TEXT DEFAULT 'manual',       -- manual / calendar（将来のカレンダー自動算出用の出自記録）
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(owner, day)
);
CREATE INDEX IF NOT EXISTS idx_odc_owner_day ON owner_daily_capacity(owner, day);

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

-- 週次稼働の手動編集（#稼働予定）。owner×week（ISO月曜）ごとに、Deliveryやベースの
-- 自動計算を複製して手動で組み替えた「手動プラン」。存在する(owner,week)は手動採用、
-- 無ければ自動。項目(label×pct×種別)＋セル単位の備考。Hishoダッシュボードから編集(POST)。
CREATE TABLE IF NOT EXISTS weekly_workload_manual (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,             -- メンバー
    week        TEXT NOT NULL,             -- ISO月曜 'YYYY-MM-DD'
    label       TEXT NOT NULL,             -- 項目名（案件名/機能名/自由入力）
    kind        TEXT,                      -- 'D'(Delivery)/'base'/'other'
    pct         REAL NOT NULL DEFAULT 0,   -- 稼働率(%)
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS weekly_workload_note (
    owner       TEXT NOT NULL,
    week        TEXT NOT NULL,
    note        TEXT,                      -- セル単位の備考メモ
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (owner, week)
);
-- コンサルタスク「今日明日のタスク」機能（#101, 2026-08-27）。担当が今日/明日やる分を
-- 軽い/重いに仕分けて15分刻みカレンダーへ配置し、確定した状態をスナップショットとして
-- 追記保存する（上書きしない＝再計画のたびに新しい行が増える）。
CREATE TABLE IF NOT EXISTS daily_task_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    label       TEXT NOT NULL,             -- "早瀬/8/27 09:30時点"
    base_date   TEXT NOT NULL,             -- 当日基準日 YYYY-MM-DD（翌日はbase_date+1日）
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS daily_task_plan_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL REFERENCES daily_task_plans(id) ON DELETE CASCADE,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day_offset    INTEGER NOT NULL,        -- 0=当日起点の経過日数（既定は直近5日分＝0〜4。2026-08-31改称・拡張）
    start_min     INTEGER NOT NULL,        -- 06:00からの経過分（0〜899、15分刻み）
    duration_min  INTEGER NOT NULL,
    lane          INTEGER NOT NULL DEFAULT 0,
    bucket        TEXT NOT NULL            -- '軽い' / '重い'
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
        acc_cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
        if "aliases" not in acc_cols:
            # 取り込みインボックスの候補検出（_inbox_candidates）が社名の慣用的な略称
            # （例: 住友重工業→住重）を機械的には検出できないため、手動登録の辞書として追加。
            con.execute("ALTER TABLE accounts ADD COLUMN aliases TEXT")
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
        if "first_seen_at" not in thread_cols:
            con.execute("ALTER TABLE slack_threads ADD COLUMN first_seen_at TEXT")
            con.execute("UPDATE slack_threads SET first_seen_at=created_at WHERE first_seen_at IS NULL")
        if "reminded_at" not in thread_cols:
            con.execute("ALTER TABLE slack_threads ADD COLUMN reminded_at TEXT")
        # 取り込み文字起こしの出典を各生成物（活動履歴/ヒアリング結果/セッション/リッチメモ）から
        # 追える紐づけ列（intake_transcript_id）。無ければ後付けで追加。
        act_cols = {r[1] for r in con.execute("PRAGMA table_info(activities)")}
        if "intake_transcript_id" not in act_cols:
            con.execute("ALTER TABLE activities ADD COLUMN intake_transcript_id INTEGER")
        hr_cols = {r[1] for r in con.execute("PRAGMA table_info(hearing_results)")}
        if "intake_transcript_id" not in hr_cols:
            con.execute("ALTER TABLE hearing_results ADD COLUMN intake_transcript_id INTEGER")
        hs_cols = {r[1] for r in con.execute("PRAGMA table_info(hearing_sessions)")}
        if "intake_transcript_id" not in hs_cols:
            con.execute("ALTER TABLE hearing_sessions ADD COLUMN intake_transcript_id INTEGER")
        rn_cols = {r[1] for r in con.execute("PRAGMA table_info(rich_notes)")}
        if "intake_transcript_id" not in rn_cols:
            con.execute("ALTER TABLE rich_notes ADD COLUMN intake_transcript_id INTEGER")
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
        # ソフト削除（削除の確認・復活用）。deleted_at が入っていれば削除済み扱い。
        if _task_cols and "deleted_at" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")
        # 事務員向けタスク（/desk-tasks）用の後方互換追加。破壊的変更はしない。
        if _task_cols and "is_admin" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN is_admin INTEGER DEFAULT 0")
        if _task_cols and "requester" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN requester TEXT")
        if _task_cols and "slack_permalink" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN slack_permalink TEXT")
        # 繰り返し発生（定期複製）用の後方互換追加。テンプレカードにフラグ＋頻度＋複製日＋
        # 最終複製期間キーを持たせる（duplicate_due_recurring_tasks が使用）。破壊的変更はしない。
        if _task_cols and "is_recurring" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN is_recurring INTEGER DEFAULT 0")
        if _task_cols and "recur_freq" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN recur_freq TEXT")
        if _task_cols and "recur_dup_day" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN recur_dup_day INTEGER")
        if _task_cols and "recur_last_period" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN recur_last_period TEXT")
        # コンサルタスクのガントチャート用「工数感」（軽/中/重/超重）。破壊的変更はしない。
        if _task_cols and "effort_level" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN effort_level TEXT")
        # 所要作業時間(h)。effort_levelの明示的な上書き（2026-08-24、工数時間ベースのスケジューリング）。
        if _task_cols and "effort_hours" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN effort_hours REAL")
        # ガント開始日の手動上書き（#152、2026-09-03）。未設定ならtask_gantt_range/
        # compute_owner_scheduleによる自動計算のまま。ガント上でバーをドラッグ移動/
        # 左端をリサイズすると、ここに実際の開始日が保存され、以後は自動計算より優先される
        # （dev_projectsの「既存値があれば再計算で上書きしない」という考え方と同じ設計）。
        if _task_cols and "gantt_start_date" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN gantt_start_date TEXT")
        # 事務タスクの期限確認プロセス（2026-08-27）。Slack起票時のAI抽出/既定値はあくまで提案で、
        # 依頼者本人の「OK」または期限の返信で確定するまでは0（未確定）。既定1＝確認不要
        # （通常タスク・Web手入力等、そもそもこのフローの対象外のものは常に確定扱い）。
        if _task_cols and "due_date_confirmed" not in _task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN due_date_confirmed INTEGER DEFAULT 1")
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
        # Delivery確度の手修正（自動導出=商談ステージ連動に対する人間の上書き。NULL=自動）。
        if _dv_cols and "confidence_override" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN confidence_override TEXT")
        # Delivery 外注費（2026-08）: 報酬額と同じ仕組みで月額/総額のどちらかを入力→相互換算。
        if _dv_cols and "cost_mode" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN cost_mode TEXT DEFAULT 'monthly'")
        if _dv_cols and "cost_monthly" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN cost_monthly REAL")
        if _dv_cols and "cost_total" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN cost_total REAL")
        if _dv_cols and "cost_vendor" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN cost_vendor TEXT")
        # 月別入金計画（2026-08）: 検収月から何ヶ月後に入金されるか。
        if _dv_cols and "payment_cycle_months" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN payment_cycle_months INTEGER DEFAULT 1")
        # 事業種別L1/L2の手修正（2026-08）。既定は紐づく商談のL1/L2を継承。
        if _dv_cols and "business_type_l1_override" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN business_type_l1_override TEXT")
        if _dv_cols and "business_type_l2_override" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN business_type_l2_override TEXT")
        # 責任者/担当者（アサインリストから選択。2026-08-28）＋請求関連（ユーザー要望2026-08-28）。
        if _dv_cols and "responsible_owner" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN responsible_owner TEXT")
        if _dv_cols and "handling_owner" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN handling_owner TEXT")
        if _dv_cols and "billing_method" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN billing_method TEXT")
        if _dv_cols and "billing_due" not in _dv_cols:
            con.execute(f"ALTER TABLE deliveries ADD COLUMN billing_due TEXT DEFAULT '{DELIVERY_BILLING_DUE_DEFAULT}'")
        if _dv_cols and "billing_recipient" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN billing_recipient TEXT")
        if _dv_cols and "expense_billing" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN expense_billing TEXT")
        if _dv_cols and "expense_billing_note" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN expense_billing_note TEXT")
        # 成果報酬有無/比率（2026-08-30。「有」の場合のみ比率入力が必須＝webapp側でチェック）。
        if _dv_cols and "performance_fee" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN performance_fee TEXT")
        if _dv_cols and "performance_fee_ratio" not in _dv_cols:
            con.execute("ALTER TABLE deliveries ADD COLUMN performance_fee_ratio REAL")
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
        # 論点: 責任者カラムを後方互換で追加。
        _issue_cols = {c[1] for c in con.execute("PRAGMA table_info(deal_issues)")}
        if "responsible" not in _issue_cols:
            con.execute("ALTER TABLE deal_issues ADD COLUMN responsible TEXT")
        if "company_function" not in _issue_cols:  # #147
            con.execute("ALTER TABLE deal_issues ADD COLUMN company_function TEXT")
        # 論点メモのパスワードロック（2026-08）。
        for _col, _ddl in (
            ("note_lock_salt", "TEXT"), ("note_lock_hash", "TEXT"),
            ("note_lock_recovery_email", "TEXT"), ("note_lock_reset_token", "TEXT"),
            ("note_lock_reset_expires", "TEXT"),
        ):
            if _col not in _issue_cols:
                con.execute(f"ALTER TABLE deal_issues ADD COLUMN {_col} {_ddl}")
        # 議論メンバーを区分（経営/営業担当/営業+開発担当/開発コア）から社員名方式へ移行。
        # 旧区分のみで構成された members は一括クリアし、以後は社員名で入れ直す（ユーザー確定 2026-08-13）。
        # 冪等: クリア後は名前が入るため下の判定に再度該当せず、二重実行しても影響しない。
        _legacy_member_tokens = {"経営", "営業担当", "営業+開発担当", "開発コア"}
        for _rid, _mem in con.execute(
            "SELECT id, members FROM deal_issues WHERE members IS NOT NULL AND members != ''"
        ).fetchall():
            _toks = [t.strip() for t in (_mem or "").split(",") if t.strip()]
            if _toks and all(t in _legacy_member_tokens for t in _toks):
                con.execute("UPDATE deal_issues SET members=NULL WHERE id=?", (_rid,))
        # 取り込み原本テーブルに自動連携(インボックス)用カラムを後方互換で追加。
        _it_cols = {c[1] for c in con.execute("PRAGMA table_info(intake_transcripts)")}
        for _col, _decl in (
            ("external_source", "TEXT"), ("external_id", "TEXT"), ("title", "TEXT"),
            ("occurred_on", "TEXT"), ("attendees_json", "TEXT"), ("raw_summary", "TEXT"),
            ("status", "TEXT"),
        ):
            if _col not in _it_cols:
                con.execute(f"ALTER TABLE intake_transcripts ADD COLUMN {_col} {_decl}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_intake_transcripts_ext "
                    "ON intake_transcripts(external_source, external_id)")
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


# ---- コンサルタスクの容量管理（2026-08-24） ----

TASK_DAILY_CAPACITY_DEFAULT_HOURS = 4.0  # グローバル既定（担当者別既定値も未設定の場合の最終フォールバック）


def get_owner_daily_capacity_default(con) -> dict:
    """担当者→1日あたり既定作業可能時間(h) のマップ（masters key='owner_daily_capacity_default'）。
    未設定は空dict（=全員TASK_DAILY_CAPACITY_DEFAULT_HOURSにフォールバック）。"""
    row = con.execute(
        "SELECT values_json FROM masters WHERE key='owner_daily_capacity_default'").fetchone()
    if row:
        try:
            data = _json.loads(row[0])
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items() if str(v).strip()}
        except (ValueError, TypeError):
            print("[masters] owner_daily_capacity_default broken, ignoring", flush=True)
    return {}


def set_owner_daily_capacity_default(con, mapping: dict) -> None:
    """担当者別の既定作業可能時間(h)を保存。空/不正値は除外（＝未設定＝グローバル既定にフォールバック）。"""
    clean = {}
    for k, v in mapping.items():
        k = str(k).strip()
        if not k:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            clean[k] = fv
    con.execute(
        "INSERT INTO masters(key,values_json) VALUES('owner_daily_capacity_default',?) "
        "ON CONFLICT(key) DO UPDATE SET values_json=excluded.values_json",
        (_json.dumps(clean, ensure_ascii=False),))
    con.commit()


def capacity_at(con, owner: str, day: str) -> float:
    """指定担当者の指定日(YYYY-MM-DD)の作業可能時間(h)。
    フォールバックチェイン: owner_daily_capacity(その日の実測/手修正値) →
    owner_daily_capacity_default[owner] → TASK_DAILY_CAPACITY_DEFAULT_HOURS。base_max_atと同型。"""
    row = con.execute("SELECT hours FROM owner_daily_capacity WHERE owner=? AND day=?",
                      (owner, day)).fetchone()
    if row is not None:
        return float(row["hours"])
    default = get_owner_daily_capacity_default(con).get(owner)
    return default if default is not None else TASK_DAILY_CAPACITY_DEFAULT_HOURS


def has_owner_capacity_data(con, owner: str) -> bool:
    """この担当者に容量データ（実測日 or 既定値）が1件でもあるか。無ければスケジューラは
    従来のtask_gantt_range（工数感→固定営業日数の逆算のみ）にフォールバックする
    （容量未設定の担当者の挙動を変えないための無停止移行ガード）。"""
    if owner in get_owner_daily_capacity_default(con):
        return True
    row = con.execute("SELECT 1 FROM owner_daily_capacity WHERE owner=? LIMIT 1", (owner,)).fetchone()
    return row is not None


def set_owner_daily_capacity(con, owner: str, day: str, hours, source: str = "manual") -> None:
    """日別の作業可能時間(h)を保存（「毎朝手修正する」対象そのもの）。hours空/不正時はその日の
    実測値を削除（＝既定値へフォールバックし戻す）。"""
    try:
        fh = float(hours) if hours not in (None, "") else None
    except (TypeError, ValueError):
        fh = None
    if fh is None:
        con.execute("DELETE FROM owner_daily_capacity WHERE owner=? AND day=?", (owner, day))
    else:
        con.execute(
            "INSERT INTO owner_daily_capacity (owner, day, hours, source) VALUES (?,?,?,?) "
            "ON CONFLICT(owner, day) DO UPDATE SET hours=excluded.hours, source=excluded.source, "
            "updated_at=datetime('now')", (owner, day, fh, source))
    con.commit()


def list_owner_daily_capacity(con, owner: str, from_day: str, to_day: str) -> dict:
    """[from_day, to_day]（両端含む、YYYY-MM-DD）の実測/手修正値のみを {day: hours} で返す
    （既定値へのフォールバックはcapacity_at側の責務。ここは「明示的に設定済みの日」だけ）。"""
    rows = con.execute(
        "SELECT day, hours FROM owner_daily_capacity WHERE owner=? AND day BETWEEN ? AND ? ORDER BY day",
        (owner, from_day, to_day)).fetchall()
    return {r["day"]: r["hours"] for r in rows}


def compute_owner_schedule(con, owner: str, *, horizon_days: int = 90, today: date | None = None) -> dict | None:
    """担当者ownerの未完了コンサルタスクを、capacity_atの容量に従って営業日ごとに貪欲に割り当てる
    （キュー順=list_tasksの既定順＝pinned優先→期限昇順→id降順、をそのまま使う）。
    戻り値: {"starts": {task_id: 'YYYY-MM-DD'}, "ends": {...},
             "daily_load": {day: {"allocated": h, "capacity": h}}}。
    容量データが1件も無い担当者はNone（呼び出し側はtask_gantt_rangeへフォールバックすること＝
    容量未設定の担当者の挙動を変えないための無停止移行ガード）。"""
    if not owner or not has_owner_capacity_data(con, owner):
        return None
    today = today or date.today()
    tasks = [t for t in list_tasks(con, assignee=owner, admin=False)
             if (t.get("status") or "") not in ("完了", "保留")]
    starts, ends, daily_load = {}, {}, {}

    def _next_business_day(d):
        while not is_business_day(d):
            d += timedelta(days=1)
        return d

    day = _next_business_day(today)
    day_remaining = capacity_at(con, owner, day.isoformat())
    daily_load[day.isoformat()] = {"allocated": 0.0, "capacity": day_remaining}

    for t in tasks:
        remaining = effective_effort_hours(t)
        if remaining <= 0:
            continue
        first_day = last_day = None
        while remaining > 1e-9:
            if day_remaining <= 1e-9:
                day = _next_business_day(day + timedelta(days=1))
                if (day - today).days > horizon_days:
                    break  # 計算範囲超過は打ち切り（このタスクは以降未割当のまま）
                day_remaining = capacity_at(con, owner, day.isoformat())
                daily_load[day.isoformat()] = {"allocated": 0.0, "capacity": day_remaining}
                continue
            alloc = min(remaining, day_remaining)
            daily_load[day.isoformat()]["allocated"] += alloc
            remaining -= alloc
            day_remaining -= alloc
            first_day = first_day or day.isoformat()
            last_day = day.isoformat()
        if first_day:
            starts[t["id"]] = first_day
            ends[t["id"]] = last_day
    return {"starts": starts, "ends": ends, "daily_load": daily_load}


def latest_start_date(con, task: dict, owner: str | None = None) -> str | None:
    """タスク単体の「これより後に始めると期限に間に合わない」開始日（他タスクとの競合は見ない、
    タスク単体の安全マージンのみ）。担当者の容量データが無ければ既存のtask_gantt_range
    （工数感→固定営業日数の逆算）にフォールバックする（無停止移行）。期限が無ければNone。"""
    due = (task.get("due_date") or "").strip()
    if not due:
        return None
    owner = owner if owner is not None else (task.get("assignee") or "")
    if not owner or not has_owner_capacity_data(con, owner):
        start, _ = task_gantt_range(due, task.get("effort_level"))
        return start
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return None
    remaining = effective_effort_hours(task)
    guard = 0
    while remaining > 1e-9 and guard < 3650:
        if not is_business_day(d):
            d -= timedelta(days=1)
            guard += 1
            continue
        remaining -= capacity_at(con, owner, d.isoformat())
        if remaining > 1e-9:
            d -= timedelta(days=1)
        guard += 1
    return d.isoformat()


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
    q = """SELECT d.*, a.name AS account_name, a.industry, a.company_size, a.aliases AS account_aliases
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
    """指定日が次回MS日付、活動履歴の実施日、または（キャッシュ上の「次回MS」以外も含めた）
    未完了の個別MS日付のいずれかに一致する商談を返す。1商談に複数MS（#48）がある場合、
    deals.next_milestone_dateキャッシュは「未完了で最も早い1件」しか反映しないため、
    2件目以降のMS日をdeal_milestonesテーブル自体からも見て拾う（そうしないと2件目以降のMS日で
    この画面を検索しても該当商談が出てこない）。過去の振り返り用途のためクローズ済み商談も
    含める（statusで絞らない）。"""
    q = """SELECT DISTINCT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE (d.next_milestone_date = ?
                  OR EXISTS (SELECT 1 FROM activities act WHERE act.deal_id = d.id AND act.occurred_on = ?)
                  OR EXISTS (SELECT 1 FROM deal_milestones dm
                             WHERE dm.deal_id = d.id AND dm.done = 0 AND dm.ms_date = ?))"""
    params: list = [date, date, date]
    if owner:
        q += " AND (d.owner = ? OR d.sub_owner = ?)"
        params.append(owner)
        params.append(owner)
    q += " ORDER BY a.name"
    return [dict(r) for r in con.execute(q, params)]


def list_deals_by_week(con, week_start: str, week_end: str, owner: str | None = None) -> list[dict]:
    """週(week_start〜week_end, 両端含む)に次回MS日付、活動実施日、または（キャッシュ上の
    「次回MS」以外も含めた）未完了の個別MS日付が含まれる商談を返す（list_deals_by_dateと同じ理由で
    deal_milestonesテーブル自体も見る。#48）。過去の振り返り用途のためクローズ済み商談も
    含める（statusで絞らない）。"""
    q = """SELECT DISTINCT d.*, a.name AS account_name, a.industry, a.company_size
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE ((d.next_milestone_date BETWEEN ? AND ?)
                  OR EXISTS (SELECT 1 FROM activities act WHERE act.deal_id = d.id
                             AND act.occurred_on BETWEEN ? AND ?)
                  OR EXISTS (SELECT 1 FROM deal_milestones dm
                             WHERE dm.deal_id = d.id AND dm.done = 0
                             AND dm.ms_date BETWEEN ? AND ?))"""
    params: list = [week_start, week_end, week_start, week_end, week_start, week_end]
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
_UNSET = object()


def upsert_account(con, *, id=None, name, industry=None, company_size=None, note=None,
                   aliases=_UNSET, commit: bool = True) -> int:
    """aliases省略時（既存の /account/save 等の呼び出し）は列を触らない。
    None/文字列を明示的に渡した時だけ更新する（一括登録画面からの誤クリア防止）。"""
    if id is not None:
        if aliases is _UNSET:
            con.execute(
                "UPDATE accounts SET name=?, industry=?, company_size=?, note=?, "
                "updated_at=datetime('now') WHERE id=?",
                (name, industry, company_size, note, id),
            )
        else:
            con.execute(
                "UPDATE accounts SET name=?, industry=?, company_size=?, note=?, aliases=?, "
                "updated_at=datetime('now') WHERE id=?",
                (name, industry, company_size, note, aliases, id),
            )
        if commit:
            con.commit()
        return int(id)
    cur = con.execute(
        "INSERT INTO accounts (name, industry, company_size, note, aliases) VALUES (?,?,?,?,?)",
        (name, industry, company_size, note, (None if aliases is _UNSET else aliases)),
    )
    if commit:
        con.commit()
    return cur.lastrowid


def set_account_aliases(con, account_id: int, aliases: str, commit: bool = True) -> None:
    """候補検出用の略称辞書（読点/カンマ区切り）を1件更新する（一括登録画面から使用）。"""
    con.execute("UPDATE accounts SET aliases=?, updated_at=datetime('now') WHERE id=?",
                ((aliases or "").strip() or None, int(account_id)))
    if commit:
        con.commit()


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


def duplicate_deal(con, deal_id: int) -> int | None:
    """商談を複製し新規商談(status='open')を作成する。ユーザー確定方針:
    ステージは先頭ステージへリセット、現状メモは「複製である旨」を明示した上で引き継ぐ。
    活動履歴・マイルストーン・ヒアリング結果・論点・取り込み文字起こし・Hisho同期状態
    (theme_id)・Slack紐付け・終了理由・次回MSは引き継がない（新規案件として真っ白から
    始める）。開発案件(dev_projects)は元商談を参照したまま＝新商談には何も紐付かない。
    戻り値は新規商談id（元商談が無ければNone）。"""
    src = get_deal(con, deal_id)
    if not src:
        return None
    first_stage = (get_master_list(con, "deal_stages") or DEAL_STAGES)[0]
    orig_label = "/".join(x for x in (src.get("account_name"), src.get("deal_name")) if x)
    note_prefix = f"※SFA#{deal_id}" + (f"（{orig_label}）" if orig_label else "") + "の複製です。\n\n"
    new_id = upsert_deal(
        con,
        account_id=src.get("account_id"),
        deal_name=(src.get("deal_name") or "(無題)") + "（コピー）",
        stage=first_stage,
        business_type_l1=src.get("business_type_l1"),
        business_type_l2=src.get("business_type_l2"),
        lead_pattern=src.get("lead_pattern"),
        owner=src.get("owner"),
        sub_owner=src.get("sub_owner"),
        client_contact=src.get("client_contact"),
        client_dept=src.get("client_dept"),
        value_lumpsum=src.get("value_lumpsum"),
        value_lumpsum_monthly=src.get("value_lumpsum_monthly"),
        value_recurring=src.get("value_recurring"),
        client_budget=src.get("client_budget"),
        note=note_prefix + (src.get("note") or ""),
        goal=src.get("goal"),
        importance=src.get("importance"),
        status="open",
        cost_stage=src.get("cost_stage"),
        approach_value=src.get("approach_value"),
        approach_rate=src.get("approach_rate"),
        reduction_rate=src.get("reduction_rate"),
        fee_rate=src.get("fee_rate"),
        diagnosis_cost=src.get("diagnosis_cost"),
    )
    # exhibition_nameはDEAL_FIELDS外（一括タグ付け専用UPDATE運用のため）。分類属性として引き継ぐ。
    if src.get("exhibition_name"):
        con.execute("UPDATE deals SET exhibition_name=? WHERE id=?", (src["exhibition_name"], new_id))
        con.commit()
    return new_id


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


def complete_past_milestones(con, deal_id: int, as_of_date: str, commit: bool = True) -> int:
    """次回MSライフサイクル是正（新規）: as_of_date以前の未完了MSを全件完了(done=1)にする。
    活動履歴が記録された＝その日までのMSは実施済みとみなせる、という前提の自動安全網。
    完了件数を返す（0なら何もしていない）。"""
    if not as_of_date:
        return 0
    cur = con.execute(
        "UPDATE deal_milestones SET done=1 WHERE deal_id=? AND done=0 "
        "AND ms_date IS NOT NULL AND ms_date != '' AND ms_date <= ?",
        (int(deal_id), as_of_date))
    n = cur.rowcount or 0
    if n:
        recompute_deal_next_milestone(con, deal_id, commit=False)
    if commit:
        con.commit()
    return n


def add_activity(con, *, deal_id, type=None, occurred_on=None, contact_name=None, body=None,
                 intake_transcript_id=None) -> int:
    cur = con.execute(
        "INSERT INTO activities (deal_id, type, occurred_on, contact_name, body, intake_transcript_id) "
        "VALUES (?,?,?,?,?,?)",
        (deal_id, type, occurred_on, contact_name, body,
         int(intake_transcript_id) if intake_transcript_id else None),
    )
    if occurred_on:
        complete_past_milestones(con, deal_id, occurred_on, commit=False)
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
                       conducted_on=None, answers: list[dict], activity_id=None,
                       intake_transcript_id=None) -> int:
    cur = con.execute(
        "INSERT INTO hearing_results "
        "(deal_id, template_id, template_name, conducted_on, answers_json, activity_id, intake_transcript_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (int(deal_id), int(template_id) if template_id else None, template_name,
         conducted_on, _json.dumps(answers or [], ensure_ascii=False),
         int(activity_id) if activity_id else None,
         int(intake_transcript_id) if intake_transcript_id else None),
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


# ---- ヒアリングAI(#29): 面談セッション（文字起こし→AI整形→確認→確定の作業台） ----
def _hydrate_hearing_session(r: dict) -> dict:
    try:
        r["structured"] = _json.loads(r.get("structured_json") or "{}")
    except (ValueError, TypeError):
        r["structured"] = {}
    return r


def create_hearing_session(con, *, deal_id, source="paste", external_id=None,
                           template_id=None, conducted_on=None, transcript="",
                           structured=None, status="imported", intake_transcript_id=None) -> int:
    cur = con.execute(
        "INSERT INTO hearing_sessions "
        "(deal_id, template_id, source, external_id, conducted_on, transcript, structured_json, status, "
        "intake_transcript_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (int(deal_id), int(template_id) if template_id else None, source, external_id,
         conducted_on, transcript or "", _json.dumps(structured or {}, ensure_ascii=False), status,
         int(intake_transcript_id) if intake_transcript_id else None))
    con.commit()
    return cur.lastrowid


def get_hearing_session(con, id: int) -> dict | None:
    r = con.execute("SELECT * FROM hearing_sessions WHERE id=?", (int(id),)).fetchone()
    return _hydrate_hearing_session(dict(r)) if r else None


def update_hearing_session(con, id: int, *, structured=None, status=None,
                           transcript=None, result_id=None, template_id=None) -> None:
    sets, vals = [], []
    if structured is not None:
        sets.append("structured_json=?"); vals.append(_json.dumps(structured, ensure_ascii=False))
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if transcript is not None:
        sets.append("transcript=?"); vals.append(transcript)
    if result_id is not None:
        sets.append("result_id=?"); vals.append(int(result_id))
    if template_id is not None:
        sets.append("template_id=?"); vals.append(int(template_id))
    if not sets:
        return
    sets.append("updated_at=datetime('now')")
    con.execute(f"UPDATE hearing_sessions SET {', '.join(sets)} WHERE id=?", (*vals, int(id)))
    con.commit()


def get_hearing_session_by_external(con, source: str, external_id: str) -> dict | None:
    """自動取込の冪等化用。同一(source, external_id)の既存セッションを返す。"""
    if not external_id:
        return None
    r = con.execute("SELECT * FROM hearing_sessions WHERE source=? AND external_id=? LIMIT 1",
                    (source, external_id)).fetchone()
    return _hydrate_hearing_session(dict(r)) if r else None


# ---- AI整形の取り込み原本（文字起こしローデータ＋原本ファイル） ----

def add_intake_transcript(con, *, kind: str, entity_id: int, source: str = "paste",
                          filename: str | None = None, transcript: str | None = None,
                          file_blob: bytes | None = None) -> int:
    cur = con.execute(
        "INSERT INTO intake_transcripts (kind, entity_id, source, filename, transcript, file_blob) "
        "VALUES (?,?,?,?,?,?)",
        (kind, int(entity_id), source, filename, transcript,
         sqlite3.Binary(file_blob) if file_blob else None),
    )
    con.commit()
    return cur.lastrowid


def list_intake_transcripts(con, kind: str, entity_id: int) -> list[dict]:
    """一覧用（file_blobは載せない。サイズと転送量の節約）。新しい順。"""
    rows = con.execute(
        "SELECT id, kind, entity_id, source, filename, "
        "LENGTH(file_blob) AS file_size, LENGTH(transcript) AS text_len, created_at "
        "FROM intake_transcripts WHERE kind=? AND entity_id=? ORDER BY id DESC",
        (kind, int(entity_id)),
    ).fetchall()
    return [dict(r) for r in rows]


def find_intake_transcript_usages(con, transcript_id: int) -> list[dict]:
    """指定の取り込み原本(intake_transcripts.id)を出典として作られた生成物を逆引きする
    （原本一覧側に「→どのノート/活動履歴になったか」を明示するため）。
    [{type:'activity'|'hearing_result'|'rich_note', id, label}] を返す。"""
    out = []
    for r in con.execute(
        "SELECT id, occurred_on, type FROM activities WHERE intake_transcript_id=?", (int(transcript_id),)
    ):
        out.append({"type": "activity", "id": r["id"],
                    "label": f"活動履歴（{r['occurred_on'] or '—'}・{r['type'] or ''}）"})
    for r in con.execute(
        "SELECT id, conducted_on, template_name FROM hearing_results WHERE intake_transcript_id=?",
        (int(transcript_id),)
    ):
        out.append({"type": "hearing_result", "id": r["id"],
                    "label": f"ヒアリング結果（{r['template_name'] or ''} ・{r['conducted_on'] or '—'}）"})
    for r in con.execute(
        "SELECT id, title FROM rich_notes WHERE intake_transcript_id=?", (int(transcript_id),)
    ):
        out.append({"type": "rich_note", "id": r["id"], "label": f"ノート「{r['title'] or '無題'}」"})
    return out


# intake_transcript_id導入（2026-08-17）より前に作られた既存データは出典が未記録（NULL）のまま。
# 「そのエンティティの文字起こしが1件・かつ未紐づけの対象行も1件」の場合のみ、確実な1:1と
# みなして自動で紐づける（両方1件でなければ、当時どちらがどれの出典か復元できないため何もしない）。
_BACKFILL_DEAL_TABLES = ("activities", "hearing_results", "hearing_sessions")


def find_intake_transcript_backfill_candidates(con) -> dict:
    """既存データのintake_transcript_idバックフィル候補を検出する（読み取り専用）。
    戻り値: {"apply": [{table,row_id,intake_transcript_id,kind,entity_id}],
             "ambiguous": [{kind,entity_id,reason}]}（曖昧で自動判定できないもの）。"""
    apply_list, ambiguous = [], []
    entities = con.execute(
        "SELECT DISTINCT kind, entity_id FROM intake_transcripts WHERE kind IN ('deal','issue')"
    ).fetchall()
    for e in entities:
        kind, eid = e["kind"], e["entity_id"]
        trs = con.execute(
            "SELECT id FROM intake_transcripts WHERE kind=? AND entity_id=?", (kind, eid)
        ).fetchall()
        if len(trs) != 1:
            ambiguous.append({"kind": kind, "entity_id": eid,
                              "reason": f"文字起こしが{len(trs)}件あり一意に決められない"})
            continue
        itid = trs[0]["id"]
        rn = con.execute(
            "SELECT id FROM rich_notes WHERE kind=? AND entity_id=? AND intake_transcript_id IS NULL",
            (kind, eid)
        ).fetchall()
        if len(rn) == 1:
            apply_list.append({"table": "rich_notes", "row_id": rn[0]["id"],
                               "intake_transcript_id": itid, "kind": kind, "entity_id": eid})
        elif len(rn) > 1:
            ambiguous.append({"kind": kind, "entity_id": eid,
                              "reason": f"未紐づけのrich_notesが{len(rn)}件あり一意に決められない"})
        if kind == "deal":
            for table in _BACKFILL_DEAL_TABLES:
                rows = con.execute(
                    f"SELECT id FROM {table} WHERE deal_id=? AND intake_transcript_id IS NULL", (eid,)
                ).fetchall()
                if len(rows) == 1:
                    apply_list.append({"table": table, "row_id": rows[0]["id"],
                                       "intake_transcript_id": itid, "kind": kind, "entity_id": eid})
                elif len(rows) > 1:
                    ambiguous.append({"kind": kind, "entity_id": eid,
                                      "reason": f"未紐づけの{table}が{len(rows)}件あり一意に決められない"})
    return {"apply": apply_list, "ambiguous": ambiguous}


def apply_intake_transcript_backfill(con, apply_list: list[dict]) -> int:
    """find_intake_transcript_backfill_candidates()のapply分をDBへ反映する。反映件数を返す。"""
    for item in apply_list:
        con.execute(f"UPDATE {item['table']} SET intake_transcript_id=? WHERE id=?",
                    (item["intake_transcript_id"], item["row_id"]))
    con.commit()
    return len(apply_list)


def get_intake_transcript(con, id: int) -> dict | None:
    """本文表示/原本DL用（file_blob込み）。"""
    r = con.execute("SELECT * FROM intake_transcripts WHERE id=?", (int(id),)).fetchone()
    return dict(r) if r else None


def delete_intake_transcript(con, id: int) -> None:
    con.execute("DELETE FROM intake_transcripts WHERE id=?", (int(id),))
    con.commit()


# ---- 自動連携（Jamie/Zoom）: 受信インボックス ----

def get_intake_by_external(con, external_source: str, external_id: str) -> dict | None:
    """冪等化用: 同一(external_source, external_id)の既存受信を返す。"""
    if not external_id:
        return None
    r = con.execute(
        "SELECT id, kind, entity_id, status FROM intake_transcripts "
        "WHERE external_source=? AND external_id=? LIMIT 1",
        (external_source, external_id)).fetchone()
    return dict(r) if r else None


def add_inbox_transcript(con, *, external_source: str, external_id: str | None,
                         title: str | None = None, occurred_on: str | None = None,
                         transcript: str | None = None, attendees_json: str | None = None,
                         raw_summary: str | None = None) -> int:
    """自動受信した文字起こしを未割り当て(inbox)として保存。冪等: 既存があればそのidを返す。"""
    ex = get_intake_by_external(con, external_source, external_id) if external_id else None
    if ex:
        return ex["id"]
    cur = con.execute(
        "INSERT INTO intake_transcripts "
        "(kind, entity_id, source, transcript, external_source, external_id, title, "
        " occurred_on, attendees_json, raw_summary, status) "
        "VALUES ('inbox', 0, ?, ?, ?, ?, ?, ?, ?, ?, 'inbox')",
        (external_source, transcript, external_source, external_id, title,
         occurred_on, attendees_json, raw_summary),
    )
    con.commit()
    return cur.lastrowid


def list_inbox_transcripts(con) -> list[dict]:
    """未割り当て(inbox)の受信一覧（blobは載せない）。新しい順。"""
    rows = con.execute(
        "SELECT id, source, external_source, external_id, title, occurred_on, "
        "attendees_json, LENGTH(transcript) AS text_len, created_at "
        "FROM intake_transcripts WHERE status='inbox' ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def count_inbox_transcripts(con) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM intake_transcripts WHERE status='inbox'").fetchone()[0]


def assign_inbox_transcript(con, id: int, *, kind: str, entity_id: int) -> None:
    """インボックスの受信を商談/論点へ割り当て（以後その対象の取り込み原本として表示される）。"""
    con.execute(
        "UPDATE intake_transcripts SET kind=?, entity_id=?, status='assigned' WHERE id=?",
        (kind, int(entity_id), int(id)))
    con.commit()


def find_activity_by_deal_and_date(con, deal_id: int, occurred_on: str) -> dict | None:
    """#98: Slack確定済み活動の有無チェック（同一(deal_id,面談日)の突合キー）。最新1件。"""
    r = con.execute(
        "SELECT * FROM activities WHERE deal_id=? AND occurred_on=? ORDER BY id DESC LIMIT 1",
        (deal_id, occurred_on)).fetchone()
    return dict(r) if r else None


def find_assigned_jamie_transcript(con, deal_id: int, occurred_on: str) -> dict | None:
    """#98: 同一(deal_id,面談日)で割当済み・未消化のJamie全文があれば返す（Slack確定時の統合用）。"""
    r = con.execute(
        "SELECT * FROM intake_transcripts WHERE kind='deal' AND entity_id=? AND occurred_on=? "
        "AND source='jamie' AND status='assigned' ORDER BY id DESC LIMIT 1",
        (deal_id, occurred_on)).fetchone()
    return dict(r) if r else None


def mark_intake_transcript_status(con, id: int, status: str) -> None:
    con.execute("UPDATE intake_transcripts SET status=? WHERE id=?", (status, int(id)))
    con.commit()


def list_stale_nego_threads(con, hours: float = 3.0) -> list[dict]:
    """#新規: SlackのNegoCollectionスレッドのうち、completedではなく、first_seen_atからhours時間
    以上経過し、まだ放置リマインドを送っていない(reminded_at IS NULL)ものを返す（放置検知）。"""
    rows = con.execute(
        "SELECT * FROM slack_threads WHERE state != 'completed' AND reminded_at IS NULL "
        "AND first_seen_at IS NOT NULL AND first_seen_at <= datetime('now', ?) "
        "ORDER BY first_seen_at ASC",
        (f"-{float(hours)} hours",),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_nego_thread_reminded(con, thread_ts: str) -> None:
    con.execute("UPDATE slack_threads SET reminded_at=datetime('now') WHERE thread_ts=?", (thread_ts,))
    con.commit()


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

DEAL_ISSUE_FIELDS = ["deal_id", "issue", "members", "responsible", "status", "due_date", "company_function"]

_DEAL_ISSUE_SELECT = (
    "SELECT i.*, d.deal_name, d.owner AS sales_owner, d.sub_owner AS sales_sub_owner, "
    "a.name AS account_name FROM deal_issues i "
    "LEFT JOIN deals d ON d.id = i.deal_id LEFT JOIN accounts a ON a.id = d.account_id"
)

DEAL_ISSUE_SORTS = ["due_date", "status", "updated_at"]


def list_deal_issues(con, *, deal_id: int | None = None, status: str | None = None,
                      member: str | None = None, responsible: str | None = None,
                      q: str | None = None, company_function: str | None = None,
                      sort: str = "due_date") -> list[dict]:
    """論点一覧。deal_id以外はすべて一覧画面の絞り込み用。
    member指定時は議論メンバー（社員名のカンマ区切り複数選択）にその名前を含む論点のみ返す。
    responsible指定時は責任者がその社員名の論点のみ返す。
    q指定時はアカウント名・商談名の部分一致で絞り込む。
    company_function指定時は会社機能（#147、商談共通論点のみ持つ）で絞り込む。"""
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
    if responsible:
        conds.append("i.responsible = ?")
        params.append(responsible)
    if q:
        conds.append("(a.name LIKE ? OR d.deal_name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if company_function:
        conds.append("i.company_function = ?")
        params.append(company_function)
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


# ── 論点メモのパスワードロック（2026-08）。鍵の単位は論点(deal_issue)1件ごと。 ──
ISSUE_NOTE_LOCK_RESET_VALID_HOURS = 24


def _issue_note_lock_hash(password: str, salt: str) -> str:
    return hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()


def issue_note_lock_status(con, issue_id: int) -> dict:
    """{"locked": bool, "recovery_email": str|None}。"""
    row = con.execute("SELECT note_lock_hash, note_lock_recovery_email FROM deal_issues WHERE id=?",
                       (int(issue_id),)).fetchone()
    if not row:
        return {"locked": False, "recovery_email": None}
    return {"locked": bool(row["note_lock_hash"]), "recovery_email": row["note_lock_recovery_email"]}


def set_issue_note_lock(con, issue_id: int, password: str, recovery_email: str) -> None:
    """論点メモにパスワードロックを設定（既存ロックも上書き）。パスワード忘れ時の連絡先メール必須。"""
    salt = secrets.token_hex(16)
    con.execute(
        "UPDATE deal_issues SET note_lock_salt=?, note_lock_hash=?, note_lock_recovery_email=?, "
        "note_lock_reset_token=NULL, note_lock_reset_expires=NULL, updated_at=datetime('now') WHERE id=?",
        (salt, _issue_note_lock_hash(password, salt), (recovery_email or "").strip() or None, int(issue_id)))
    con.commit()


def clear_issue_note_lock(con, issue_id: int) -> None:
    con.execute(
        "UPDATE deal_issues SET note_lock_salt=NULL, note_lock_hash=NULL, note_lock_recovery_email=NULL, "
        "note_lock_reset_token=NULL, note_lock_reset_expires=NULL, updated_at=datetime('now') WHERE id=?",
        (int(issue_id),))
    con.commit()


def verify_issue_note_lock(con, issue_id: int, password: str) -> bool:
    """ロック無し（or 論点が存在しない）ならTrue。ロックありならパスワード一致でTrue。"""
    row = con.execute("SELECT note_lock_salt, note_lock_hash FROM deal_issues WHERE id=?",
                       (int(issue_id),)).fetchone()
    if not row or not row["note_lock_hash"]:
        return True
    calc = _issue_note_lock_hash(password or "", row["note_lock_salt"] or "")
    return hmac.compare_digest(calc, row["note_lock_hash"])


def request_issue_note_lock_reset(con, issue_id: int) -> dict | None:
    """パスワード忘れ: リセット用トークンを発行（24時間有効）。未ロック/連絡先未設定ならNone。
    呼び出し側（webapp）はこのtokenでリセットURLを作り、recovery_emailへmailto等で送る。"""
    row = con.execute("SELECT note_lock_hash, note_lock_recovery_email FROM deal_issues WHERE id=?",
                       (int(issue_id),)).fetchone()
    if not row or not row["note_lock_hash"] or not row["note_lock_recovery_email"]:
        return None
    token = secrets.token_urlsafe(24)
    expires = (datetime.now() + timedelta(hours=ISSUE_NOTE_LOCK_RESET_VALID_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("UPDATE deal_issues SET note_lock_reset_token=?, note_lock_reset_expires=? WHERE id=?",
                (token, expires, int(issue_id)))
    con.commit()
    return {"token": token, "recovery_email": row["note_lock_recovery_email"]}


def confirm_issue_note_lock_reset(con, issue_id: int, token: str) -> bool:
    """本人確認クリック（メール内のリセットリンク）からの確定処理。トークン一致＆期限内ならロック解除。"""
    row = con.execute(
        "SELECT note_lock_reset_token, note_lock_reset_expires FROM deal_issues WHERE id=?",
        (int(issue_id),)).fetchone()
    if not row or not row["note_lock_reset_token"] or not token:
        return False
    if not hmac.compare_digest(row["note_lock_reset_token"], token):
        return False
    try:
        if datetime.now() > datetime.strptime(row["note_lock_reset_expires"], "%Y-%m-%d %H:%M:%S"):
            return False
    except (TypeError, ValueError):
        return False
    clear_issue_note_lock(con, issue_id)
    return True


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
    clear_orphaned_task_links(con)


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


def list_issue_materials(con, issue_id: int) -> list[dict]:
    """論点の検討材料（社内資料の体系化・層1）を追加順で返す。"""
    return [dict(r) for r in con.execute(
        "SELECT * FROM issue_materials WHERE issue_id=? ORDER BY id", (int(issue_id),))]


def add_issue_material(con, issue_id: int, content: str, *, title: str | None = None,
                       source_url: str | None = None, added_by: str | None = None) -> int | None:
    """検討材料を1件追加。contentが空なら何もしない（Noneを返す）。
    titleが空ならcontent先頭30文字から自動生成する。"""
    content = (content or "").strip()
    if not content:
        return None
    title = (title or "").strip()
    if not title:
        _first_line = content.splitlines()[0].strip().lstrip("#").strip()
        title = (_first_line or content)[:30]
    cur = con.execute(
        "INSERT INTO issue_materials (issue_id, title, content, source_url, added_by) VALUES (?,?,?,?,?)",
        (int(issue_id), title, content, (source_url or "").strip() or None, (added_by or "").strip() or None))
    con.commit()
    return cur.lastrowid


def delete_issue_material(con, material_id: int) -> None:
    con.execute("DELETE FROM issue_materials WHERE id=?", (int(material_id),))
    con.commit()


def create_doc(con, *, kind: str, title: str, body_html: str, template: str | None = None,
               issue_id: int | None = None, created_by: str | None = None) -> int:
    """社内資料の体系化・層2/3（検討資料/報告ペーパー）を1件保存する。"""
    cur = con.execute(
        "INSERT INTO docs (kind, template, title, issue_id, body_html, created_by) VALUES (?,?,?,?,?,?)",
        (kind, template, title, issue_id, body_html, (created_by or "").strip() or None))
    con.commit()
    return cur.lastrowid


def get_doc(con, doc_id: int) -> dict | None:
    r = con.execute("SELECT * FROM docs WHERE id=?", (int(doc_id),)).fetchone()
    return dict(r) if r else None


def list_docs(con, *, issue_id: int | None = None) -> list[dict]:
    """新しい順。issue_id指定でその論点分のみ。"""
    q = "SELECT * FROM docs"
    params: list = []
    if issue_id is not None:
        q += " WHERE issue_id=?"
        params.append(int(issue_id))
    q += " ORDER BY created_at DESC, id DESC"
    return [dict(r) for r in con.execute(q, params)]


def delete_doc(con, doc_id: int) -> None:
    con.execute("DELETE FROM docs WHERE id=?", (int(doc_id),))
    con.commit()


def delete_deal_issue_memo(con, memo_id: int) -> None:
    con.execute("DELETE FROM deal_issue_memos WHERE id=?", (int(memo_id),))
    con.commit()


# ---- タスク管理（#30） ----

TASK_FIELDS = [
    "title", "detail", "project", "next_action", "assignee", "due_date", "status",
    "priority", "category", "is_admin", "requester", "link_type", "link_id", "source",
    "slack_channel", "slack_ts", "slack_permalink", "created_by", "effort_level", "effort_hours",
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
               admin: bool | None = None, only_deleted: bool = False,
               issue_company_function: str | None = None) -> list[dict]:
    """admin=True で事務タスク(is_admin=1)のみ、admin=False で通常タスク(is_admin=0/NULL)のみ、
    admin=None（既定）で両方。既存呼び出しは admin=None のため挙動不変。
    既定はソフト削除済み(deleted_at)を除外。only_deleted=True で削除済みのみ（復活画面用）。
    issue_company_function指定時（#147）は、紐づく論点(issue)のcompany_functionがその値の
    タスクのみ返す（レガシー単一列・task_entity_linksの両方の紐づけを対象、商談紐づけの
    論点はcompany_functionを持たないため対象外）。"""
    q = "SELECT * FROM tasks"
    conds: list = []
    params: list = []
    if only_deleted:
        conds.append("deleted_at IS NOT NULL AND deleted_at != ''")
    else:
        conds.append("(deleted_at IS NULL OR deleted_at = '')")
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
    if link_type == "__none__":
        # 紐づけ無しのみ（ユーザー要望2026-08-27）。link_idは無視する。レガシー単一列と
        # task_entity_links（#146・複数関連付け）の両方に何も無いことを確認する。
        conds.append(
            "(link_type IS NULL OR link_type = '') AND NOT EXISTS "
            "(SELECT 1 FROM task_entity_links tel WHERE tel.task_id = tasks.id)")
    elif link_type:
        # レガシー単一列(link_type[/link_id])、またはtask_entity_links（#146）のどちらかに
        # マッチすれば該当（1タスクが複数の紐づけ先を持てるため）。
        if link_id is not None:
            conds.append(
                "((link_type = ? AND link_id = ?) OR EXISTS "
                "(SELECT 1 FROM task_entity_links tel WHERE tel.task_id = tasks.id "
                "AND tel.link_type = ? AND tel.link_id = ?))")
            params += [link_type, int(link_id), link_type, int(link_id)]
        else:
            conds.append(
                "(link_type = ? OR EXISTS "
                "(SELECT 1 FROM task_entity_links tel WHERE tel.task_id = tasks.id "
                "AND tel.link_type = ?))")
            params += [link_type, link_type]
    if issue_company_function:
        # #147: 紐づく論点(issue)のcompany_functionで絞り込む（レガシー単一列/
        # task_entity_linksの両方の紐づけ経路を対象）。
        conds.append(
            "(EXISTS (SELECT 1 FROM deal_issues di WHERE di.id = tasks.link_id "
            "AND tasks.link_type = 'issue' AND di.company_function = ?) "
            "OR EXISTS (SELECT 1 FROM task_entity_links tel JOIN deal_issues di ON di.id = tel.link_id "
            "WHERE tel.task_id = tasks.id AND tel.link_type = 'issue' AND di.company_function = ?))")
        params += [issue_company_function, issue_company_function]
    if conds:
        q += " WHERE " + " AND ".join(conds)
    # ★ピン最優先→期限昇順(未設定は末尾)→id
    q += (" ORDER BY COALESCE(pinned,0) DESC, (due_date IS NULL OR due_date='') ASC, "
          "due_date ASC, id DESC")
    return [dict(r) for r in con.execute(q, params)]


def get_task(con, id: int) -> dict | None:
    r = con.execute("SELECT * FROM tasks WHERE id=?", (int(id),)).fetchone()
    return dict(r) if r else None


def task_link_label(con, link_type: str | None, link_id: int | None) -> str | None:
    """タスクのlink_type/link_id（deal/issue/delivery/dev_project）の表示ラベル。
    対象が消えていたらNone。
    ユーザー要望2026-08-24: コンサルタスクを商談/論点に紐づけられるようにする機能で使用。
    2026-08-27にDeliveryを追加。2026-09-02(#146)にdev_projectを追加
    （複数関連付け対応で、従来webapp.py側に別出しだったdev_projectのラベル解決を統一）。"""
    if not link_type or not link_id:
        return None
    if link_type == "deal":
        d = get_deal(con, link_id)
        if not d:
            return None
        return f'{d.get("account_name") or "—"}：{d.get("deal_name") or "—"}'
    if link_type == "issue":
        it = get_deal_issue(con, link_id)
        if not it:
            return None
        base = it.get("issue") or "(無題)"
        if it.get("deal_name"):
            return f'{base}（{it.get("account_name") or "—"}：{it["deal_name"]}）'
        if it.get("company_function"):
            # #147: 商談共通論点に会社機能が設定されていれば、コンサルタスク側の
            # 紐づけラベルにも反映する（「商談共通」より具体的な文脈になる）。
            return f'{base}（{it["company_function"]}）'
        return f"{base}（商談共通）"
    if link_type == "delivery":
        dv = get_delivery(con, link_id)
        if not dv:
            return None
        return f'{dv.get("account_name") or "—"}：{dv.get("title") or dv.get("deal_name") or "—"}'
    if link_type == "dev_project":
        dp = get_dev_project(con, link_id)
        if not dp:
            return None
        return f'{dp.get("account_name") or "—"}：{dp.get("theme") or "開発案件"}'
    return None


TASK_LINK_TYPES_MULTI = ("deal", "issue", "delivery", "dev_project")


def set_task_links(con, task_id: int, links: list[tuple[str, int]], commit: bool = True) -> None:
    """タスクの関連付け（商談/論点/Delivery/開発案件、複数可）を一括置き換える（#146）。
    linksは[(link_type, link_id), ...]。種別が不正・link_id欠落・重複は無視する。
    旧来の単一紐づけ(tasks.link_type/link_id)は、後方互換のため先頭の1件を反映する
    （レガシー参照箇所向けのミラー。実体はtask_entity_linksが正）。"""
    seen: set[tuple[str, int]] = set()
    clean: list[tuple[str, int]] = []
    for lt, lid in (links or []):
        if lt not in TASK_LINK_TYPES_MULTI or not lid:
            continue
        try:
            lid = int(lid)
        except (TypeError, ValueError):
            continue
        if (lt, lid) in seen:
            continue
        seen.add((lt, lid))
        clean.append((lt, lid))
    tid = int(task_id)
    con.execute("DELETE FROM task_entity_links WHERE task_id=?", (tid,))
    if clean:
        con.executemany(
            "INSERT INTO task_entity_links(task_id, link_type, link_id) VALUES (?,?,?)",
            [(tid, lt, lid) for lt, lid in clean])
    first_lt, first_li = clean[0] if clean else (None, None)
    con.execute("UPDATE tasks SET link_type=?, link_id=? WHERE id=?", (first_lt, first_li, tid))
    if commit:
        con.commit()


def get_task_links(con, task_id: int) -> list[dict]:
    """タスクの全関連付け（商談/論点/Delivery/開発案件、複数可, #146）。
    task_entity_links（正）とtasks.link_type/link_id（レガシー単一列。#146以前の
    upsert_task経由の紐づけがまだこちらにしか無いケースを拾う）の両方を見て、
    重複除去のうえ返す。ラベルが解決できない（参照切れ）ものは除外。"""
    t = get_task(con, task_id)
    pairs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    if t and t.get("link_type") and t.get("link_id"):
        key = (t["link_type"], t["link_id"])
        pairs.append(key); seen.add(key)
    for r in con.execute(
        "SELECT link_type, link_id FROM task_entity_links WHERE task_id=? ORDER BY id",
        (int(task_id),)):
        key = (r["link_type"], r["link_id"])
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    out = []
    for lt, lid in pairs:
        label = task_link_label(con, lt, lid)
        if label:
            out.append({"link_type": lt, "link_id": lid, "label": label})
    return out


def get_task_links_map(con, task_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """複数タスク分の関連付けをまとめて取得（#146・N+1回避用）。get_task_linksの一括版。
    task_idsを指定するとその範囲のみ、Noneなら全タスク対象。"""
    if task_ids is not None:
        ids = [int(i) for i in task_ids]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        legacy_rows = con.execute(
            f"SELECT id, link_type, link_id FROM tasks WHERE id IN ({ph}) "
            f"AND link_type IS NOT NULL AND link_type != '' AND link_id IS NOT NULL", ids).fetchall()
        multi_rows = con.execute(
            f"SELECT task_id, link_type, link_id FROM task_entity_links WHERE task_id IN ({ph}) "
            f"ORDER BY id", ids).fetchall()
    else:
        legacy_rows = con.execute(
            "SELECT id, link_type, link_id FROM tasks "
            "WHERE link_type IS NOT NULL AND link_type != '' AND link_id IS NOT NULL").fetchall()
        multi_rows = con.execute(
            "SELECT task_id, link_type, link_id FROM task_entity_links ORDER BY id").fetchall()
    pairs_by_task: dict[int, list[tuple[str, int]]] = {}
    seen_by_task: dict[int, set] = {}
    for r in legacy_rows:
        tid, key = r["id"], (r["link_type"], r["link_id"])
        pairs_by_task.setdefault(tid, []).append(key)
        seen_by_task.setdefault(tid, set()).add(key)
    for r in multi_rows:
        tid, key = r["task_id"], (r["link_type"], r["link_id"])
        if key in seen_by_task.get(tid, set()):
            continue
        pairs_by_task.setdefault(tid, []).append(key)
        seen_by_task.setdefault(tid, set()).add(key)
    label_cache: dict[tuple[str, int], str | None] = {}
    out: dict[int, list[dict]] = {}
    for tid, pairs in pairs_by_task.items():
        items = []
        for lt, lid in pairs:
            if (lt, lid) not in label_cache:
                label_cache[(lt, lid)] = task_link_label(con, lt, lid)
            label = label_cache[(lt, lid)]
            if label:
                items.append({"link_type": lt, "link_id": lid, "label": label})
        if items:
            out[tid] = items
    return out


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
    """ソフト削除（deleted_at を打つ）。復活可能。物理削除は hard_delete_task。"""
    con.execute("UPDATE tasks SET deleted_at=datetime('now'), updated_at=datetime('now') WHERE id=?", (int(id),))
    con.commit()


def restore_task(con, id: int) -> None:
    """ソフト削除の復活（deleted_at を解除）。"""
    con.execute("UPDATE tasks SET deleted_at=NULL, updated_at=datetime('now') WHERE id=?", (int(id),))
    con.commit()


def hard_delete_task(con, id: int) -> None:
    con.execute("DELETE FROM tasks WHERE id=?", (int(id),))
    con.commit()


# ---- 「今日明日のタスク」日次プラン（#101）----
# 担当が選んだタスクを軽い/重いに仕分け、当日+翌日の15分刻みカレンダーへ配置した結果を
# 確定スナップショットとして保存する。上書きせず毎回新しい行を追加する（再計画の履歴として残す）。

def create_daily_task_plan(con, *, owner: str, base_date: str, label: str, items: list[dict]) -> int:
    """1件の確定プラン＋配置済みタスク群をまとめて保存する。itemsは各要素
    {task_id, day_offset(0〜、既定は直近5日分), start_min, duration_min, lane, bucket} の辞書。"""
    cur = con.execute(
        "INSERT INTO daily_task_plans (owner, label, base_date) VALUES (?, ?, ?)",
        (owner, label, base_date))
    plan_id = cur.lastrowid
    for it in items:
        con.execute(
            "INSERT INTO daily_task_plan_items "
            "(plan_id, task_id, day_offset, start_min, duration_min, lane, bucket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan_id, int(it["task_id"]), int(it["day_offset"]), int(it["start_min"]),
             int(it["duration_min"]), int(it.get("lane") or 0), it["bucket"]))
    con.commit()
    return plan_id


def get_daily_task_plan(con, plan_id: int) -> dict | None:
    r = con.execute("SELECT * FROM daily_task_plans WHERE id=?", (int(plan_id),)).fetchone()
    return dict(r) if r else None


def get_latest_daily_task_plan(con, owner: str, base_date: str | None = None) -> dict | None:
    """指定担当の直近のプラン（base_date指定時はその日限定）。確認表示用。"""
    q = "SELECT * FROM daily_task_plans WHERE owner=?"
    params: list = [owner]
    if base_date:
        q += " AND base_date=?"
        params.append(base_date)
    q += " ORDER BY id DESC LIMIT 1"
    r = con.execute(q, params).fetchone()
    return dict(r) if r else None


def list_daily_task_plan_items(con, plan_id: int) -> list[dict]:
    """プランの配置済みアイテムに、対象タスクのtitle/due_dateを添えて返す（表示用）。"""
    rows = con.execute(
        "SELECT i.*, t.title AS task_title, t.due_date AS task_due_date, t.status AS task_status "
        "FROM daily_task_plan_items i LEFT JOIN tasks t ON t.id=i.task_id "
        "WHERE i.plan_id=? ORDER BY i.day_offset, i.start_min", (int(plan_id),)).fetchall()
    return [dict(r) for r in rows]


# ---- 繰り返し発生（定期複製）（#事務タスク） ----
# テンプレ側カードに is_recurring=1 と 頻度/複製日 を持たせておくと、日次判定
# （duplicate_due_recurring_tasks）で複製タイミングが来た期間分のカードを新規複製する。
# 複製先の列は既存の新規起票ルールに従い、複製カード自身は通常カード（繰り返しOFF）にする。

def set_task_recur(con, task_id: int, *, is_recurring: bool,
                   recur_freq: str | None = None, recur_dup_day=None,
                   commit: bool = True) -> None:
    """タスクの繰り返し設定を保存（テンプレ側カードにのみ付与）。
    OFFにしたら頻度・複製日をクリアする。recur_last_period はここでは触らない
    （複製の冪等キーは duplicate_due_recurring_tasks が管理する）。"""
    if not is_recurring:
        con.execute("UPDATE tasks SET is_recurring=0, recur_freq=NULL, recur_dup_day=NULL, "
                    "updated_at=datetime('now') WHERE id=?", (int(task_id),))
    else:
        freq = recur_freq if recur_freq in TASK_RECUR_FREQS else "monthly"
        try:
            dd = int(recur_dup_day)
        except (TypeError, ValueError):
            dd = None
        con.execute("UPDATE tasks SET is_recurring=1, recur_freq=?, recur_dup_day=?, "
                    "updated_at=datetime('now') WHERE id=?", (freq, dd, int(task_id)))
    if commit:
        con.commit()


def _recur_period_key(freq: str, d: date) -> str:
    """複製の冪等キー。毎週='YYYY-Www'（ISO週）／毎月='YYYY-MM'。"""
    if freq == "weekly":
        iso = d.isocalendar()
        return f"{iso[0]}-W{int(iso[1]):02d}"
    return f"{d.year}-{d.month:02d}"


def _recur_period_suffix(freq: str, d: date) -> str:
    """複製カードの名称サフィックス。毎月='N月分'／毎週='M/D週分'（その週の月曜起点）。"""
    if freq == "weekly":
        monday = d - timedelta(days=d.weekday())
        return f"{monday.month}/{monday.day}週分"
    return f"{d.month}月分"


def _days_in_month(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def _recur_is_due(freq: str, dup_day, today: date) -> bool:
    """複製タイミングが来ているか。毎月=today.day>=複製日（月末超えはその月の末日にクランプ）、
    毎週=today.weekday()>=複製曜日。実行漏れ（cron未実行日）があっても、当該期間内で
    タイミング日以降なら発火する（期間キーの冪等ガードで二重複製は防ぐ）。"""
    if freq == "weekly":
        try:
            wd = int(dup_day)
        except (TypeError, ValueError):
            wd = 0
        wd = max(0, min(6, wd))
        return today.weekday() >= wd
    # monthly
    try:
        dd = int(dup_day)
    except (TypeError, ValueError):
        dd = 1
    dd = max(1, min(31, dd))
    eff = min(dd, _days_in_month(today.year, today.month))
    return today.day >= eff


def duplicate_due_recurring_tasks(con, today: date | None = None,
                                  commit: bool = True) -> list[int]:
    """繰り返し発生テンプレ（is_recurring=1）のうち、複製タイミングが来ていて
    かつ当該期間をまだ複製していないものを、新規カードとして複製する。冪等（同日再実行で
    増えない）。複製した/スキップした期間キーをテンプレの recur_last_period に記録する。
    新規複製カードのidリストを返す。"""
    if today is None:
        from datetime import datetime, timezone
        today = datetime.now(timezone(timedelta(hours=9))).date()  # JST基準
    templates = [dict(r) for r in con.execute(
        "SELECT * FROM tasks WHERE COALESCE(is_recurring,0)=1 "
        "AND (deleted_at IS NULL OR deleted_at='')")]
    new_ids: list[int] = []
    for t in templates:
        freq = t.get("recur_freq") or "monthly"
        if freq not in TASK_RECUR_FREQS:
            continue
        if not _recur_is_due(freq, t.get("recur_dup_day"), today):
            continue
        pkey = _recur_period_key(freq, today)
        if (t.get("recur_last_period") or "") == pkey:
            continue  # 当該期間は複製済み（冪等）
        suffix = _recur_period_suffix(freq, today)
        base_title = (t.get("title") or "").strip()
        new_title = (f"{base_title} {suffix}").strip()
        # 二重複製の保険: 同名の通常カード（テンプレ以外）が既にあればスキップ
        dup = con.execute(
            "SELECT 1 FROM tasks WHERE title=? AND COALESCE(is_recurring,0)=0 "
            "AND (deleted_at IS NULL OR deleted_at='') LIMIT 1", (new_title,)).fetchone()
        if not dup:
            is_admin = 1 if t.get("is_admin") else 0
            assignee = (t.get("assignee") or "").strip() or None
            # 期限: テンプレに相対期限の仕組みは無いため、事務タスクは新規起票と同じ既定
            # （3営業日後）を入れて受付ルールに乗せる。通常タスクは空のまま。
            due = add_business_days(today, 3).isoformat() if is_admin else None
            nid = upsert_task(
                con, title=new_title,
                detail=t.get("detail") or None,
                requester=t.get("requester") or None,
                assignee=assignee,
                due_date=due,
                priority=t.get("priority") or "中",
                category=t.get("category") or None,
                status="受信箱", is_admin=is_admin, source="recur",
                commit=False,
            )
            # 受付ルール: 担当＋期限が揃えば受信箱→未着手へ自動整理（手動/Slack起票と同じ）
            if assignee and due:
                set_task_status(con, nid, "未着手", commit=False)
            new_ids.append(nid)
        # 期間キーを記録（複製有無に関わらず＝当該期間はこれ以上動かさない）
        con.execute("UPDATE tasks SET recur_last_period=?, updated_at=datetime('now') WHERE id=?",
                    (pkey, t["id"]))
    if commit:
        con.commit()
    return new_ids


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


def list_task_links(con, task_id: int) -> list[dict]:
    """タスクに貼られた関連リンクを追加順で返す（ユーザー要望2026-08-28）。"""
    return [dict(r) for r in con.execute(
        "SELECT * FROM task_links WHERE task_id=? ORDER BY id", (int(task_id),))]


def add_task_link(con, task_id: int, url: str, label: str | None = None) -> int | None:
    """関連リンクを1件追加。urlが空なら何もしない（Noneを返す）。httpスキーム無しは https:// を補う。"""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    cur = con.execute("INSERT INTO task_links (task_id, url, label) VALUES (?,?,?)",
                      (int(task_id), url, (label or "").strip() or None))
    con.commit()
    return cur.lastrowid


def delete_task_link(con, link_id: int) -> None:
    con.execute("DELETE FROM task_links WHERE id=?", (int(link_id),))
    con.commit()


# ── Slack起票の「分割するか確認」待ち（#151） ──

def create_pending_task_split(con, *, channel: str, thread_ts: str, text: str,
                              prefills: list[dict], is_admin: bool = False,
                              user_id: str | None = None, token: str | None = None) -> int:
    """AIが複数タスクに分割できると判定した際の確認待ちデータを保存する。
    ボタン押下(get_pending_task_split→delete_pending_task_split)まではタスク化しない。"""
    cur = con.execute(
        "INSERT INTO pending_task_splits (channel, thread_ts, user_id, text, is_admin, token, "
        "prefills_json) VALUES (?,?,?,?,?,?,?)",
        (channel, thread_ts, user_id, text, 1 if is_admin else 0, token,
         _json.dumps(prefills, ensure_ascii=False)))
    con.commit()
    return cur.lastrowid


def get_pending_task_split(con, split_id: int) -> dict | None:
    r = con.execute("SELECT * FROM pending_task_splits WHERE id=?", (int(split_id),)).fetchone()
    if not r:
        return None
    out = dict(r)
    try:
        out["prefills"] = _json.loads(out.get("prefills_json") or "[]")
    except Exception:
        out["prefills"] = []
    return out


def delete_pending_task_split(con, split_id: int) -> None:
    con.execute("DELETE FROM pending_task_splits WHERE id=?", (int(split_id),))
    con.commit()


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


def clear_orphaned_task_links(con) -> int:
    """紐づけ先(商談/論点/Delivery/開発案件)が削除済みで参照切れになっている紐づけを
    クリアする（自己修復）。返り値はクリア・削除した件数の合計。
    ユーザー報告2026-08-29: 商談/論点/Deliveryを削除すると、それを参照していたタスクの
    link_type/link_idが残ったままになり、看板上部の「紐づけられている案件」一覧から
    静かに消えてしまっていた（task_link_labelが参照先を解決できずNoneを返すため）。
    delete_deal/delete_deal_issue/delete_deliveryの直後、およびtask_link_summary計算時に
    呼んで自己修復する。2026-09-02(#146): 複数関連付け対応のtask_entity_linksと
    dev_projectも対象に拡張。"""
    cleared = 0
    for link_type, table in (("deal", "deals"), ("issue", "deal_issues"),
                             ("delivery", "deliveries"), ("dev_project", "dev_projects")):
        rows = con.execute(
            f"SELECT id FROM tasks WHERE link_type=? AND link_id IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id = tasks.link_id)",
            (link_type,)).fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            con.execute(
                f"UPDATE tasks SET link_type=NULL, link_id=NULL WHERE id IN ({','.join('?' for _ in ids)})",
                ids)
            cleared += len(ids)
        cur = con.execute(
            f"DELETE FROM task_entity_links WHERE link_type=? "
            f"AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id = task_entity_links.link_id)",
            (link_type,))
        if cur.rowcount and cur.rowcount > 0:
            cleared += cur.rowcount
    if cleared:
        con.commit()
    return cleared


def task_link_summary(con) -> dict:
    """コンサルタスクの紐づけ先(商談/論点/Delivery)ごとの未完了/完了件数集計
    （看板上部の「紐づけられている案件」一覧用、ユーザー要望2026-08-27）。
    未完了タスクが1件も無い紐づけ先（完了にしか登場しない案件）は結果に含めない。
    2026-09-02(#146): 複数関連付け対応。1タスクが複数の紐づけ先を持つ場合、
    該当する全ての紐づけ先に重複してカウントする（ユーザー確定仕様）。"""
    clear_orphaned_task_links(con)  # 参照切れの紐づけがあれば自己修復してから集計
    out: dict = {"deal": [], "issue": [], "delivery": []}
    # (task_id, link_type, link_id) をキーに、レガシー単一列とtask_entity_linksを
    # マージ（同じ組が両方にあっても二重カウントしない）。
    status_by_key: dict[tuple, str] = {}
    for r in con.execute(
        "SELECT id AS task_id, link_type, link_id, status FROM tasks "
        "WHERE COALESCE(is_admin,0)=0 AND (deleted_at IS NULL OR deleted_at='') "
        "AND link_type IN ('deal','issue','delivery') AND link_id IS NOT NULL"):
        status_by_key[(r["task_id"], r["link_type"], r["link_id"])] = r["status"] or ""
    for r in con.execute(
        "SELECT tel.task_id AS task_id, tel.link_type AS link_type, tel.link_id AS link_id, "
        "t.status AS status FROM task_entity_links tel JOIN tasks t ON t.id = tel.task_id "
        "WHERE tel.link_type IN ('deal','issue','delivery') "
        "AND COALESCE(t.is_admin,0)=0 AND (t.deleted_at IS NULL OR t.deleted_at='')"):
        status_by_key.setdefault((r["task_id"], r["link_type"], r["link_id"]), r["status"] or "")
    agg: dict[tuple[str, int], dict] = {}
    for (_task_id, lt, lid), status in status_by_key.items():
        slot = agg.setdefault((lt, lid), {"open_n": 0, "done_n": 0})
        if status == "完了":
            slot["done_n"] += 1
        else:
            slot["open_n"] += 1
    for (lt, lid), slot in agg.items():
        if not slot["open_n"]:
            continue
        label = task_link_label(con, lt, lid)
        if not label:
            continue
        out[lt].append({"id": lid, "label": label, "open_n": slot["open_n"], "done_n": slot["done_n"]})
    for k in out:
        out[k].sort(key=lambda x: -x["open_n"])
    return out


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
    Hisho側（todos/dev_projects）のクリーンアップは呼び出し側でtheme_client経由の best-effort。
    タスクの紐づけ(link_type/link_id)がこの商談・配下の論点・Deliveryを参照していた場合は
    clear_orphaned_task_linksで自己修復する（削除後に静かに参照切れになるのを防ぐ）。"""
    con.execute("DELETE FROM deals WHERE id=?", (int(deal_id),))
    con.commit()
    clear_orphaned_task_links(con)


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
                     body: str | None = None, intake_transcript_id=None) -> int:
    """新規ノートを作成し idを返す。sort_orderは末尾。"""
    nx = con.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM rich_notes WHERE kind=? AND entity_id=?",
                     (kind, int(entity_id))).fetchone()[0]
    cur = con.execute(
        "INSERT INTO rich_notes (kind, entity_id, title, body, sort_order, intake_transcript_id) "
        "VALUES (?,?,?,?,?,?)",
        (kind, int(entity_id), (title or None), (body or None), nx,
         int(intake_transcript_id) if intake_transcript_id else None))
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
                    overview: str = "", confidence_override: str | None = None) -> int:
    """confidence_override省略時（既定）は自動導出（商談stage/statusに連動）。
    起票時点で確度を確定/見込み等に固定したい場合はDELIVERY_CONFIDENCE_LEVELSのいずれかを渡す。"""
    if confidence_override not in DELIVERY_CONFIDENCE_LEVELS:
        confidence_override = None
    cur = con.execute(
        "INSERT INTO deliveries (deal_id, title, start_week, end_week, status, overview, confidence_override) "
        "VALUES (?,?,?,?,?,?,?)",
        (int(deal_id), title or None, start_week or None, end_week or None,
         status or "進行中", overview or None, confidence_override))
    con.commit()
    return cur.lastrowid


def get_delivery(con, delivery_id: int) -> dict | None:
    r = con.execute(
        "SELECT dv.*, d.deal_name, d.stage AS deal_stage, d.status AS deal_status, "
        "d.business_type_l1 AS deal_business_type_l1, d.business_type_l2 AS deal_business_type_l2, "
        "acc.name AS account_name "
        "FROM deliveries dv JOIN deals d ON d.id=dv.deal_id "
        "LEFT JOIN accounts acc ON acc.id=d.account_id WHERE dv.id=?",
        (int(delivery_id),)).fetchone()
    return dict(r) if r else None


def list_deliveries(con, *, deal_id: int | None = None) -> list[dict]:
    """Delivery一覧（deal名・stage・status付き）。deal_id指定でその商談分のみ。"""
    sql = ("SELECT dv.*, d.deal_name, d.stage AS deal_stage, d.status AS deal_status, "
           "d.business_type_l1 AS deal_business_type_l1, d.business_type_l2 AS deal_business_type_l2, "
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


def delivery_display_costs(dv: dict) -> tuple:
    """一覧・出力の表示用 (cost_monthly, cost_total)。外注費。delivery_display_feesと同じロジック
    （cost_modeの入力値を正とし、現在の月数でもう一方を都度再計算する）。"""
    months = delivery_month_count(dv.get("start_week"), dv.get("end_week"))
    if (dv.get("cost_mode") or "monthly") == "total":
        return compute_delivery_fee("total", None, dv.get("cost_total"), months)
    return compute_delivery_fee("monthly", dv.get("cost_monthly"), None, months)


def delivery_profit(dv: dict) -> tuple:
    """(profit_monthly, profit_total) = 報酬額 − 外注費。両方未入力ならNone、片方だけ未入力は
    0扱いで差額を出す（外注費未入力＝外注費0として利益=報酬額、が直感に合う）。"""
    fee_mon, fee_tot = delivery_display_fees(dv)
    cost_mon, cost_tot = delivery_display_costs(dv)

    def _sub(fee, cost):
        if fee is None and cost is None:
            return None
        return (fee or 0.0) - (cost or 0.0)

    return (_sub(fee_mon, cost_mon), _sub(fee_tot, cost_tot))


def delivery_business_type_effective(dv: dict) -> tuple:
    """(l1, l2) 有効値。business_type_l1/l2_override（人間の手修正）があれば優先、無ければ
    紐づく商談のbusiness_type_l1/l2を継承（1商談から複数Deliveryが分かれる場合に、
    Deliveryごとに事業種別を分けたいケースに対応。ユーザー要望2026-08-23）。
    dv は deal_business_type_l1/l2/business_type_l1_override/l2_overrideを含む辞書
    （get_delivery/list_deliveriesの戻り値）。"""
    l1 = dv.get("business_type_l1_override") or dv.get("deal_business_type_l1")
    l2 = dv.get("business_type_l2_override") or dv.get("deal_business_type_l2")
    return (l1, l2)


def compute_delivery_fee(mode: str | None, monthly, total, months) -> tuple:
    """(fee_monthly, fee_total) を返す。mode='monthly'なら月額を正とし総額=月額×月数、
    mode='total'なら総額を正とし月額=総額÷月数。月数は合計週数÷4（小数可）。空/不正は None。
    両方に値がある場合は再計算せずそのまま返す（新規タスク: 自動換算後に人間が灰色側を
    手修正できる仕様。クライアント側で既に自動換算済み・または手修正済みの値を尊重する。
    片方が空の場合のみ、もう一方から補完する）。"""
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
    if mo is not None and to is not None:
        return (mo, to)
    if (mode or "monthly") == "total":
        return (round(to / m, 2) if to is not None else None, to)
    return (mo, round(mo * m, 2) if mo is not None else None)


def update_delivery(con, delivery_id: int, **fields) -> None:
    allowed = {"title", "start_week", "end_week", "status", "overview",
               "fee_mode", "fee_monthly", "fee_total", "confidence_override",
               "cost_mode", "cost_monthly", "cost_total", "cost_vendor",
               "payment_cycle_months", "business_type_l1_override", "business_type_l2_override",
               "responsible_owner", "handling_owner", "billing_method", "billing_due",
               "billing_recipient", "expense_billing", "expense_billing_note",
               "performance_fee", "performance_fee_ratio"}
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


def duplicate_delivery(con, delivery_id: int) -> int | None:
    """Deliveryを複製する（ユーザー要望2026-08-27）。deal_duplicateと同じ思想：
    計画情報（体制の目標役割・報酬/外注費設定・期間の初期値）は引き継ぎ、実行済みの
    実績データ（アサイン=誰がいつ稼働したか・月別検収額）と確度の手動固定は引き継がず、
    ステータスは「進行中」から真っ白に始める。戻り値は新規Delivery id（元が無ければNone）。"""
    src = get_delivery(con, delivery_id)
    if not src:
        return None
    new_id = create_delivery(
        con, deal_id=src["deal_id"], title=(src.get("title") or "(無題)") + "（コピー）",
        start_week=src.get("start_week"), end_week=src.get("end_week"),
        status="進行中", overview=src.get("overview") or "")
    update_delivery(
        con, new_id,
        fee_mode=src.get("fee_mode"), fee_monthly=src.get("fee_monthly"), fee_total=src.get("fee_total"),
        cost_mode=src.get("cost_mode"), cost_monthly=src.get("cost_monthly"), cost_total=src.get("cost_total"),
        cost_vendor=src.get("cost_vendor"), payment_cycle_months=src.get("payment_cycle_months"),
        performance_fee=src.get("performance_fee"), performance_fee_ratio=src.get("performance_fee_ratio"),
        business_type_l1_override=src.get("business_type_l1_override"),
        business_type_l2_override=src.get("business_type_l2_override"),
        # 請求関連は計画情報として引き継ぐ。責任者/担当者はアサインリストから選ぶ値のため、
        # 複製直後はアサインが空（実行データは引き継がない方針）に合わせて引き継がない。
        billing_method=src.get("billing_method"), billing_due=src.get("billing_due"),
        billing_recipient=src.get("billing_recipient"), expense_billing=src.get("expense_billing"),
        expense_billing_note=src.get("expense_billing_note"))
    for role in list_delivery_roles(con, delivery_id):
        add_delivery_role(con, delivery_id=new_id, role=role["role"],
                          fte_billing=role.get("fte_billing"), fte_pct=role.get("fte_pct"))
    return new_id


def delete_delivery(con, delivery_id: int) -> None:
    # delivery_assignments/delivery_receipts は ON DELETE CASCADE。念のためFK ON前提でなくても消す。
    con.execute("DELETE FROM delivery_assignments WHERE delivery_id=?", (int(delivery_id),))
    con.execute("DELETE FROM delivery_receipts WHERE delivery_id=?", (int(delivery_id),))
    con.execute("DELETE FROM deliveries WHERE id=?", (int(delivery_id),))
    con.commit()
    clear_orphaned_task_links(con)


def list_delivery_receipts(con, delivery_id: int) -> list[dict]:
    """月別検収額（月別入金計画）の登録済み行。月昇順。"""
    return [dict(r) for r in con.execute(
        "SELECT * FROM delivery_receipts WHERE delivery_id=? ORDER BY month",
        (int(delivery_id),))]


def set_delivery_receipt(con, delivery_id: int, month: str, amount) -> None:
    """月別検収額を1ヶ月分保存。amountが空/不正ならその月の行を削除（未入力に戻す）。"""
    month = (str(month) or "")[:7]
    try:
        amt = float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amt = None
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:7].isdigit():
        return
    if amt is None:
        con.execute("DELETE FROM delivery_receipts WHERE delivery_id=? AND month=?",
                    (int(delivery_id), month))
    else:
        con.execute(
            "INSERT INTO delivery_receipts (delivery_id, month, amount) VALUES (?,?,?) "
            "ON CONFLICT(delivery_id, month) DO UPDATE SET amount=excluded.amount",
            (int(delivery_id), month, amt))
    con.commit()


def _add_months_ym(year: int, month: int, n: int) -> tuple:
    idx = year * 12 + (month - 1) + n
    return (idx // 12, idx % 12 + 1)


def delivery_month_range(dv: dict, extra_months: int = 0) -> list:
    """開始週〜終了週を月初単位の'YYYY-MM'リストに展開（末尾にextra_months分延長）。不正/未設定は[]。"""
    try:
        sd = date.fromisoformat(str(dv.get("start_week"))[:10])
        ed = date.fromisoformat(str(dv.get("end_week"))[:10])
    except (TypeError, ValueError):
        return []
    s_idx = sd.year * 12 + (sd.month - 1)
    e_idx = ed.year * 12 + (ed.month - 1) + max(0, int(extra_months or 0))
    months = []
    idx = s_idx
    while idx <= e_idx:
        y, m = idx // 12, idx % 12 + 1
        months.append(f"{y:04d}-{m:02d}")
        idx += 1
    return months


def delivery_cashflow(con, delivery_id: int) -> dict:
    """月別入金計画: {"months": [...], "receipts": {月: 検収額}, "payments": {月: 入金額}}。
    入金額は検収額をpayment_cycle_months分先の月へずらして算出（同月に複数検収があれば合算）。"""
    dv = get_delivery(con, delivery_id)
    if not dv:
        return {"months": [], "receipts": {}, "payments": {}}
    cycle = int(dv.get("payment_cycle_months") or 0)
    months = set(delivery_month_range(dv, extra_months=cycle))
    receipts = {r["month"]: r["amount"] for r in list_delivery_receipts(con, delivery_id)}
    payments = {}
    for m, amt in receipts.items():
        try:
            y, mo = int(m[:4]), int(m[5:7])
        except (TypeError, ValueError):
            continue
        py, pmo = _add_months_ym(y, mo, cycle)
        pm = f"{py:04d}-{pmo:02d}"
        payments[pm] = (payments.get(pm) or 0.0) + (amt or 0.0)
        months.add(m)
        months.add(pm)
    return {"months": sorted(months), "receipts": receipts, "payments": payments}


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


def close_won_if_needed(con, deal_id: int, *, commit: bool = False) -> bool:
    """「受注・契約処理完了」の明示的なクローズ処理: stage='受注' かつ未クローズなら
    status='closed' にする（close_reason は既存が空のとき '受注' を既定投入。stage は
    '受注' のまま保持）。クローズしたら True。

    2026-08時点の方針: stage を '受注' にした時点では自動クローズしない
    （受注確定はしたが契約処理が終わっていない、というopenな期間を許容するため）。
    人間が明示的に「受注・契約処理完了」ボタン（POST /deal/{id}/close-won、
    deal_hygiene_pageの②一覧からも同じ経路）を押した時だけこの関数が呼ばれる。
    commit=False（既定）は呼び出し側でまとめて commit する想定。"""
    row = con.execute("SELECT stage, status FROM deals WHERE id=?", (int(deal_id),)).fetchone()
    if not row or (row["stage"] or "") != "受注" or (row["status"] or "open") == "closed":
        return False
    con.execute(
        "UPDATE deals SET status='closed', "
        "close_reason=COALESCE(NULLIF(close_reason,''),'受注'), "
        "updated_at=datetime('now') WHERE id=?", (int(deal_id),))
    if commit:
        con.commit()
    return True


def close_deal_to_lead(con, deal_id: int, close_reason: str, memo: str = "") -> int | None:
    """商談を『受注に至らずクローズ』し、リードへ差し戻す統一処理（#67）。
    /deal/{id}/revert_to_lead のクローズ用モーダルから呼ぶのが正規のUIだが、ステージを
    直接「失注」に変更した場合（フォーム保存・一括編集・インライン変更・ヒアリング取込経由の
    どれでも）も同じ処理を通す（2026-08-18: 直接変更だと本処理がスキップされ、status='closed'
    にならない・リード化されない・紐づくDeliveryも止まらないままになっていたバグの修正）。
    close_reason は呼び出し元で CLOSE_REASONS に含まれることを確認してから渡すこと。
    既にクローズ済みの商談には何もせず None を返す。作成/更新したリードのidを返す。"""
    deal = get_deal(con, deal_id)
    if not deal or (deal.get("status") or "open") == "closed":
        return None
    acct_row = con.execute("SELECT * FROM accounts WHERE id=?", (deal.get("account_id"),)).fetchone()
    acct = dict(acct_row) if acct_row else {}

    close_line = "アポ未獲得のためクローズ（リードに戻す）"
    if close_reason:
        close_line += f"／終了理由: {close_reason}"
    existing_note = deal.get("note") or ""
    body = f"{existing_note}\n{close_line}" if existing_note else close_line
    new_note = f"[リードに戻す時のメモ] {memo}\n{body}" if memo else body

    con.execute(
        "UPDATE deals SET status='closed', note=?, "
        "close_reason=COALESCE(?, close_reason), "
        "stage=CASE WHEN ?='失注' THEN '失注' ELSE stage END, "
        "updated_at=datetime('now') WHERE id=?",
        (new_note, close_reason, close_reason, deal_id),
    )
    close_deliveries_on_deal_lost(con, deal_id, close_reason)

    lid = None
    lead_row = con.execute("SELECT * FROM leads WHERE deal_id=? LIMIT 1", (deal_id,)).fetchone()
    if lead_row:
        lead = dict(lead_row)
        lid = lead["id"]
        lead_notes = lead.get("notes") or ""
        lead_new_notes = f"{new_note}\n{lead_notes}" if lead_notes else new_note
        con.execute(
            "UPDATE leads SET lead_status='following', deal_id=NULL, "
            "industry=COALESCE(?, industry), company_size=COALESCE(?, company_size), "
            "notes=?, updated_at=datetime('now') WHERE id=?",
            (acct.get("industry"), acct.get("company_size"), lead_new_notes, lid),
        )
        activity_note = "アポ未獲得のため商談からリードへ戻す（フォロー中に変更）。"
        if memo:
            activity_note += f" メモ: {memo}"
        con.execute(
            "INSERT INTO lead_activities (lead_id,type,content,author) VALUES (?,?,?,?)",
            (lid, "note", activity_note, "システム"),
        )
    else:
        origin_line = f"商談 #{deal_id}（{deal.get('deal_name', '')}）からリードに戻す"
        lid = upsert_lead(
            con, name=acct.get("name", "（不明）"),
            company=acct.get("name", "（不明）"),
            industry=acct.get("industry"),
            company_size=acct.get("company_size"),
            lead_status="following",
            notes=f"{origin_line}\n{new_note}",
            assigned_to=deal.get("owner"),
        )
    con.commit()
    return lid


def close_deliveries_on_deal_lost(con, deal_id: int, close_reason: str | None) -> int:
    """商談をリードに戻す（＝受注に至らずクローズ）際、紐づくDeliveryのうち進行中のものを
    連動して止める。終了理由が「保留・時期尚早」ならDelivery側も「保留」、それ以外
    （失注/ニーズなし/キャンセル/自社都合で撤退）は「中止」にする。既に完了・保留・中止
    済みのDeliveryは上書きしない（手動で状態管理されている前提を尊重）。更新件数を返す。"""
    new_status = "保留" if close_reason == "保留・時期尚早" else "中止"
    rows = con.execute(
        "SELECT id FROM deliveries WHERE deal_id=? AND status='進行中'", (int(deal_id),)
    ).fetchall()
    for r in rows:
        update_delivery(con, r["id"], status=new_status)
    return len(rows)


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
    # 並びは id 順＝SFAベース工数フォームで最後に保存したスロット順（機能名のアルファベット順ではない）。
    # replace_base_workload_for_owner が DELETE→INSERT でスロット順に採番するため、id順＝入力順になる。
    # これによりSFAフォーム・Hishoダッシュボード（base_items）の表示順が一致する。
    if owner:
        return [dict(r) for r in con.execute(
            "SELECT * FROM base_workload WHERE owner=? ORDER BY id", (owner,))]
    return [dict(r) for r in con.execute(
        "SELECT * FROM base_workload ORDER BY owner, id")]


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


# ---- 週次稼働の手動編集（#稼働予定）。owner×week に手動プラン（項目＋備考）を持つ。 ----
def get_weekly_workload(con, owner: str, week: str) -> dict:
    """1セル分の手動プラン。{owner, week, items:[{id,label,kind,pct,sort_order}], note, exists}。"""
    items = [dict(r) for r in con.execute(
        "SELECT id, label, kind, pct, sort_order FROM weekly_workload_manual "
        "WHERE owner=? AND week=? ORDER BY sort_order, id", (owner, week))]
    nr = con.execute("SELECT note FROM weekly_workload_note WHERE owner=? AND week=?",
                     (owner, week)).fetchone()
    note = (nr["note"] if nr else "") or ""
    return {"owner": owner, "week": week, "items": items, "note": note,
            "exists": bool(items) or bool(note)}


def save_weekly_workload(con, owner: str, week: str, items: list, note=None) -> None:
    """1セルの手動プランを全置換で保存。items=[{label,pct,kind}]。noteはNoneなら据え置き。
    itemsが空でnoteも空なら手動プランを消して自動に戻す（clearと同義）。"""
    owner = (owner or "").strip()
    week = (week or "").strip()
    if not owner or not week:
        return
    norm = []
    for it in (items or []):
        lab = (str(it.get("label") or "")).strip()
        if not lab:
            continue
        try:
            pct = float(it.get("pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        norm.append((lab, (it.get("kind") or "other"), pct))
    note_val = None if note is None else (str(note).strip())
    if not norm and not (note_val or ""):
        clear_weekly_workload(con, owner, week)
        return
    con.execute("DELETE FROM weekly_workload_manual WHERE owner=? AND week=?", (owner, week))
    for i, (lab, kind, pct) in enumerate(norm):
        con.execute(
            "INSERT INTO weekly_workload_manual(owner, week, label, kind, pct, sort_order) "
            "VALUES(?,?,?,?,?,?)", (owner, week, lab, kind, pct, i))
    if note_val is not None:
        con.execute(
            "INSERT INTO weekly_workload_note(owner, week, note, updated_at) "
            "VALUES(?,?,?,datetime('now')) "
            "ON CONFLICT(owner, week) DO UPDATE SET note=excluded.note, updated_at=datetime('now')",
            (owner, week, note_val))
    con.commit()


def clear_weekly_workload(con, owner: str, week: str) -> None:
    """手動プランを削除して自動に戻す。"""
    con.execute("DELETE FROM weekly_workload_manual WHERE owner=? AND week=?", (owner, week))
    con.execute("DELETE FROM weekly_workload_note WHERE owner=? AND week=?", (owner, week))
    con.commit()


def list_weekly_workload_all(con) -> dict:
    """全手動セルを {owner: {week: {items:[{label,kind,pct}], note, total}}} で返す（グリッド表示用）。"""
    out: dict = {}
    for r in con.execute(
            "SELECT owner, week, label, kind, pct FROM weekly_workload_manual "
            "ORDER BY owner, week, sort_order, id"):
        cell = out.setdefault(r["owner"], {}).setdefault(r["week"], {"items": [], "note": "", "total": 0.0})
        cell["items"].append({"label": r["label"], "kind": r["kind"], "pct": r["pct"]})
        cell["total"] += (r["pct"] or 0)
    for r in con.execute("SELECT owner, week, note FROM weekly_workload_note"):
        cell = out.setdefault(r["owner"], {}).setdefault(r["week"], {"items": [], "note": "", "total": 0.0})
        cell["note"] = r["note"] or ""
    return out


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


_DELIVERY_CONFIDENCE_BUCKET = {
    "確定": "committed",
    "見込み(クロージング)": "closing",
    "見込み(提案中)": "proposal",
}   # "無効(終了)" はバケット無し＝集計除外


def compute_delivery_load(con, *, start_week: str | None = None,
                          n_weeks: int = DELIVERY_VIEW_WEEKS,
                          internal_only: bool = False) -> dict:
    """全社の Delivery 稼働(FTE%)を メンバー×週 に展開・合算（#75）。
    確度（見込み(提案中)/見込み(クロージング)/確定/無効(終了)）は delivery_confidence_effective で
    導出（自動＝商談stage/statusから、confidence_overrideがあればそれを優先）。
    確度=無効(終了)、または当該Deliveryのstatus≠進行中（完了/保留/中止）は集計から除外
    （delivery_is_active）。internal_only=True で 外部メンバー(member_kind='外部') を除外
    （Hisho稼働予定はこちら）。items にはクリック明細用の各アサイン（案件名・役割・期間・稼働率・確度）を返す。
    デモ開発負荷は別系統(Hisho側で加算)。ここでは delivery と base のみ返す。"""
    start_week = start_week or _monday_of(date.today())
    weeks = _weeks_from(start_week, n_weeks)
    week_end = weeks[-1] if weeks else start_week
    # owner -> week -> {actual:{committed,closing,proposal}, billing:{committed,closing,proposal}}
    cells: dict = {}
    items: list = []

    def _blank():
        return {"actual": {"committed": 0.0, "closing": 0.0, "proposal": 0.0},
                "billing": {"committed": 0.0, "closing": 0.0, "proposal": 0.0}}

    # 案件ごとの体制(役割)並び順（delivery_form と同じ id 昇順）。クリック内訳をこの順で並べる用。
    _role_order: dict = {}
    for rr in con.execute("SELECT delivery_id, role FROM delivery_roles ORDER BY delivery_id, id"):
        _m = _role_order.setdefault(rr["delivery_id"], {})
        if rr["role"] not in _m:
            _m[rr["role"]] = len(_m)

    for r in con.execute(
        "SELECT da.owner, da.role, da.member_kind, da.from_week, da.to_week, da.fte_pct, da.fte_billing, "
        "d.stage AS deal_stage, d.status AS deal_status, d.deal_name, "
        "dv.id AS delivery_id, dv.title AS delivery_title, dv.status AS dv_status, "
        "dv.confidence_override AS confidence_override, "
        "dv.start_week AS delivery_start, acc.name AS account_name "
        "FROM delivery_assignments da "
        "JOIN deliveries dv ON dv.id=da.delivery_id "
        "JOIN deals d ON d.id=dv.deal_id "
        "LEFT JOIN accounts acc ON acc.id=d.account_id"):
        _dv_like = {"status": r["dv_status"], "confidence_override": r["confidence_override"],
                    "deal_stage": r["deal_stage"], "deal_status": r["deal_status"]}
        if not delivery_is_active(_dv_like):
            continue  # 状態≠進行中、または確度=無効(終了)は集計から除外
        if internal_only and (r["member_kind"] or "内部") == "外部":
            continue
        confidence = delivery_confidence_effective(_dv_like)
        key = _DELIVERY_CONFIDENCE_BUCKET[confidence]
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
                "actual": actual, "billing": billing, "confidence": key,
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
