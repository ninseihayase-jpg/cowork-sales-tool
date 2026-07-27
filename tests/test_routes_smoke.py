"""cowork/webapp.py の主要ルートに対するスモークテスト。

_make_handler(db_path, theme_client) で得たハンドラを一時ポート(OS自動割当)で
ThreadingHTTPServerに載せ、実際にHTTPリクエストを送って200/303/404/401/503が
返ること・例外や500にならないことを確認する。DBは一時ファイル、theme_client=None。
"""
from __future__ import annotations

import base64
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
    body = urllib.parse.urlencode(data).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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
