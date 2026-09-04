"""業務フロー作成機能(#164, 2026-09-04)フェーズ1の回帰テスト。

ユーザー確定仕様:
- レーン(縦)×プロセス(横)の論理グリッドにボックスを配置(自由配置は許さない)。
- 1セル(レーン×プロセス)に複数ボックスを許容する。
- 矢印はレーン・プロセスをまたいでも自由に引ける(フェーズ3で実装予定、DB層は先行実装)。
- tasks/deal_issues等の既存機能とは連携せず完全独立。
- フロー削除でレーン/プロセス/ボックス/矢印もCASCADE削除される。
- フェーズ1は「DBスキーマ＋一覧ページ＋静的グリッド表示(手動追加のみ)」の範囲。
"""
from __future__ import annotations

import base64
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_biz_flow_")
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
    monkeypatch.setattr(webapp, "_call_claude_haiku", lambda *a, **k: "")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
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


def _get(url):
    req = urllib.request.Request(url, headers=_auth_header(), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, data: dict):
    import urllib.parse as up
    body = up.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=_auth_header(), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.geturl(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, url, e.read()


# ── sfa_db層: CRUD・カスケード削除 ──

def test_create_and_list_business_flow(con):
    fid = sfa_db.create_business_flow(con, "請求書処理フロー", "説明", "経理業務")
    rows = sfa_db.list_business_flows(con)
    assert len(rows) == 1
    assert rows[0]["id"] == fid
    assert rows[0]["name"] == "請求書処理フロー"
    assert rows[0]["category"] == "経理業務"
    assert rows[0]["lane_count"] == 0
    assert rows[0]["process_count"] == 0


def test_set_business_flow_field_rejects_unknown_field(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    with pytest.raises(ValueError):
        sfa_db.set_business_flow_field(con, fid, "not_a_field", "x")


def test_lane_process_box_crud_and_stack_order(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    lid = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    lid2 = sfa_db.add_business_flow_lane(con, fid, "経企/早瀬")
    pid = sfa_db.add_business_flow_process(con, fid, "請求書受領")

    lanes = sfa_db.list_business_flow_lanes(con, fid)
    assert [l["name"] for l in lanes] == ["経理/磯部", "経企/早瀬"]
    assert [l["sort_order"] for l in lanes] == [0, 1]

    b1 = sfa_db.add_business_flow_box(con, fid, lid, pid, "受領登録")
    b2 = sfa_db.add_business_flow_box(con, fid, lid, pid, "二重チェック")
    boxes = sfa_db.list_business_flow_boxes(con, fid)
    same_cell = [b for b in boxes if b["lane_id"] == lid and b["process_id"] == pid]
    assert len(same_cell) == 2  # 1セル複数ボックスを許容（ユーザー確定）
    assert {b["stack_order"] for b in same_cell} == {0, 1}

    sfa_db.update_business_flow_box(con, b1, label="受領登録(改)")
    assert sfa_db.list_business_flow_boxes(con, fid)[0]["label"] in ("受領登録(改)", "二重チェック")

    sfa_db.delete_business_flow_box(con, b2)
    assert len(sfa_db.list_business_flow_boxes(con, fid)) == 1
    assert lid2  # 未使用レーンも一覧に残る


def test_arrow_can_cross_lanes_and_go_backwards(con):
    """矢印はレーン・プロセスをまたいでも、時系列を無視した逆行も自由に引ける（ユーザー確定）。"""
    fid = sfa_db.create_business_flow(con, "フローA")
    lid1 = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    lid2 = sfa_db.add_business_flow_lane(con, fid, "経企/早瀬")
    p1 = sfa_db.add_business_flow_process(con, fid, "受領")
    p2 = sfa_db.add_business_flow_process(con, fid, "確認")
    b_later = sfa_db.add_business_flow_box(con, fid, lid1, p2, "確認作業")
    b_earlier = sfa_db.add_business_flow_box(con, fid, lid2, p1, "差し戻し")
    aid = sfa_db.add_business_flow_arrow(con, fid, b_later, b_earlier, "差し戻し")
    arrows = sfa_db.list_business_flow_arrows(con, fid)
    assert len(arrows) == 1
    assert arrows[0]["from_box_id"] == b_later
    assert arrows[0]["to_box_id"] == b_earlier

    sfa_db.delete_business_flow_arrow(con, aid)
    assert sfa_db.list_business_flow_arrows(con, fid) == []


def test_delete_business_flow_cascades_lanes_processes_boxes_arrows(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    lid = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    pid = sfa_db.add_business_flow_process(con, fid, "受領")
    b1 = sfa_db.add_business_flow_box(con, fid, lid, pid, "受領登録")
    pid2 = sfa_db.add_business_flow_process(con, fid, "確認")
    b2 = sfa_db.add_business_flow_box(con, fid, lid, pid2, "確認")
    sfa_db.add_business_flow_arrow(con, fid, b1, b2)

    sfa_db.delete_business_flow(con, fid)

    assert sfa_db.list_business_flow_lanes(con, fid) == []
    assert sfa_db.list_business_flow_processes(con, fid) == []
    assert sfa_db.list_business_flow_boxes(con, fid) == []
    assert sfa_db.list_business_flow_arrows(con, fid) == []
    assert sfa_db.list_business_flows(con) == []


def test_delete_lane_cascades_only_its_boxes(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    lid1 = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    lid2 = sfa_db.add_business_flow_lane(con, fid, "経企/早瀬")
    pid = sfa_db.add_business_flow_process(con, fid, "受領")
    sfa_db.add_business_flow_box(con, fid, lid1, pid, "A")
    sfa_db.add_business_flow_box(con, fid, lid2, pid, "B")

    sfa_db.delete_business_flow_lane(con, lid1)

    boxes = sfa_db.list_business_flow_boxes(con, fid)
    assert len(boxes) == 1
    assert boxes[0]["label"] == "B"
    assert [l["id"] for l in sfa_db.list_business_flow_lanes(con, fid)] == [lid2]


# ── HTTPルート スモークテスト ──

def test_business_flows_list_page_loads(server):
    code, body = _get(f"{server}/business-flows")
    assert code == 200
    assert "業務フロー一覧".encode() in body


def test_create_flow_via_http_and_view_detail(server):
    code, url, body = _post(f"{server}/business-flows/new",
                            {"name": "請求書処理フロー", "description": "説明"})
    assert code == 200
    assert "/business-flow/" in url
    assert "請求書処理フロー".encode() in body


def test_full_lane_process_box_flow_via_http(server):
    import re

    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])

    code, _, body = _post(f"{server}/business-flow/{fid}/lane", {"name": "経理/磯部"})
    assert code == 200
    assert "経理/磯部".encode() in body
    lane_id = int(re.search(rb"/business-flow-lane/(\d+)/delete", body).group(1))

    code, _, body = _post(f"{server}/business-flow/{fid}/process", {"name": "請求書受領"})
    assert code == 200
    assert "請求書受領".encode() in body
    process_id = int(re.search(rb"/business-flow-process/(\d+)/delete", body).group(1))

    code, _, body = _post(f"{server}/business-flow/{fid}/box",
                          {"lane_id": lane_id, "process_id": process_id, "label": "受領登録"})
    assert code == 200
    assert "受領登録".encode() in body


def test_delete_flow_via_http_removes_it_from_list(server):
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "削除対象フロー", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    _post(f"{server}/business-flow/{fid}/delete", {})
    code, body = _get(f"{server}/business-flows")
    assert code == 200
    assert "削除対象フロー".encode() not in body
