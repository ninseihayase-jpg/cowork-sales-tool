"""コンサルタスク「今日明日のタスク」機能（#101, 2026-08-27）の回帰テスト。

ユーザー確定仕様: 担当のタスクから今日/明日やる分を選び、軽い/重いに仕分けてから
当日+翌日の15分刻みカレンダーへドラッグ&ドロップで配置し、確定スナップショットとして
保存する。ドラッグ&ドロップ・リサイズ自体はブラウザ操作が必要なため、ここではデータ層
（CRUD・保存の追記性）とサーバ側ページ/ルートの骨格を検証する。
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

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_daily_plan_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_schema_has_daily_task_plan_tables(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "daily_task_plans" in tables
    assert "daily_task_plan_items" in tables


def test_create_and_list_daily_task_plan(con):
    tid = sfa_db.upsert_task(con, title="資料作成", assignee="早瀬")
    plan_id = sfa_db.create_daily_task_plan(
        con, owner="早瀬", base_date="2026-08-27", label="早瀬/8/27 09:30時点",
        items=[{"task_id": tid, "day_offset": 0, "start_min": 30, "duration_min": 90,
                "lane": 0, "bucket": "重い"}])
    plan = sfa_db.get_daily_task_plan(con, plan_id)
    assert plan["owner"] == "早瀬"
    assert plan["label"] == "早瀬/8/27 09:30時点"
    assert plan["base_date"] == "2026-08-27"

    items = sfa_db.list_daily_task_plan_items(con, plan_id)
    assert len(items) == 1
    assert items[0]["task_id"] == tid
    assert items[0]["task_title"] == "資料作成"
    assert items[0]["duration_min"] == 90
    assert items[0]["bucket"] == "重い"


def test_get_latest_daily_task_plan_returns_most_recent(con):
    tid = sfa_db.upsert_task(con, title="X", assignee="早瀬")
    item = [{"task_id": tid, "day_offset": 0, "start_min": 0, "duration_min": 30, "lane": 0, "bucket": "軽い"}]
    sfa_db.create_daily_task_plan(con, owner="早瀬", base_date="2026-08-27", label="1回目", items=item)
    p2 = sfa_db.create_daily_task_plan(con, owner="早瀬", base_date="2026-08-27", label="2回目", items=item)
    latest = sfa_db.get_latest_daily_task_plan(con, "早瀬", base_date="2026-08-27")
    assert latest["id"] == p2
    assert latest["label"] == "2回目"


def test_saving_a_new_plan_does_not_overwrite_previous_ones(con):
    """保存は追記型（上書きしない＝再計画の履歴として全部残る）。"""
    tid = sfa_db.upsert_task(con, title="X", assignee="早瀬")
    item = [{"task_id": tid, "day_offset": 0, "start_min": 0, "duration_min": 30, "lane": 0, "bucket": "軽い"}]
    p1 = sfa_db.create_daily_task_plan(con, owner="早瀬", base_date="2026-08-27", label="1回目", items=item)
    p2 = sfa_db.create_daily_task_plan(con, owner="早瀬", base_date="2026-08-27", label="2回目", items=item)
    assert p1 != p2
    assert sfa_db.get_daily_task_plan(con, p1) is not None
    assert sfa_db.get_daily_task_plan(con, p2) is not None


def test_deleting_task_cascades_plan_items(con):
    tid = sfa_db.upsert_task(con, title="X", assignee="早瀬")
    plan_id = sfa_db.create_daily_task_plan(
        con, owner="早瀬", base_date="2026-08-27", label="L",
        items=[{"task_id": tid, "day_offset": 0, "start_min": 0, "duration_min": 30, "lane": 0, "bucket": "軽い"}])
    sfa_db.hard_delete_task(con, tid)
    assert sfa_db.list_daily_task_plan_items(con, plan_id) == []


# ── ページ・ルート ────────────────────────────────────────────────────────

def test_daily_plan_page_without_assignee_shows_owner_picker(con):
    html = webapp.daily_plan_page(con)
    assert "担当を選択" in html
    assert "DP_TASKS_BY_ID" not in html


def test_daily_plan_page_with_assignee_embeds_open_tasks_only(con):
    open_id = sfa_db.upsert_task(con, title="アルファタスク", assignee="早瀬", status="未着手")
    sfa_db.upsert_task(con, title="ベータタスク", assignee="早瀬", status="完了")
    sfa_db.upsert_task(con, title="ガンマタスク", assignee="高橋", status="未着手")
    html = webapp.daily_plan_page(con, assignee="早瀬")
    assert "アルファタスク" in html
    assert "ベータタスク" not in html
    assert "ガンマタスク" not in html
    data = json.loads(html.split("window.DP_TASKS_BY_ID = ", 1)[1].split(";\n", 1)[0])
    assert str(open_id) in data


def test_daily_plan_page_links_to_latest_plan_for_today(con):
    tid = sfa_db.upsert_task(con, title="X", assignee="早瀬")
    today = webapp._today_jst().isoformat()
    plan_id = sfa_db.create_daily_task_plan(
        con, owner="早瀬", base_date=today, label="早瀬/直近プラン",
        items=[{"task_id": tid, "day_offset": 0, "start_min": 0, "duration_min": 30, "lane": 0, "bucket": "軽い"}])
    html = webapp.daily_plan_page(con, assignee="早瀬")
    assert f"/tasks/daily-plan/plan/{plan_id}" in html
    assert "早瀬/直近プラン" in html


def test_daily_task_plan_view_page_renders_blocks_with_kanban_link(con):
    tid = sfa_db.upsert_task(con, title="表示確認タスク", assignee="早瀬")
    plan_id = sfa_db.create_daily_task_plan(
        con, owner="早瀬", base_date="2026-08-27", label="早瀬/8/27 09:30時点",
        items=[{"task_id": tid, "day_offset": 1, "start_min": 60, "duration_min": 90,
                "lane": 0, "bucket": "重い"}])
    html = webapp.daily_task_plan_view_page(con, plan_id)
    assert "表示確認タスク" in html
    assert f"/tasks#tc-{tid}" in html
    assert "dp-heavy" in html


def test_daily_task_plan_view_page_missing_plan(con):
    html = webapp.daily_task_plan_view_page(con, 999999)
    assert "見つかりません" in html


# ── サーバ経由の保存ルート ─────────────────────────────────────────────────

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


def test_daily_plan_save_route_persists_and_returns_plan_id(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X", assignee="早瀬")
    con.close()

    items = [{"task_id": tid, "day_offset": 0, "start_min": 45, "duration_min": 30, "lane": 0, "bucket": "軽い"}]
    code, body = _post(base + "/tasks/daily-plan/save",
                       {"assignee": "早瀬", "items": json.dumps(items)}, headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    plan_id = data["plan_id"]

    con = sfa_db.connect(db_path)
    plan = sfa_db.get_daily_task_plan(con, plan_id)
    assert plan["owner"] == "早瀬"
    assert "時点" in plan["label"]
    saved_items = sfa_db.list_daily_task_plan_items(con, plan_id)
    con.close()
    assert len(saved_items) == 1
    assert saved_items[0]["start_min"] == 45


def test_daily_plan_save_route_rejects_empty_items(server):
    base, _db_path = server
    code, body = _post(base + "/tasks/daily-plan/save", {"assignee": "早瀬", "items": "[]"},
                       headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is False


def test_daily_plan_save_route_rejects_missing_assignee(server):
    base, _db_path = server
    code, body = _post(base + "/tasks/daily-plan/save",
                       {"assignee": "", "items": json.dumps([{"task_id": 1, "day_offset": 0,
                        "start_min": 0, "duration_min": 30, "lane": 0, "bucket": "軽い"}])},
                       headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is False
