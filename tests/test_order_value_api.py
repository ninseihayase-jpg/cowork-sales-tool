"""受注高（月別）API `/api/order_value` と `webapp.order_value_by_month` の回帰テスト。

ユーザー要望(2026-10-01)「受注高の集計もしたいから、個別Deliveryに受注日を入力できる
ように」「受注高タブを追加」。受注高は資金繰り（経営ロール限定）と異なり他タブ同様
誰でも見られる想定のため、ブラウザ埋め込みのSFA_API_TOKEN（SFA_CASHFLOW_TOKENではない）
で保護する。集計元は月別売上(delivery_receipts)ではなく報酬総額(fee_total)（ユーザー確認
2026-10-01: 受注日を入力した時点で金額が反映されるようにしたいため、後から埋める月別売上
の内訳入力を待たない方針）。一時DBのみ使用。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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


def _delivery(con, acc_name="A社", order_date=None, fee_total=None):
    aid = sfa_db.upsert_account(con, name=acc_name)
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="D", stage="受注")
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
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}}


def test_order_value_by_month_sums_fee_total_by_order_date_month(con):
    _delivery(con, order_date="2026-09-15", fee_total=300)
    result = webapp.order_value_by_month(con)
    assert result == {"months": ["2026-09"], "order_value": {"2026-09": 300}}


def test_order_value_by_month_combines_multiple_deliveries_in_same_month(con):
    _delivery(con, acc_name="A社", order_date="2026-09-01", fee_total=100)
    _delivery(con, acc_name="B社", order_date="2026-09-28", fee_total=50)

    result = webapp.order_value_by_month(con)
    assert result == {"months": ["2026-09"], "order_value": {"2026-09": 150}}


def test_order_value_by_month_excludes_deliveries_without_order_date(con):
    _delivery(con, order_date=None, fee_total=999)
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}}


def test_order_value_by_month_excludes_deliveries_without_fee_total(con):
    _delivery(con, order_date="2026-09-01")  # 受注日はあるが報酬総額/月額とも未入力
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}}


def test_order_value_by_month_ignores_monthly_receipts_entirely(con):
    """月別売上(delivery_receipts)はもう受注高の集計に使わない
    （ユーザー確認2026-10-01: 受注日入力時点で金額を即反映したいため報酬総額へ切替）。"""
    dv = _delivery(con, order_date="2026-09-15", fee_total=None)
    sfa_db.set_delivery_receipt(con, dv, "2026-09", 9999)
    assert webapp.order_value_by_month(con) == {"months": [], "order_value": {}}


# ── HTTPルート ──

def _run_server(db_path):
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_order_value_route_requires_sfa_api_token_not_cashflow_token(monkeypatch, tmp_path):
    """資金繰り(SFA_CASHFLOW_TOKEN)とは別の、既存SFA_API_TOKENで保護されること
    （受注高は資金繰りと違い経営ロール限定ではないため）。"""
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "api-token")
    monkeypatch.setattr(webapp, "SFA_CASHFLOW_TOKEN", "cashflow-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/order_value?token=cashflow-token")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/order_value?token=api-token", timeout=10)
        assert resp.getcode() == 200
        assert json.loads(resp.read()) == {"months": [], "order_value": {}}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_order_value_route_returns_aggregated_data(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv2.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    _delivery(con2, order_date="2026-09-10", fee_total=500)
    con2.close()

    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "api-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/order_value?token=api-token", timeout=10)
        data = json.loads(resp.read())
        assert data == {"months": ["2026-09"], "order_value": {"2026-09": 500}}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_order_value_route_disabled_when_no_token_configured(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv3.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "")
    srv, t = _run_server(db_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/order_value?token=anything")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


# ── Delivery編集フォームの受注日保存 ──

def test_update_delivery_persists_order_date(con):
    dv = _delivery(con, order_date=None)
    sfa_db.update_delivery(con, dv, order_date="2026-09-20")
    assert sfa_db.get_delivery(con, dv)["order_date"] == "2026-09-20"


def test_update_delivery_clears_order_date_with_empty_string(con):
    dv = _delivery(con, order_date="2026-09-20")
    sfa_db.update_delivery(con, dv, order_date="")
    assert sfa_db.get_delivery(con, dv)["order_date"] is None
