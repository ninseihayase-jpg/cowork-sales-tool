"""論点メモのパスワードロック機能（2026-08-23、ユーザー要望）の回帰テスト。

鍵の単位は論点(deal_issue)1件ごと。パスワード忘れ時は「登録済み連絡先メールへ本人が
リセットリンクを送る（mailtoで下書きを開くのみ・SMTP送信は行わない）→リンクを開いて
本人確認クリック→ロック解除」という運用。DB層のハッシュ照合・トークン発行/検証・
webapp層のルート（/rich-notes等の閲覧・保存・削除ブロック含む）を検証する。
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

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_issue_lock_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    return p


@pytest.fixture
def con(db_path):
    conn = sfa_db.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def issue_id(con):
    acc = sfa_db.upsert_account(con, name="テスト社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点X")


@pytest.fixture
def server(db_path, monkeypatch):
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", BASIC_USER)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", BASIC_PASS)
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


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── DB層 ──

def test_lock_unset_by_default(con, issue_id):
    st = sfa_db.issue_note_lock_status(con, issue_id)
    assert st == {"locked": False, "recovery_email": None}
    assert sfa_db.verify_issue_note_lock(con, issue_id, "") is True
    assert sfa_db.verify_issue_note_lock(con, issue_id, "anything") is True


def test_set_and_verify_lock(con, issue_id):
    sfa_db.set_issue_note_lock(con, issue_id, "sesame", "boss@example.com")
    st = sfa_db.issue_note_lock_status(con, issue_id)
    assert st == {"locked": True, "recovery_email": "boss@example.com"}
    assert sfa_db.verify_issue_note_lock(con, issue_id, "sesame") is True
    assert sfa_db.verify_issue_note_lock(con, issue_id, "wrong") is False
    assert sfa_db.verify_issue_note_lock(con, issue_id, "") is False


def test_clear_lock(con, issue_id):
    sfa_db.set_issue_note_lock(con, issue_id, "sesame", "boss@example.com")
    sfa_db.clear_issue_note_lock(con, issue_id)
    assert sfa_db.issue_note_lock_status(con, issue_id)["locked"] is False
    assert sfa_db.verify_issue_note_lock(con, issue_id, "wrong") is True


def test_reset_flow_requires_lock_and_recovery_email(con, issue_id):
    assert sfa_db.request_issue_note_lock_reset(con, issue_id) is None  # ロック無し
    sfa_db.set_issue_note_lock(con, issue_id, "sesame", "boss@example.com")
    req = sfa_db.request_issue_note_lock_reset(con, issue_id)
    assert req and req["recovery_email"] == "boss@example.com" and req["token"]
    assert sfa_db.confirm_issue_note_lock_reset(con, issue_id, "wrong-token") is False
    assert sfa_db.issue_note_lock_status(con, issue_id)["locked"] is True  # 誤トークンでは解除されない
    assert sfa_db.confirm_issue_note_lock_reset(con, issue_id, req["token"]) is True
    assert sfa_db.issue_note_lock_status(con, issue_id)["locked"] is False
    # 使い切ったトークンは再利用不可（クリア済みなのでcolumnがNULL→不一致でFalse）
    assert sfa_db.confirm_issue_note_lock_reset(con, issue_id, req["token"]) is False


def test_reset_token_expired_is_rejected(con, issue_id, monkeypatch):
    sfa_db.set_issue_note_lock(con, issue_id, "sesame", "boss@example.com")
    req = sfa_db.request_issue_note_lock_reset(con, issue_id)
    # 期限を過去に書き換えて期限切れを再現
    con.execute("UPDATE deal_issues SET note_lock_reset_expires='2000-01-01 00:00:00' WHERE id=?",
                (issue_id,))
    con.commit()
    assert sfa_db.confirm_issue_note_lock_reset(con, issue_id, req["token"]) is False
    assert sfa_db.issue_note_lock_status(con, issue_id)["locked"] is True


# ── webapp層: 画面のレンダリング ──

def test_detail_page_shows_lock_button_when_unlocked(con, issue_id):
    html = webapp.deal_issue_detail_page(con, sfa_db.get_deal_issue(con, issue_id))
    assert "🔒 鍵をかける" in html
    assert "🔒 論点メモがロックされているため非表示です" not in html


def test_detail_page_redacts_summary_and_notes_when_locked(con, issue_id):
    sfa_db.set_deal_issue_ai_summary(con, issue_id, "極秘の中身")
    sfa_db.create_rich_note(con, kind="issue", entity_id=issue_id, title="秘密メモ", body="機密内容")
    sfa_db.set_issue_note_lock(con, issue_id, "sesame", "boss@example.com")
    html = webapp.deal_issue_detail_page(con, sfa_db.get_deal_issue(con, issue_id))
    assert "極秘の中身" not in html
    assert "機密内容" not in html
    assert "秘密メモ" not in html
    assert "🔒 論点メモがロックされているため非表示です" in html
    assert "🔓 鍵を外す" in html and "パスワードを忘れた" in html


# ── webapp層: ルート ──

def test_note_lock_set_requires_password_and_email(server, issue_id):
    code, body = _post(server + f"/deal-issue/{issue_id}/note-lock/set", {"password": "", "recovery_email": ""},
                       headers=_auth_header())
    assert code == 200
    assert b'"ok": false' in body.lower() or b'"ok":false' in body.lower()


def test_note_lock_set_then_rich_notes_route_blocks_without_password(server, db_path, issue_id):
    code, _ = _post(server + f"/deal-issue/{issue_id}/note-lock/set",
                    {"password": "sesame", "recovery_email": "boss@example.com"}, headers=_auth_header())
    assert code == 200

    con = sfa_db.connect(db_path)
    sfa_db.create_rich_note(con, kind="issue", entity_id=issue_id, title="T", body="secret-body")
    con.close()

    code, body = _get(server + f"/rich-notes?kind=issue&id={issue_id}", headers=_auth_header())
    assert code == 200
    assert b"secret-body" not in body
    assert b'"locked": true' in body.lower() or b'"locked":true' in body.lower()

    code, body = _get(server + f"/rich-notes?kind=issue&id={issue_id}&pw=wrong", headers=_auth_header())
    assert b"secret-body" not in body

    code, body = _get(server + f"/rich-notes?kind=issue&id={issue_id}&pw=sesame", headers=_auth_header())
    assert b"secret-body" in body


def test_note_lock_clear_requires_correct_password(server, issue_id):
    _post(server + f"/deal-issue/{issue_id}/note-lock/set",
          {"password": "sesame", "recovery_email": "boss@example.com"}, headers=_auth_header())
    code, body = _post(server + f"/deal-issue/{issue_id}/note-lock/clear", {"password": "wrong"},
                       headers=_auth_header())
    assert b'"ok": false' in body.lower() or b'"ok":false' in body.lower()
    code, body = _post(server + f"/deal-issue/{issue_id}/note-lock/clear", {"password": "sesame"},
                       headers=_auth_header())
    assert b'"ok": true' in body.lower() or b'"ok":true' in body.lower()


def test_note_lock_reset_route_end_to_end(server, db_path, issue_id):
    _post(server + f"/deal-issue/{issue_id}/note-lock/set",
          {"password": "sesame", "recovery_email": "boss@example.com"}, headers=_auth_header())
    code, body = _post(server + f"/deal-issue/{issue_id}/note-lock/request-reset", {}, headers=_auth_header())
    assert code == 200
    import json
    data = json.loads(body)
    assert data["ok"] is True and data["recovery_email"] == "boss@example.com"
    token = data["reset_url"].split("token=")[1]

    code, page = _get(server + f"/deal-issue/{issue_id}/note-lock/reset?token={token}", headers=_auth_header())
    assert code == 200
    assert "解除しました".encode("utf-8") in page

    con = sfa_db.connect(db_path)
    assert sfa_db.issue_note_lock_status(con, issue_id)["locked"] is False
    con.close()
