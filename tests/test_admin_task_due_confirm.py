"""事務タスクの期限確認プロセス（2026-08-27）の回帰テスト。

ユーザー要望: 事務タスクは起票したスレッド内で「期限はxxxでよろしいですか」→
「ok」or「xxx」→確定、SFA更新、というプロセスを経て確実に期限を設定する。
既存の期限クイックボタン（当日/+1営業日/+3営業日）はそのまま維持し、それに加えて
スレッド返信でも確定できるようにする。人間の返信は表記揺れ（オッケー/了解/大丈夫です等、
9/5・来週金曜・明後日等）に耐えられること。
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

from cowork import sfa_db, slack_tasks, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_due_confirm_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_schema_has_due_date_confirmed_column(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
    assert "due_date_confirmed" in cols


# ── #149(2026-09-02): 日付単体の返信が読み取れなかった不具合の回帰テスト ──
# ユーザー報告: 「9/2(本日)」「9/2」という単純な日付返信が2回とも失敗していた。
# 原因はai_extract_task（「次の文から社内タスクを1件抽出し」というタスク抽出前提の
# プロンプト）を日付抽出に流用していたこと。専用の_parse_due_date_replyへ切り出した。

def test_parse_due_date_reply_bare_date(monkeypatch):
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: '{"due_date":"2026-09-02"}')
    assert slack_tasks._parse_due_date_reply("9/2", today="2026-09-02") == "2026-09-02"


def test_parse_due_date_reply_bare_date_with_note(monkeypatch):
    """「9/2(本日)」のような注釈付きの日付返信も読み取れること。"""
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: '{"due_date":"2026-09-02"}')
    assert slack_tasks._parse_due_date_reply("9/2 (本日)", today="2026-09-02") == "2026-09-02"


def test_parse_due_date_reply_returns_empty_on_malformed_response(monkeypatch):
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: "not json")
    assert slack_tasks._parse_due_date_reply("うーん、ちょっと考えます") == ""


def test_parse_due_date_reply_empty_text_returns_empty_without_calling_claude(monkeypatch):
    calls = []
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: (calls.append(prompt), "{}")[1])
    assert slack_tasks._parse_due_date_reply("") == ""
    assert not calls


def test_handle_admin_due_reply_accepts_bare_date_via_parse_due_date_reply(con, monkeypatch):
    """handle_admin_due_replyが実際に_parse_due_date_reply経由で日付返信を確定できること
    （ai_extract_task経由ではなくなったことの結線確認）。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: '{"due_date":"2026-09-02"}')

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "9/2 (本日)"},
        token="xoxb-desk")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-02"
    assert row["due_date_confirmed"] == 1


def test_existing_tasks_default_to_confirmed(con):
    """既存の（通常/事務問わず）タスクはこのフローの対象外＝常に確定扱い（新列の既定値1）。"""
    tid = sfa_db.upsert_task(con, title="普通のタスク")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 1


def test_admin_slack_task_is_created_unconfirmed(con):
    """事務タスクをSlack起票すると、期限(AI抽出or既定値)は提案に過ぎず未確定で作成される。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="請求書作成", is_admin=1, requester="高橋",
        slack_channel="C1", slack_ts="100.1")
    row = sfa_db.get_task(con, tid)
    assert row["due_date"]  # 既定=3営業日後が入っている
    assert row["due_date_confirmed"] == 0


def test_admin_web_task_is_created_confirmed(con):
    """Web手入力（Slackスレッドが無い）は人間が直接期限を入力するので確認フロー不要＝確定扱い。"""
    tid = slack_tasks.create_task_from_fields(con, title="Web手入力タスク", is_admin=1, due_date="2026-09-01")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 1


def test_non_admin_slack_task_is_created_confirmed(con):
    """通常タスク(コンサル)はこのフローの対象外＝常に確定扱い。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="通常タスク", is_admin=0, slack_channel="C1", slack_ts="200.1")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 1


def test_admin_due_context_asks_confirmation_question(con):
    tid = sfa_db.upsert_task(con, title="X", is_admin=1, due_date="2026-09-05")
    block = slack_tasks._admin_due_context(con, tid)
    text = block["elements"][0]["text"]
    assert "2026-09-05" in text
    assert "でよろしいですか" in text
    assert "OK" in text


def test_affirmative_reply_confirms_proposed_date_unchanged(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 0

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])

    for text in ("OK", "オッケーです", "了解", "大丈夫です", "それでお願いします"):
        con.execute("UPDATE tasks SET due_date=?, due_date_confirmed=0 WHERE id=?",
                    ("2026-09-05", tid))
        con.commit()
        posts.clear()
        slack_tasks.handle_admin_due_reply(
            con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": text}, token="xoxb-desk")
        row = sfa_db.get_task(con, tid)
        assert row["due_date"] == "2026-09-05", f"failed for reply text: {text!r}"
        assert row["due_date_confirmed"] == 1, f"failed for reply text: {text!r}"
        assert posts and "設定しました" in posts[-1]["text"]
        assert posts[-1]["token"] == "xoxb-desk"
        assert posts[-1]["thread_ts"] == "100.1"


def test_free_text_date_reply_sets_new_confirmed_date(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_parse_due_date_reply",
                        lambda text, **kw: "2026-09-10")

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "来週木曜でお願いします"},
        token="xoxb-desk")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-10"
    assert row["due_date_confirmed"] == 1
    assert posts and "2026-09-10" in posts[-1]["text"]


def test_unparseable_reply_leaves_unconfirmed_and_asks_again(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_parse_due_date_reply", lambda text, **kw: "")

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "うーん、ちょっと考えます"},
        token="xoxb-desk")

    row = sfa_db.get_task(con, tid)
    assert row["due_date"] == "2026-09-05"     # 変更されない
    assert row["due_date_confirmed"] == 0       # 未確定のまま
    assert posts and "読み取れません" in posts[-1]["text"]


def test_reply_ignored_when_no_pending_task(con, monkeypatch):
    """関連する未確定タスクが無いスレッドへの返信では何も起きない（無関係な雑談に反応しない）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])

    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C9", "ts": "999.1", "thread_ts": "998.1", "text": "OK"}, token="xoxb-desk")

    assert not posts


def test_reply_ignored_when_task_already_confirmed(con, monkeypatch):
    tid = slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")
    con.execute("UPDATE tasks SET due_date_confirmed=1 WHERE id=?", (tid,))
    con.commit()

    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "150.1", "thread_ts": "100.1", "text": "9/20でお願いします"},
        token="xoxb-desk")

    assert not posts   # 既に確定済みなので反応しない（誤って再確認・再変更しない）
    assert sfa_db.get_task(con, tid)["due_date"] == "2026-09-05"


def test_top_level_message_is_not_treated_as_a_reply(con, monkeypatch):
    """thread_ts==ts（スレッド内の返信ではなく新規投稿そのもの）は無視する。"""
    slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append(kw), {"ok": True})[1])
    slack_tasks.handle_admin_due_reply(
        con, {"channel": "C1", "ts": "100.1", "thread_ts": "100.1", "text": "OK"}, token="xoxb-desk")
    assert not posts


def test_task_snooze_button_click_also_confirms_due_date(con):
    """既存の期限クイックボタン（当日/+1営業日/+3営業日）はそのまま維持しつつ、
    ボタンでの明示的な期限設定も確定扱いにする。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="X", is_admin=1, due_date="2026-09-05", slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 0
    slack_tasks._respond_url = lambda url, text: None
    slack_tasks._handle_block_action(con, {
        "actions": [{"action_id": f"task_snooze:{tid}:1", "value": "1"}],
        "trigger_id": "", "response_url": "https://example.com/respond",
    })
    assert sfa_db.get_task(con, tid)["due_date_confirmed"] == 1


# ── /task/{id}/field 経由での直接編集も確定扱いになること ─────────────────

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


def test_field_route_due_date_edit_marks_confirmed(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X", is_admin=1, due_date="2026-09-05")
    con.execute("UPDATE tasks SET due_date_confirmed=0 WHERE id=?", (tid,))
    con.commit()
    con.close()

    code, body = _post(base + f"/task/{tid}/field", {"field": "due_date", "value": "2026-09-12"},
                       headers=_auth_header())
    assert code in (200, 303)

    con = sfa_db.connect(db_path)
    row = sfa_db.get_task(con, tid)
    con.close()
    assert row["due_date"] == "2026-09-12"
    assert row["due_date_confirmed"] == 1
