"""週次レポート数字パックの経営KPI（A/C/F/G）計算テスト（compute_kpi_pack）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
共通定義（厳守）:
  open=status='open' or NULL / closed=status='closed'
  sales=business_type_l1≠'コスト削減' / 受注=stage='受注'(全status)
  失注=closed & close_reason='失注' / 面談=type='面談' & occurred_on非空
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from cowork import sfa_db, weekly_report

TODAY = date(2026, 7, 26)


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_kpi_test_")
    db = str(Path(d) / "t.db")
    sfa_db.init_db(db)
    conn = sfa_db.connect(db)
    try:
        yield conn
    finally:
        conn.close()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def acc(con):
    aid = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    con.commit()
    return aid


def _deal(con, acc, **f):
    # close_reason は DEAL_FIELDS 外（クローズ処理側の管理）なので別途SQLで設定する。
    close_reason = f.pop("close_reason", None)
    f.setdefault("account_id", acc)
    f.setdefault("deal_name", "D")
    f.setdefault("business_type_l1", "コンサルティング")  # 既定はsales
    f.setdefault("status", "open")
    did = sfa_db.upsert_deal(con, **f)
    if close_reason is not None:
        con.execute("UPDATE deals SET close_reason=? WHERE id=?", (close_reason, did))
        con.commit()
    return did


def _mtg(con, did, d):
    sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on=d)


# ---- A. 商談ポジション内訳 ----

def test_position_buckets(con, acc):
    _deal(con, acc, stage="要件詰め", value_lumpsum=100, value_recurring=10)   # pipe
    _deal(con, acc, stage="受注", status="open")                              # won(open)
    _deal(con, acc, stage="受注", status="closed")                           # won(closed)も受注
    _deal(con, acc, stage="提案", status="closed", close_reason="失注")       # lost
    _deal(con, acc, stage="提案", status="closed", close_reason="ニーズなし")  # どのバケットにも入らない
    _deal(con, acc, stage="要件詰め", business_type_l1="コスト削減")            # cost(open)
    A = weekly_report.compute_kpi_pack(con, today=TODAY)["A"]
    assert A["nPipe"] == 1
    assert A["nWon"] == 2      # open/closed問わず受注
    assert A["nLost"] == 1
    assert A["nCost"] == 1
    assert A["nTotal"] == 5    # ニーズなしclosedは含まれない
    # パイプライン金額(年額換算)=単発100 + 継続10×12 = 220
    assert A["pipeline_annual"] == 220


# ---- C. プロセス品質 ----

def test_cq_budget_rate(con, acc):
    _deal(con, acc, stage="要件詰め", client_budget="500万")   # 達成
    _deal(con, acc, stage="提案", client_budget="")            # 未達(空)
    _deal(con, acc, stage="クロージング", client_budget="未確認")  # 未達
    _deal(con, acc, stage="受注", client_budget="")            # 母数外(受注)
    b = weekly_report.compute_kpi_pack(con, today=TODAY)["C"]["budget"]
    assert b["denom"] == 3
    assert b["miss"] == 2
    assert b["rate"] == pytest.approx(round(1 / 3 * 100, 1))


def test_cq_ms_within_week(con, acc):
    # 直近面談 7/24・today=7/26。MS 7/28(gap4・未来)=達成 / 8/10(gap17)=未達 /
    # 7/25(過去日<today)=未達 / 未設定=未達
    d_ok = _deal(con, acc, stage="要件詰め", next_milestone_date="2026-07-28")
    d_far = _deal(con, acc, stage="要件詰め", next_milestone_date="2026-08-10")
    d_past = _deal(con, acc, stage="要件詰め", next_milestone_date="2026-07-25")
    d_none = _deal(con, acc, stage="要件詰め")
    for did in (d_ok, d_far, d_past, d_none):
        _mtg(con, did, "2026-07-24")
    ms = weekly_report.compute_kpi_pack(con, today=TODAY)["C"]["ms"]
    assert ms["denom"] == 4
    assert ms["miss"] == 3            # far/past/none
    # 実績平均=面談→MS日数(MSのある母数=ok4, far17, past1) → 平均 (4+17+1)/3
    assert ms["avg"] == pytest.approx(round((4 + 17 + 1) / 3, 1))


def test_cq_close30(con, acc):
    # 初回面談から日数: within=10日(達成) / over=40日(未達) / 受注は母数だが未達対象外
    d_within = _deal(con, acc, stage="提案")
    d_over = _deal(con, acc, stage="提案")
    d_won = _deal(con, acc, stage="受注")
    _mtg(con, d_within, (TODAY - timedelta(days=10)).isoformat())
    _mtg(con, d_over, (TODAY - timedelta(days=40)).isoformat())
    _mtg(con, d_won, (TODAY - timedelta(days=100)).isoformat())
    c = weekly_report.compute_kpi_pack(con, today=TODAY)["C"]["close30"]
    assert c["denom"] == 3           # 面談1回以上のsales全部（受注含む）
    assert c["miss"] == 1            # overのみ（受注はopen&stage≠受注でないため未達対象外）
    assert c["avg"] == pytest.approx(25.0)   # 進行中(open&stage≠受注)母数=within10,over40 → 平均25


def test_cq_meeting_cap(con, acc):
    d_ok = _deal(con, acc, stage="初回アポ実施")      # 上限1
    d_over = _deal(con, acc, stage="初回アポ実施")     # 上限1・2回で超過
    _mtg(con, d_ok, "2026-07-01")
    _mtg(con, d_over, "2026-07-01")
    _mtg(con, d_over, "2026-07-02")
    cap = weekly_report.compute_kpi_pack(con, today=TODAY)["C"]["mtgcap"]
    assert cap["denom"] == 2
    assert cap["miss"] == 1
    assert cap["rate"] == pytest.approx(50.0)


def test_cq_zero_denominator(con, acc):
    # 母数0なら rate=0扱い
    b = weekly_report.compute_kpi_pack(con, today=TODAY)["C"]["budget"]
    assert b["denom"] == 0 and b["rate"] == 0.0


# ---- F. アウトカム転換率 ----

def test_outcome_conversion(con, acc):
    d_fm = _deal(con, acc, stage="要件詰め")          # 面談あり・提案未到達
    d_prop = _deal(con, acc, stage="提案")            # 面談あり・提案到達
    d_won = _deal(con, acc, stage="受注")             # 面談あり・提案到達・受注
    _deal(con, acc, stage="コスト削減案件", business_type_l1="コスト削減")  # sales外→除外
    for did in (d_fm, d_prop, d_won):
        _mtg(con, did, "2026-07-01")
    F = weekly_report.compute_kpi_pack(con, today=TODAY)["F"]
    assert F["reachedFM"] == 3
    assert F["reachedProp"] == 2      # 提案・受注
    assert F["won"] == 1
    assert F["cv1"] == pytest.approx(round(2 / 3 * 100, 1))
    assert F["cv2"] == pytest.approx(50.0)


# ---- G. デリバリー ----

def test_delivery_dedup_amount(con, acc):
    # 1商談を2 deliveryに分割 → active_countは商談1件、金額はdeal単位で1回だけ
    d1 = _deal(con, acc, stage="受注", value_recurring=30, value_lumpsum=200)
    sfa_db.create_delivery(con, deal_id=d1, title="前半", status="進行中")
    sfa_db.create_delivery(con, deal_id=d1, title="後半", status="進行中")
    # 別商談・完了deliveryは非active
    d2 = _deal(con, acc, stage="受注", value_recurring=99)
    sfa_db.create_delivery(con, deal_id=d2, title="完了案件", status="完了")
    G = weekly_report.compute_kpi_pack(con, today=TODAY)["G"]
    assert G["active_count"] == 1        # d1のみ（deal_idユニーク）
    assert G["recurring"] == 30          # 30を1回だけ（按分/二重計上しない）
    assert G["lumpsum"] == 200


def test_delivery_status_fallback_to_deal(con, acc):
    # status未設定(NULL)のdeliveryはDBのDEFAULT '進行中'と同じ解釈で既定active。
    # 除外されるのは確度が無効(終了)＝クローズ済みかつ非受注（失注等）の場合のみ
    # （sfa_db.delivery_is_active）。受注してクローズ済み（成約後の納品継続）はactive対象のまま。
    # create_delivery は空statusを'進行中'に補正するため、NULLは直接INSERTで用意する。
    d_won_closed = _deal(con, acc, stage="受注", status="closed", value_recurring=10)
    d_lost_closed = _deal(con, acc, stage="提案", status="closed", value_recurring=10)
    con.execute("INSERT INTO deliveries(deal_id, status) VALUES(?, NULL)", (d_won_closed,))
    con.execute("INSERT INTO deliveries(deal_id, status) VALUES(?, NULL)", (d_lost_closed,))
    con.commit()
    G = weekly_report.compute_kpi_pack(con, today=TODAY)["G"]
    assert G["active_count"] == 1        # 受注クローズのみactive（失注クローズは無効(終了)で除外）
    assert G["recurring"] == 10


# ---- 統合: compute_weekly_numbers と render_number_rail ----

def test_weekly_numbers_includes_kpi_and_rail(con, acc):
    _deal(con, acc, stage="要件詰め", value_lumpsum=100, client_budget="300万")
    nums = weekly_report.compute_weekly_numbers(con, as_of=TODAY)
    assert "kpi" in nums and "A" in nums["kpi"]
    rail = weekly_report.render_number_rail(nums)
    assert "会社全体の商談ポジション" in rail
    assert "プロセス品質" in rail
    assert "アウトカム転換率" in rail
    assert "デリバリー（進行中）" in rail


def test_snapshot_persists_kpi_keys(con, acc):
    _deal(con, acc, stage="要件詰め", value_lumpsum=100, value_recurring=10)
    m = weekly_report.compute_snapshot_metrics(con)
    for k in ("pos_pipe", "pos_won", "pos_lost", "pos_cost", "pipeline_annual",
              "cq_budget_rate", "cq_ms_rate", "cq_close30_rate", "cq_mtgcap_rate",
              "delivery_active_count", "delivery_recurring"):
        assert k in m
    assert m["pos_pipe"] == 1
    assert m["pipeline_annual"] == 220
