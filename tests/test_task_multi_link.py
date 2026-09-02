"""コンサルタスクの複数関連付け（#146, 2026-09-02）の回帰テスト。

ユーザー確定仕様:
- 種別（商談/論点/Delivery/開発案件）をまたいで自由に複数の関連付けができる。
- 看板上部の紐づけ一覧・ガントの紐づけ単位グルーピング・直近タスク設計のトレイでは、
  複数関連付けされたタスクは該当する全ての箇所に重複して表示する。

tasks.link_type/link_id（レガシー単一列、#146以前の唯一の紐づけ経路）は非破壊のため
残置し、以後は set_task_links が正のtask_entity_linksテーブルへ書く。読み出しは
get_task_links/get_task_links_map が両方をマージするため、旧データを直接
upsert_task(..., link_type=, link_id=)で作った既存タスクも問題なく解決できる。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_task_multi_link_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def deal_issue_delivery(con):
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点A")
    dv = sfa_db.create_delivery(con, deal_id=did, title="DeliveryA")
    return did, iid, dv


# ── sfa_db層のCRUD ──

def test_set_and_get_task_links_cross_type(con, deal_issue_delivery):
    did, iid, dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="複数紐づけタスク", status="未着手")
    sfa_db.set_task_links(con, tid, [("deal", did), ("issue", iid), ("delivery", dv)])
    links = sfa_db.get_task_links(con, tid)
    assert {(l["link_type"], l["link_id"]) for l in links} == {
        ("deal", did), ("issue", iid), ("delivery", dv)}


def test_set_task_links_dedups_and_ignores_invalid(con, deal_issue_delivery):
    did, _iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="X", status="未着手")
    sfa_db.set_task_links(con, tid, [("deal", did), ("deal", did), ("でたらめ", 1), ("deal", None)])
    links = sfa_db.get_task_links(con, tid)
    assert [(l["link_type"], l["link_id"]) for l in links] == [("deal", did)]


def test_set_task_links_replaces_previous_set(con, deal_issue_delivery):
    did, iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="X", status="未着手")
    sfa_db.set_task_links(con, tid, [("deal", did)])
    sfa_db.set_task_links(con, tid, [("issue", iid)])
    links = sfa_db.get_task_links(con, tid)
    assert [(l["link_type"], l["link_id"]) for l in links] == [("issue", iid)]


def test_set_task_links_empty_clears_legacy_single_column(con, deal_issue_delivery):
    """set_task_linksは後方互換のためtasks.link_type/link_idへ先頭の1件をミラーする。
    空リストで呼べばレガシー単一列も含めて完全にクリアされる。"""
    did, _iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="X", link_type="deal", link_id=did, status="未着手")
    sfa_db.set_task_links(con, tid, [])
    assert sfa_db.get_task_links(con, tid) == []
    row = con.execute("SELECT link_type, link_id FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["link_type"] is None and row["link_id"] is None


def test_get_task_links_merges_legacy_single_column_upsert(con, deal_issue_delivery):
    """#146以前と同じ書き方(upsert_task(..., link_type=, link_id=))で作ったタスクも、
    set_task_linksを一度も呼んでいなくてもget_task_linksで正しく解決できる（後方互換）。"""
    did, _iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="旧方式タスク", link_type="deal", link_id=did, status="未着手")
    links = sfa_db.get_task_links(con, tid)
    assert [(l["link_type"], l["link_id"]) for l in links] == [("deal", did)]


def test_get_task_links_map_bulk_matches_individual(con, deal_issue_delivery):
    did, iid, dv = deal_issue_delivery
    t1 = sfa_db.upsert_task(con, title="A", status="未着手")
    sfa_db.set_task_links(con, t1, [("deal", did), ("issue", iid)])
    t2 = sfa_db.upsert_task(con, title="B", link_type="delivery", link_id=dv, status="未着手")
    t3 = sfa_db.upsert_task(con, title="C", status="未着手")
    m = sfa_db.get_task_links_map(con, [t1, t2, t3])
    assert {(l["link_type"], l["link_id"]) for l in m[t1]} == {("deal", did), ("issue", iid)}
    assert {(l["link_type"], l["link_id"]) for l in m[t2]} == {("delivery", dv)}
    assert t3 not in m


def test_task_link_label_dev_project(con):
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    dp = sfa_db.upsert_dev_project(con, deal_id=did, theme="AI基盤")
    assert sfa_db.task_link_label(con, "dev_project", dp) == "テスト商事：AI基盤"


def test_clear_orphaned_task_links_cleans_task_entity_links_too(con, deal_issue_delivery):
    did, _iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="X", status="未着手")
    sfa_db.set_task_links(con, tid, [("deal", did)])
    sfa_db.delete_deal(con, did)
    assert sfa_db.get_task_links(con, tid) == []
    assert con.execute("SELECT COUNT(*) c FROM task_entity_links WHERE task_id=?",
                       (tid,)).fetchone()["c"] == 0


# ── list_tasks フィルタ ──

def test_list_tasks_filters_by_multi_link(con, deal_issue_delivery):
    did, iid, _dv = deal_issue_delivery
    t1 = sfa_db.upsert_task(con, title="両方紐づけ", status="未着手")
    sfa_db.set_task_links(con, t1, [("deal", did), ("issue", iid)])
    t2 = sfa_db.upsert_task(con, title="論点のみ", status="未着手")
    sfa_db.set_task_links(con, t2, [("issue", iid)])
    t3 = sfa_db.upsert_task(con, title="紐づけなし", status="未着手")

    by_deal = {t["title"] for t in sfa_db.list_tasks(con, link_type="deal", link_id=did)}
    assert by_deal == {"両方紐づけ"}
    by_issue = {t["title"] for t in sfa_db.list_tasks(con, link_type="issue", link_id=iid)}
    assert by_issue == {"両方紐づけ", "論点のみ"}
    none_only = {t["title"] for t in sfa_db.list_tasks(con, link_type="__none__")}
    assert none_only == {"紐づけなし"}


# ── task_link_summary の重複カウント ──

def test_task_link_summary_counts_multi_linked_task_in_both_entries(con, deal_issue_delivery):
    did, iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="両方紐づけ", status="未着手")
    sfa_db.set_task_links(con, tid, [("deal", did), ("issue", iid)])
    summary = sfa_db.task_link_summary(con)
    assert len(summary["deal"]) == 1 and summary["deal"][0]["open_n"] == 1
    assert len(summary["issue"]) == 1 and summary["issue"][0]["open_n"] == 1


# ── /tasks/save ルート(task_form経由・links_json) ──

def test_tasks_save_route_persists_multiple_links_via_links_json(con, deal_issue_delivery, monkeypatch, tmp_path):
    import base64
    import urllib.request
    from http.server import ThreadingHTTPServer

    did, iid, dv = deal_issue_delivery
    con.close()
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con2, name="テスト商事")
    did2 = sfa_db.upsert_deal(con2, account_id=acc, deal_name="案件A", status="open")
    iid2 = sfa_db.upsert_deal_issue(con2, deal_id=did2, issue="論点A")
    con2.close()

    monkeypatch.setattr(webapp, "SFA_BASIC_USER", "u")
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        token = base64.b64encode(b"u:p").decode()
        headers = {"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"}
        links_json = json.dumps([{"type": "deal", "id": did2}, {"type": "issue", "id": iid2}])
        import urllib.parse
        body = urllib.parse.urlencode({
            "title": "複数関連付けタスク", "status": "未着手", "links_json": links_json,
        }).encode()
        req = urllib.request.Request(base + "/tasks/save", data=body, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.getcode() == 200
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT id FROM tasks WHERE title=?", ("複数関連付けタスク",)).fetchone()
    links = sfa_db.get_task_links(con3, row["id"])
    con3.close()
    assert {(l["link_type"], l["link_id"]) for l in links} == {("deal", did2), ("issue", iid2)}


# ── ガント group_by=link の重複表示 ──

def test_gantt_group_by_link_duplicates_multi_linked_task_across_groups(con, deal_issue_delivery):
    did, iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="複数紐づけ", status="未着手", effort_level="中",
                             due_date="2026-09-10")
    sfa_db.set_task_links(con, tid, [("deal", did), ("issue", iid)])
    html = webapp.tasks_gantt_page(con, group_by="link")
    assert html.count("複数紐づけ") >= 2


# ── 直近タスク設計トレイの重複表示 ──

def test_daily_plan_tray_shows_multi_linked_task_in_both_boxes(con, deal_issue_delivery):
    did, iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="複数紐づけタスク", assignee="早瀬", status="未着手")
    sfa_db.set_task_links(con, tid, [("deal", did), ("issue", iid)])
    html = webapp.daily_plan_page(con, assignee="早瀬", picked=[tid])
    box_deal = html.split(f'id="dpBox-deal-{did}"', 1)[1].split("</div>", 1)[0]
    box_issue = html.split(f'id="dpBox-issue-{iid}"', 1)[1].split("</div>", 1)[0]
    assert f'data-task-id="{tid}"' in box_deal
    assert f'data-task-id="{tid}"' in box_issue
    assert "function dpLinkGroupIds" in html
