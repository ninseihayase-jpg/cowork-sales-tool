"""Googleログイン(2026-09-20〜)の回帰テスト。

2026-09-24〜、唯一の認証経路（旧ID/PWログイン・従来のBasic認証は廃止）。@inproc.orgの
Googleアカウントのみを許可する設計（OAuth同意画面の「内部」設定＋サーバー側のemailドメイン
検証の二重防御）。実際のGoogleへの通信は行わず、urllib.request.urlopenをモックして
トークン交換・userinfo取得部分を検証する。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

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


GOOGLE_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "test-client-secret"


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_google_oauth_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "google_oauth_test.db")
    sfa_db.init_db(p)
    return p


@pytest.fixture
def google_configured(monkeypatch):
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)
    yield


@pytest.fixture
def server(db_path):
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get(url, headers=None):
    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        resp = opener.open(req, timeout=10)
        return resp.getcode(), resp
    except urllib.error.HTTPError as e:
        return e.code, e


def _fake_urlopen(token_json, userinfo_json):
    """トークン交換→userinfo取得の2段階呼び出しをURLで振り分けるurlopen差し替え。"""
    class _FakeResp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fn(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if "oauth2.googleapis.com/token" in url:
            return _FakeResp(token_json)
        if "userinfo" in url:
            return _FakeResp(userinfo_json)
        raise AssertionError(f"unexpected urlopen call: {url}")

    return _fn


def test_login_page_hides_google_button_when_unconfigured(server):
    code, resp = _get(server + "/login")
    body = resp.read().decode("utf-8")
    assert code == 200
    assert "Googleでログイン" not in body


def test_login_page_shows_google_button_when_configured(server, google_configured):
    code, resp = _get(server + "/login")
    body = resp.read().decode("utf-8")
    assert code == 200
    assert "Googleでログイン" in body
    assert "/auth/google/login?next=" in body


def test_google_login_503_when_unconfigured(server):
    code, resp = _get(server + "/auth/google/login")
    assert code == 503


def test_google_login_redirects_to_google_with_state_cookie(server, google_configured):
    code, resp = _get(server + "/auth/google/login?next=/deals")
    assert code == 302
    location = resp.headers.get("Location")
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    qs = urllib.parse.parse_qs(location.split("?", 1)[1])
    assert qs["client_id"] == [GOOGLE_CLIENT_ID]
    assert qs["hd"] == ["inproc.org"]
    assert qs["state"][0].endswith(":/deals")
    setcookie = resp.headers.get("Set-Cookie", "")
    assert "sfa_oauth_state=" in setcookie
    assert "HttpOnly" in setcookie


def test_google_callback_rejects_missing_state_cookie(server, google_configured):
    code, resp = _get(server + "/auth/google/callback?code=abc&state=nonce123:/deals")
    body = resp.read().decode("utf-8")
    assert code == 401
    assert "ログイン" in body


def test_google_callback_rejects_state_nonce_mismatch(server, google_configured):
    code, resp = _get(
        server + "/auth/google/callback?code=abc&state=wrong-nonce:/deals",
        headers={"Cookie": "sfa_oauth_state=correct-nonce"},
    )
    assert code == 401


def test_google_callback_rejects_non_inproc_domain(server, google_configured, monkeypatch):
    monkeypatch.setattr(
        webapp.urllib.request, "urlopen",
        _fake_urlopen(
            {"access_token": "tok"},
            {"email": "someone@gmail.com", "email_verified": True},
        ),
    )
    code, resp = _get(
        server + "/auth/google/callback?code=abc&state=nonce123:/deals",
        headers={"Cookie": "sfa_oauth_state=nonce123"},
    )
    body = resp.read().decode("utf-8")
    assert code == 403
    assert "inproc.org" in body


def test_google_callback_rejects_unverified_email(server, google_configured, monkeypatch):
    monkeypatch.setattr(
        webapp.urllib.request, "urlopen",
        _fake_urlopen(
            {"access_token": "tok"},
            {"email": "someone@inproc.org", "email_verified": False},
        ),
    )
    code, resp = _get(
        server + "/auth/google/callback?code=abc&state=nonce123:/deals",
        headers={"Cookie": "sfa_oauth_state=nonce123"},
    )
    assert code == 403


def test_google_callback_success_sets_session_cookie_and_redirects(server, google_configured, monkeypatch):
    monkeypatch.setattr(
        webapp.urllib.request, "urlopen",
        _fake_urlopen(
            {"access_token": "tok"},
            {"email": "Ninsei.Hayase@inproc.org", "email_verified": True},
        ),
    )
    code, resp = _get(
        server + "/auth/google/callback?code=abc&state=nonce123:/deals",
        headers={"Cookie": "sfa_oauth_state=nonce123"},
    )
    assert code == 303
    assert resp.headers.get("Location") == "/deals"
    setcookies = resp.headers.get_all("Set-Cookie") or [resp.headers.get("Set-Cookie", "")]
    joined = "; ".join(setcookies)
    assert "sfa_session=" in joined
    assert "sfa_oauth_state=; " in joined  # 使い捨てstateクッキーをクリア

    # 発行されたセッションCookieで認証必須ページに入れることを確認
    session_cookie = next(c for c in setcookies if c.startswith("sfa_session="))
    session_value = session_cookie.split(";", 1)[0]
    code2, resp2 = _get(server + "/deals", headers={"Cookie": session_value})
    assert code2 == 200
