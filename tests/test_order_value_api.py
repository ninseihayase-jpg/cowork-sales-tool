"""受注高（月別） `webapp.order_value_by_month` の回帰テスト。

ユーザー要望(2026-10-01)「受注高の集計もしたいから、個別Deliveryに受注日を入力できる
ように」。集計元は月別売上(delivery_receipts)ではなく報酬総額（ユーザー確認2026-10-01:
受注日を入力した時点で金額が反映されるようにしたいため、後から埋める月別売上の内訳入力を
待たない方針）。

**2026-10-02修正**: 当初は`delivery_display_fees()`の固定報酬のみを使っており、固定報酬
総額が0円（成果報酬のみで契約しているDelivery）が受注高から漏れていた不具合をユーザー
報告で発見・`_delivery_fee_grand_total()`（固定報酬総額＋成果報酬額）に修正。

当初は独立タブ+SFA_API_TOKEN(非経営ロール限定)の専用ルート/api/order_valueだったが、
同日中にユーザー要望で「収支状況」タブの計算基準の3つ目の選択肢として統合され、経営
ロール限定に変更。独立ルートは廃止し、/api/cashflow_forecast(SFA_CASHFLOW_TOKEN保護)の
レスポンスに"order_value"キーとして合流した（HTTPルートのテストはtest_cashflow_forecast_
api.pyに統合済み）。一時DBのみ使用。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_order_value_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _delivery(con, acc_name="A社", order_date=None, fee_total=None, owner=None, deal_name="D"):
    aid = sfa_db.upsert_account(con, name=acc_name)
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name=deal_name, stage="受注", owner=owner)
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-09-14")
    fields = {}
    if order_date is not None:
        fields["order_date"] = order_date
    if fee_total is not None:
        fields["fee_mode"] = "total"
        fields["fee_total"] = fee_total
    if fields:
        sfa_db.update_delivery(con, dvid, **fields)
    return dvid


# ── webapp.order_value_by_month ──

def test_order_value_by_month_empty_db_returns_empty(con):
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}, "deliveries": {}}


def test_order_value_by_month_sums_fee_total_by_order_date_month(con):
    dv = _delivery(con, order_date="2026-09-15", fee_total=300, owner="岩崎")
    result = webapp.order_value_by_month(con)
    assert result["months"] == ["2026-09"]
    assert result["order_value"] == {"2026-09": 300}
    assert result["deliveries"]["2026-09"] == [{"id": dv, "name": "A社 / D", "amount": 300, "owner": "岩崎"}]


def test_order_value_by_month_combines_multiple_deliveries_in_same_month(con):
    _delivery(con, acc_name="A社", order_date="2026-09-01", fee_total=100, owner="岩崎")
    _delivery(con, acc_name="B社", order_date="2026-09-28", fee_total=50, owner="早瀬")

    result = webapp.order_value_by_month(con)
    assert result["months"] == ["2026-09"]
    assert result["order_value"] == {"2026-09": 150}
    assert {d["owner"] for d in result["deliveries"]["2026-09"]} == {"岩崎", "早瀬"}
    # 金額の大きい順にソートされていること
    assert [d["amount"] for d in result["deliveries"]["2026-09"]] == [100, 50]


def test_order_value_by_month_excludes_deliveries_without_order_date(con):
    _delivery(con, order_date=None, fee_total=999)
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}, "deliveries": {}}


def test_order_value_by_month_excludes_deliveries_without_fee_total(con):
    _delivery(con, order_date="2026-09-01")  # 受注日はあるが報酬総額/月額とも未入力
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}, "deliveries": {}}


def test_order_value_by_month_ignores_monthly_receipts_entirely(con):
    """月別売上(delivery_receipts)はもう受注高の集計に使わない
    （ユーザー確認2026-10-01: 受注日入力時点で金額を即反映したいため報酬総額へ切替）。"""
    dv = _delivery(con, order_date="2026-09-15", fee_total=None)
    sfa_db.set_delivery_receipt(con, dv, "2026-09", 9999)
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}, "deliveries": {}}


def test_order_value_by_month_uses_deal_owner_not_delivery_responsible_owner(con):
    """主担当は商談(deals.owner)を使う（Deliveryのresponsible_owner＝納品責任者とは別概念）。"""
    dv = _delivery(con, order_date="2026-09-15", fee_total=100, owner="岩崎")
    sfa_db.update_delivery(con, dv, responsible_owner="山崎")  # 納品責任者は別の人
    result = webapp.order_value_by_month(con)
    assert result["deliveries"]["2026-09"][0]["owner"] == "岩崎"


def test_order_value_by_month_includes_performance_fee_only_delivery(con):
    """2026-10-02ユーザー報告の回帰テスト:「この9/1受注案件が表示されていない」
    「固定報酬総額0円、報酬総額あり、の場合、表示されていない気がする」「報酬総額
    （成果報酬含む）で計算されていない気がする」。固定報酬総額0円＋成果報酬(想定
    インパクト348万×比率10%=34.8万)のDeliveryが、34.8万として受注高に含まれること。"""
    dv = _delivery(con, order_date="2026-09-01", fee_total=0.0, owner="岩崎")
    sfa_db.update_delivery(con, dv, performance_fee="有", performance_fee_ratio=10.0, expected_impact=348.0)
    result = webapp.order_value_by_month(con)
    assert result["order_value"] == {"2026-09": 34.8}
    assert result["deliveries"]["2026-09"][0]["amount"] == 34.8


def test_order_value_by_month_excludes_when_performance_fee_flag_not_yes(con):
    """成果報酬有無が「有」以外（無/不明(要確認)/未設定）なら、想定インパクト・比率が
    入っていても成果報酬額は加算しない（JS側の#dvFeeGrandTotalの判定と同じ）。"""
    dv = _delivery(con, order_date="2026-09-01", fee_total=0.0)
    sfa_db.update_delivery(con, dv, performance_fee="無", performance_fee_ratio=10.0, expected_impact=348.0)
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}, "deliveries": {}}


def test_delivery_fee_grand_total_adds_fixed_and_performance_fee(con):
    """固定報酬総額＋成果報酬額（想定インパクト×比率÷100、JS側#dvFeeGrandTotalと同じ式）。"""
    dv = _delivery(con, fee_total=100)
    sfa_db.update_delivery(con, dv, performance_fee="有", performance_fee_ratio=20.0, expected_impact=500.0)
    dv_dict = sfa_db.get_delivery(con, dv)
    assert webapp._delivery_fee_grand_total(dv_dict) == 200.0  # 100 + 500*20/100


# ── Delivery編集フォームの受注日保存 ──

def test_update_delivery_persists_order_date(con):
    dv = _delivery(con, order_date=None)
    sfa_db.update_delivery(con, dv, order_date="2026-09-20")
    assert sfa_db.get_delivery(con, dv)["order_date"] == "2026-09-20"


def test_update_delivery_clears_order_date_with_empty_string(con):
    dv = _delivery(con, order_date="2026-09-20")
    sfa_db.update_delivery(con, dv, order_date="")
    assert sfa_db.get_delivery(con, dv)["order_date"] is None
