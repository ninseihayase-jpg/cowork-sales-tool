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


# ── #164追加要望(2026-09-04): 一覧の表形式化・複製・AIドラフト ──

def test_business_flows_page_is_single_flat_table_not_grouped_sections(con):
    """一覧は分類ごとにセクションを分けず、1本の表に「分類」列で出す（ユーザー確定）。"""
    sfa_db.create_business_flow(con, "NDA締結", "", "契約業務")
    sfa_db.create_business_flow(con, "請求書処理フロー", "", "請求業務")
    html = webapp.business_flows_page(con)
    assert html.count("<table>") == 1  # カテゴリごとの複数<table>ではなく単一の表
    assert "<th>分類</th>" in html
    assert "契約業務" in html and "請求業務" in html


def test_duplicate_business_flow_copies_structure(con):
    fid = sfa_db.create_business_flow(con, "フローA", "説明", "経理業務")
    lid = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    pid1 = sfa_db.add_business_flow_process(con, fid, "受領")
    pid2 = sfa_db.add_business_flow_process(con, fid, "確認")
    b1 = sfa_db.add_business_flow_box(con, fid, lid, pid1, "受領登録")
    b2 = sfa_db.add_business_flow_box(con, fid, lid, pid2, "確認")
    sfa_db.add_business_flow_arrow(con, fid, b1, b2, "note")

    new_id = sfa_db.duplicate_business_flow(con, fid)

    new_flow = sfa_db.get_business_flow(con, new_id)
    assert new_flow["name"] == "フローA(コピー)"
    assert new_flow["category"] == "経理業務"
    assert [l["name"] for l in sfa_db.list_business_flow_lanes(con, new_id)] == ["経理/磯部"]
    assert [p["name"] for p in sfa_db.list_business_flow_processes(con, new_id)] == ["受領", "確認"]
    new_boxes = sfa_db.list_business_flow_boxes(con, new_id)
    assert {b["label"] for b in new_boxes} == {"受領登録", "確認"}
    new_arrows = sfa_db.list_business_flow_arrows(con, new_id)
    assert len(new_arrows) == 1 and new_arrows[0]["label"] == "note"
    # 元フローは変更されない
    assert len(sfa_db.list_business_flow_boxes(con, fid)) == 2


def test_duplicate_business_flow_missing_returns_none(con):
    assert sfa_db.duplicate_business_flow(con, 999) is None


def test_duplicate_route_via_http_creates_copy_and_redirects(server):
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "元フロー", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    code, new_url, body = _post(f"{server}/business-flow/{fid}/duplicate", {})
    assert code == 200
    assert new_url != f"{server}/business-flow/{fid}"
    assert "元フロー(コピー)".encode() in body


# ── AIドラフト機能 ──

def test_ai_draft_block_shows_start_button_when_empty(con):
    fid = sfa_db.create_business_flow(con, "請求書処理フロー", "")
    html = webapp.business_flow_detail_page(con, fid)
    assert "質問を生成" in html
    assert "action=\"/business-flow/%d/ai-draft/questions\"" % fid in html


def test_ai_draft_block_hidden_once_lanes_exist(con):
    fid = sfa_db.create_business_flow(con, "請求書処理フロー", "")
    sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    html = webapp.business_flow_detail_page(con, fid)
    assert "質問を生成" not in html
    assert "AIにドラフトを作ってもらう" not in html


def test_ai_draft_block_renders_question_form(con):
    fid = sfa_db.create_business_flow(con, "請求書処理フロー", "")
    html = webapp.business_flow_detail_page(con, fid, ai_questions=["どの部署が関わりますか？", "承認は何段階ですか？"])
    assert "どの部署が関わりますか？" in html
    assert "承認は何段階ですか？" in html
    assert html.count('name="question"') == 2
    assert 'action="/business-flow/%d/ai-draft/build"' % fid in html


def test_ai_draft_block_renders_error(con):
    fid = sfa_db.create_business_flow(con, "請求書処理フロー", "")
    html = webapp.business_flow_detail_page(con, fid, ai_error="生成に失敗しました")
    assert "生成に失敗しました" in html


def test_ai_draft_questions_route_renders_form(server, monkeypatch):
    monkeypatch.setattr(webapp, "_business_flow_ai_questions",
                        lambda name, desc: ["どの部署が関わりますか？", "承認フローはありますか？"])
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "請求書処理フロー", "description": "説明"})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])

    code, _, body = _post(f"{server}/business-flow/{fid}/ai-draft/questions", {})
    assert code == 200
    assert "どの部署が関わりますか？".encode() in body
    assert "承認フローはありますか？".encode() in body


def test_ai_draft_questions_route_failure_shows_error(server, monkeypatch):
    monkeypatch.setattr(webapp, "_business_flow_ai_questions", lambda name, desc: [])
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "請求書処理フロー", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])

    code, _, body = _post(f"{server}/business-flow/{fid}/ai-draft/questions", {})
    assert code == 200
    assert "質問の生成に失敗しました".encode() in body


def test_ai_draft_build_creates_lanes_processes_boxes(monkeypatch, server):
    monkeypatch.setattr(webapp, "_business_flow_ai_draft", lambda name, desc, qa: {
        "lanes": ["経理/磯部", "経企/早瀬"],
        "processes": ["受領", "確認", "支払い"],
        "boxes": [
            {"lane": "経理/磯部", "process": "受領", "label": "受領登録"},
            {"lane": "経企/早瀬", "process": "確認", "label": "承認判断"},
        ],
    })
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "請求書処理フロー", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])

    code, redirected_url, body = _post(f"{server}/business-flow/{fid}/ai-draft/build",
                                       {"question": "どの部署が関わりますか？", "answer": "経理と経企です"})
    assert code == 200
    assert redirected_url == f"{server}/business-flow/{fid}"
    assert "受領登録".encode() in body
    assert "承認判断".encode() in body
    assert "経理/磯部".encode() in body
    assert "経企/早瀬".encode() in body


def test_ai_draft_build_ignores_boxes_with_unknown_lane_or_process(monkeypatch, server):
    """AIがlanes/processesリストに無い値をboxに書いてきても無視する（データ不整合防止）。"""
    monkeypatch.setattr(webapp, "_business_flow_ai_draft", lambda name, desc, qa: {
        "lanes": ["経理/磯部"],
        "processes": ["受領"],
        "boxes": [
            {"lane": "経理/磯部", "process": "受領", "label": "正常なボックス"},
            {"lane": "存在しないレーン", "process": "受領", "label": "無視されるはず"},
        ],
    })
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])

    _code, _, body = _post(f"{server}/business-flow/{fid}/ai-draft/build", {})
    assert "正常なボックス".encode() in body
    assert "無視されるはず".encode() not in body


def test_reorder_business_flow_lanes(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    l1 = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    l2 = sfa_db.add_business_flow_lane(con, fid, "経企/早瀬")
    assert [l["id"] for l in sfa_db.list_business_flow_lanes(con, fid)] == [l1, l2]

    sfa_db.reorder_business_flow_lanes(con, fid, [l2, l1])
    assert [l["id"] for l in sfa_db.list_business_flow_lanes(con, fid)] == [l2, l1]

    l3 = sfa_db.add_business_flow_lane(con, fid, "PM")
    assert [l["id"] for l in sfa_db.list_business_flow_lanes(con, fid)] == [l2, l1, l3]


def test_reorder_business_flow_lanes_ignores_other_flow_ids(con):
    f1 = sfa_db.create_business_flow(con, "フローA")
    f2 = sfa_db.create_business_flow(con, "フローB")
    l1 = sfa_db.add_business_flow_lane(con, f1, "経理/磯部")
    l2 = sfa_db.add_business_flow_lane(con, f1, "経企/早瀬")
    other = sfa_db.add_business_flow_lane(con, f2, "他フローのレーン")

    sfa_db.reorder_business_flow_lanes(con, f1, [l2, other, l1])

    assert [l["id"] for l in sfa_db.list_business_flow_lanes(con, f1)] == [l2, l1]


def test_reorder_business_flow_processes(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    p1 = sfa_db.add_business_flow_process(con, fid, "受領")
    p2 = sfa_db.add_business_flow_process(con, fid, "確認")
    sfa_db.reorder_business_flow_processes(con, fid, [p2, p1])
    assert [p["id"] for p in sfa_db.list_business_flow_processes(con, fid)] == [p2, p1]


def test_lanes_reorder_route_via_http(server):
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    import re
    _, _, body1 = _post(f"{server}/business-flow/{fid}/lane", {"name": "経理/磯部"})
    l1 = int(re.search(rb"/business-flow-lane/(\d+)/delete", body1).group(1))
    _, _, body2 = _post(f"{server}/business-flow/{fid}/lane", {"name": "経企/早瀬"})
    ids2 = [int(m) for m in re.findall(rb"/business-flow-lane/(\d+)/delete", body2)]
    l2 = [x for x in ids2 if x != l1][0]

    code, _, _ = _post(f"{server}/business-flow/{fid}/lanes/reorder", {"order": f"{l2},{l1}"})
    assert code == 204


def test_processes_reorder_route_via_http(server):
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    import re
    _, _, body1 = _post(f"{server}/business-flow/{fid}/process", {"name": "受領"})
    p1 = int(re.search(rb"/business-flow-process/(\d+)/delete", body1).group(1))
    _, _, body2 = _post(f"{server}/business-flow/{fid}/process", {"name": "確認"})
    ids2 = [int(m) for m in re.findall(rb"/business-flow-process/(\d+)/delete", body2)]
    p2 = [x for x in ids2 if x != p1][0]

    code, _, _ = _post(f"{server}/business-flow/{fid}/processes/reorder", {"order": f"{p2},{p1}"})
    assert code == 204


def test_detail_page_grid_has_drag_handles_and_data_attrs(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    lid = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    pid = sfa_db.add_business_flow_process(con, fid, "受領")
    html = webapp.business_flow_detail_page(con, fid)
    assert 'id="bfGridTable"' in html
    assert f'data-lane-id="{lid}"' in html
    assert f'data-process-id="{pid}"' in html
    assert "initBusinessFlowDrag(" in html
    assert html.count("drag-handle") >= 2  # レーン用+プロセス用


def test_ai_draft_build_failure_shows_error_and_creates_nothing(server, monkeypatch):
    monkeypatch.setattr(webapp, "_business_flow_ai_draft", lambda name, desc, qa: {})
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])

    code, _, body = _post(f"{server}/business-flow/{fid}/ai-draft/build", {})
    assert code == 200
    assert "ドラフトの生成に失敗しました".encode() in body


# ── #164フェーズ2/3: ボックスのセル間ドラッグ移動・矢印接続 ──

def test_move_business_flow_box_to_another_cell(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    l1 = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    l2 = sfa_db.add_business_flow_lane(con, fid, "経企/早瀬")
    p1 = sfa_db.add_business_flow_process(con, fid, "受領")
    bid = sfa_db.add_business_flow_box(con, fid, l1, p1, "受領登録")

    sfa_db.move_business_flow_box(con, bid, l2, p1)

    box = sfa_db.get_business_flow_box(con, bid)
    assert box["lane_id"] == l2 and box["process_id"] == p1
    assert box["stack_order"] == 0


def test_move_business_flow_box_appends_to_end_of_target_cell(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    l1 = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    l2 = sfa_db.add_business_flow_lane(con, fid, "経企/早瀬")
    p1 = sfa_db.add_business_flow_process(con, fid, "受領")
    sfa_db.add_business_flow_box(con, fid, l2, p1, "既存ボックス")
    bid = sfa_db.add_business_flow_box(con, fid, l1, p1, "移動対象")

    sfa_db.move_business_flow_box(con, bid, l2, p1)

    moved = sfa_db.get_business_flow_box(con, bid)
    assert moved["stack_order"] == 1  # 既存ボックス(0)の次に積まれる


def test_get_business_flow_box_missing_returns_none(con):
    assert sfa_db.get_business_flow_box(con, 999) is None


def test_add_business_flow_arrow_is_idempotent_for_same_pair(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    lid = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    p1 = sfa_db.add_business_flow_process(con, fid, "受領")
    p2 = sfa_db.add_business_flow_process(con, fid, "確認")
    b1 = sfa_db.add_business_flow_box(con, fid, lid, p1, "受領登録")
    b2 = sfa_db.add_business_flow_box(con, fid, lid, p2, "確認")

    aid1 = sfa_db.add_business_flow_arrow(con, fid, b1, b2)
    aid2 = sfa_db.add_business_flow_arrow(con, fid, b1, b2)

    assert aid1 == aid2
    assert len(sfa_db.list_business_flow_arrows(con, fid)) == 1


def test_move_box_route_via_http(server):
    import re
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    _, _, lane_body = _post(f"{server}/business-flow/{fid}/lane", {"name": "経理/磯部"})
    l1 = int(re.search(rb"/business-flow-lane/(\d+)/delete", lane_body).group(1))
    _, _, lane_body2 = _post(f"{server}/business-flow/{fid}/lane", {"name": "経企/早瀬"})
    ids2 = [int(m) for m in re.findall(rb"/business-flow-lane/(\d+)/delete", lane_body2)]
    l2 = [x for x in ids2 if x != l1][0]
    _, _, proc_body = _post(f"{server}/business-flow/{fid}/process", {"name": "受領"})
    p1 = int(re.search(rb"/business-flow-process/(\d+)/delete", proc_body).group(1))
    _, _, box_body = _post(f"{server}/business-flow/{fid}/box",
                           {"lane_id": l1, "process_id": p1, "label": "受領登録"})
    bid = int(re.search(rb'data-box-id="(\d+)"', box_body).group(1))

    code, _, _ = _post(f"{server}/business-flow-box/{bid}/move", {"lane_id": l2, "process_id": p1})
    assert code == 204

    code, body = _get(f"{server}/business-flow/{fid}")
    assert code == 200
    assert f'data-lane-id="{l2}"'.encode() in body


def test_arrow_route_via_http_creates_and_shows_in_list(server):
    import re
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    _, _, lane_body = _post(f"{server}/business-flow/{fid}/lane", {"name": "経理/磯部"})
    l1 = int(re.search(rb"/business-flow-lane/(\d+)/delete", lane_body).group(1))
    _, _, proc_body = _post(f"{server}/business-flow/{fid}/process", {"name": "受領"})
    p1 = int(re.search(rb"/business-flow-process/(\d+)/delete", proc_body).group(1))
    _, _, proc_body2 = _post(f"{server}/business-flow/{fid}/process", {"name": "確認"})
    ids2 = [int(m) for m in re.findall(rb"/business-flow-process/(\d+)/delete", proc_body2)]
    p2 = [x for x in ids2 if x != p1][0]
    _, _, box_body1 = _post(f"{server}/business-flow/{fid}/box",
                            {"lane_id": l1, "process_id": p1, "label": "受領登録"})
    b1 = int(re.search(rb'data-box-id="(\d+)"', box_body1).group(1))
    _, _, box_body2 = _post(f"{server}/business-flow/{fid}/box",
                            {"lane_id": l1, "process_id": p2, "label": "承認判断"})
    ids2b = [int(m) for m in re.findall(rb'data-box-id="(\d+)"', box_body2)]
    b2 = [x for x in ids2b if x != b1][0]

    code, _, _ = _post(f"{server}/business-flow-box/{b1}/arrow", {"to_box_id": b2})
    assert code == 204

    code, body = _get(f"{server}/business-flow/{fid}")
    assert code == 200
    assert "受領登録 → 承認判断".encode() in body


def test_arrow_delete_route_via_http(server):
    import re
    _code, url, _ = _post(f"{server}/business-flows/new", {"name": "フローA", "description": ""})
    fid = int(url.rstrip("/").rsplit("/", 1)[-1])
    _, _, lane_body = _post(f"{server}/business-flow/{fid}/lane", {"name": "経理/磯部"})
    l1 = int(re.search(rb"/business-flow-lane/(\d+)/delete", lane_body).group(1))
    _, _, proc_body = _post(f"{server}/business-flow/{fid}/process", {"name": "受領"})
    p1 = int(re.search(rb"/business-flow-process/(\d+)/delete", proc_body).group(1))
    _, _, box_body1 = _post(f"{server}/business-flow/{fid}/box",
                            {"lane_id": l1, "process_id": p1, "label": "A"})
    b1 = int(re.search(rb'data-box-id="(\d+)"', box_body1).group(1))
    _, _, box_body2 = _post(f"{server}/business-flow/{fid}/box",
                            {"lane_id": l1, "process_id": p1, "label": "B"})
    ids2 = [int(m) for m in re.findall(rb'data-box-id="(\d+)"', box_body2)]
    b2 = [x for x in ids2 if x != b1][0]
    _post(f"{server}/business-flow-box/{b1}/arrow", {"to_box_id": b2})

    _code, body_get = _get(f"{server}/business-flow/{fid}")
    aid_match = re.search(rb"/business-flow-arrow/(\d+)/delete", body_get)
    assert aid_match is not None
    aid = int(aid_match.group(1))

    code, _, _ = _post(f"{server}/business-flow-arrow/{aid}/delete", {})
    assert code == 200

    _, body_after = _get(f"{server}/business-flow/{fid}")
    assert "A → B".encode() not in body_after


def test_detail_page_renders_arrow_svg_and_print_button(con):
    fid = sfa_db.create_business_flow(con, "フローA")
    lid = sfa_db.add_business_flow_lane(con, fid, "経理/磯部")
    pid = sfa_db.add_business_flow_process(con, fid, "受領")
    b1 = sfa_db.add_business_flow_box(con, fid, lid, pid, "A")
    pid2 = sfa_db.add_business_flow_process(con, fid, "確認")
    b2 = sfa_db.add_business_flow_box(con, fid, lid, pid2, "B")
    sfa_db.add_business_flow_arrow(con, fid, b1, b2)

    html = webapp.business_flow_detail_page(con, fid)
    assert 'id="bfArrowSvg"' in html
    assert "renderBfArrows" in html
    assert "window.print()" in html
    assert "bf-connect-handle" in html
    assert f'"from": {b1}, "to": {b2}' in html or f'"from":{b1},"to":{b2}' in html.replace(" ", "")
