"""タスク看板の上部集計ボックス（期限超過/開始遅延/今日まで/明日まで/保留中/最優先ピン）が、
完了等のステータス変更後にページ再読込無しで更新されない不具合（ユーザー報告2026-08-25:
「完了を押しても上部のカード数が自動計算されない。再読込が必要」）の回帰テスト。

_task_urgency_counts（tasks_page/desk_tasks_page/`/task/{id}/field`で共通利用）と、
`/task/{id}/field`のAJAXレスポンスに最新件数(counts)が含まれることを確認する。
"""
from __future__ import annotations

import base64
import datetime as dt
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
def con():
    d = tempfile.mkdtemp(prefix="sfa_urgency_counts_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def server(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", BASIC_USER)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", BASIC_PASS)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", db_path
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


def test_task_urgency_counts_reflects_completion(con):
    overdue_date = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    tid = sfa_db.upsert_task(con, title="期限超過タスク", due_date=overdue_date)
    before = webapp._task_urgency_counts(con, admin=False)
    assert before["overdue"] == 1
    sfa_db.set_task_status(con, tid, "完了")
    after = webapp._task_urgency_counts(con, admin=False)
    assert after["overdue"] == 0


def test_task_urgency_counts_admin_and_consultant_are_independent(con):
    overdue_date = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    sfa_db.upsert_task(con, title="コンサル期限超過", due_date=overdue_date, is_admin=0)
    sfa_db.upsert_task(con, title="事務期限超過", due_date=overdue_date, is_admin=1)
    consultant = webapp._task_urgency_counts(con, admin=False)
    admin = webapp._task_urgency_counts(con, admin=True)
    assert consultant["overdue"] == 1
    assert admin["overdue"] == 1
    assert "start_overdue" not in admin  # 事務タスクには開始遅延の概念を持たせない


def test_task_field_status_route_returns_updated_counts(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    overdue_date = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    tid = sfa_db.upsert_task(con, title="X", due_date=overdue_date)
    con.close()

    code, body = _post(base + f"/task/{tid}/field", {"field": "status", "value": "完了"},
                       headers=_auth_header())
    assert code in (200, 303)
    import json
    data = json.loads(body)
    assert data["ok"] is True
    assert "counts" in data
    assert data["counts"]["overdue"] == 0


def test_task_field_pinned_route_returns_updated_counts(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X")
    con.close()

    code, body = _post(base + f"/task/{tid}/field", {"field": "pinned", "value": "1"},
                       headers=_auth_header())
    assert code in (200, 303)
    import json
    data = json.loads(body)
    assert data["ok"] is True
    assert data["counts"]["pinned"] == 1


def test_task_field_due_date_route_returns_updated_counts(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X")
    con.close()

    overdue_date = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    code, body = _post(base + f"/task/{tid}/field", {"field": "due_date", "value": overdue_date},
                       headers=_auth_header())
    assert code in (200, 303)
    import json
    data = json.loads(body)
    assert data["ok"] is True
    assert data["counts"]["overdue"] == 1


def test_task_field_title_route_does_not_include_counts(server):
    """countsの計算はdue_date/status/pinned/effort系/assigneeの変更時のみ（無駄な再計算を避ける）。"""
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X")
    con.close()

    code, body = _post(base + f"/task/{tid}/field", {"field": "title", "value": "Y"},
                       headers=_auth_header())
    assert code in (200, 303)
    import json
    data = json.loads(body)
    assert data["ok"] is True
    assert "counts" not in data


def test_tasks_page_boxes_carry_data_agg_count_attrs(con):
    html = webapp.tasks_page(con)
    for key in ("overdue", "start_overdue", "today", "tomorrow", "hold", "pinned"):
        assert f'data-agg-count="{key}"' in html
    assert "function tcApplyCounts" in html


def test_desk_tasks_page_boxes_carry_data_agg_count_attrs(con):
    html = webapp.desk_tasks_page(con)
    for key in ("overdue", "today", "tomorrow", "hold", "pinned"):
        assert f'data-agg-count="{key}"' in html


def test_pinned_count_excludes_completed_tasks(con):
    """ユーザー報告2026-08-25: 完了した優先ピンがカウントされてしまう。"""
    open_id = sfa_db.upsert_task(con, title="未完了ピン")
    done_id = sfa_db.upsert_task(con, title="完了済みピン", status="完了")
    con.execute("UPDATE tasks SET pinned=1 WHERE id IN (?,?)", (open_id, done_id))
    con.commit()
    counts = webapp._task_urgency_counts(con, admin=False)
    assert counts["pinned"] == 1
