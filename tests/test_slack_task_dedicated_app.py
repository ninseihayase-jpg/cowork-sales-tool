"""#93: 通常タスク専用Slackアプリ（/slack/task-events）の回帰テスト。

desk-tasks(#事務タスク)が別Slackアプリ(/slack/desk-events, SLACK_DESK_*)で
🎯/📋リアクション・@メンションを専用トークン経由で処理しているのと対になる仕組みを、
通常タスク側にも用意した。handle_mention_task/handle_reactionはtoken引数を通じて
複数Botの切り替えに対応済み（元々token引数はdesk用に汎用実装されていたため、
通常タスク側もtokenを渡すだけで動く）。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, slack_tasks


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_task_app_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_handle_mention_task_passes_token_to_slack_calls(con, monkeypatch):
    """#93: handle_mention_taskにtokenを渡すと、返信(_slack_post)・担当者解決
    (owner_from_slack_user)の両方にそのtokenが伝播すること（通常タスクBot専用トークン）。"""
    posts = []

    def _fake_post(method, **kwargs):
        posts.append((method, kwargs))
        return {"ok": True, "ts": "100.1"}

    resolved_tokens = []

    def _fake_owner(user_id, token=None):
        resolved_tokens.append(token)
        return "早瀬"

    monkeypatch.setattr(slack_tasks, "_slack_post", _fake_post)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", _fake_owner)

    tid = slack_tasks.handle_mention_task(
        con, "C123", "100.0", "資料を金曜までに作成する", "U999", token="xoxb-task-dedicated")

    assert resolved_tokens == ["xoxb-task-dedicated"]
    assert posts, "chat.postMessageが呼ばれていない"
    method, kwargs = posts[0]
    assert method == "chat.postMessage"
    assert kwargs.get("token") == "xoxb-task-dedicated"

    task = sfa_db.get_task(con, tid)
    assert task["assignee"] == "早瀬"
    assert task["slack_channel"] == "C123"


def test_handle_reaction_posts_to_thread_not_ephemeral(con, monkeypatch):
    """#93追加要望: リアクション起票も@メンション起票と同様にスレッド投稿にする
    （以前はchat.postEphemeralでリアクションした本人にしか見えなかった）。
    通常タスク(dart)・事務タスク(clipboard)の両方で確認。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_fetch_message",
                        lambda channel, ts, token=None: {"text": "見積を金曜までに送る", "user": "U_AUTHOR"})
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    slack_tasks.handle_reaction(con, {
        "reaction": "dart", "user": "U_REACTOR",
        "item": {"channel": "C1", "ts": "111.1"},
    })
    method, kwargs = posts[-1]
    assert method == "chat.postMessage"
    assert kwargs.get("thread_ts") == "111.1"
    assert kwargs.get("channel") == "C1"
    assert "user" not in kwargs  # postEphemeral特有のuser指定が残っていない

    posts.clear()
    slack_tasks.handle_reaction(con, {
        "reaction": "clipboard", "user": "U_REACTOR2",
        "item": {"channel": "C2", "ts": "222.2"},
    })
    method2, kwargs2 = posts[-1]
    assert method2 == "chat.postMessage"
    assert kwargs2.get("thread_ts") == "222.2"


def test_handle_mention_task_without_token_falls_back_to_default(con, monkeypatch):
    """token省略時は従来通りNone扱い（＝_slack_post内部でSLACK_TOKENへフォールバック）。
    後方互換の確認（既存のNegoCollection経由の呼び出しを壊さないこと）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: None)

    slack_tasks.handle_mention_task(con, "C1", "1.0", "見積を送る", "U1")

    assert posts[0][1].get("token") is None
