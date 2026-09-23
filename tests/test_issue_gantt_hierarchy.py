"""社内PJ管理ガント(deal_issues_gantt_page)のタスク2階層化＋インライン追加(2026-09-11)の回帰テスト。

ユーザー要望:
1. タスクは2階層（メインタスク／サブタスク）。サブタスク追加でメインタスクに紐づき、
   メインタスクは子を持つ場合に折りたたみができる。
2. サブタスクの期間がメインタスクの期間よりはみ出す場合、メインタスクを自動延伸する
   （縮める方向へは動かず、メインタスクの手動延伸も維持される）。
3. 「＋」は分散配置——社内PJ行の「＋」はメインタスク追加、メインタスク行右側の「＋」は
   そのサブタスク追加。
4. タスク追加はフローティングのポップアップではなく、「＋」クリックで直下の予約行に
   タスク名/概要/期間の3項目が現れ、Enterで次項目→最後の項目のEnterで送信する。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import base64
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


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_issue_gantt_hier_")
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
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
    monkeypatch.setattr(webapp, "_parse_issue_period_text", lambda text: ("2026-09-10", "2026-09-20"))
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", db_path
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _auth_header():
    return {"Cookie": f"sfa_session={webapp._make_session_token()}"}


def _post(url, data: dict):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=_auth_header(), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.geturl(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, url, e.read()


def _issue(con, acc_name="A社", deal_name="X", issue="論点A"):
    acc = sfa_db.upsert_account(con, name=acc_name)
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name=deal_name, status="open")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue=issue)


# ── init_db()マイグレーション: 本番再現 ──

def test_init_db_migrates_legacy_deal_issue_subitems_without_parent_id():
    """本番再現(2026-09-11): parent_id列の無い旧deal_issue_subitemsに対しinit_dbが失敗せず
    列＋索引を追加すること。SCHEMA側のCREATE TABLE IF NOT EXISTSは既存テーブルがあるとno-opに
    なる一方、直後のCREATE INDEX ... ON deal_issue_subitems(parent_id)は無条件実行されるため、
    ALTER TABLEでparent_idを追加する後方互換マイグレーションより前に走ると
    `sqlite3.OperationalError: no such column: parent_id` で本番のinit_db自体が落ちる
    （実際に本番デプロイで発生した回帰）。"""
    import sqlite3
    d = tempfile.mkdtemp(prefix="sfa_ig_legacy_")
    try:
        path = str(Path(d) / "t.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE deal_issues(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "deal_id INTEGER, issue TEXT NOT NULL, members TEXT)")
        con.execute(
            "CREATE TABLE deal_issue_subitems(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "issue_id INTEGER NOT NULL, title TEXT NOT NULL, start_date TEXT, end_date TEXT, "
            "sort_order INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))")
        con.execute("INSERT INTO deal_issues(id, deal_id, issue) VALUES (1, NULL, '旧PJ')")
        con.execute(
            "INSERT INTO deal_issue_subitems(issue_id, title, start_date, end_date) "
            "VALUES (1, '旧タスク', '2026-09-01', '2026-09-05')")
        con.commit()
        con.close()

        sfa_db.init_db(path)   # 例外なく完了すること
        sfa_db.init_db(path)   # 冪等

        con2 = sfa_db.connect(path)
        cols = {r[1] for r in con2.execute("PRAGMA table_info(deal_issue_subitems)")}
        assert "parent_id" in cols
        idx = {r[0] for r in con2.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_deal_issue_subitems_parent" in idx
        row = con2.execute("SELECT title, parent_id FROM deal_issue_subitems WHERE issue_id=1").fetchone()
        assert row[0] == "旧タスク"
        assert row[1] is None
        con2.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── sfa_db層: parent_id CRUD ──

def test_create_subitem_with_parent_id(con):
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sub_id = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-15",
                                              parent_id=main_id)
    row = sfa_db.get_deal_issue_subitem(con, sub_id)
    assert row["parent_id"] == main_id


def test_create_subitem_without_parent_id_defaults_to_none(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["parent_id"] is None


def test_deleting_main_task_cascades_to_children(con):
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sub_id = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-15",
                                              parent_id=main_id)
    sfa_db.delete_deal_issue_subitem(con, main_id)
    assert sfa_db.get_deal_issue_subitem(con, sub_id) is None


# ── sync_subitem_parent_range: 自動延伸ルール ──

def test_child_end_date_beyond_parent_extends_parent_end(con):
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-25",
                                     parent_id=main_id)
    parent = sfa_db.get_deal_issue_subitem(con, main_id)
    assert parent["end_date"] == "2026-09-25"
    assert parent["start_date"] == "2026-09-10"  # 開始日は変わらない


def test_child_start_date_before_parent_extends_parent_start(con):
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-05", "2026-09-15",
                                     parent_id=main_id)
    parent = sfa_db.get_deal_issue_subitem(con, main_id)
    assert parent["start_date"] == "2026-09-05"
    assert parent["end_date"] == "2026-09-20"


def test_child_fully_within_parent_range_does_not_change_parent(con):
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-15",
                                     parent_id=main_id)
    parent = sfa_db.get_deal_issue_subitem(con, main_id)
    assert parent["start_date"] == "2026-09-10"
    assert parent["end_date"] == "2026-09-20"


def test_manual_parent_extension_beyond_children_is_not_reverted(con):
    """メインタスクを手動でサブタスクより長くすることはできる（縮める方向へは自動で動かない）。"""
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sub_id = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-15",
                                              parent_id=main_id)
    sfa_db.update_deal_issue_subitem(con, main_id, end_date="2026-09-30")
    parent = sfa_db.get_deal_issue_subitem(con, main_id)
    assert parent["end_date"] == "2026-09-30"
    # サブタスク側の更新をもう一度流しても、手動延伸した終了日を縮めない
    sfa_db.update_deal_issue_subitem(con, sub_id, end_date="2026-09-16")
    parent2 = sfa_db.get_deal_issue_subitem(con, main_id)
    assert parent2["end_date"] == "2026-09-30"


def test_updating_child_dates_triggers_parent_sync(con):
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sub_id = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-15",
                                              parent_id=main_id)
    sfa_db.update_deal_issue_subitem(con, sub_id, end_date="2026-09-28")
    parent = sfa_db.get_deal_issue_subitem(con, main_id)
    assert parent["end_date"] == "2026-09-28"


def test_sync_subitem_parent_range_noop_for_top_level_task(con):
    """parent_idを持たない（メインタスク自身の）呼び出しは何もしない。"""
    iid = _issue(con)
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sfa_db.sync_subitem_parent_range(con, main_id)  # 例外なく完了すること
    row = sfa_db.get_deal_issue_subitem(con, main_id)
    assert row["start_date"] == "2026-09-10" and row["end_date"] == "2026-09-20"


# ── /deal-issue-subitem/new ルート: parent_idの防御的検証 ──

def test_route_creates_subtask_with_valid_parent(server):
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点X")
    main_id = sfa_db.create_deal_issue_subitem(con2, iid, "メイン1", "2026-09-01", "2026-09-05")
    con2.close()

    _post(f"{base}/deal-issue-subitem/new",
         {"issue_id": iid, "parent_id": main_id, "title": "サブ新規", "period_text": "来週から3週間"})

    con3 = sfa_db.connect(db_path)
    rows = [r for r in sfa_db.list_deal_issue_subitems(con3, iid) if r["title"] == "サブ新規"]
    assert len(rows) == 1
    assert rows[0]["parent_id"] == main_id


def test_route_rejects_parent_from_different_issue(server):
    """issue_idをまたいだ親子付けは拒否し、トップレベルタスクとして作成すること。"""
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid1 = _issue(con2, issue="論点A")
    iid2 = _issue(con2, issue="論点B")
    other_main_id = sfa_db.create_deal_issue_subitem(con2, iid2, "他PJのメイン", "2026-09-01", "2026-09-05")
    con2.close()

    _post(f"{base}/deal-issue-subitem/new",
         {"issue_id": iid1, "parent_id": other_main_id, "title": "誤った子", "period_text": "来週から3週間"})

    con3 = sfa_db.connect(db_path)
    rows = [r for r in sfa_db.list_deal_issue_subitems(con3, iid1) if r["title"] == "誤った子"]
    assert len(rows) == 1
    assert rows[0]["parent_id"] is None  # 親子付けは無視され、トップレベルとして作られる


def test_route_rejects_grandparent_relationship(server):
    """サブタスク自身を親に指定した場合は拒否し、3階層化を防ぐこと。"""
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点C")
    main_id = sfa_db.create_deal_issue_subitem(con2, iid, "メイン1", "2026-09-01", "2026-09-05")
    sub_id = sfa_db.create_deal_issue_subitem(con2, iid, "サブ1", "2026-09-02", "2026-09-03",
                                              parent_id=main_id)
    con2.close()

    _post(f"{base}/deal-issue-subitem/new",
         {"issue_id": iid, "parent_id": sub_id, "title": "孫タスク", "period_text": "来週から3週間"})

    con3 = sfa_db.connect(db_path)
    rows = [r for r in sfa_db.list_deal_issue_subitems(con3, iid) if r["title"] == "孫タスク"]
    assert len(rows) == 1
    assert rows[0]["parent_id"] is None  # 孫階層は作られず、トップレベル扱いになる


def test_route_without_parent_id_still_creates_top_level_task(server):
    """既存の呼び出し（parent_id未指定）は従来通りメインタスクとして作成されること（後方互換）。"""
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点D")
    con2.close()

    _post(f"{base}/deal-issue-subitem/new",
         {"issue_id": iid, "title": "旧来通りのタスク", "period_text": "来週から3週間"})

    con3 = sfa_db.connect(db_path)
    rows = sfa_db.list_deal_issue_subitems(con3, iid)
    assert len(rows) == 1
    assert rows[0]["parent_id"] is None


# ── UI描画: 階層・折りたたみ・インライン追加 ──

def test_gantt_page_renders_main_and_sub_task_add_triggers(con):
    iid = _issue(con, issue="論点E")
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メインタスク1", "2026-09-10", "2026-09-20")
    html = webapp.deal_issues_gantt_page(con)
    # 社内PJ行の「＋」＝メインタスク追加
    assert f"igShowInlineAdd('ig-mainadd-{iid}')" in html
    assert f'id="ig-mainadd-{iid}"' in html
    assert f'data-issue-id="{iid}" data-parent-id=""' in html
    # メインタスク行右側の「＋」＝サブタスク追加
    assert f"igShowInlineAdd('ig-subadd-{main_id}')" in html
    assert f'id="ig-subadd-{main_id}"' in html
    assert f'data-parent-id="{main_id}"' in html


def test_gantt_page_inline_add_rows_hidden_by_default(con):
    iid = _issue(con, issue="論点F")
    html = webapp.deal_issues_gantt_page(con)
    assert 'class="ig-add-wrap"' in html
    assert "display:none" in html.split('class="ig-add-wrap"')[1][:200]


def test_gantt_page_collapse_toggle_present_only_when_has_children(con):
    iid = _issue(con, issue="論点G")
    main_with_child = sfa_db.create_deal_issue_subitem(con, iid, "子ありメイン", "2026-09-10", "2026-09-20")
    main_without_child = sfa_db.create_deal_issue_subitem(con, iid, "子なしメイン", "2026-09-10", "2026-09-20")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-14",
                                     parent_id=main_with_child)
    html = webapp.deal_issues_gantt_page(con)
    assert f"igToggleChildren({main_with_child},this)" in html
    assert f"igToggleChildren({main_without_child},this)" not in html


def test_gantt_page_sub_task_rows_tagged_with_parent_task(con):
    iid = _issue(con, issue="論点H")
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メイン1", "2026-09-10", "2026-09-20")
    sub_id = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-14",
                                              parent_id=main_id)
    html = webapp.deal_issues_gantt_page(con)
    assert f'data-parent-task="{main_id}"' in html
    assert f'data-iid="{sub_id}"' in html


def test_creating_subtask_under_dateless_parent_auto_fills_parent_dates(con):
    """親（メインタスク）がまだ日程未設定の状態でサブタスクを追加すると、
    sync_subitem_parent_rangeにより親も自動的に子の日程を引き継いで「ready」になる
    （NULLは「はみ出し」判定で常に更新対象になるため）。結果としてUI上は
    孤立した未確定サブタスクという状態は生まれず、通常のメイン/サブ階層として描画される。"""
    iid = _issue(con, issue="論点I")
    parent_id = sfa_db.create_deal_issue_subitem(con, iid, "未確定メイン")  # 日程なし
    child_id = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-12", "2026-09-14",
                                                parent_id=parent_id)
    parent = sfa_db.get_deal_issue_subitem(con, parent_id)
    assert parent["start_date"] == "2026-09-12"
    assert parent["end_date"] == "2026-09-14"
    html = webapp.deal_issues_gantt_page(con)
    assert f'data-iid="{child_id}"' in html
    assert f'data-parent-task="{parent_id}"' in html


def test_gantt_page_no_old_popup_add_step_functions(con):
    """旧ポップアップ方式のJS関数は廃止され、残っていないこと
    （2026-09-11要望: 「フローティングされた画面で入力するUIはやめたい」）。"""
    _issue(con, issue="論点J")
    html = webapp.deal_issues_gantt_page(con)
    assert "igOpenAddStep" not in html
    assert "igAddStepHtml" not in html
    assert "closeIgAddStep" not in html
    assert "igSubmitAddStep" not in html
    # 既存タスクの編集ポップアップは維持されている
    assert "igOpenItem" in html
    assert "igPopHtml" in html


def test_gantt_page_new_inline_add_js_functions_present(con):
    _issue(con, issue="論点K")
    html = webapp.deal_issues_gantt_page(con)
    for fn in ("igShowInlineAdd", "igHideInlineAdd", "igSubmitInlineAdd", "igToggleChildren"):
        assert f"function {fn}(" in html
