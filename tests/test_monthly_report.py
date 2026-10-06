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


def test_pipeline_excludes_completed_delivery(db_path):
    """2026-10-06ユーザー要望「Delivery案件は、完了した案件は載せない」の回帰テスト。"""
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    d1 = sfa_db.upsert_deal(con, account_id=aid, deal_name="進行中案件", stage="受注")
    dv1 = sfa_db.create_delivery(con, deal_id=d1, start_week="2026-09-07", end_week="2026-09-14")
    d2 = sfa_db.upsert_deal(con, account_id=aid, deal_name="完了済み案件", stage="受注")
    dv2 = sfa_db.create_delivery(con, deal_id=d2, start_week="2026-09-01", end_week="2026-09-07")
    sfa_db.update_delivery(con, dv2, status="完了")

    pl = webapp._monthly_report_pipeline_lists(con)
    names = {it["name"] for it in pl["Delivery"]}
    assert "進行中案件" in names
    assert "完了済み案件" not in names


def test_pipeline_sorted_by_start_date_ascending(db_path):
    """2026-10-06ユーザー要望「案件はすべて、開始日が古いものから表示」の回帰テスト。
    開始日未設定の案件は末尾へ回す。"""
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    d_new = sfa_db.upsert_deal(con, account_id=aid, deal_name="新しい開始", stage="受注")
    sfa_db.create_delivery(con, deal_id=d_new, start_week="2026-11-01", end_week="2026-12-01")
    d_old = sfa_db.upsert_deal(con, account_id=aid, deal_name="古い開始", stage="受注")
    sfa_db.create_delivery(con, deal_id=d_old, start_week="2026-08-01", end_week="2026-09-01")
    d_mid = sfa_db.upsert_deal(con, account_id=aid, deal_name="中間開始", stage="受注")
    sfa_db.create_delivery(con, deal_id=d_mid, start_week="2026-09-15", end_week="2026-10-01")
    d_nodate = sfa_db.upsert_deal(con, account_id=aid, deal_name="日付未設定", stage="受注")
    sfa_db.create_delivery(con, deal_id=d_nodate, start_week="", end_week="")

    pl = webapp._monthly_report_pipeline_lists(con)
    assert [it["name"] for it in pl["Delivery"]] == ["古い開始", "中間開始", "新しい開始", "日付未設定"]


def test_pipeline_schedule_appears_before_account_name_in_html(server, db_path):
    """2026-10-06ユーザー要望「日付はアカウントの前に表示」の回帰テスト
    （当初「スケジュールは案件の左に配置」でアカウント名の直後に置いたが、さらに
    アカウント名より前（行の一番左）へ変更した）。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    aid = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="X案件", stage="受注")
    sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-09-14")
    con.close()

    code, body = _get(server + "/monthly-report/2026-10", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    date_pos = html.index('class="mr-plist-date"')
    acc_pos = html.index('class="acc"')
    deal_pos = html.index('class="deal"')
    assert date_pos < acc_pos < deal_pos


def test_chart_target_bar_is_light_blue_not_dashed(server, db_path):
    """2026-10-06ユーザー要望「目標は、点線ではなく、薄い水色とかで」+「上部の数値の一番左に
    『目標』『実績』と項目名を記載して」の回帰テスト。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    aid = sfa_db.upsert_account(con, name="テスト社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="D", stage="受注", business_type_l1="コスト削減")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.update_delivery(con, dvid, order_date="2026-09-15", fee_mode="total", fee_total=200)
    sfa_db.upsert_monthly_targets(
        con, [{"year": 2026, "month": 9, "metric": "order_value", "business_type_l1": "コスト削減",
               "target_value": 100}],
        updated_by=KEIEI_EMAIL)
    con.close()

    code, body = _get(server + "/monthly-report/2026-10", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert webapp._MONTHLY_REPORT_TARGET_COLOR in html
    # 旧実装の目標バー(点線枠)の特徴的なスタイル文字列が残っていないこと
    # （「dashed」という単語自体は他機能の無関係なCSSにも登場するため、この固有パターンで判定する）。
    assert "border:1.5px dashed" not in html
    assert ">実績<" in html and ">目標<" in html


def test_chart_tooltip_supports_click_to_pin(server, db_path):
    """2026-10-06ユーザー要望「案件表示は、クリックするとこのフローティングを固定できる仕様」
    の回帰テスト。クリック用のmrPinTooltip呼び出しと固定解除関数がページに存在すること、
    案件行がnowrapで折り返し不可（はみ出しの原因）になっていないことを確認する。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    aid = sfa_db.upsert_account(con, name="テスト社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="D", stage="受注", business_type_l1="コスト削減")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-09-14")
    sfa_db.update_delivery(con, dvid, order_date="2026-09-15", fee_mode="total", fee_total=200)
    con.close()

    code, body = _get(server + "/monthly-report/2026-10", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert "onclick=\"mrPinTooltip(event," in html
    assert "function mrUnpinBox" in html
    assert "white-space:nowrap" not in html.split("function _mrDeliveryBoxHtml", 1)[1].split("function _mrClampPos", 1)[0]


def test_chart_tooltip_supports_multiple_draggable_pins(server, db_path):
    """2026-10-06ユーザー要望「ドラッグして動かせるように」「複数固定表示できるように」の
    回帰テスト。固定ボックスはキー別に管理され(複数同時固定可能)、見出しにドラッグ用の
    cursor:moveが付いていることを確認する。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()

    code, body = _get(server + "/monthly-report/2026-10", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert "var mrPinnedBoxes" in html
    assert "function _mrMakeDraggable" in html
    assert "mr-tip-head" in html and "cursor:move" in html
    # 同じセグメントの再クリックだけトグルし、他の固定ボックスには影響しない設計
    # （mrPinnedBoxes[key]で個別管理、mrUnpinToolTip的な単一グローバル状態は使わない）。
    assert "mrPinned = false" not in html
    assert "mrPinned &&" not in html


def test_chart_deal_display_toggle_feature_removed(server, db_path):
    """2026-10-06ユーザー要望「『案件を表示』機能は削除」の回帰テスト。
    クリック固定(ピン留め)機能と役割が重複するため撤去した。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()

    code, body = _get(server + "/monthly-report/2026-10", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert "案件を表示" not in html
    assert "mrToggleDealList" not in html
    assert "mr-deal-list" not in html


def test_chart_panel_title_is_more_prominent_and_order_value_shows_months(server, db_path):
    """2026-10-06ユーザー要望「『受注高』『売上』の表示をもう1~2割目立つように」
    「受注高グラフにも月を表示」の回帰テスト。"""
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, "2026-10")
    con.close()

    code, body = _get(server + "/monthly-report/2026-10", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    # タイトル「受注高」「売上」は14px/800（旧12px/700から約17%拡大）
    assert 'font-size:14px;font-weight:800;color:#2B2723">受注高<' in html
    assert 'font-size:14px;font-weight:800;color:#2B2723">売上<' in html
    # 月ラベルが2回（受注高・売上の両方）出現する（従来は売上のみの1回だった）
    assert html.count(">7月<") == 2


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
