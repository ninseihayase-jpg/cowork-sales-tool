"""コンサルタスクの工数時間ベース・スケジューリング Phase 1（ユーザー要望2026-08-24）の回帰テスト。

Phase 1範囲: effort_hours列（工数感からの自動換算＋明示入力優先）・担当者ごとの1日あたり
作業可能時間(owner_daily_capacity/owner_daily_capacity_default/capacity_at)・容量の手動編集
ビュー(/tasks/capacity)。スケジューラ本体（Phase 2）はここでは対象外。
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
def con():
    d = tempfile.mkdtemp(prefix="sfa_task_effort_")
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


# ── effort_hours ──

def test_effective_effort_hours_prefers_explicit_over_level():
    assert sfa_db.effective_effort_hours({"effort_level": "中", "effort_hours": 7.5}) == 7.5


def test_effective_effort_hours_falls_back_to_level_mapping():
    assert sfa_db.effective_effort_hours({"effort_level": "軽", "effort_hours": None}) == \
        sfa_db.TASK_EFFORT_HOURS["軽"]
    assert sfa_db.effective_effort_hours({"effort_level": "超重", "effort_hours": None}) == \
        sfa_db.TASK_EFFORT_HOURS["超重"]


def test_effective_effort_hours_default_when_nothing_set():
    assert sfa_db.effective_effort_hours({}) == sfa_db.TASK_EFFORT_HOURS_DEFAULT


def test_task_form_shows_effort_hours_input_and_prefill(con):
    tid = sfa_db.upsert_task(con, title="X", effort_level="中", effort_hours=6.0)
    html = webapp.task_form(con, sfa_db.get_task(con, tid))
    assert 'name="effort_hours"' in html
    assert 'value="6.0"' in html


def test_tasks_save_route_persists_effort_hours(server):
    base, db_path = server
    code, _ = _post(base + "/tasks/save", {"title": "工数入力", "effort_hours": "3.5"},
                    headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM tasks WHERE title=?", ("工数入力",)).fetchone()
    assert row["effort_hours"] == 3.5
    con2.close()


def test_task_field_route_updates_effort_hours(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X")
    con.close()

    code, _ = _post(base + f"/task/{tid}/field", {"field": "effort_hours", "value": "2.0"},
                    headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    assert con2.execute("SELECT effort_hours FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 2.0
    con2.close()

    code2, body2 = _post(base + f"/task/{tid}/field", {"field": "effort_hours", "value": "not-a-number"},
                         headers=_auth_header())
    assert code2 == 200
    assert b'"ok": false' in body2.lower() or b'"ok":false' in body2.lower()


def test_tasks_capacity_set_route(server):
    base, db_path = server
    code, _ = _post(base + "/tasks/capacity/set", {"owner": "早瀬", "day": "2026-08-25", "hours": "2.5"},
                    headers=_auth_header())
    assert code in (200, 303)
    con2 = sfa_db.connect(db_path)
    assert sfa_db.capacity_at(con2, "早瀬", "2026-08-25") == 2.5
    con2.close()


# ── 容量: DB層 ──

def test_capacity_at_fallback_chain(con):
    assert sfa_db.capacity_at(con, "早瀬", "2026-08-25") == sfa_db.TASK_DAILY_CAPACITY_DEFAULT_HOURS
    sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 5.0})
    assert sfa_db.capacity_at(con, "早瀬", "2026-08-25") == 5.0
    sfa_db.set_owner_daily_capacity(con, "早瀬", "2026-08-25", 2.0)
    assert sfa_db.capacity_at(con, "早瀬", "2026-08-25") == 2.0
    assert sfa_db.capacity_at(con, "早瀬", "2026-08-26") == 5.0  # 他の日は既定値のまま


def test_set_owner_daily_capacity_clears_on_blank(con):
    sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 5.0})
    sfa_db.set_owner_daily_capacity(con, "早瀬", "2026-08-25", 2.0)
    sfa_db.set_owner_daily_capacity(con, "早瀬", "2026-08-25", "")
    assert sfa_db.capacity_at(con, "早瀬", "2026-08-25") == 5.0
    assert sfa_db.list_owner_daily_capacity(con, "早瀬", "2026-08-01", "2026-08-31") == {}


def test_has_owner_capacity_data(con):
    assert sfa_db.has_owner_capacity_data(con, "早瀬") is False
    sfa_db.set_owner_daily_capacity(con, "早瀬", "2026-08-25", 3.0)
    assert sfa_db.has_owner_capacity_data(con, "早瀬") is True
    assert sfa_db.has_owner_capacity_data(con, "中島") is False


def test_owner_daily_capacity_default_ignores_invalid_entries(con):
    sfa_db.set_owner_daily_capacity_default(con, {"早瀬": "5.0", "": "3.0", "中島": "でたらめ", "土屋": "-1"})
    d = sfa_db.get_owner_daily_capacity_default(con)
    assert d == {"早瀬": 5.0}


# ── webapp: 管理ページ・容量ビュー ──

def test_masters_page_shows_daily_capacity_card(con):
    html = webapp.masters_page(con)
    assert "担当者の1日あたり作業可能時間" in html
    assert 'name="dailycap_owner[]"' in html
    assert 'name="dailycap_hours[]"' in html


def test_tasks_capacity_page_renders_owners_and_days(con):
    sfa_db.set_owner_daily_capacity(con, "早瀬", "2026-08-25", 3.0)
    html = webapp.tasks_capacity_page(con)
    assert "早瀬" in html
    assert "capSet(" in html
    assert "/tasks/capacity/set" in html


# ── Phase 2: スケジューラ（compute_owner_schedule / latest_start_date） ──

def test_compute_owner_schedule_none_without_capacity_data(con):
    sfa_db.upsert_task(con, title="X", assignee="早瀬", effort_level="中", due_date="2026-09-01")
    assert sfa_db.compute_owner_schedule(con, "早瀬") is None


def test_compute_owner_schedule_packs_tasks_across_business_days():
    d = tempfile.mkdtemp(prefix="sfa_sched_")
    try:
        from datetime import date
        path = str(Path(d) / "t.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 4.0})
        today = date(2026, 8, 24)  # 月曜
        tid1 = sfa_db.upsert_task(con, title="A", assignee="早瀬", effort_hours=10.0, due_date="2026-09-01")
        tid2 = sfa_db.upsert_task(con, title="B", assignee="早瀬", effort_hours=4.0, due_date="2026-09-05")
        sched = sfa_db.compute_owner_schedule(con, "早瀬", today=today)
        assert sched["starts"][tid1] == "2026-08-24"
        assert sched["ends"][tid1] == "2026-08-26"
        assert sched["starts"][tid2] == "2026-08-26"  # Aの残り2hと同日に相乗り
        assert sched["ends"][tid2] == "2026-08-27"
        # 容量を超えて割り当てられていないこと
        for day, load in sched["daily_load"].items():
            assert load["allocated"] <= load["capacity"] + 1e-9
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_compute_owner_schedule_excludes_done_and_hold():
    d = tempfile.mkdtemp(prefix="sfa_sched2_")
    try:
        path = str(Path(d) / "t.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 4.0})
        done_id = sfa_db.upsert_task(con, title="完了済み", assignee="早瀬", effort_hours=4.0,
                                     due_date="2026-09-01", status="完了")
        hold_id = sfa_db.upsert_task(con, title="保留中", assignee="早瀬", effort_hours=4.0,
                                     due_date="2026-09-01", status="保留")
        sched = sfa_db.compute_owner_schedule(con, "早瀬")
        assert done_id not in sched["starts"]
        assert hold_id not in sched["starts"]
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_latest_start_date_matches_manual_calc_with_capacity():
    d = tempfile.mkdtemp(prefix="sfa_latest_")
    try:
        path = str(Path(d) / "t.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 4.0})
        tid = sfa_db.upsert_task(con, title="A", assignee="早瀬", effort_hours=10.0, due_date="2026-09-01")
        t = sfa_db.get_task(con, tid)
        # 09/01(火)=4h(rem6) -> 08/31(月)=4h(rem2) -> 08/28(金,土日跨ぎ)=4h(rem<=0) で確定
        assert sfa_db.latest_start_date(con, t) == "2026-08-28"
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_latest_start_date_falls_back_without_capacity_data(con):
    tid = sfa_db.upsert_task(con, title="X", assignee="中島", effort_level="中", due_date="2026-09-01")
    t = sfa_db.get_task(con, tid)
    fallback_start, _ = sfa_db.task_gantt_range("2026-09-01", "中")
    assert sfa_db.latest_start_date(con, t) == fallback_start


def test_latest_start_date_none_without_due_date(con):
    tid = sfa_db.upsert_task(con, title="X", assignee="早瀬")
    t = sfa_db.get_task(con, tid)
    assert sfa_db.latest_start_date(con, t) is None


# ── Phase 2: ガント統合・アラート ──

def test_tasks_gantt_page_uses_capacity_schedule_when_available(con):
    sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 4.0})
    sfa_db.upsert_task(con, title="NoCapOwnerTask", assignee="中島", effort_level="中", due_date="2026-09-10")
    sfa_db.upsert_task(con, title="CapOwnerTask", assignee="早瀬", effort_hours=8.0, due_date="2026-09-10")
    html = webapp.tasks_gantt_page(con)
    assert "NoCapOwnerTask" in html
    assert "CapOwnerTask" in html


def test_tasks_page_shows_start_overdue_alert_box_and_filters(con):
    sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 4.0})
    import datetime as _dt
    tid = sfa_db.upsert_task(con, title="開始遅延タスク", assignee="早瀬", effort_hours=40.0,
                             due_date=(_dt.date.today() + _dt.timedelta(days=1)).isoformat())
    html = webapp.tasks_page(con)
    assert "開始遅延" in html
    assert "urgency=start_overdue" in html
    filtered = webapp.tasks_page(con, urgency="start_overdue")
    assert "開始遅延タスク" in filtered


# ── Phase 3: 容量ビューの必要工数(Over/Under)表示 ──

def test_tasks_capacity_page_shows_daily_load_for_owners_with_capacity(con):
    sfa_db.set_owner_daily_capacity_default(con, {"早瀬": 4.0})
    sfa_db.upsert_task(con, title="A", assignee="早瀬", effort_hours=20.0, due_date="2026-09-10")
    sfa_db.upsert_task(con, title="B", assignee="中島", effort_level="中", due_date="2026-09-10")
    html = webapp.tasks_capacity_page(con)
    assert "必要/容量(h)" in html
    assert "早瀬" in html and "中島" in html


def test_tasks_capacity_page_omits_load_row_without_capacity_data(con):
    sfa_db.upsert_task(con, title="B", assignee="中島", effort_level="中", due_date="2026-09-10")
    html = webapp.tasks_capacity_page(con)
    # 中島には容量データが無いため必要工数行(必要/容量(h))自体が出ない
    assert "必要/容量(h)" not in html
