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


@pytest.mark.parametrize("path", ["/", "/deals", "/dev-projects", "/deal-issues", "/accounts", "/leads"])
def test_get_main_routes_return_200(server, path):
    code, resp = _get(server + path, headers=_auth_header())
    assert code == 200, f"{path} returned {code}"
    body = resp.read()
    assert len(body) > 0


def test_health_ok_without_auth(server):
    code, resp = _get(server + "/health")
    assert code == 200
    assert resp.read() == b'{"status":"ok"}'


def test_get_root_without_auth_is_401(server):
    code, resp = _get(server + "/")
    assert code == 401
    assert resp.headers.get("WWW-Authenticate") is not None


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

    # urllibはデフォルトで303リダイレクトを自動追従するため、最終的に/deal/<id>へGETした
    # 200が返る(もしくはリダイレクト自体を検知できるようredirect_handlerを外して確認)。
    assert code == 200
    assert "/deal/" in location

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
    assert resp.headers.get("Location", "").startswith("/deal/")
