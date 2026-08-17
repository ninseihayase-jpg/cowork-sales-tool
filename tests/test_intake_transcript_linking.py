"""取り込み文字起こし(intake_transcripts)と、そこから作られた生成物
（活動履歴/ヒアリング結果/商談メモ/論点メモ）の紐づけ(intake_transcript_id)の回帰テスト。

商談×顧客面談・商談×社内議論(メモ)・論点×議論整形メモ、の3経路それぞれで、
生成物のintake_transcript_idが正しく出典を指すこと、逆引き(find_intake_transcript_usages)で
発見できることを検証する。
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
    d = tempfile.mkdtemp(prefix="sfa_intake_link_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    return p


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
    body = urllib.parse.urlencode(data).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_deal_meeting_flow_links_activity_and_hearing_result(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        acc = sfa_db.upsert_account(con, name="テスト社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        con.commit()
    finally:
        con.close()

    code, body = _post(server + "/hearing/intake/structure",
                       {"deal_id": str(did), "record_kind": "meeting", "transcript": "面談の文字起こし本文です。",
                        "conducted_on": "2026-08-17", "act_type": "面談", "make_hearing": "1"},
                       headers=_auth_header())
    assert code == 200
    import re
    m = re.search(r'name="session_id" value="(\d+)"', body)
    assert m, "session_idが取れない"
    sid = m.group(1)

    con = sfa_db.connect(db_path)
    try:
        it = con.execute("SELECT id FROM intake_transcripts WHERE kind='deal' AND entity_id=?", (did,)).fetchone()
        assert it, "intake_transcriptsに保存されていない"
        itid = it["id"]
        sess = sfa_db.get_hearing_session(con, int(sid))
        assert sess["intake_transcript_id"] == itid
    finally:
        con.close()

    code, _ = _post(server + "/hearing/intake/commit",
                    {"session_id": sid, "item_count": "0", "overview": "全体像テキスト",
                     "make_tasks": "0"}, headers=_auth_header())
    assert code in (200, 303)

    con = sfa_db.connect(db_path)
    try:
        act = con.execute("SELECT * FROM activities WHERE deal_id=?", (did,)).fetchone()
        assert act is not None and act["intake_transcript_id"] == itid
        hr = con.execute("SELECT * FROM hearing_results WHERE deal_id=?", (did,)).fetchone()
        assert hr is not None and hr["intake_transcript_id"] == itid
        usages = sfa_db.find_intake_transcript_usages(con, itid)
        types = {u["type"] for u in usages}
        assert types == {"activity", "hearing_result"}
    finally:
        con.close()


def test_deal_memo_flow_links_rich_note(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        acc = sfa_db.upsert_account(con, name="テスト社2")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D2", stage="提案")
        con.commit()
    finally:
        con.close()

    code, body = _post(server + "/hearing/intake/structure",
                       {"deal_id": str(did), "record_kind": "memo", "transcript": "社内議論の文字起こし本文です。"},
                       headers=_auth_header())
    assert code == 200
    import re
    m = re.search(r'name="session_id" value="(\d+)"', body)
    assert m
    sid = m.group(1)

    con = sfa_db.connect(db_path)
    try:
        it = con.execute("SELECT id FROM intake_transcripts WHERE kind='deal' AND entity_id=?", (did,)).fetchone()
        itid = it["id"]
    finally:
        con.close()

    code, _ = _post(server + "/hearing/intake/commit",
                    {"session_id": sid, "overview": "全体像", "note_title": "テストメモ"},
                    headers=_auth_header())
    assert code in (200, 303)

    con = sfa_db.connect(db_path)
    try:
        note = con.execute("SELECT * FROM rich_notes WHERE kind='deal' AND entity_id=?", (did,)).fetchone()
        assert note is not None and note["intake_transcript_id"] == itid
        usages = sfa_db.find_intake_transcript_usages(con, itid)
        assert len(usages) == 1 and usages[0]["type"] == "rich_note"
    finally:
        con.close()


def test_issue_intake_flow_links_rich_note(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")
        con.commit()
    finally:
        con.close()

    code, body = _post(server + f"/deal-issue/{iid}/intake/structure",
                       {"transcript": "論点の文字起こし本文です。"}, headers=_auth_header())
    assert code == 200
    import re
    m = re.search(r'name="intake_transcript_id" value="(\d+)"', body)
    assert m, "intake_transcript_idの隠しフィールドが無い"
    itid = int(m.group(1))

    con = sfa_db.connect(db_path)
    try:
        it = con.execute("SELECT id FROM intake_transcripts WHERE kind='issue' AND entity_id=?", (iid,)).fetchone()
        assert it["id"] == itid
    finally:
        con.close()

    code, _ = _post(server + f"/deal-issue/{iid}/intake/commit",
                    {"intake_transcript_id": str(itid), "overview": "全体像", "note_title": "論点メモ"},
                    headers=_auth_header())
    assert code in (200, 303)

    con = sfa_db.connect(db_path)
    try:
        note = con.execute("SELECT * FROM rich_notes WHERE kind='issue' AND entity_id=?", (iid,)).fetchone()
        assert note is not None and note["intake_transcript_id"] == itid
        usages = sfa_db.find_intake_transcript_usages(con, itid)
        assert len(usages) == 1 and usages[0]["type"] == "rich_note"
    finally:
        con.close()
