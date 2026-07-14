"""sfa_db.py の純関数（副作用なし・DB不要）のテスト。

開発案件の受注余地判定・日本の祝日/営業日計算・開発スケジュール自動逆算という
業務ロジックの根幹を対象にする。ここが壊れると開発案件の期日・受注見込みが狂う。
"""
from datetime import date

from cowork import sfa_db


# ---- compute_dev_order_potential（受注余地の自動判定） ----

def test_order_potential_budget_x_is_low():
    # 予算確認×なら他がどうであれ「低」
    assert sfa_db.compute_dev_order_potential(
        budget_confirmed="×", resolution="〇", difficulty="易") == "低"


def test_order_potential_high_conditions():
    # 予算〇 かつ 解像度〇 かつ 難易度が易/中 → 高
    assert sfa_db.compute_dev_order_potential(
        budget_confirmed="〇", resolution="〇", difficulty="易") == "高"
    assert sfa_db.compute_dev_order_potential(
        budget_confirmed="〇", resolution="〇", difficulty="中") == "高"


def test_order_potential_hard_is_medium():
    # 難易度「難」は高条件を満たさず「中」
    assert sfa_db.compute_dev_order_potential(
        budget_confirmed="〇", resolution="〇", difficulty="難") == "中"


def test_order_potential_defaults_to_medium():
    assert sfa_db.compute_dev_order_potential(
        budget_confirmed=None, resolution=None, difficulty=None) == "中"


# ---- 日本の祝日・営業日計算 ----

def test_known_holidays_2026():
    hol = sfa_db._jp_holidays(2026)
    assert date(2026, 1, 1) in hol       # 元日
    assert date(2026, 2, 11) in hol      # 建国記念の日
    assert date(2026, 5, 3) in hol       # 憲法記念日
    assert date(2026, 11, 23) in hol     # 勤労感謝の日


def test_is_business_day_weekend_and_holiday():
    assert sfa_db.is_business_day(date(2026, 7, 6)) is True    # 月曜（平日）
    assert sfa_db.is_business_day(date(2026, 7, 4)) is False   # 土曜
    assert sfa_db.is_business_day(date(2026, 7, 5)) is False   # 日曜
    assert sfa_db.is_business_day(date(2026, 1, 1)) is False   # 元日


def test_add_business_days_skips_weekend():
    # 金曜(2026-07-10)から1営業日後は月曜(07-13)。土日を飛ばす
    assert sfa_db.add_business_days(date(2026, 7, 10), 1) == date(2026, 7, 13)


def test_add_business_days_negative_and_zero():
    # 月曜(07-13)から1営業日前は金曜(07-10)
    assert sfa_db.add_business_days(date(2026, 7, 13), -1) == date(2026, 7, 10)
    # 0日はその日のまま
    assert sfa_db.add_business_days(date(2026, 7, 13), 0) == date(2026, 7, 13)


# ---- dev_period_days（ステージ係数: プロト×1/PoC×4/本番×2） ----

def test_dev_period_days_prototype_base():
    # プロト・バックエンド無し・難易度なし → 基本2日 ×1
    assert sfa_db.dev_period_days("プロト", "無し", "易") == 2


def test_dev_period_days_backend_and_difficulty():
    # 基本2 + バックエンド有り+3 + 難+5 = 10日（プロト×1）
    assert sfa_db.dev_period_days("プロト", "有り", "難") == 10


def test_dev_period_days_stage_multipliers():
    base = sfa_db.dev_period_days("プロト", "無し", "易")  # 2
    # ステージ倍率 プロト×1 / PoC×2 / 本番×2（PoCは4→2に緩和済み・#42）
    assert sfa_db.dev_period_days("PoC", "無し", "易") == base * 2
    assert sfa_db.dev_period_days("本番", "無し", "易") == base * 2


# ---- compute_dev_schedule（期限から開始/終了を逆算） ----

def test_compute_dev_schedule_none_when_no_deadline():
    assert sfa_db.compute_dev_schedule(None, "プロト", "無し", "易") == (None, None)


def test_compute_dev_schedule_invalid_date():
    assert sfa_db.compute_dev_schedule("not-a-date", "プロト", "無し", "易") == (None, None)


def test_compute_dev_schedule_end_equals_deadline():
    start, end = sfa_db.compute_dev_schedule("2026-08-31", "プロト", "無し", "易")
    assert end == "2026-08-31"
    # 開始日は終了日以前で、営業日ベースで逆算されている
    assert start is not None and start < end


def test_compute_dev_schedule_poc_starts_earlier_than_prototype():
    # 同条件ならPoC(×4)の方がプロト(×1)より開始が早い（期間が長い）
    s_proto, _ = sfa_db.compute_dev_schedule("2026-08-31", "プロト", "無し", "易")
    s_poc, _ = sfa_db.compute_dev_schedule("2026-08-31", "PoC", "無し", "易")
    assert s_poc < s_proto
