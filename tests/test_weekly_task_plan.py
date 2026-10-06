"""週次タスク設計（簡素化版、2026-10）のDB層/ルートテスト。

設計doc: docs/09_直近タスク設計_簡素化_設計構想.md（確定）。
既存の「直近タスク設計」(/tasks/daily-plan、時間軸+GCal連携)とは別の新機能。
「テーマ」は新規マスタではなく既存のタスク紐づけ(task_entity_links/link_type+link_id、
Delivery/商談/社内PJ)をそのまま使う。test_monthly_report.pyと同じ流儀
（ThreadingHTTPServer + 実HTTPリクエスト）で検証する。
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"
KEIEI_EMAIL = "keiei@inproc.org"


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_weekly_plan_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    con = sfa_db.connect(p)
    sfa_db.set_user_role(con, KEIEI_EMAIL, "経営")
    con.close()
    return p


@pytest.fixture
def con(db_path):
    c = sfa_db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def basic_auth_env(monkeypatch):
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
    yield


@pytest.fixture
def server(db_path, basic_auth_env):
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


def _header(email):
    return {"Cookie": f"sfa_session={webapp._make_session_token(email)}"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


# ── DB層: スキーマ・CRUD ──

def test_theme_milestone_crud(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    mid = sfa_db.add_theme_milestone(con, "delivery", dvid, "2026-10-09", "KO")
    ms = sfa_db.list_theme_milestones(con, "delivery", dvid)
    assert len(ms) == 1 and ms[0]["title"] == "KO" and ms[0]["status"] == "未達成"
    sfa_db.update_theme_milestone(con, mid, title="KO準備", status="達成")
    ms2 = sfa_db.list_theme_milestones(con, "delivery", dvid)
    assert ms2[0]["title"] == "KO準備" and ms2[0]["status"] == "達成"
    ms3 = sfa_db.list_theme_milestones(con, "delivery", dvid, include_done=False)
    assert ms3 == []
    sfa_db.delete_theme_milestone(con, mid)
    assert sfa_db.list_theme_milestones(con, "delivery", dvid) == []


def test_theme_milestones_map_bulk(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid1 = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    dvid2 = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    sfa_db.add_theme_milestone(con, "delivery", dvid1, "2026-10-09", "M1")
    out = sfa_db.list_theme_milestones_map(con, [("delivery", dvid1), ("delivery", dvid2)])
    assert len(out[("delivery", dvid1)]) == 1
    assert out[("delivery", dvid2)] == []


def test_task_link_bucket_label_and_sort_key(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="社内業務整備", status="議論中",
                                   company_function="経営企画")
    iid2 = sfa_db.upsert_deal_issue(con, deal_id=did, issue="個別案件PJ", status="議論中")
    assert sfa_db.task_link_bucket_label(con, "delivery", dvid) == "Delivery"
    assert sfa_db.task_link_bucket_label(con, "deal", did) == "商談"
    assert sfa_db.task_link_bucket_label(con, "issue", iid) == "社内PJ（経営企画）"
    assert sfa_db.task_link_bucket_label(con, "issue", iid2) == "社内PJ（商談個別）"
    # 表示順: Delivery(0) < 商談(1) < 社内PJ(2)
    assert sfa_db.task_link_bucket_sort_key(con, "delivery", dvid)[0] == 0
    assert sfa_db.task_link_bucket_sort_key(con, "deal", did)[0] == 1
    assert sfa_db.task_link_bucket_sort_key(con, "issue", iid)[0] == 2


def test_weekly_task_plan_item_placement_crud(con):
    tid = sfa_db.upsert_task(con, title="T1", assignee="早瀬")
    pid = sfa_db.create_weekly_task_plan(con, "早瀬", "通常業務", "2026-10-05")
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    items = sfa_db.list_weekly_task_plan_items(con, pid)
    assert len(items) == 1 and items[0]["day_index"] is None and items[0]["week_offset"] == 0
    sfa_db.set_weekly_task_plan_item_placement(con, pid, tid, week_offset=0, day_index=2)
    items2 = sfa_db.list_weekly_task_plan_items(con, pid)
    assert items2[0]["day_index"] == 2
    # 既に入っているタスクの再追加は何もしない（重複行を作らない、UNIQUE(plan_id,task_id)）
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    assert len(sfa_db.list_weekly_task_plan_items(con, pid)) == 1
    sfa_db.remove_weekly_task_plan_item(con, pid, tid)
    assert sfa_db.list_weekly_task_plan_items(con, pid) == []


# ── ページ関数: テーマグルーピング（複数紐づけの重複表示） ──

def test_board_page_duplicates_multi_linked_task_across_themes(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="社内PJ名", status="議論中",
                                   company_function="経営企画")
    tid = sfa_db.upsert_task(con, title="マルチテーマ", assignee="早瀬")
    sfa_db.set_task_links(con, tid, [("delivery", dvid), ("issue", iid)])
    pid = sfa_db.create_weekly_task_plan(con, "早瀬", "T", "2026-10-05")
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    html = webapp.weekly_task_plan_board_page(con, pid, week_offset=0)
    assert html.count(f'data-task-id="{tid}"') == 2  # Delivery行・社内PJ行の両方に出る
    # ボード画面の行見出しはtask_link_label()の個別表示名（issueのタイトル＋会社機能）。
    # 大分類見出し自体の文字列("社内PJ（経営企画）")はOutput1側にのみ出る(bucket_order)。
    assert "社内PJ名（経営企画）" in html


def test_board_page_flags_unlinked_tasks(con):
    tid = sfa_db.upsert_task(con, title="未紐付", assignee="早瀬")
    pid = sfa_db.create_weekly_task_plan(con, "早瀬", "T", "2026-10-05")
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    html = webapp.weekly_task_plan_board_page(con, pid, week_offset=0)
    assert "テーマに紐づいていないタスク" in html
    assert "未紐付" in html


def test_milestone_beyond_week_goes_to_next_column(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    sfa_db.add_theme_milestone(con, "delivery", dvid, "2026-10-14", "来週MS")  # 月曜(10/5)週の外
    tid = sfa_db.upsert_task(con, title="T", assignee="早瀬")
    sfa_db.set_task_links(con, tid, [("delivery", dvid)])
    pid = sfa_db.create_weekly_task_plan(con, "早瀬", "T", "2026-10-05")
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    html = webapp.weekly_task_plan_board_page(con, pid, week_offset=0)
    assert 'data-col="next"' in html
    assert "来週MS" in html


def test_milestone_chip_onclick_survives_special_characters(con):
    """回帰テスト: マイルストン名に二重引用符/&/'を含むと、onclick属性(json.dumps由来の
    二重引用符をそのまま埋め込んでいた旧実装)がHTML属性境界と衝突し属性が破損していた。
    onclick値全体を_esc()でHTMLエスケープして解決。"""
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    sfa_db.add_theme_milestone(con, "delivery", dvid, "2026-10-09", '客先確認"要" & O\'Brien対応')
    tid = sfa_db.upsert_task(con, title="T", assignee="早瀬")
    sfa_db.set_task_links(con, tid, [("delivery", dvid)])
    pid = sfa_db.create_weekly_task_plan(con, "早瀬", "T", "2026-10-05")
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    html = webapp.weekly_task_plan_board_page(con, pid, week_offset=0)
    import re
    m = re.search(r'onclick="([^"]*wpOpenMilestone[^"]*)"', html)
    assert m, "onclick属性が二重引用符で途中で切れている（エスケープ漏れ）"


def test_output1_and_output2_contents(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    sfa_db.add_theme_milestone(con, "delivery", dvid, "2026-10-09", "KO")
    tid = sfa_db.upsert_task(con, title="見積書作成", assignee="早瀬")
    sfa_db.set_task_links(con, tid, [("delivery", dvid)])
    pid = sfa_db.create_weekly_task_plan(con, "早瀬", "通常業務", "2026-10-05")
    sfa_db.add_weekly_task_plan_item(con, pid, tid)
    html = webapp.weekly_task_plan_output_page(con, pid, week_offset=0)
    assert "Delivery" in html
    assert "10/9 KO" in html  # mm/dd形式、ハイフンでなくスラッシュ・スペースあり
    assert "見積書作成" in html
    assert "Output1" in html and "Output2" in html


# ── HTTPルート ──

def test_weekly_plan_route_access_inherits_tasks_prefix(server, db_path):
    """ROUTE_ACCESSに専用エントリは無いが、/tasksプレフィックスの最長一致により
    経営ロールはfull、外部ロールはhiddenを継承することを確認する（/tasks自体と同じ方針）。"""
    outsider = "partner@inproc.org"
    con = sfa_db.connect(db_path)
    sfa_db.set_user_role(con, outsider, "外部")
    con.close()
    code, _ = _get(server + "/tasks/weekly-plan", headers=_header(outsider))
    assert code == 403
    code, _ = _get(server + "/tasks/weekly-plan", headers=_header(KEIEI_EMAIL))
    assert code == 200


def test_weekly_plan_full_flow_via_http(server, db_path):
    con2 = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con2, name="A社")
    did = sfa_db.upsert_deal(con2, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    tid = sfa_db.upsert_task(con2, title="T1", assignee="早瀬")
    sfa_db.set_task_links(con2, tid, [("delivery", dvid)])
    con2.close()

    keiei = _header(KEIEI_EMAIL)
    code, _ = _get(server + "/tasks/weekly-plan", headers=keiei)
    assert code == 200

    # urlopenは303を自動フォローするため、Locationヘッダではなく実際に作成された行をDBから拾う
    # （test_monthly_report.pyでも踏んだ既知の罠: レスポンスのcodeは常に最終到達先の200になる）。
    code, body, headers = _post(
        server + "/tasks/weekly-plan/new",
        {"owner": "早瀬", "label": "通常業務", "week_start": "2026-10-05"}, headers=keiei)
    assert code in (200, 303)
    con_check = sfa_db.connect(db_path)
    plans = sfa_db.list_weekly_task_plans(con_check, "早瀬")
    assert len(plans) == 1 and plans[0]["label"] == "通常業務"
    plan_id = plans[0]["id"]
    con_check.close()

    code, _, _ = _post(server + f"/tasks/weekly-plan/{plan_id}/add-tasks",
                       {"task_ids": [str(tid)]}, headers=keiei)
    assert code in (200, 303)

    con3 = sfa_db.connect(db_path)
    items = sfa_db.list_weekly_task_plan_items(con3, plan_id)
    assert len(items) == 1
    con3.close()

    code, body, _ = _post(server + f"/tasks/weekly-plan/{plan_id}/place",
                          {"task_id": str(tid), "col": "2", "week_offset": "0"}, headers=keiei)
    assert code == 200
    import json as _json
    assert _json.loads(body)["ok"] is True

    con4 = sfa_db.connect(db_path)
    items2 = sfa_db.list_weekly_task_plan_items(con4, plan_id)
    assert items2[0]["day_index"] == 2
    con4.close()

    code, body, _ = _post(server + f"/tasks/weekly-plan/{plan_id}/milestone/save",
                          {"link_type": "delivery", "link_id": str(dvid),
                           "due_date": "2026-10-09", "title": "KO"}, headers=keiei)
    assert code == 200 and _json.loads(body)["ok"] is True

    con5 = sfa_db.connect(db_path)
    ms = sfa_db.list_theme_milestones(con5, "delivery", dvid)
    assert len(ms) == 1 and ms[0]["title"] == "KO"
    mid = ms[0]["id"]
    con5.close()

    code, _, _ = _post(server + f"/tasks/weekly-plan/{plan_id}/milestone/save",
                       {"milestone_id": str(mid), "link_type": "delivery", "link_id": str(dvid),
                        "due_date": "2026-10-10", "title": "KO準備完了"}, headers=keiei)
    assert code == 200
    con6 = sfa_db.connect(db_path)
    ms2 = sfa_db.list_theme_milestones(con6, "delivery", dvid)
    assert len(ms2) == 1 and ms2[0]["title"] == "KO準備完了"
    con6.close()

    code, body, _ = _post(server + f"/tasks/weekly-plan/{plan_id}/milestone/delete",
                          {"milestone_id": str(mid)}, headers=keiei)
    assert code == 200
    con7 = sfa_db.connect(db_path)
    assert sfa_db.list_theme_milestones(con7, "delivery", dvid) == []
    con7.close()

    code, _ = _get(server + f"/tasks/weekly-plan/{plan_id}/output", headers=keiei)
    assert code == 200

    code, body, _ = _post(server + f"/tasks/weekly-plan/{plan_id}/remove-task",
                          {"task_id": str(tid)}, headers=keiei)
    assert code == 200 and _json.loads(body)["ok"] is True
    con8 = sfa_db.connect(db_path)
    assert sfa_db.list_weekly_task_plan_items(con8, plan_id) == []
    con8.close()

    code, _, headers2 = _post(server + f"/tasks/weekly-plan/{plan_id}/delete", {}, headers=keiei)
    assert code in (200, 303)
    con9 = sfa_db.connect(db_path)
    assert sfa_db.get_weekly_task_plan(con9, plan_id) is None
    con9.close()


def test_weekly_plan_reuses_kanban_picking_ui(server, db_path):
    """2026-10-06ユーザー要望「最初のタスクを選ぶUIはすべて直近タスク設計を流用して」の
    回帰テスト。看板(/tasks)のピック機構がpickmode=weeklyで正しく出し分けられ、
    /tasks/weekly-plan/new(GET)がpicked済みタスクを持つ新規プランを作成しボードへ
    リダイレクトすることを確認する。"""
    con2 = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con2, name="A社")
    did = sfa_db.upsert_deal(con2, account_id=acc, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, start_week="2026-10-05", end_week="2026-10-12")
    tid = sfa_db.upsert_task(con2, title="T1", assignee="早瀬")
    sfa_db.set_task_links(con2, tid, [("delivery", dvid)])
    con2.close()

    keiei = _header(KEIEI_EMAIL)

    # 担当未選択 → ゲートポップアップに「週次タスク設計」ラベルが出る（直近タスク設計と混同しない）
    code, body = _get(server + "/tasks?pick=1&pickmode=weekly", headers=keiei)
    assert code == 200
    html = body.decode()
    assert "🗓️ 週次タスク設計" in html and "dpGatePop" in html

    # 担当選択後 → ピックモードON、pickbarに「次へ（ボードへ）」が出る（直近タスク設計は「仕分けへ」）
    code, body = _get(
        server + "/tasks?assignee=" + urllib.parse.quote("早瀬") + "&pick=1&pickmode=weekly",
        headers=keiei)
    assert code == 200
    html = body.decode()
    assert "次へ（ボードへ）" in html
    assert 'TC_PICK_MODE="weekly"' in html

    # リスト画面の新規作成導線も看板ピックへのリンクに置き換わっている（手打ちフォームは撤去）
    code, body = _get(server + "/tasks/weekly-plan", headers=keiei)
    assert code == 200
    assert "/tasks?pick=1&pickmode=weekly" in body.decode()

    # 看板でチェックしたタスクを引っ提げて新規プラン作成(GET /tasks/weekly-plan/new)
    code, body = _get(
        server + "/tasks/weekly-plan/new?assignee=" + urllib.parse.quote("早瀬") + f"&picked={tid}",
        headers=keiei)
    assert code == 200  # urlopenが303を自動フォロー、最終到達先のボードページが200で返る
    con3 = sfa_db.connect(db_path)
    plans = sfa_db.list_weekly_task_plans(con3, "早瀬")
    assert len(plans) == 1
    items = sfa_db.list_weekly_task_plan_items(con3, plans[0]["id"])
    assert len(items) == 1 and items[0]["task_id"] == tid
    con3.close()

    # assignee/pickedが欠けている場合は一覧へフォールバック(プランを作成しない)
    code, _ = _get(server + "/tasks/weekly-plan/new", headers=keiei)
    assert code == 200
    con4 = sfa_db.connect(db_path)
    assert len(sfa_db.list_weekly_task_plans(con4, "早瀬")) == 1  # 増えていない
    con4.close()
