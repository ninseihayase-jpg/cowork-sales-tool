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
    sfa_db.update_delivery(con, dv_kakutei, payment_cycle_months=0, cost_payment_cycle_months=0)
    sfa_db.set_delivery_cost_receipt(con, dv_kakutei, "2026-09", 30)

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

    # 2026-09-30ユーザー要望: 月別入金のホバーで案件別内訳(案件名・入金額・外注費)を出せるように
    deliveries = result["deliveries"]["2026-09"]
    assert len(deliveries) == 3  # 無効(終了)は除外される
    kakutei_row = next(d for d in deliveries if d["confidence"] == "確定")
    assert kakutei_row == {"id": dv_kakutei, "name": "テスト社 / D", "confidence": "確定",
                            "inflow": 100, "cost": 30}
    assert {d["confidence"] for d in deliveries} == {"確定", "見込み(クロージング)", "見込み(提案中)"}


def test_cashflow_forecast_by_confidence_accrual_basis_uses_unshifted_receipts(con, acc_id):
    """2026-09-30ユーザー要望「計上ベース/実収支ベースをタブで切替」: 支払いサイトで月がずれる
    実収支ベース(by_confidence/deliveries)に対し、計上ベース(by_confidence_accrual/
    deliveries_accrual)は検収・売上計上月のまま（ずらさない）ことを確認する。"""
    d = _deal(con, acc_id, "受注")
    dv = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.update_delivery(con, dv, payment_cycle_months=2, cost_payment_cycle_months=1)
    sfa_db.set_delivery_receipt(con, dv, "2026-09", 100)
    sfa_db.set_delivery_cost_receipt(con, dv, "2026-09", 30)

    result = webapp.cashflow_forecast_by_confidence(con)

    # 実収支ベース: 売上は+2ヶ月(2026-11)へ、外注費検収は+1ヶ月(2026-10)へずれる
    assert result["by_confidence"]["確定"]["2026-11"] == {"inflow": 100, "cost": 0}
    assert result["by_confidence"]["確定"]["2026-10"] == {"inflow": 0, "cost": 30}
    assert "2026-09" not in result["by_confidence"].get("確定", {})

    # 計上ベース: 計上月(2026-09)のまま、ずらさない
    assert result["by_confidence_accrual"]["確定"]["2026-09"] == {"inflow": 100, "cost": 30}
    assert "2026-10" not in result["by_confidence_accrual"]["確定"]
    assert "2026-11" not in result["by_confidence_accrual"]["確定"]

    accrual_rows = result["deliveries_accrual"]["2026-09"]
    assert accrual_rows == [{"id": dv, "name": "テスト社 / D", "confidence": "確定",
                              "inflow": 100, "cost": 30}]


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
        assert data == {"months": [], "by_confidence": {}, "deliveries": {},
                         "by_confidence_accrual": {}, "deliveries_accrual": {},
                         "order_value": {"months": [], "order_value": {}, "deliveries": {}}}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_cashflow_forecast_route_includes_order_value(monkeypatch, tmp_path):
    """2026-10-01ユーザー要望: 「受注高」ビューを収支状況タブの計算基準に統合（経営ロール
    限定に）。独立だった/api/order_value(SFA_API_TOKEN)は廃止し、この経営ロール限定の
    /api/cashflow_forecastへ"order_value"キーとして合流させた。"""
    db_path = str(tmp_path / "srv4.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = con2.execute("INSERT INTO accounts(name) VALUES('A社')").lastrowid
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.update_delivery(con2, dvid, order_date="2026-09-10", fee_mode="total", fee_total=500)
    con2.close()

    monkeypatch.setattr(webapp, "SFA_CASHFLOW_TOKEN", "cashflow-secret-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/cashflow_forecast?token=cashflow-secret-token", timeout=10)
        data = json.loads(resp.read())
        assert data["order_value"]["months"] == ["2026-09"]
        assert data["order_value"]["order_value"] == {"2026-09": 500}
        assert data["order_value"]["deliveries"]["2026-09"][0]["amount"] == 500

        # 旧専用ルートは廃止済み（存在しないこと）
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/order_value?token=cashflow-secret-token")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 404
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


def test_keiei_emails_route_returns_only_keiei_role_sorted(monkeypatch, tmp_path):
    """/api/keiei_emails: ユーザー要望(2026-09-30)「経営ロールって、この割り当て（SFA-CRM側の
    権限管理）を採用できないの？」。Hisho側で別管理せず、SFA-CRM側のuser_rolesを単一の正と
    する。role='経営'の行だけをメール昇順で返し、他ロール(マネージャー等)は含めないこと。
    init_db()のロックアウト防止シード(ninsei.hayase@inproc.org=経営)も含まれることを踏まえる。"""
    db_path = str(tmp_path / "srv3.db")
    sfa_db.init_db(db_path)
    con = sfa_db.connect(db_path)
    sfa_db.set_user_role(con, "yasutaka.nakajima@inproc.org", "経営", display_name="中島")
    sfa_db.set_user_role(con, "eijiro.iwasaki@inproc.org", "マネージャー", display_name="岩崎")
    con.close()
    monkeypatch.setattr(webapp, "SFA_CASHFLOW_TOKEN", "cashflow-secret-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/keiei_emails")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401

        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/keiei_emails?token=cashflow-secret-token", timeout=10)
        data = json.loads(resp.read())
        assert data == {"emails": ["ninsei.hayase@inproc.org", "yasutaka.nakajima@inproc.org"]}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
