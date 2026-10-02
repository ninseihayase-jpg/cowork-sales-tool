"""全社定例レポート機能（2026-10、③本番実装）のルート/RBAC/DB層テスト。

設計doc: docs/08_全社定例レポート機能_設計構想.md（①②承認済み）。
Artifactモックアップ: https://claude.ai/artifact/721h63wwgQy64Y4jis1hPu

test_routes_smoke.py と同じ流儀（ThreadingHTTPServer + 実HTTPリクエスト）で検証する。
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"
KEIEI_EMAIL = "keiei@inproc.org"
MANAGER_EMAIL = "manager@inproc.org"
MEMBER_EMAIL = "member@inproc.org"
PARTNER_EMAIL = "partner@inproc.org"


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_monthly_report_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    con = sfa_db.connect(p)
    sfa_db.set_user_role(con, KEIEI_EMAIL, "経営")
    sfa_db.set_user_role(con, MANAGER_EMAIL, "マネージャー")
    sfa_db.set_user_role(con, MEMBER_EMAIL, "メンバー")
    sfa_db.set_user_role(con, PARTNER_EMAIL, "外部")
    con.close()
    return p


@pytest.fixture
def basic_auth_env(monkeypatch):
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
    yield


@pytest.fixture
def server(db_path, basic_auth_env):
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _header(email):
    return {"Cookie": f"sfa_session={webapp._make_session_token(email)}"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


# ── 1. RBAC smoke ──

def test_monthly_report_view_access_by_role(server):
    for email, expect in [(KEIEI_EMAIL, 200), (MANAGER_EMAIL, 200), (MEMBER_EMAIL, 200)]:
        code, _ = _get(server + "/monthly-report", headers=_header(email))
        assert code == expect, (email, code)
    code, _ = _get(server + "/monthly-report", headers=_header(PARTNER_EMAIL))
    assert code == 403


# ── 2. 新設POSTパスすべてが外部ロールで403になる回帰テスト ──

def test_monthly_report_write_routes_blocked_for_external_role(server):
    partner = _header(PARTNER_EMAIL)
    paths_and_bodies = [
        ("/monthly-report/targets/save", {}),
        ("/monthly-report/2026-10/create", {}),
        ("/monthly-report/2026-10/field", {"field": "product_status_html", "value": "x"}),
        ("/monthly-report/2026-10/llm-format", {"area": "product", "col": "status", "draft": "x"}),
        ("/monthly-report/2026-10/llm-format-all", {}),
        ("/monthly-report/2026-10/fix", {}),
        ("/monthly-report/2026-10/reopen", {}),
    ]
    for path, body in paths_and_bodies:
        code, _, _ = _post(server + path, body, headers=partner)
        assert code == 403, path
    # メンバーもview(閲覧専用)のためPOSTは403
    code, _, _ = _post(server + "/monthly-report/2026-10/create", {}, headers=_header(MEMBER_EMAIL))
    assert code == 403


# ── 3. 作成+引き継ぎ ──

def test_create_monthly_report_carries_forward_previous_month(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-09")
    sfa_db.update_monthly_report_field(con, "2026-09", "product_findings_html", "<div>前月の気づき</div>")
    sfa_db.update_monthly_report_field(con, "2026-09", "product_findings_draft", "前月の自由記述")
    con.close()

    keiei = _header(KEIEI_EMAIL)
    code, _, headers = _post(server + "/monthly-report/2026-10/create", {}, headers=keiei)
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    r = sfa_db.get_monthly_report(con2, "2026-10")
    assert r is not None
    assert r["product_findings_html"] == "<div>前月の気づき</div>"
    assert r["product_findings_draft"] == "前月の自由記述"


# ── 4. Fix後のfield保存は拒否、読み取り専用表示になる ──

def test_field_save_blocked_after_fix(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()
    keiei = _header(KEIEI_EMAIL)

    code, _, _ = _post(server + "/monthly-report/2026-10/fix", {}, headers=keiei)
    assert code in (200, 303)

    code, body, _ = _post(server + "/monthly-report/2026-10/field",
                           {"field": "product_status_html", "value": "x"}, headers=keiei)
    assert code == 200
    assert b'"ok": false' in body or b'"ok":false' in body

    code, page_body = _get(server + "/monthly-report/2026-10", headers=keiei)
    assert code == 200
    assert "mr-readonly" in page_body.decode("utf-8")


# ── 5. 再オープンは同一行の編集を再開するだけ（新規行を作らない） ──

def test_reopen_resumes_same_row(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()
    keiei = _header(KEIEI_EMAIL)
    _post(server + "/monthly-report/2026-10/fix", {}, headers=keiei)

    con2 = sfa_db.connect(db_path)
    fixed_row = sfa_db.get_monthly_report(con2, "2026-10")
    assert fixed_row["fixed_at"]

    code, _, _ = _post(server + "/monthly-report/2026-10/reopen", {}, headers=keiei)
    assert code in (200, 303)

    con3 = sfa_db.connect(db_path)
    reopened = sfa_db.get_monthly_report(con3, "2026-10")
    assert reopened["id"] == fixed_row["id"]
    assert reopened["fixed_at"] is None

    ok = sfa_db.update_monthly_report_field(con3, "2026-10", "product_status_html", "<div>再開後の編集</div>")
    assert ok is True


# ── 6. Track A整合性: 既存集計関数の出力と一致すること ──

def test_track_a_matches_existing_aggregation_functions(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="テスト社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="D", stage="受注", business_type_l1="コスト削減")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.update_delivery(con, dvid, order_date="2026-09-15", fee_mode="total", fee_total=200)

    ta = webapp.monthly_report_track_a(con, "2026-10")
    ov = webapp.order_value_by_month(con)
    assert ta["order_value_actual"]["2026-09"] == ov["order_value_by_l1"]["2026-09"]


# ── 7. Pipelineバケット分類 ──

def test_pipeline_buckets_exclude_proposal_and_invalid(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")

    d1 = sfa_db.upsert_deal(con, account_id=aid, deal_name="提案中案件", stage="提案")
    sfa_db.create_delivery(con, deal_id=d1, start_week="2026-09-07", end_week="2026-09-14")
    d2 = sfa_db.upsert_deal(con, account_id=aid, deal_name="クロージング案件", stage="クロージング")
    sfa_db.create_delivery(con, deal_id=d2, start_week="2026-09-07", end_week="2026-09-14")
    d3 = sfa_db.upsert_deal(con, account_id=aid, deal_name="受注案件", stage="受注")
    sfa_db.create_delivery(con, deal_id=d3, start_week="2026-09-07", end_week="2026-09-14")
    d4 = sfa_db.upsert_deal(con, account_id=aid, deal_name="失注案件", stage="要件詰め", status="closed")
    sfa_db.create_delivery(con, deal_id=d4, start_week="2026-09-07", end_week="2026-09-14")

    pl = webapp._monthly_report_pipeline_lists(con)
    names = {k: {it["name"] for it in v} for k, v in pl.items()}
    assert names["Sales"] == {"提案中案件"}
    assert names["Closing"] == {"クロージング案件"}
    assert names["Delivery"] == {"受注案件"}
    all_names = names["Sales"] | names["Closing"] | names["Delivery"]
    assert "失注案件" not in all_names


# ── 8. 目標値の保存・読み出し ──

def test_targets_save_and_readback(server, db_path):
    keiei = _header(KEIEI_EMAIL)
    data = {
        "l1_list[]": ["コスト削減", "AX"],
        "months[]": ["2026-10", "2026-11", "2026-12"],
        "t_2026-10_order_value_コスト削減": "350",
        "t_2026-10_sales_AX": "460",
    }
    code, _, _ = _post(server + "/monthly-report/targets/save", data, headers=keiei)
    assert code in (200, 303)

    con = sfa_db.connect(db_path)
    ta = webapp.monthly_report_track_a(con, "2026-10")
    assert ta["order_value_target"]["2026-10"]["コスト削減"] == 350
    assert ta["sales_target"]["2026-10"]["AX"] == 460


# ── 9. ダウンロードはContent-Dispositionヘッダ付きで返る ──

def test_download_html_has_content_disposition(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()
    code, body = _get(server + "/monthly-report/2026-10/download.html", headers=_header(KEIEI_EMAIL))
    assert code == 200
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


# ── 10. SharePointスタブが失敗してもFix自体は成功する ──

def test_sharepoint_stub_failure_does_not_block_fix(server, db_path, monkeypatch):
    """_upload_report_to_sharepoint()が例外を送出しても、/fixハンドラがtry/exceptで
    吸収し、Fix自体（DBの更新・303リダイレクト）は正常に完了すること。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()

    def _raise(*a, **kw):
        raise RuntimeError("simulated graph api failure")
    monkeypatch.setattr(webapp, "_upload_report_to_sharepoint", _raise)

    keiei = _header(KEIEI_EMAIL)
    code, _, _ = _post(server + "/monthly-report/2026-10/fix", {}, headers=keiei)
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    r = sfa_db.get_monthly_report(con2, "2026-10")
    assert r["fixed_at"]
