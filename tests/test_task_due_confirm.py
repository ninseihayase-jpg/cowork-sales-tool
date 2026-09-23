"""TaskBot（通常/コンサルタスク）への期限確認プロセス追加(2026-09-14)の回帰テスト。

ユーザー要望:「Taskbotにも、Opebotと同じ、期日確認機能を追加」。事務タスク(OpeBot)には
既に「期限はXXXでよろしいですか？」→「OK」or自由文の期限→確定、という期限確認フロー
（tests/test_admin_task_due_confirm.py参照）があったが、通常タスク(TaskBot)には無かった。

実装方針: create_task_from_fieldsにconfirm_due(bool)を追加し、TaskBotの3つの起票経路
（handle_reaction/handle_mention_task/_finalize_normal_tasks）だけがconfirm_due=Trueを
渡す。既存の_admin_due_context/_admin_default_due（事務タスク専用だった）をそのまま流用し、
handle_admin_due_reply（返信処理）はis_adminによる絞り込みを外して両方のBotで共通化した。
/taskモーダル送信等、confirm_due=Falseの通常タスク作成経路は従来通り確認フローの対象外
（tests/test_admin_task_due_confirm.py::test_non_admin_slack_task_is_created_confirmedで
既に固定化済み・本ファイルでは変更しない）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, slack_tasks


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_task_due_confirm_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _fake_claude_json_array(items):
    return lambda prompt: json.dumps(items, ensure_ascii=False)


def _due_block(kwargs):
    """chat.postMessage呼び出しkwargsから期限確認ブロック（"でよろしいですか"を含む
    sectionブロック）を1つ取り出す。無ければNone。"""
    for block in kwargs["blocks"]:
        if block.get("type") == "section" and "でよろしいですか" in block.get("text", {}).get("text", ""):
            return block
    return None


# ── create_task_from_fields: confirm_due ──

def test_confirm_due_true_fills_default_due_date_when_missing(con):
    tid = slack_tasks.create_task_from_fields(
        con, title="X", confirm_due=True, slack_channel="C1", slack_ts="100.1")
    row = sfa_db.get_task(con, tid)
    assert row["due_date"]  # 既定=3営業日後が入っている
    assert row["due_date_confirmed"] == 0


def test_confirm_due_true_keeps_ai_extracted_due_date_but_still_unconfirmed(con):
    """AIが期限を抽出できていても、confirm_due=Trueなら提案に過ぎず未確定のまま。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="X", due_date="2026-09-20", confirm_due=True,
        slack_channel="C1", slack_ts="100.1")
    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-20"
    assert row["due_date_confirmed"] == 0


def test_confirm_due_false_default_stays_confirmed(con):
    """confirm_due未指定(既定False)の通常タスクは従来通り確認フロー対象外
    （test_admin_task_due_confirm.pyの既存テストと同じ前提の再確認）。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="X", slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 1


def test_confirm_due_true_without_slack_thread_stays_confirmed(con):
    """Slackスレッドが無い（channel/ts無し）場合は確認フローの前提が成立しないため確定扱い。"""
    tid = slack_tasks.create_task_from_fields(con, title="X", confirm_due=True)
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 1


# ── handle_reaction（🎯dartリアクション）に期限確認ブロックが付くこと ──

def test_dart_reaction_reply_includes_due_confirmation_block(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_fetch_message",
                        lambda channel, ts, token=None: {"text": "資料を作る", "user": "U_AUTHOR"})
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    slack_tasks.handle_reaction(con, {
        "reaction": "dart", "user": "U_REACTOR", "item": {"channel": "C1", "ts": "111.1"},
    })
    _, kwargs = posts[-1]
    block = _due_block(kwargs)
    assert block is not None
    assert block["text"]["text"].startswith("<@U_REACTOR> ")

    tid = sfa_db.list_tasks(con, admin=False)[0]["id"]
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 0


# ── handle_mention_task（@メンション起票）に期限確認ブロックが付くこと ──

def test_mention_task_reply_includes_due_confirmation_block(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    tids = slack_tasks.handle_mention_task(con, "C1", "1.0", "見積を送る", "U1")
    _, kwargs = posts[-1]
    block = _due_block(kwargs)
    assert block is not None
    assert block["text"]["text"].startswith("<@U1> ")
    assert sfa_db.get_task(con, tids[0])["due_date_confirmed"] == 0


# ── _finalize_normal_tasks（分割確認の決着後）に期限確認ブロックが付くこと ──

def test_finalize_normal_tasks_single_includes_due_confirmation(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "見積を送る", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_mention_task(con, "C1", "500.0", "見積を送る", "U1")
    _, kwargs = posts[-1]
    block = _due_block(kwargs)
    assert block is not None

    tid = sfa_db.list_tasks(con, admin=False)[0]["id"]
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 0


def test_finalize_normal_tasks_multi_mentions_only_first_task(con, monkeypatch):
    """複数件に分割した場合、このスレッドへの「OK」返信は元tsをそのまま保つ1件目にしか
    ヒットしないため、メンションは1件目の期限確認ブロックにのみ付ける
    （事務タスクの_finalize_admin_tasksと同じ扱い）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "タスクA", "next_action": "", "due_date": "", "category": ""},
        {"title": "タスクB", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_mention_task(con, "C1", "600.0", "・タスクA\n・タスクB", "U1")
    split_kwargs = posts[0][1]
    action_id = next(
        el["action_id"] for block in split_kwargs["blocks"] if block.get("type") == "actions"
        for el in block["elements"] if el["action_id"].startswith("task_split_yes:"))
    split_id = int(action_id.split(":", 1)[1])
    posts.clear()
    slack_tasks._handle_split_decision(con, split_id, "yes")

    _, kwargs = posts[-1]
    due_blocks = [b for b in kwargs["blocks"]
                  if b.get("type") == "section" and "でよろしいですか" in b.get("text", {}).get("text", "")]
    assert len(due_blocks) == 2  # 各タスクに1つずつ
    assert due_blocks[0]["text"]["text"].startswith("<@U1> ")
    assert "<@" not in due_blocks[1]["text"]["text"]

    tasks = sfa_db.list_tasks(con, admin=False)
    assert len(tasks) == 2
    assert all(t["due_date_confirmed"] == 0 for t in tasks)


# ── handle_admin_due_reply がis_admin問わず動くこと（TaskBot側） ──

def test_handle_admin_due_reply_confirms_non_admin_task_on_affirmative(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="見積を送る", due_date="2026-09-20", confirm_due=True,
        slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 0

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "OK"}, token="xoxb-task")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-20"
    assert row["due_date_confirmed"] == 1
    assert posts and "設定しました" in posts[-1]["text"]
    assert posts[-1]["token"] == "xoxb-task"


def test_handle_admin_due_reply_confirms_non_admin_task_with_free_text_date(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="見積を送る", due_date="2026-09-20", confirm_due=True,
        slack_channel="C1", slack_ts="100.1")

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_parse_due_date_reply", lambda text, **kw: "2026-09-25")

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "9/25でお願いします"},
        token="xoxb-task")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-25"
    assert row["due_date_confirmed"] == 1


def test_handle_admin_due_reply_still_works_for_admin_tasks(con, monkeypatch):
    """is_admin絞り込みを外した後も、事務タスク側の既存挙動が壊れていないこと（回帰）。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="請求書作成", is_admin=1, due_date="2026-09-05",
        slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 0

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "OK"}, token="xoxb-desk")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-05"
    assert row["due_date_confirmed"] == 1


def test_handle_admin_due_reply_unparseable_reply_leaves_non_admin_task_unconfirmed(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="見積を送る", due_date="2026-09-20", confirm_due=True,
        slack_channel="C1", slack_ts="100.1")

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_parse_due_date_reply", lambda text, **kw: "")

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "うーん"}, token="xoxb-task")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-20"       # 変更されない
    assert row["due_date_confirmed"] == 0         # 未確定のまま
    assert posts and "読み取れません" in posts[-1]["text"]


# ── /task/{id}/field 経由の直接編集も確定扱いになること（通常タスクでも既存動作の確認） ──

def test_field_route_due_date_edit_marks_non_admin_task_confirmed(monkeypatch, tmp_path):
    from cowork import webapp
    import base64
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading

    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", "u")
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        con2 = sfa_db.connect(db_path)
        tid = slack_tasks.create_task_from_fields(
            con2, title="X", due_date="2026-09-20", confirm_due=True,
            slack_channel="C1", slack_ts="100.1")
        con2.close()

        body = urllib.parse.urlencode({"field": "due_date", "value": "2026-09-25"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/task/{tid}/field", data=body,
            headers={"Cookie": f"sfa_session={webapp._make_session_token()}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.getcode() in (200, 303)

        con3 = sfa_db.connect(db_path)
        row = sfa_db.get_task(con3, tid)
        con3.close()
        assert row["due_date"] == "2026-09-25"
        assert row["due_date_confirmed"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
