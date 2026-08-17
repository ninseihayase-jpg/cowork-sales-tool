"""/api/nego_threads_stale・/api/nego_threads_stale/ack の回帰テスト。

SlackのNegoCollectionスレッドが「確定」/「ok」されずに放置されている見逃し検知
（scripts/nego_thread_reminder.py が叩くAPI）が、閾値時間以上経過したpendingスレッドのみを
返すこと、completedは対象外なこと、リマインド済み(reminded_at)は再度返さないこと、
ack後に状態が変わるとまた放置対象に戻れることを検証する。
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

from cowork import sfa_db, slack_bot, webapp


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_nego_stale_api_")
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


def _post_json(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_unauthorized_without_token(server):
    code, _ = _get(server + "/api/nego_threads_stale")
    assert code == 401


def test_only_returns_pending_past_threshold(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        slack_bot.save_pending_thread(con, "ts_old", "C1", 1, "bts_old", state="pending")
        con.execute("UPDATE slack_threads SET first_seen_at=datetime('now','-5 hours') WHERE thread_ts=?",
                    ("ts_old",))
        slack_bot.save_pending_thread(con, "ts_recent", "C1", 2, "bts_recent", state="pending")
        con.execute("UPDATE slack_threads SET first_seen_at=datetime('now','-1 hours') WHERE thread_ts=?",
                    ("ts_recent",))
        slack_bot.save_pending_thread(con, "ts_completed", "C1", 3, "bts_done", state="pending")
        con.execute("UPDATE slack_threads SET first_seen_at=datetime('now','-10 hours') WHERE thread_ts=?",
                    ("ts_completed",))
        slack_bot.mark_completed(con, "ts_completed")
        con.commit()
    finally:
        con.close()

    code, body = _get(server + "/api/nego_threads_stale?token=tok123&hours=3")
    assert code == 200
    ts_list = [t["thread_ts"] for t in body["threads"]]
    assert "ts_old" in ts_list           # 5時間経過・pending → 対象
    assert "ts_recent" not in ts_list    # 1時間しか経過していない → 対象外
    assert "ts_completed" not in ts_list  # completed → 対象外


def test_ack_suppresses_until_state_changes(server, db_path):
    con = sfa_db.connect(db_path)
    try:
        slack_bot.save_pending_thread(con, "ts1", "C1", 1, "bts1", state="pending")
        con.execute("UPDATE slack_threads SET first_seen_at=datetime('now','-5 hours') WHERE thread_ts=?",
                    ("ts1",))
        con.commit()
    finally:
        con.close()

    code, body = _get(server + "/api/nego_threads_stale?token=tok123&hours=3")
    assert code == 200 and "ts1" in [t["thread_ts"] for t in body["threads"]]

    code, body = _post_json(server + "/api/nego_threads_stale/ack?token=tok123", {"thread_ts": "ts1"})
    assert code == 200 and body["ok"] is True

    code, body = _get(server + "/api/nego_threads_stale?token=tok123&hours=3")
    assert "ts1" not in [t["thread_ts"] for t in body["threads"]]   # ack後は再送されない

    # 状態が変わる（save_pending_threadが再度呼ばれる）とreminded_atがリセットされ、再度対象になる
    con2 = sfa_db.connect(db_path)
    try:
        slack_bot.save_pending_thread(con2, "ts1", "C1", 1, "bts1", state="new_deal_ask")
        con2.execute("UPDATE slack_threads SET first_seen_at=datetime('now','-5 hours') WHERE thread_ts=?",
                     ("ts1",))
        con2.commit()
    finally:
        con2.close()
    code, body = _get(server + "/api/nego_threads_stale?token=tok123&hours=3")
    assert "ts1" in [t["thread_ts"] for t in body["threads"]]
