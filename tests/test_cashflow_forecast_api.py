"""資金繰りシミュレーション用の集計関数・APIルートのテスト（2026-09-30）。

ユーザー要望: 「dashboard側(Hisho)に、資金繰りシミュレーションを追加。経営層ロールしか
見られない仕様」。既存のSFA_API_TOKEN(ブラウザに埋め込まれ他のdashboard機能から使われる)
とは別の専用トークンSFA_CASHFLOW_TOKENで保護し、財務情報がブラウザ経由で誰でも取得できて
しまわないようにする（Hishoバックエンドからサーバー間でのみ叩かれる想定）。
"""
from __future__ import annotations

import json
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
    d = tempfile.mkdtemp(prefix="sfa_cashflow_forecast_test_")
    db = str(Path(d) / "t.db")
    sfa_db.init_db(db)
    conn = sfa_db.connect(db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def acc_id(con):
    aid = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    con.commit()
    return aid


def _deal(con, acc_id, stage, status="open"):
    return sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage=stage, status=status)


def test_cashflow_forecast_by_confidence_buckets_by_confidence_and_excludes_invalid(con, acc_id):
    """確定/見込み(クロージング)/見込み(提案中)/無効(終了)の4件を用意し、無効(終了)だけ
    集計から除外されること、他は確度別に正しく分かれて合算されることを確認する。"""
    d_kakutei = _deal(con, acc_id, "受注")  # 確定
    dv_kakutei = sfa_db.create_delivery(con, deal_id=d_kakutei, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.set_delivery_receipt(con, dv_kakutei, "2026-09", 100)
    sfa_db.update_delivery(con, dv_kakutei, payment_cycle_months=0)
    sfa_db.set_delivery_cost_payment(con, dv_kakutei, "2026-09", 30)

    d_closing = _deal(con, acc_id, "クロージング")  # 見込み(クロージング)
    dv_closing = sfa_db.create_delivery(con, deal_id=d_closing, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.set_delivery_receipt(con, dv_closing, "2026-09", 200)
    sfa_db.update_delivery(con, dv_closing, payment_cycle_months=0)

    d_proposal = _deal(con, acc_id, "提案")  # 見込み(提案中)
    dv_proposal = sfa_db.create_delivery(con, deal_id=d_proposal, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.set_delivery_receipt(con, dv_proposal, "2026-09", 400)
    sfa_db.update_delivery(con, dv_proposal, payment_cycle_months=0)

    d_invalid = _deal(con, acc_id, "要件詰め", status="closed")  # 無効(終了)
    dv_invalid = sfa_db.create_delivery(con, deal_id=d_invalid, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.set_delivery_receipt(con, dv_invalid, "2026-09", 9999)
    sfa_db.update_delivery(con, dv_invalid, payment_cycle_months=0)

    result = webapp.cashflow_forecast_by_confidence(con)
    assert result["months"] == ["2026-09"]
    by_conf = result["by_confidence"]
    assert "無効(終了)" not in by_conf
    assert by_conf["確定"]["2026-09"] == {"inflow": 100, "cost": 30}
    assert by_conf["見込み(クロージング)"]["2026-09"] == {"inflow": 200, "cost": 0}
    assert by_conf["見込み(提案中)"]["2026-09"] == {"inflow": 400, "cost": 0}


def _run_server(db_path):
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_cashflow_forecast_route_requires_dedicated_token_not_sfa_api_token(monkeypatch, tmp_path):
    """SFA_CASHFLOW_TOKENはSFA_API_TOKENとは別の専用トークンであること（既存のSFA_API_TOKEN
    はブラウザのdashboard.htmlに埋め込まれているため、それを使い回すと財務情報が誰でも
    取得できてしまう）。"""
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "browser-embedded-token")
    monkeypatch.setattr(webapp, "SFA_CASHFLOW_TOKEN", "cashflow-secret-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        # 既存のブラウザ埋め込みトークンでは通らないこと
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/cashflow_forecast?token=browser-embedded-token")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

        # 専用トークンなら通ること
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/cashflow_forecast?token=cashflow-secret-token", timeout=10)
        assert resp.getcode() == 200
        data = json.loads(resp.read())
        assert data == {"months": [], "by_confidence": {}}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_cashflow_forecast_route_disabled_when_no_token_configured(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv2.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_CASHFLOW_TOKEN", "")
    srv, t = _run_server(db_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/cashflow_forecast?token=anything")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
