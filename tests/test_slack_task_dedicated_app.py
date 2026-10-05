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


def test_create_task_from_fields_auto_triages_when_assignee_and_due_date_both_present(con):
    """ユーザー報告2026-08-27: Slackで期限指定しても(担当がセットできていても)受信箱にたまる。
    起票時の担当＋期限そろい判定を、渡されたローカル変数ではなく実際に保存された行の
    再読み込み(_task_auto_triage)に一本化。担当・期限どちらも揃っていれば起票直後に
    「未着手」へ自動整理されること。"""
    tid = slack_tasks.create_task_from_fields(
        con, title="資料を金曜までに作成する", assignee="早瀬", due_date="2026-08-28",
        slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["status"] == "未着手"


def test_create_task_from_fields_stays_inbox_when_assignee_missing(con):
    tid = slack_tasks.create_task_from_fields(
        con, title="資料を金曜までに作成する", assignee=None, due_date="2026-08-28",
        slack_channel="C1", slack_ts="100.1")
    assert sfa_db.get_task(con, tid)["status"] == "受信箱"


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
        con, "C123", "100.0", "資料を金曜までに作成する", "U999", token="xoxb-task-dedicated")[0]

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


def test_dart_reaction_reply_includes_effort_level_buttons(con, monkeypatch):
    """#93拡張(ガント): dartリアクション起票の返信に工数感(軽/中/重/超重)ボタンが付くこと。
    3秒制約の起票フローではモーダルを挟まず、後から1クリックで工数感を設定できるように。"""
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
    action_ids = [el["action_id"] for block in kwargs["blocks"] if block.get("type") == "actions"
                  for el in block["elements"]]
    effort_labels = [el["text"]["text"] for block in kwargs["blocks"] if block.get("type") == "actions"
                     for el in block["elements"] if el["action_id"].startswith("task_effort:")]
    assert any(a.startswith("task_effort:") for a in action_ids)
    assert sorted(l.replace("工数感:", "") for l in effort_labels) == sorted(sfa_db.TASK_EFFORT_LEVELS)


def test_mention_task_reply_includes_effort_level_buttons(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    slack_tasks.handle_mention_task(con, "C1", "1.0", "見積を送る", "U1")
    _, kwargs = posts[-1]
    action_ids = [el["action_id"] for block in kwargs["blocks"] if block.get("type") == "actions"
                  for el in block["elements"]]
    assert any(a.startswith("task_effort:") for a in action_ids)


def test_task_effort_button_click_updates_effort_level(con, monkeypatch):
    tid = sfa_db.upsert_task(con, title="ガント確認用タスク")
    monkeypatch.setattr(slack_tasks, "_respond_url", lambda url, text: None)

    slack_tasks._handle_block_action(con, {
        "actions": [{"action_id": f"task_effort:{tid}", "value": "重"}],
        "trigger_id": "", "response_url": "https://example.com/respond",
    })
    assert sfa_db.get_task(con, tid)["effort_level"] == "重"


def test_task_action_block_offers_today_tomorrow_and_3days_later():
    """ユーザー要望2026-08-26で追加した期限クイックボタンを2026-10-05に改訂。
    「+1営業日」「+3営業日」という表記、かつ現在のdue_date起点で営業日を加算する
    実装だったため「期限は10/8でいいですか？に対し+3営業日を押すと10/8からさらに
    3営業日後になる」という混乱を招いた。曖昧さのない「当日/明日/3日後」（常に今日
    起点の暦日加算）に変更した。"""
    block = slack_tasks._task_action_block(123)
    labels = [el["text"]["text"] for el in block["elements"]]
    assert "⏰当日" in labels
    assert "⏰明日" in labels
    assert "⏰3日後" in labels
    action_ids = [el["action_id"] for el in block["elements"]]
    assert len(action_ids) == len(set(action_ids)), f"action_idが重複している: {action_ids}"


def test_task_snooze_today_resets_due_date_regardless_of_current_due(con):
    import datetime
    tid = sfa_db.upsert_task(con, title="X", due_date="2026-08-20")
    responses = []
    slack_tasks._respond_url = lambda url, text: responses.append(text)
    slack_tasks._handle_block_action(con, {
        "actions": [{"action_id": f"task_snooze:{tid}:0", "value": "0"}],
        "trigger_id": "", "response_url": "https://example.com/respond",
    })
    assert sfa_db.get_task(con, tid)["due_date"] == datetime.date.today().isoformat()
    assert "当日" in responses[-1]


def test_task_snooze_tomorrow_is_always_from_today_not_current_due(con):
    """回帰テスト(2026-10-05): 「明日」(value=1)は現在のdue_dateに関係なく常に
    今日+1暦日になること（旧実装は現在のdue_dateを起点に営業日を加算していたため、
    起票時の提案期限から更にずれていくバグがあった）。"""
    import datetime
    tid = sfa_db.upsert_task(con, title="X", due_date="2026-08-24")  # 現在の期限は無関係
    responses = []
    slack_tasks._respond_url = lambda url, text: responses.append(text)
    slack_tasks._handle_block_action(con, {
        "actions": [{"action_id": f"task_snooze:{tid}:1", "value": "1"}],
        "trigger_id": "", "response_url": "https://example.com/respond",
    })
    expected = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    assert sfa_db.get_task(con, tid)["due_date"] == expected
    assert "明日" in responses[-1]


def test_task_snooze_3days_later_is_always_from_today_not_current_due(con):
    """回帰テスト(2026-10-05): 「3日後」(value=3)も同様に常に今日+3暦日。
    営業日計算(add_business_days)は使わない——土日を挟んでも単純な暦日加算。"""
    import datetime
    tid = sfa_db.upsert_task(con, title="X", due_date="2026-08-24")
    responses = []
    slack_tasks._respond_url = lambda url, text: responses.append(text)
    slack_tasks._handle_block_action(con, {
        "actions": [{"action_id": f"task_snooze:{tid}:3", "value": "3"}],
        "trigger_id": "", "response_url": "https://example.com/respond",
    })
    expected = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    assert sfa_db.get_task(con, tid)["due_date"] == expected
    assert "3日後" in responses[-1]


def test_task_effort_block_has_unique_action_ids():
    """回帰テスト(2026-08-24、根本原因確定): 4つの工数感ボタンが全て同じaction_id
    ("task_effort:{tid}")を共有していたため、Slackのchat.postMessageがinvalid_blocksで
    拒否し、タスク自体は作成されるのに確認の返信だけが（無言で）消えていた（2026-08-20の
    導入から気づかれずに残っていたバグ）。ボタンごとにaction_idが一意であることを確認する。"""
    block = slack_tasks._task_effort_block(123)
    action_ids = [el["action_id"] for el in block["elements"]]
    assert len(action_ids) == len(sfa_db.TASK_EFFORT_LEVELS)
    assert len(action_ids) == len(set(action_ids)), f"action_idが重複している: {action_ids}"


def test_task_effort_button_click_still_works_with_new_action_id_format(con, monkeypatch):
    """_task_effort_blockが生成する新形式(task_effort:{tid}:{level})のaction_idでも
    _handle_block_actionが正しくtidと工数感を反映できること（value経由でlevelを取得するため、
    action_id側の追加サフィックスは無視してよい）。"""
    tid = sfa_db.upsert_task(con, title="新形式テスト", effort_level="軽")
    responses = []
    monkeypatch.setattr(slack_tasks, "_respond_url", lambda url, text: responses.append(text))

    block = slack_tasks._task_effort_block(tid)
    button = next(el for el in block["elements"] if el["value"] == "重")
    slack_tasks._handle_block_action(con, {
        "actions": [button], "trigger_id": "", "response_url": "https://example.com/respond",
    })
    assert sfa_db.get_task(con, tid)["effort_level"] == "重"
    assert responses and "重" in responses[0]


def test_task_effort_button_click_rejects_invalid_level(con, monkeypatch):
    tid = sfa_db.upsert_task(con, title="不正値テスト用タスク", effort_level="軽")
    responses = []
    monkeypatch.setattr(slack_tasks, "_respond_url", lambda url, text: responses.append(text))

    slack_tasks._handle_block_action(con, {
        "actions": [{"action_id": f"task_effort:{tid}", "value": "でたらめ"}],
        "trigger_id": "", "response_url": "https://example.com/respond",
    })
    assert sfa_db.get_task(con, tid)["effort_level"] == "軽"  # 変更されない
    assert responses and "不正" in responses[0]


def test_build_create_modal_includes_effort_level_select(con):
    modal = slack_tasks.build_create_modal(con, {}, {})
    block_ids = [b.get("block_id") for b in modal["blocks"]]
    assert "effort_level" in block_ids


def test_handle_mention_task_without_token_falls_back_to_default(con, monkeypatch):
    """token省略時は従来通りNone扱い（＝_slack_post内部でSLACK_TOKENへフォールバック）。
    後方互換の確認（既存のNegoCollection経由の呼び出しを壊さないこと）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: None)

    slack_tasks.handle_mention_task(con, "C1", "1.0", "見積を送る", "U1")

    assert posts[0][1].get("token") is None


def test_handle_mention_task_dms_user_when_reply_post_fails(con, monkeypatch):
    """ユーザー報告2026-08-24: タスク起票は成功するがチャンネルへの返信投稿(chat.postMessage)が
    APIレベルで失敗(例: not_in_channel)した場合、無言で消えず、起票した本人へDMでフォールバック
    通知すること。DMはchannelにユーザーIDを渡すchat.postMessageで送る（notify_task_createdと同じ
    経路）ため、チャンネル固有の理由で失敗していても届く見込みが高い。"""
    posts = []

    def _fake_post(method, **kw):
        posts.append((method, kw))
        if method == "chat.postMessage" and kw.get("channel") == "C1":
            return {"ok": False, "error": "not_in_channel"}
        return {"ok": True}

    monkeypatch.setattr(slack_tasks, "_slack_post", _fake_post)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    tid = slack_tasks.handle_mention_task(con, "C1", "1.0", "見積を送る", "U1", token="xoxb-task")[0]

    dm_posts = [kw for m, kw in posts if m == "chat.postMessage" and kw.get("channel") == "U1"]
    assert dm_posts, "チャンネル投稿失敗時にU1へのDMフォールバックが送られていない"
    assert dm_posts[0].get("token") == "xoxb-task"
    assert "not_in_channel" in dm_posts[0]["text"]
    assert str(tid) in dm_posts[0]["text"] or f"tc-{tid}" in dm_posts[0]["text"]


def test_handle_mention_task_no_dm_fallback_when_reply_succeeds(con, monkeypatch):
    """返信が正常に投稿できた場合はDMフォールバックを送らない（誤検知防止）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    slack_tasks.handle_mention_task(con, "C1", "1.0", "見積を送る", "U1", token="xoxb-task")

    dm_posts = [kw for m, kw in posts if m == "chat.postMessage" and kw.get("channel") == "U1"]
    assert not dm_posts
