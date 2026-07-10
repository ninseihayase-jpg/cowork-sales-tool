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
