"""#98: /api/intake_inbox_stale の回帰テスト。

見逃し検知（scripts/intake_inbox_stale_slack_reminder.py が叩くAPI）が、
「前日以前から残っている」ものだけを正しく2種類（inbox放置／assigned放置）で
返すこと、当日分は含めないこと、トークン認証が効くことを検証する。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_stale_api_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    return p


@pytest.fixture
def server(db_path, monkeypatch):
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "tok123")
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


def _get(url):
    req = urllib.request.Request(url, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_unauthorized_without_token(server):
    code, body = _get(server + "/api/intake_inbox_stale")
    assert code == 401


def test_only_returns_items_from_before_today(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        # 前日以前(inbox放置) — created_atを直接過去日付に書き換えて模擬
        iid_old = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="a1",
                                              title="旧い未処理", occurred_on="2026-08-10",
                                              transcript="x", attendees_json="[]")
        con.execute("UPDATE intake_transcripts SET created_at='2026-08-10 09:00:00' WHERE id=?", (iid_old,))
        # 当日分(inbox) — 今日作成なので対象外のはず
        sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="a2",
                                    title="今日届いた分", occurred_on="2026-08-16",
                                    transcript="y", attendees_json="[]")
        # assigned放置(jamie, 前日以前)
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        iid_assigned = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="a3",
                                                   title="割当済み放置", occurred_on="2026-08-10",
                                                   transcript="z", attendees_json="[]")
        sfa_db.assign_inbox_transcript(con, iid_assigned, kind="deal", entity_id=did)
        con.execute("UPDATE intake_transcripts SET created_at='2026-08-10 09:00:00' WHERE id=?", (iid_assigned,))
        con.commit()
    finally:
        con.close()

    code, body = _get(server + "/api/intake_inbox_stale?token=tok123")
    assert code == 200
    inbox_titles = [r["title"] for r in body["inbox"]]
    assigned_titles = [r["title"] for r in body["assigned_unconsumed"]]
    assert "旧い未処理" in inbox_titles
    assert "今日届いた分" not in inbox_titles
    assert "割当済み放置" in assigned_titles
