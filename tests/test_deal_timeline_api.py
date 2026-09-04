"""Hisho案件カレンダー(#166)向けAPI `/api/deal_timeline` と `sfa_db.bulk_deal_timeline` の回帰テスト。

Hisho dashboard.htmlは活動履歴(activities)・複数MS(deal_milestones)をHisho側DBに一切
同期していないため、既存の`/api/theme_deal_map`と同じ「ブラウザから直接SFA APIを叩く」
方式で新設した。一時DBのみ使用。
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_deal_timeline_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _deal(con, theme_id=None, acc_name="A社", deal_name="X"):
    acc = sfa_db.upsert_account(con, name=acc_name)
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name=deal_name, status="open")
    if theme_id is not None:
        con.execute("UPDATE deals SET theme_id=? WHERE id=?", (theme_id, did))
        con.commit()
    return did


# ── sfa_db.bulk_deal_timeline ──

def test_bulk_deal_timeline_empty_db_returns_empty_dict(con):
    assert sfa_db.bulk_deal_timeline(con) == {}


def test_bulk_deal_timeline_excludes_deals_without_theme_id(con):
    _deal(con, theme_id=None)  # 未連携
    assert sfa_db.bulk_deal_timeline(con) == {}


def test_bulk_deal_timeline_includes_activities_and_milestones(con):
    did = _deal(con, theme_id=101)
    sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-01")
    sfa_db.add_activity(con, deal_id=did, type="電話", occurred_on="2026-08-10")
    sfa_db.add_deal_milestone(con, did, date="2026-09-10", label="次アポ", ms_type="アポ")

    result = sfa_db.bulk_deal_timeline(con)
    assert set(result.keys()) == {"101"}
    entry = result["101"]
    assert entry["deal_id"] == did
    assert [a["date"] for a in entry["activities"]] == ["2026-08-01", "2026-08-10"]
    assert entry["activities"][0]["type"] == "面談"
    assert entry["first_activity_date"] == "2026-08-01"
    assert len(entry["milestones"]) == 1
    assert entry["milestones"][0] == {"date": "2026-09-10", "label": "次アポ", "type": "アポ", "done": 0}
    assert entry["updated_at"]  # 何らかのタイムスタンプが入っている


def test_bulk_deal_timeline_deal_without_activities_or_milestones(con):
    _deal(con, theme_id=202)
    result = sfa_db.bulk_deal_timeline(con)
    entry = result["202"]
    assert entry["activities"] == []
    assert entry["milestones"] == []
    assert entry["first_activity_date"] is None


def test_bulk_deal_timeline_activities_without_date_excluded(con):
    did = _deal(con, theme_id=303)
    sfa_db.add_activity(con, deal_id=did, type="メモ", occurred_on=None)
    entry = sfa_db.bulk_deal_timeline(con)["303"]
    assert entry["activities"] == []
    assert entry["first_activity_date"] is None


def test_bulk_deal_timeline_multiple_deals_scoped_correctly(con):
    d1 = _deal(con, theme_id=11, deal_name="D1")
    d2 = _deal(con, theme_id=12, deal_name="D2")
    sfa_db.add_activity(con, deal_id=d1, type="面談", occurred_on="2026-07-01")
    sfa_db.add_activity(con, deal_id=d2, type="電話", occurred_on="2026-07-02")

    result = sfa_db.bulk_deal_timeline(con)
    assert set(result.keys()) == {"11", "12"}
    assert result["11"]["activities"][0]["date"] == "2026-07-01"
    assert result["12"]["activities"][0]["date"] == "2026-07-02"


def test_bulk_deal_timeline_milestone_done_flag_preserved(con):
    did = _deal(con, theme_id=404)
    sfa_db.add_deal_milestone(con, did, date="2026-06-01", label="完了済MS", ms_type="タスク", done=True)
    entry = sfa_db.bulk_deal_timeline(con)["404"]
    assert entry["milestones"][0]["done"] == 1


# ── HTTPルート ──

def _run_server(db_path):
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_deal_timeline_route_requires_token(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "secret-token")
    srv, t = _run_server(db_path)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/deal_timeline")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_deal_timeline_route_returns_data_with_valid_token(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv2.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    did = _deal(con2, theme_id=55)
    sfa_db.add_activity(con2, deal_id=did, type="面談", occurred_on="2026-08-01")
    con2.close()

    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "secret-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/deal_timeline?token=secret-token", timeout=10)
        assert resp.getcode() == 200
        data = json.loads(resp.read())
        assert data["55"]["activities"][0]["date"] == "2026-08-01"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_deal_timeline_route_no_basic_auth_required(monkeypatch, tmp_path):
    """/api/* はBasic認証をバイパスする既存仕様（dashboard.htmlからのクロスオリジン
    フェッチはBasic認証ヘッダを付けられないため）。SFA_BASIC_USER/PASSを設定しても
    トークンのみでアクセスできることを確認する。"""
    db_path = str(tmp_path / "srv3.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "tok")
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", "u")
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", "p")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/deal_timeline?token=tok", timeout=10)
        assert resp.getcode() == 200
        assert json.loads(resp.read()) == {}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_deal_timeline_route_disabled_when_no_token_configured(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv4.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "")
    srv, t = _run_server(db_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/deal_timeline?token=anything")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
