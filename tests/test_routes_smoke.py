"""cowork/webapp.py の主要ルートに対するスモークテスト。

_make_handler(db_path, theme_client) で得たハンドラを一時ポート(OS自動割当)で
ThreadingHTTPServerに載せ、実際にHTTPリクエストを送って200/303/404/401/503が
返ること・例外や500にならないことを確認する。DBは一時ファイル、theme_client=None。
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db
from cowork import webapp


BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_routes_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "routes_test.db")
    sfa_db.init_db(p)
    return p


@pytest.fixture
def basic_auth_env(monkeypatch):
    """SFA_BASIC_USER/PASSをwebappモジュールの変数として直接設定する。

    webapp.SFA_BASIC_USER/SFA_BASIC_PASS はモジュールインポート時にos.environから
    読み込まれるグローバル変数だが、_check_basic_auth() は呼び出しのたびに
    モジュールの現在のグローバル値を参照するため、モジュール属性を直接書き換えれば
    再インポート不要でテストに反映できる。
    """
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", BASIC_USER)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", BASIC_PASS)
    yield


@pytest.fixture
def server(db_path, basic_auth_env):
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _auth_header():
    token = base64.b64encode(f"{BASIC_USER}:{BASIC_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp
    except urllib.error.HTTPError as e:
        return e.code, e


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """リダイレクトを追わず、302/303応答自体をそのまま返す（Locationヘッダ検証用）。"""
    def http_error_302(self, req, fp, code, msg, hdrs):
        return fp
    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302


def test_data_tagging_route_200(server):
    code, resp = _get(server + "/data-tagging", headers=_auth_header())
    assert code == 200
    assert len(resp.read()) > 0


def test_close_requires_reason(server, db_path):
    """クローズ(=リード戻し)は終了理由が必須。理由なしはクローズされず、理由ありでクローズ＋記録。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('C社')").lastrowid
    con.commit()
    deal = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案", status="open")
    con.commit()

    # 理由なし → クローズされない
    _post(server + f"/deal/{deal}/revert_to_lead", {"memo": "x"}, headers=_auth_header())
    con2 = sfa_db.connect(db_path)
    assert con2.execute("SELECT status FROM deals WHERE id=?", (deal,)).fetchone()[0] != "closed"

    # 理由あり → クローズ＋close_reason記録
    _post(server + f"/deal/{deal}/revert_to_lead",
          {"close_reason": "ニーズなし", "memo": "詳細メモ"}, headers=_auth_header())
    row = con2.execute("SELECT status, close_reason FROM deals WHERE id=?", (deal,)).fetchone()
    assert row[0] == "closed" and row[1] == "ニーズなし"
    con2.close()
    con.close()


def test_close_to_lead_stops_linked_delivery(server, db_path):
    """商談をリードに戻す(=クローズ)と、紐づく進行中のDeliveryも連動して止まる。
    終了理由=保留・時期尚早は「保留」、それ以外(失注等)は「中止」。既に完了のDeliveryは上書きしない。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('D社')").lastrowid
    con.commit()

    deal_lost = sfa_db.upsert_deal(con, account_id=acc, deal_name="D1", stage="提案", status="open")
    dv_lost = sfa_db.create_delivery(con, deal_id=deal_lost, title="Delivery1")
    deal_hold = sfa_db.upsert_deal(con, account_id=acc, deal_name="D2", stage="提案", status="open")
    dv_hold = sfa_db.create_delivery(con, deal_id=deal_hold, title="Delivery2")
    deal_done = sfa_db.upsert_deal(con, account_id=acc, deal_name="D3", stage="提案", status="open")
    dv_done = sfa_db.create_delivery(con, deal_id=deal_done, title="Delivery3", status="完了")
    con.commit()

    _post(server + f"/deal/{deal_lost}/revert_to_lead",
          {"close_reason": "失注", "memo": "x"}, headers=_auth_header())
    _post(server + f"/deal/{deal_hold}/revert_to_lead",
          {"close_reason": "保留・時期尚早", "memo": "x"}, headers=_auth_header())
    _post(server + f"/deal/{deal_done}/revert_to_lead",
          {"close_reason": "失注", "memo": "x"}, headers=_auth_header())

    con2 = sfa_db.connect(db_path)
    assert sfa_db.get_delivery(con2, dv_lost)["status"] == "中止"
    assert sfa_db.get_delivery(con2, dv_hold)["status"] == "保留"
    assert sfa_db.get_delivery(con2, dv_done)["status"] == "完了"   # 既に完了は上書きしない
    con2.close()
    con.close()


def test_stage_direct_to_lost_matches_revert_to_lead(server, db_path):
    """ステージを直接「失注」に変更する3経路(フォーム保存/一括編集/インライン変更)が、
    いずれも「リードに戻す」(revert_to_lead)と同じ処理(status=closed・リード化・
    紐づくDeliveryの中止)をトリガーすることを確認する（従来はここが素通りしていた不具合）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('L社')").lastrowid
    con.commit()
    # 本番のdeal_stagesマスタには#67以前からの legacy値として「失注」が残っている
    # （DEAL_STAGES定数からは撤廃済みだが、DB保存済みマスタは移行されていない）。
    # 一括編集/インライン変更はこのマスタで値を検証するため、フレッシュなテストDBでも再現する。
    sfa_db.set_master_list(con, "deal_stages", list(sfa_db.DEAL_STAGES) + ["失注"])

    # ① フォーム保存 (/deal/save)
    deal1 = sfa_db.upsert_deal(con, account_id=acc, deal_name="D1", stage="提案", status="open")
    dv1 = sfa_db.create_delivery(con, deal_id=deal1, title="Delivery1")
    con.commit()
    code, _ = _post(server + "/deal/save",
                    {"id": str(deal1), "account_id": str(acc), "deal_name": "D1", "stage": "失注"},
                    headers=_auth_header())
    assert code in (200, 303)

    # ② 一括編集 (/deals/bulk_edit)
    deal2 = sfa_db.upsert_deal(con, account_id=acc, deal_name="D2", stage="提案", status="open")
    dv2 = sfa_db.create_delivery(con, deal_id=deal2, title="Delivery2")
    con.commit()
    _post(server + "/deals/bulk_edit",
          [("ids", str(deal2)), ("field", "stage"), ("value", "失注")], headers=_auth_header())

    # ③ インライン変更 (/deal/{id}/field)
    deal3 = sfa_db.upsert_deal(con, account_id=acc, deal_name="D3", stage="提案", status="open")
    dv3 = sfa_db.create_delivery(con, deal_id=deal3, title="Delivery3")
    con.commit()
    _post(server + f"/deal/{deal3}/field", {"field": "stage", "value": "失注"}, headers=_auth_header())

    con2 = sfa_db.connect(db_path)
    for deal_id, dv_id in ((deal1, dv1), (deal2, dv2), (deal3, dv3)):
        row = con2.execute("SELECT status, close_reason FROM deals WHERE id=?", (deal_id,)).fetchone()
        assert row["status"] == "closed", f"deal {deal_id} not closed"
        assert row["close_reason"] == "失注", f"deal {deal_id} close_reason wrong: {row['close_reason']}"
        lead = con2.execute("SELECT id FROM leads WHERE company='L社' AND deal_id IS NULL").fetchone()
        assert lead is not None, f"deal {deal_id} was not reverted to a lead"
        assert sfa_db.get_delivery(con2, dv_id)["status"] == "中止", f"delivery for deal {deal_id} not stopped"
    con2.close()
    con.close()


def test_dev_project_tool_add_via_http(server, db_path):
    """開発案件一覧のモーダルからの追加リンク登録（/tools/add）が実HTTPで保存される。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('T社')").lastrowid
    con.commit()
    deal = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案", status="open")
    dp = sfa_db.upsert_dev_project(con, deal_id=deal, theme="T", status="開発中", stage="プロト")
    con.commit()

    code, _ = _post(server + f"/dev-project/{dp}/tools/add",
                    {"url": "https://tool.example", "label": "設計", "return_to": "/dev-projects"},
                    headers=_auth_header())
    assert code != 500
    con2 = sfa_db.connect(db_path)
    tools = sfa_db.list_dev_project_tools(con2, dp)
    assert len(tools) == 1 and tools[0]["url"] == "https://tool.example" and tools[0]["label"] == "設計"
    # http以外は登録しない
    _post(server + f"/dev-project/{dp}/tools/add", {"url": "javascript:alert(1)"}, headers=_auth_header())
    assert len(sfa_db.list_dev_project_tools(con2, dp)) == 1
    con2.close()
    con.close()


def test_dev_project_inline_field_persist(server, db_path):
    """開発案件一覧のインライン編集(stage/status/order_potential)が実HTTPで保存され、不正値は拒否される。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('DP社')").lastrowid
    con.commit()
    deal = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案", status="open")
    dp = sfa_db.upsert_dev_project(con, deal_id=deal, theme="T", status="開発中", stage="プロト")
    con.commit()

    assert _post(server + f"/dev-project/{dp}/field", {"field": "stage", "value": "PoC"},
                 headers=_auth_header())[0] == 200
    assert _post(server + f"/dev-project/{dp}/field", {"field": "status", "value": "完成"},
                 headers=_auth_header())[0] == 200
    assert _post(server + f"/dev-project/{dp}/field", {"field": "order_potential", "value": "高"},
                 headers=_auth_header())[0] == 200
    # 不正値は拒否（値が変わらない）
    _post(server + f"/dev-project/{dp}/field", {"field": "stage", "value": "でたらめ"}, headers=_auth_header())

    con2 = sfa_db.connect(db_path)
    row = sfa_db.get_dev_project(con2, dp)
    assert row["stage"] == "PoC" and row["status"] == "完成" and row["order_potential"] == "高"
    con2.close()
    con.close()


def test_inline_close_reason_and_ms_type_persist(server, db_path):
    """データ整備タグ付けが依存するインライン更新(close_reason/next_milestone_type/lost_reason)の
    保存と、不正値の拒否を実HTTPで検証する。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('検証社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="失注X", stage="失注", status="closed")
    did2 = sfa_db.upsert_deal(con, account_id=acc, deal_name="MSX", stage="提案",
                              next_milestone_date="2026-07-20", status="open")
    con.execute("INSERT INTO leads(name,company,lead_status) VALUES('リードX','Z社','lost')")
    lid = con.execute("SELECT id FROM leads WHERE name='リードX'").fetchone()["id"]
    con.commit()

    assert _post(server + f"/deal/{did}/field", {"field": "close_reason", "value": "ニーズなし"},
                 headers=_auth_header())[0] == 200
    assert _post(server + f"/deal/{did2}/field", {"field": "next_milestone_type", "value": "タスク"},
                 headers=_auth_header())[0] == 200
    assert _post(server + f"/leads/{lid}/field", {"field": "lost_reason", "value": "キャンセル"},
                 headers=_auth_header())[0] == 200
    # 不正値は拒否（サーバは200で{ok:false}を返す運用なので、値が変わっていないことで確認）
    _post(server + f"/deal/{did}/field", {"field": "close_reason", "value": "でたらめ"},
          headers=_auth_header())

    con2 = sfa_db.connect(db_path)
    assert con2.execute("SELECT close_reason FROM deals WHERE id=?", (did,)).fetchone()[0] == "ニーズなし"
    assert con2.execute("SELECT next_milestone_type FROM deals WHERE id=?", (did2,)).fetchone()[0] == "タスク"
    assert con2.execute("SELECT lost_reason FROM leads WHERE id=?", (lid,)).fetchone()[0] == "キャンセル"
    con2.close()
    con.close()


@pytest.mark.parametrize("path", ["/", "/deals", "/dev-projects", "/deal-issues", "/accounts", "/leads",
                                  "/deal-hygiene", "/weekly-numbers/audit", "/tasks", "/desk-tasks"])
def test_get_main_routes_return_200(server, path):
    code, resp = _get(server + path, headers=_auth_header())
    assert code == 200, f"{path} returned {code}"
    body = resp.read()
    assert len(body) > 0


_REP_MARKER = "REPORT_BODY_MARKER_X1Y2Z3"


def _seed_report(db_path, slug="2026-07-12", body=None, cover=""):
    con = sfa_db.connect(db_path)
    sfa_db.upsert_weekly_report(
        con, slug, "2026.7.6 – 7.12", "展示会が終わって、最初の一週間",
        "「そんなことができるんですか」。",
        body if body is not None else f"<p>{_REP_MARKER}</p>",
        cover,
    )
    con.close()


def test_reports_index_200(server, db_path):
    _seed_report(db_path)
    code, resp = _get(server + "/reports", headers=_auth_header())
    assert code == 200
    body = resp.read()
    # 読み物サイトのガワ（CRMのガワではない）で配信されていること
    assert "InProc 営業レポート".encode() in body
    assert "Inproc Salesforce".encode() not in body  # CRMの共通ナビは出さない


def test_reports_article_wraps_fragment_in_magazine(server, db_path):
    """本文fragmentは記事の2カラム・マガジン設計(アプリ側CSS＋メタ由来の見出し)に包んで配信。"""
    _seed_report(db_path)
    code, resp = _get(server + "/reports/2026-07-12", headers=_auth_header())
    assert code == 200
    body = resp.read()
    assert _REP_MARKER.encode() in body
    assert 'class="mast"'.encode() in body             # アプリが付ける号見出し
    assert "展示会が終わって、最初の一週間".encode() in body  # メタ(title)由来の見出し
    assert b"openToolModal" not in body                # CRMの共通モーダルは無い
    assert "Inproc Salesforce".encode() not in body    # CRMの共通ナビは無い


def test_reports_legacy_fulldoc_served_raw(server, db_path):
    """旧形式（<!doctype>始まりの単体HTML本文）は後方互換でそのまま配信する。"""
    doc = f"<!doctype html><html><body>{_REP_MARKER}</body></html>"
    _seed_report(db_path, slug="2025-01-01", body=doc)
    code, resp = _get(server + "/reports/2025-01-01", headers=_auth_header())
    assert code == 200
    body = resp.read()
    assert body == doc.encode()  # ガワを足さず生のまま返す


def test_reports_unknown_slug_redirects_to_index(server, db_path):
    """未知/不正なslugは本文を返さず、一覧へリダイレクト（トラバーサル防止）。"""
    _seed_report(db_path)
    for bad in ["/reports/nonexistent", "/reports/..%2f..%2fetc"]:
        code, resp = _get(server + bad, headers=_auth_header())
        assert code == 200, f"{bad} -> {code}"
        assert _REP_MARKER.encode() not in resp.read(), bad


def test_reports_manage_save_and_delete(server, db_path):
    """管理画面から新規保存(カバー画像込み)→本文が返る→削除で消える。"""
    code, _ = _get(server + "/reports/manage", headers=_auth_header())
    assert code == 200
    cover = "data:image/jpeg;base64,/9j/AAAQSkZJRg=="
    code, _ = _post(server + "/reports/manage/save",
                    {"slug": "2099-01-01", "report_date": "2099.1.1", "title": "テスト号",
                     "lead": "リード", "html_body": f"<p>{_REP_MARKER}</p>", "cover_image": cover},
                    headers=_auth_header())
    assert code == 200  # 303→追従で号本文(200)
    con = sfa_db.connect(db_path)
    rep = sfa_db.get_weekly_report(con, "2099-01-01")
    assert rep is not None and rep["cover_image"] == cover
    # data:image/ 以外のcover_imageは弾いて空に
    _post(server + "/reports/manage/save",
          {"slug": "2099-01-01", "html_body": f"<p>{_REP_MARKER}</p>",
           "cover_image": "javascript:alert(1)"}, headers=_auth_header())
    assert sfa_db.get_weekly_report(con, "2099-01-01")["cover_image"] == ""
    # 不正slugは保存されない
    _post(server + "/reports/manage/save",
          {"slug": "../etc", "html_body": "x"}, headers=_auth_header())
    assert sfa_db.get_weekly_report(con, "../etc") is None
    # 削除
    _post(server + "/reports/manage/delete", {"slug": "2099-01-01"}, headers=_auth_header())
    assert sfa_db.get_weekly_report(con, "2099-01-01") is None
    con.close()


def test_dev_points_auto_recalc_and_master(server, db_path):
    """開発点数: 作業種別×分類係数×難易度係数で自動付与→変更で再計算→手動上書き、マスタ/キャパ保存。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('点数社')").lastrowid
    con.commit()
    deal = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案", status="open")
    # 自動付与: 本番向けツール(base8)×本番(1.6)×難(1.3)=16.6
    dp = sfa_db.upsert_dev_project(con, deal_id=deal, theme="T", status="開発中",
                                   stage="本番", dev_audience="社外向け",
                                   work_type="本番向けツール", difficulty="難")
    con.commit()
    con2 = sfa_db.connect(db_path)
    val = lambda: con2.execute("SELECT dev_points FROM dev_projects WHERE id=?", (dp,)).fetchone()[0]
    assert val() == 16.6
    assert _get(server + "/dev-point-master", headers=_auth_header())[0] == 200
    # インライン作業種別変更(新規フロントエンド base3) → (3+0)×本番1.6×難1.3=6.2 に再計算
    assert _post(server + f"/dev-project/{dp}/field", {"field": "work_type", "value": "新規フロントエンド"},
                 headers=_auth_header())[0] == 200
    assert val() == 6.2
    # バックエンド有り → 加点+2: (3+2)×1.6×1.3=10.4
    assert _post(server + f"/dev-project/{dp}/field", {"field": "has_backend", "value": "有り"},
                 headers=_auth_header())[0] == 200
    assert val() == 10.4
    # 点数の手動上書き
    _post(server + f"/dev-project/{dp}/field", {"field": "dev_points", "value": "7.5"},
          headers=_auth_header())
    assert val() == 7.5
    # 点数マスタ保存(既存をid基準で更新＋新規追加)・担当キャパ保存
    _mid = [m["id"] for m in sfa_db.list_dev_point_master(con2) if m["work_type"] == "本番向けツール"][0]
    _post(server + "/dev-point-master/save",
          {f"wt__{_mid}": "本番向けツール", f"bp__{_mid}": "10",
           "new_work_type": "図面OCR研究", "new_base_points": "5"},
          headers=_auth_header())
    assert sfa_db.get_dev_point_base(con2, "本番向けツール") == 10.0
    assert sfa_db.get_dev_point_base(con2, "図面OCR研究") == 5.0
    _post(server + "/dev-point-master/capacity",
          {"cap__早瀬__base": "20", "cap__早瀬__from": "2026-08-01", "cap__早瀬__p2": "30"},
          headers=_auth_header())
    _cap = sfa_db.get_owner_capacities(con2).get("早瀬")
    assert _cap["base"] == 20.0 and _cap["from"] == "2026-08-01" and _cap["base2"] == 30.0
    # 週で上限が切り替わる
    assert sfa_db.owner_cap_for_week(_cap, "2026-07-14") == 20.0
    assert sfa_db.owner_cap_for_week(_cap, "2026-08-04") == 30.0
    # 係数の編集
    _post(server + "/dev-point-master/coef",
          {"cf__stage__本番": "2.0", "cf__difficulty__難": "1.5"}, headers=_auth_header())
    _c = sfa_db.get_dev_coefs(con2)
    assert _c["stage"]["本番"] == 2.0 and _c["difficulty"]["難"] == 1.5
    con2.close()
    con.close()


def test_health_ok_without_auth(server):
    code, resp = _get(server + "/health")
    assert code == 200
    assert resp.read() == b'{"status":"ok"}'


def test_get_root_without_auth_redirects_to_login(server):
    # 未認証GETは /login へ302誘導（ネイティブBasicダイアログは出さない, #54）。
    # urllibはリダイレクトを追うため最終的にログイン画面(200)に着地する。
    code, resp = _get(server + "/")
    assert code == 200
    assert resp.geturl().rstrip("/").endswith("/login") or "/login?" in resp.geturl()
    body = resp.read().decode("utf-8")
    assert 'name="password"' in body and "ログイン" in body


def test_basic_auth_still_works(server):
    # 従来のBasic認証は併存（PC等の既存運用を壊さない）。
    code, resp = _get(server + "/", headers=_auth_header())
    assert code == 200


def test_form_login_sets_cookie_and_grants_access(server):
    import http.cookies
    # 誤資格情報→401（ログイン画面再表示）
    code, _ = _post(server + "/login", {"username": BASIC_USER, "password": "wrong", "next": "/"})
    assert code == 401
    # 正しい資格情報→303 + Set-Cookie（urllibは303を追うのでHTTPErrorにならずcookieを拾えないため手動）
    body = urllib.parse.urlencode({"username": BASIC_USER, "password": BASIC_PASS, "next": "/"}).encode()
    req = urllib.request.Request(server + "/login", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(req, timeout=10)
        setcookie = None
    except urllib.error.HTTPError as e:
        assert e.code == 303
        setcookie = e.headers.get("Set-Cookie")
    assert setcookie and "sfa_session=" in setcookie
    ck = http.cookies.SimpleCookie(setcookie)
    tok = ck["sfa_session"].value
    # 取得したセッションCookieで保護ページにアクセスできる
    code, _ = _get(server + "/", headers={"Cookie": f"sfa_session={tok}"})
    assert code == 200


def test_basic_auth_fails_closed_with_503_when_unset(db_path, monkeypatch):
    """SFA_BASIC_USER/PASSが未設定(空文字)ならfail-closedで503になること。"""
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", "")
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", "")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, _ = _get(f"http://127.0.0.1:{port}/")
        assert code == 503
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_get_deal_invalid_id_returns_404_not_crash(server):
    code, _ = _get(server + "/deal/abc", headers=_auth_header())
    assert code == 404


def test_get_deal_missing_id_returns_404(server):
    code, _ = _get(server + "/deal/999999", headers=_auth_header())
    assert code == 404


def test_post_deal_save_minimal_form_redirects_and_persists(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        before = len(sfa_db.list_deals(con, status=None))
    finally:
        con.close()

    form = "deal_name=%E3%82%B9%E3%83%A2%E3%83%BC%E3%82%AF%E7%A2%BA%E8%AA%8D%E5%95%86%E8%AB%87"  # deal_name=スモーク確認商談
    req = urllib.request.Request(
        server + "/deal/save", data=form.encode("utf-8"), method="POST",
        headers={
            **_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        code = resp.getcode()
        location = resp.geturl()
    except urllib.error.HTTPError as e:
        code = e.code
        location = e.headers.get("Location")

    # 保存後は商談一覧(/deals)へリダイレクト（連打・多重保存の防止）。303を自動追従して200。
    assert code == 200
    assert location.endswith("/deals")

    con2 = sfa_db.connect(db_path)
    try:
        after = len(sfa_db.list_deals(con2, status=None))
    finally:
        con2.close()
    assert after == before + 1


def test_post_deal_save_returns_303_without_redirect_following(server):
    class NoRedirect(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response
        https_response = http_response

    opener = urllib.request.build_opener(NoRedirect)
    form = "deal_name=Redirect+Check"
    req = urllib.request.Request(
        server + "/deal/save", data=form.encode("utf-8"), method="POST",
        headers={
            **_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = opener.open(req, timeout=10)
    assert resp.getcode() == 303
    assert resp.headers.get("Location", "") == "/deals"


def test_unified_deal_table_has_dev_project_link(db_path):
    """商談一覧の共通テーブル(unified_deal_table)に開発案件への導線が出ること。
    開発案件が無い商談は「＋開発案件」新規追加リンク(deal_id/return_to付き)、
    有る商談は当該開発案件へのリンク＋（複数付くため）追加リンクの両方を出す。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('UT社')").lastrowid
    con.commit()
    # 開発案件なしの商談
    d_none = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件なし", stage="提案", status="open")
    # 開発案件ありの商談
    d_has = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件あり", stage="提案", status="open")
    sfa_db.upsert_dev_project(con, deal_id=d_has, theme="UTテーマ", status="開発中", stage="プロト")
    con.commit()

    deals = sfa_db.list_deals(con, status="open")
    html_out = webapp.unified_deal_table(con, deals, return_to_url="/deals?owner=X")

    # 新規追加リンク（無い商談側）: deal_id と return_to を含む
    assert f"/dev-projects/new?deal_id={d_none}&return_to=" in html_out
    assert "＋開発案件" in html_out
    # return_to は現在の一覧URLがエスケープされて渡る
    assert "return_to=%2Fdeals%3Fowner%3DX" in html_out
    # 既存開発案件がある商談側: その案件の編集リンクとテーマ名
    assert "/dev-project/" in html_out and "/edit?return_to=" in html_out
    assert "UTテーマ" in html_out
    # 1商談に複数つくため、既存があっても「＋開発案件」追加リンクを出す
    assert f"/dev-projects/new?deal_id={d_has}&return_to=" in html_out
    con.close()


def test_resolve_default_deals_tab_fallback(tmp_dir):
    """tab未指定時: MS超過0件なら'active'、超過ありなら'overdue'。明示tabは尊重。"""
    db_path = str(tmp_dir / "tabtest.db")
    sfa_db.init_db(db_path)
    con = sfa_db.connect(db_path)
    try:
        # MS超過(要フォロー)が0件 → activeへフォールバック
        assert webapp.resolve_default_deals_tab(con, None) == "active"
        # 明示指定は尊重
        assert webapp.resolve_default_deals_tab(con, "overdue") == "overdue"
        assert webapp.resolve_default_deals_tab(con, "byDate") == "byDate"
        # 次回MS未設定の進行中商談を作ると要フォロー化 → overdueが既定に
        acc = sfa_db.upsert_account(con, id=None, name="タブ社")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="MS未設定", stage="提案")
        assert webapp.resolve_default_deals_tab(con, None) == "overdue"
    finally:
        con.close()


def test_resolve_default_tab_excludes_today_for_fallback(tmp_dir):
    """当日ちょうどのMSしか無い場合、既定タブは overdue でなく active（当日除外で0件）。"""
    from datetime import date as _d
    db_path = str(tmp_dir / "tabtoday.db")
    sfa_db.init_db(db_path)
    con = sfa_db.connect(db_path)
    try:
        acc = sfa_db.upsert_account(con, id=None, name="当日社")
        today = _d.today().isoformat()  # webapp側は_today_jstだが判定関数はDB件数のみ見る
        sfa_db.upsert_deal(con, account_id=acc, deal_name="当日MSのみ", stage="提案",
                           next_milestone_date=today)
        # 当日MSしか無い → 当日除外の要フォローは0 → active へフォールバック
        # （resolve内部は_today_jst基準。ここでは当日=今日として判定が active になることを確認）
        assert webapp.resolve_default_deals_tab(con, None) == "active"
    finally:
        con.close()


def test_activity_add_requires_date_and_type(server, db_path):
    """活動追加: 日付なしで内容だけだと登録されずエラー。日付+種別ありで登録。メモ/MSのみは空活動を作らない。"""
    con = sfa_db.connect(db_path)
    try:
        acc = sfa_db.upsert_account(con, id=None, name="活動検証社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="活動検証商談")
        con.commit()
    finally:
        con.close()

    def _act_count():
        c = sfa_db.connect(db_path)
        try:
            return c.execute("SELECT COUNT(*) n FROM activities WHERE deal_id=?", (did,)).fetchone()["n"]
        finally:
            c.close()

    # 日付なし・内容ありは登録されない（エラー差し戻し）
    code, body = _post(server + "/activity/add",
                       {"deal_id": str(did), "type": "面談", "body": "日付なしメモ"},
                       headers=_auth_header())
    assert code == 200 and "日付".encode() in body
    assert _act_count() == 0

    # メモ/MSだけ更新（活動欄すべて空）は空活動を作らない
    _post(server + "/activity/add",
          {"deal_id": str(did), "type": "面談", "update_note": "現状メモだけ更新"},
          headers=_auth_header())
    assert _act_count() == 0

    # 日付+種別ありは登録される
    _post(server + "/activity/add",
          {"deal_id": str(did), "type": "面談", "occurred_on": "2026-07-15", "body": "面談実施"},
          headers=_auth_header())
    assert _act_count() == 1


def test_account_aliases_bulk_save_route(server, db_path):
    """/account-aliases/save がHTTP経由でも正しく複数アカウントの略称を一括保存すること。"""
    con = sfa_db.connect(db_path)
    acc1 = con.execute("INSERT INTO accounts(name) VALUES('住友重工業株式会社')").lastrowid
    acc2 = con.execute("INSERT INTO accounts(name) VALUES('川崎重工業株式会社')").lastrowid
    con.commit()
    con.close()

    code, _ = _post(server + "/account-aliases/save", {
        "aids[]": [str(acc1), str(acc2)],
        f"aliases__{acc1}": "住重、住重工",
        f"aliases__{acc2}": "川重",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row1 = con2.execute("SELECT aliases FROM accounts WHERE id=?", (acc1,)).fetchone()
    row2 = con2.execute("SELECT aliases FROM accounts WHERE id=?", (acc2,)).fetchone()
    assert row1["aliases"] == "住重、住重工"
    assert row2["aliases"] == "川重"
    con2.close()


def test_deliveries_new_route_persists_confidence_override(server, db_path):
    """POST /deliveries/new に confidence_override を渡すと、そのまま起票時に確定できること
    （ユーザー要望2026-08-23: 新規Delivery起票時に確定/見込みを選べるようにしたい）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
    con.close()

    code, _ = _post(server + "/deliveries/new",
                    {"deal_id": str(did), "confidence_override": "確定"}, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM deliveries WHERE deal_id=?", (did,)).fetchone()
    assert row is not None
    assert row["confidence_override"] == "確定"
    con2.close()


def test_delivery_save_route_persists_cost_fields(server, db_path):
    """POST /delivery/{id}/save に外注費(cost_mode/cost_monthly/cost_total/cost_vendor)を渡すと
    保存され、月額↔総額が期間の月数で相互換算されること（ユーザー要望2026-08-23）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-10-04")  # 4週=1.0ヶ月
    con.close()

    code, _ = _post(server + f"/delivery/{dvid}/save", {
        "title": "D", "start_week": "2026-09-07", "end_week": "2026-10-04", "status": "進行中",
        "fee_mode": "monthly", "fee_monthly": "150", "fee_total": "",
        "cost_mode": "monthly", "cost_monthly": "60", "cost_total": "",
        "cost_vendor": "C社",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM deliveries WHERE id=?", (dvid,)).fetchone()
    assert row["cost_vendor"] == "C社"
    assert row["cost_monthly"] == 60
    assert row["cost_total"] == 60  # 月数1.0で総額に補完
    con2.close()


def test_delivery_save_route_persists_business_type_override(server, db_path):
    """POST /delivery/{id}/save に事業種別L1/L2の手修正を渡すと保存され、無効値はNoneに落ちること
    （ユーザー要望2026-08-23）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注",
                             business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-10-04")
    con.close()

    code, _ = _post(server + f"/delivery/{dvid}/save", {
        "title": "D", "start_week": "2026-09-07", "end_week": "2026-10-04", "status": "進行中",
        "business_type_l1_override": "コンサルティング", "business_type_l2_override": "でたらめなL2",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM deliveries WHERE id=?", (dvid,)).fetchone()
    assert row["business_type_l1_override"] == "コンサルティング"
    assert row["business_type_l2_override"] is None  # 不正なL2（新L1配下に存在しない）はNoneに落ちる
    con2.close()


def test_delivery_save_route_persists_responsible_and_billing_fields(server, db_path):
    """#121: POST /delivery/{id}/save に責任者/担当者・請求方法/請求期日/請求送付先・
    経費請求有無/メモを渡すと保存されること。責任者/担当者はこのDeliveryの現在のアサイン
    リストに実在する値のみ受け付け、存在しない値はNoneへ落ちる。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-07",
                                   to_week="2026-09-14", role="コンサルタント", fte_pct=50)
    con.close()

    code, _ = _post(server + f"/delivery/{dvid}/save", {
        "title": "D", "start_week": "2026-09-07", "end_week": "2026-10-04", "status": "進行中",
        "responsible_owner": "早瀬", "handling_owner": "存在しない人",
        "billing_method": sfa_db.DELIVERY_BILLING_METHODS[0],
        "billing_due_sel": "__other__", "billing_due_other": "毎月10日",
        "billing_recipient": "経理部佐藤さん、PF提出",
        "expense_billing": "不明(要確認)", "expense_billing_note": "後で確認",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM deliveries WHERE id=?", (dvid,)).fetchone()
    assert row["responsible_owner"] == "早瀬"
    assert row["handling_owner"] is None  # アサインリストに無い値はNoneに落ちる
    assert row["billing_method"] == sfa_db.DELIVERY_BILLING_METHODS[0]
    assert row["billing_due"] == "毎月10日"  # 「他」選択時は自由入力欄の値
    assert row["billing_recipient"] == "経理部佐藤さん、PF提出"
    assert row["expense_billing"] == "不明(要確認)"
    assert row["expense_billing_note"] == "後で確認"
    con2.close()


def test_delivery_receipt_route_persists_monthly_amount(server, db_path):
    """POST /delivery/{id}/receipt で月別検収額を保存できること（月別入金計画・ユーザー要望2026-08-23）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-10-04")
    con.close()

    code, _ = _post(server + f"/delivery/{dvid}/receipt", {"month": "2026-09", "amount": "100"},
                    headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    rows = con2.execute("SELECT * FROM delivery_receipts WHERE delivery_id=?", (dvid,)).fetchall()
    assert len(rows) == 1 and rows[0]["month"] == "2026-09" and rows[0]["amount"] == 100
    con2.close()


def test_deal_reopen_from_edit(server, db_path):
    """クローズ済み商談を『商談に戻す（再開）』でopen化＋同社フォロー中リードを再紐付け。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('奥村組')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="環境", stage="初回アポ実施",
                             status="closed", close_reason="キャンセル")
    con.execute("INSERT INTO leads(name,company,lead_status,deal_id) VALUES('山下','奥村組','following',NULL)")
    con.commit()

    code, _ = _post(server + f"/deal/{did}/reopen", {"return_to": f"/deal/{did}"}, headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    d = sfa_db.get_deal(con2, did)
    assert d["status"] == "open" and not d.get("close_reason")
    lead = con2.execute("SELECT lead_status, deal_id FROM leads WHERE company='奥村組'").fetchone()
    assert lead["lead_status"] == "converted" and lead["deal_id"] == did
    con2.close()
    con.close()


def test_deal_duplicate_route_creates_new_deal_and_redirects(server, db_path):
    """商談複製ボタン(/deal/{id}/duplicate)がPOSTだけで新規商談を作成し、その編集画面へ
    303リダイレクトすること（htmlの目視ではなくルート経由の統合確認）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('加藤製作所')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="制御システム部", stage="クロージング",
                             owner="吉江", note="既存の現状メモ")
    con.close()

    code, body = _post(server + f"/deal/{did}/duplicate", {}, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    rows = con2.execute("SELECT * FROM deals WHERE account_id=? ORDER BY id", (acc,)).fetchall()
    assert len(rows) == 2
    new = dict(rows[-1])
    assert new["deal_name"] == "制御システム部（コピー）"
    assert new["stage"] == sfa_db.DEAL_STAGES[0]
    assert new["status"] == "open"
    assert f"SFA#{did}" in (new["note"] or "") and "既存の現状メモ" in new["note"]
    con2.close()


def test_delivery_duplicate_route_creates_new_delivery_and_redirects(server, db_path):
    """Delivery複製ボタン(/delivery/{id}/duplicate)がPOSTだけで新規Deliveryを作成し、
    その編集画面へリダイレクトすること（ユーザー要望2026-08-27）。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('加藤製作所')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="制御システム部", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="制御システム部支援")
    con.close()

    code, body = _post(server + f"/delivery/{dvid}/duplicate", {}, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    rows = con2.execute("SELECT * FROM deliveries WHERE deal_id=? ORDER BY id", (did,)).fetchall()
    assert len(rows) == 2
    assert dict(rows[-1])["title"] == "制御システム部支援（コピー）"
    con2.close()


def test_issue_page_shows_rich_note_links_strip(server, db_path):
    """論点編集ページに、論点メモへ貼ったリンクがボタンとして表示されること(#112 2026-08-27)。"""
    con = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="請求フロー", status="議論中")
    sfa_db.create_rich_note(con, kind="issue", entity_id=iid, title="",
                             body='<a href="https://slack.com/x" class="rn-linkchip" '
                                  'title="https://slack.com/x">🔗 請求方法の議論</a>')
    con.close()

    code, resp = _get(server + f"/deal-issue/{iid}", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert "https://slack.com/x" in body
    assert "請求方法の議論" in body


def test_issue_page_hides_rich_note_links_when_locked(server, db_path):
    """論点メモがロック中のときは、リンク一覧も本文プレビュー同様に一切出さない(#112)。"""
    con = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="請求フロー", status="議論中")
    sfa_db.create_rich_note(con, kind="issue", entity_id=iid, title="",
                             body='<a href="https://slack.com/x" class="rn-linkchip">🔗 請求方法の議論</a>')
    sfa_db.set_issue_note_lock(con, iid, "pw1234", "owner@example.com")
    con.close()

    code, resp = _get(server + f"/deal-issue/{iid}", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert "https://slack.com/x" not in body


def test_delivery_payment_schedule_xlsx_route_returns_workbook(server, db_path):
    """#115（2026-08-28修正）: /deliveries/payment-schedule.xlsx が検収/入金を
    同一シートにまとめたxlsxを返すこと。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('加藤製作所')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="制御システム部", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="制御システム部支援")
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 50)
    con.close()

    code, resp = _get(server + "/deliveries/payment-schedule.xlsx", headers=_auth_header())
    assert code == 200
    assert resp.headers.get("Content-Type") == \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_deliveries_route_renders_wide_main(server):
    """#118: /deliveries は画面幅いっぱいに表示するmain-wideクラス付きで返る。"""
    code, resp = _get(server + "/deliveries", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert '<main class="main-wide">' in body


def test_tasks_save_route_preserves_admin_flag_and_untouched_fields(server, db_path):
    """#123: 事務タスク(is_admin=1)をタスク編集フォーム(/tasks/save)経由で保存しても、
    フォームが扱わないフィールド(is_admin/priority/requester/slack_*/created_by/source)が
    消えないこと。保存後は事務タスク看板(/desk-tasks)へリダイレクトされること
    （ユーザー報告2026-08-28: 事務タスクを編集保存するとコンサルタスクに移動してしまう）。"""
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="交通費精算", is_admin=1, requester="早瀬",
                             priority="高", slack_channel="C123", slack_ts="111.222",
                             slack_permalink="https://slack.com/archives/C123/p111", source="slack",
                             created_by="U999", status="未着手", assignee="あみ")
    con.close()

    code, resp = _post(server + "/tasks/save", {
        "id": str(tid), "title": "交通費精算(編集)", "assignee": "あみ", "status": "未着手",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["title"] == "交通費精算(編集)"
    assert row["is_admin"] == 1  # 事務タスクのままであること（旧: Noneに上書きされコンサル側へ消えていた）
    assert row["requester"] == "早瀬"
    assert row["priority"] == "高"
    assert row["slack_channel"] == "C123" and row["slack_ts"] == "111.222"
    assert row["slack_permalink"] == "https://slack.com/archives/C123/p111"
    assert row["created_by"] == "U999"
    assert row["source"] == "slack"
    con2.close()

    # リダイレクト先が事務タスク看板であること
    req = urllib.request.Request(server + "/tasks/save", method="POST",
                                 headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
                                 data=urllib.parse.urlencode(
                                     {"id": str(tid), "title": "再編集", "assignee": "あみ", "status": "未着手"}
                                 ).encode())
    opener = urllib.request.build_opener(_NoRedirectHandler)
    resp2 = opener.open(req, timeout=10)
    assert resp2.getcode() == 303
    assert resp2.headers.get("Location") == "/desk-tasks"


def test_tasks_save_route_leaves_regular_task_untouched(server, db_path):
    """通常タスク(is_admin未設定)の編集保存は従来通りコンサルタスク看板(/tasks)へ戻ること。"""
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="通常タスク", assignee="早瀬", status="未着手")
    con.close()

    req = urllib.request.Request(server + "/tasks/save", method="POST",
                                 headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
                                 data=urllib.parse.urlencode(
                                     {"id": str(tid), "title": "通常タスク(編集)", "assignee": "早瀬", "status": "未着手"}
                                 ).encode())
    opener = urllib.request.build_opener(_NoRedirectHandler)
    resp = opener.open(req, timeout=10)
    assert resp.getcode() == 303
    assert resp.headers.get("Location") == "/tasks"

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["is_admin"] in (0, None)
    con2.close()


def test_task_card_assignee_badge_syncs_without_reload(server, db_path):
    """#122: 看板カードの担当バッジ(.m-asg)は担当未設定でも要素自体を残し(display:none)、
    taskField()のJSがfield==='assignee'のとき即座にテキスト/表示を更新できるようにする
    （ユーザー報告2026-08-28: 担当を変えてもリロードしないと反映されない）。"""
    con = sfa_db.connect(db_path)
    sfa_db.upsert_task(con, title="担当バッジ確認用", assignee="早瀬", status="未着手")
    con.close()

    code, resp = _get(server + "/tasks", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert 'data-emoji="👤"' in body
    assert "field==='assignee'&&card" in body


def test_tasks_gantt_route_defaults_to_link_grouping_and_wide_main(server):
    """#126（2026-08-28）: /tasks/gantt はクエリ省略時「紐づけ単位」タブが既定でアクティブ、
    かつ画面幅いっぱい(main-wide)で表示する。?group=typeで従来の作業種別ごとにも切替可能。"""
    code, resp = _get(server + "/tasks/gantt", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert '<main class="main-wide">' in body
    assert "紐づけ単位（Delivery→商談→論点→紐づけ無し" in body  # 既定=紐づけ単位の説明文

    code2, resp2 = _get(server + "/tasks/gantt?group=type", headers=_auth_header())
    body2 = resp2.read().decode("utf-8")
    assert code2 == 200
    assert "大分類（プロジェクト×種類）ごとに" in body2  # 明示指定すれば従来の作業種別ごとに戻せる


def test_task_link_add_and_delete_routes(server, db_path):
    """#127: /task/{id}/link/add でリンクを追加でき、/task-link/{id}/delete で削除できる。"""
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="リンク確認用")
    con.close()

    code, body = _post(server + f"/task/{tid}/link/add",
                       {"url": "example.com/x", "label": "参考資料"}, headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["url"] == "https://example.com/x"
    assert data["label"] == "参考資料"
    link_id = data["id"]

    con2 = sfa_db.connect(db_path)
    assert len(sfa_db.list_task_links(con2, tid)) == 1
    con2.close()

    code2, body2 = _post(server + f"/task-link/{link_id}/delete", {}, headers=_auth_header())
    assert code2 == 200
    assert json.loads(body2)["ok"] is True

    con3 = sfa_db.connect(db_path)
    assert sfa_db.list_task_links(con3, tid) == []
    con3.close()


def test_task_link_add_route_rejects_blank_url(server, db_path):
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="リンク確認用2")
    con.close()
    code, body = _post(server + f"/task/{tid}/link/add", {"url": ""}, headers=_auth_header())
    assert code == 200
    assert json.loads(body)["ok"] is False


def test_issue_material_add_and_delete_routes(server, db_path):
    """#128: /deal-issue/{id}/material/add で検討材料を追加でき、
    /issue-material/{id}/delete で削除できる（社内資料の体系化・層1）。"""
    con = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点確認用", status="議論中")
    con.close()

    code, _ = _post(server + f"/deal-issue/{iid}/material/add", {
        "title": "調査メモ", "content": "本文テキスト", "source_url": "https://example.com",
        "added_by": "早瀬",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    materials = sfa_db.list_issue_materials(con2, iid)
    assert len(materials) == 1
    assert materials[0]["title"] == "調査メモ" and materials[0]["content"] == "本文テキスト"
    mid = materials[0]["id"]
    con2.close()

    code2, _ = _post(server + f"/issue-material/{mid}/delete", {}, headers=_auth_header())
    assert code2 in (200, 303)

    con3 = sfa_db.connect(db_path)
    assert sfa_db.list_issue_materials(con3, iid) == []
    con3.close()


def test_issue_material_add_route_ignores_blank_content(server, db_path):
    con = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点確認用2", status="議論中")
    con.close()
    code, _ = _post(server + f"/deal-issue/{iid}/material/add", {"content": ""}, headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    assert sfa_db.list_issue_materials(con2, iid) == []
    con2.close()


def test_docs_list_and_view_routes(server, db_path):
    """#129: /docs 一覧・/docs/{id} 閲覧ページがナビ枠なしで返る。"""
    con = sfa_db.connect(db_path)
    did = sfa_db.create_doc(con, kind="検討資料", title="表示確認資料", body_html="<h1>本文</h1>")
    con.close()

    code, resp = _get(server + "/docs", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert "表示確認資料" in body

    code2, resp2 = _get(server + f"/docs/{did}", headers=_auth_header())
    body2 = resp2.read().decode("utf-8")
    assert code2 == 200
    assert "<h1>本文</h1>" in body2
    assert "コンサルタスク" not in body2  # CRMナビを含まない


def test_docs_view_route_404_for_missing_doc(server):
    code, resp = _get(server + "/docs/999999", headers=_auth_header())
    assert code == 404


def test_docs_upload_route_creates_doc_from_pasted_html(server, db_path):
    code, _ = _post(server + "/docs/upload", {
        "title": "手動入稿資料", "kind": "報告ペーパー", "html_text": "<h1>手貼りHTML</h1>",
    }, headers=_auth_header())
    assert code in (200, 303)
    con = sfa_db.connect(db_path)
    docs = sfa_db.list_docs(con)
    assert len(docs) == 1
    assert docs[0]["title"] == "手動入稿資料" and docs[0]["kind"] == "報告ペーパー"
    assert docs[0]["body_html"] == "<h1>手貼りHTML</h1>"
    con.close()


def test_docs_upload_route_ignores_blank_html(server, db_path):
    code, _ = _post(server + "/docs/upload", {"title": "空資料", "kind": "その他"},
                    headers=_auth_header())
    assert code in (200, 303)
    con = sfa_db.connect(db_path)
    assert sfa_db.list_docs(con) == []
    con.close()


def test_doc_delete_route(server, db_path):
    con = sfa_db.connect(db_path)
    did = sfa_db.create_doc(con, kind="その他", title="削除確認", body_html="x")
    con.close()
    code, _ = _post(server + f"/doc/{did}/delete", {}, headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    assert sfa_db.get_doc(con2, did) is None
    con2.close()


def test_deal_issue_doc_generate_route_creates_doc_when_ai_available(server, db_path, monkeypatch):
    """#129: /deal-issue/{id}/doc/generate は生成成功時に/docs/{id}へリダイレクトする。"""
    monkeypatch.setattr(webapp, "_call_claude_haiku", lambda *a, **kw: (
        "### 背景・課題認識\n内容\n### 現状の問題点\n内容\n### 提案内容\n内容\n"
        "### 期待される効果\n内容\n### 実行計画（スケジュール・担当）\n内容\n"
        "### リスク・懸念事項\n内容\n### 意思決定を求める事項\n内容\n"))
    con = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="生成確認論点", status="議論中")
    sfa_db.add_issue_material(con, iid, "材料テキスト")
    con.close()

    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(server + f"/deal-issue/{iid}/doc/generate", method="POST",
                                 headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
                                 data=urllib.parse.urlencode({"template": "process_change"}).encode())
    resp = opener.open(req, timeout=10)
    assert resp.getcode() == 303
    loc = resp.headers.get("Location")
    assert loc.startswith("/docs/")

    con2 = sfa_db.connect(db_path)
    docs = sfa_db.list_docs(con2, issue_id=iid)
    assert len(docs) == 1
    con2.close()


def test_deal_issue_doc_generate_route_redirects_back_without_doc_when_no_material(server, db_path):
    con = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="材料なし論点", status="議論中")
    con.close()
    code, _ = _post(server + f"/deal-issue/{iid}/doc/generate", {}, headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    assert sfa_db.list_docs(con2, issue_id=iid) == []
    con2.close()


def test_task_form_related_label_mentions_delivery(server, db_path):
    """#125: 「関連」欄のラベルにDeliveryが明記されていない（実際には選択肢は既に存在する）
    という報告への対応。ラベル文言にDeliveryを追加。"""
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="ラベル確認用")
    con.close()
    code, resp = _get(server + f"/tasks/{tid}/edit", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert "関連（商談・論点・開発案件・Delivery）" in body


def test_viewport_meta_allows_pinch_zoom(server):
    """スマホでピンチイン/アウトできるよう、maximum-scale/user-scalableを明示する(#113 2026-08-27)。"""
    code, resp = _get(server + "/deals", headers=_auth_header())
    body = resp.read().decode("utf-8")
    assert code == 200
    assert 'maximum-scale=5' in body
    assert 'user-scalable=yes' in body


def test_slack_desk_events_not_configured(server):
    """事務Bot未設定(環境変数なし)なら /slack/desk-events は503（500/例外にならない）。"""
    code, _ = _post(server + "/slack/desk-events", {"dummy": "1"}, headers=_auth_header())
    assert code == 503


def test_slack_task_events_not_configured(server):
    """#93 通常タスクBot未設定(環境変数なし)なら /slack/task-events は503（500/例外にならない）。"""
    code, _ = _post(server + "/slack/task-events", {"dummy": "1"}, headers=_auth_header())
    assert code == 503


def test_mobile_media_query_neutralizes_inline_min_width(server):
    """ユーザー報告2026-08-27「スマホで見ると枠が大きすぎて見づらい」への対策。
    各ページ個別のinline min-width指定(PC想定の複数カラムflex/gridの下限値)が狭幅では
    横はみ出しの原因になるため、640px以下では一括で無効化してflex/gridを縦積みにする。"""
    code, resp = _get(server + "/", headers=_auth_header())
    assert code == 200
    html = resp.read().decode("utf-8", "ignore")
    assert '@media(max-width:640px)' in html
    assert '[style*="min-width"]{min-width:0 !important}' in html
