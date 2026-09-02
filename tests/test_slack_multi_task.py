"""#148/#151(2026-09-02): TaskBotの複数タスク分割まわりの回帰テスト。

#148: 「・要件詰め案件の棚卸、注力案件の掘り起こし・特定」「・開発案件の定義書FMTを整備」
という2行の箇条書きをメンションしたら、1件のタスク（両方を要約統合したタイトル）しか
起票されなかった。原因は`ai_extract_task`が「1件抽出」前提のプロンプト・単一dict戻り値
だったこと。`ai_extract_tasks`（複数形）を新設し、箇条書き/改行ごとに複数タスクへ分割する。

#151: その後、「テレンプのAP調達未来像+デモ+ステップ+想定DB整理」のような、本来1件の
依頼が「+」区切りで4件に無確認で分割されてしまうという報告があった。分割できる場合は
必ずボタンで「分割する/1件のまま登録する」を確認するステップを挟むように変更
（pending_task_splitsテーブルに一時保存→ボタン押下で確定）。
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
    d = tempfile.mkdtemp(prefix="sfa_slack_multi_task_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


BULLET_TEXT = (
    "・要件詰め案件の棚卸、注力案件の掘り起こし・特定\n"
    "・開発案件の定義書FMTを整備"
)


def _fake_claude_json_array(items):
    return lambda prompt: json.dumps(items, ensure_ascii=False)


def _split_action_id(kwargs, decision="yes"):
    """確認メッセージ(post kwargs)の中から「分割する/1件のまま登録」ボタンのaction_idを
    取り出す（#151）。"""
    prefix = f"task_split_{decision}:"
    for block in kwargs["blocks"]:
        if block.get("type") != "actions":
            continue
        for el in block["elements"]:
            if el["action_id"].startswith(prefix):
                return el["action_id"]
    raise AssertionError(f"{prefix}... のボタンが見つからない")


def _click_split_button(con, kwargs, decision="yes"):
    """確認メッセージへのボタン押下をシミュレートする（#151）。"""
    action_id = _split_action_id(kwargs, decision)
    split_id = int(action_id.split(":", 1)[1])
    slack_tasks._handle_split_decision(con, split_id, decision)


# ── ai_extract_tasks（AI抽出の複数形） ──

def test_ai_extract_tasks_returns_one_item_per_bullet(monkeypatch):
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "要件詰め案件の棚卸と注力案件の掘り起こし・特定", "next_action": "",
         "due_date": "", "category": ""},
        {"title": "開発案件の定義書FMTを整備", "next_action": "", "due_date": "", "category": ""},
    ]))
    tasks = slack_tasks.ai_extract_tasks(BULLET_TEXT)
    assert len(tasks) == 2
    assert tasks[0]["title"] == "要件詰め案件の棚卸と注力案件の掘り起こし・特定"
    assert tasks[1]["title"] == "開発案件の定義書FMTを整備"


def test_ai_extract_tasks_single_sentence_returns_one_item(monkeypatch):
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "見積を送る", "next_action": "", "due_date": "", "category": ""},
    ]))
    tasks = slack_tasks.ai_extract_tasks("見積を送る")
    assert len(tasks) == 1
    assert tasks[0]["title"] == "見積を送る"


def test_ai_extract_tasks_falls_back_to_bullet_split_on_malformed_ai_response(monkeypatch):
    """AIがJSON配列を返せなかった場合、箇条書き行ごとに機械的に分割する
    フォールバック(_fallback_split_tasks)が効くこと。"""
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: "not json")
    tasks = slack_tasks.ai_extract_tasks(BULLET_TEXT)
    assert len(tasks) == 2
    assert tasks[0]["title"].startswith("要件詰め案件の棚卸")
    assert tasks[1]["title"] == "開発案件の定義書FMTを整備"


def test_ai_extract_tasks_falls_back_to_whole_text_when_no_bullets_and_ai_fails(monkeypatch):
    monkeypatch.setattr(slack_tasks, "_call_claude", lambda prompt: "not json")
    tasks = slack_tasks.ai_extract_tasks("急ぎで見積を送ってください")
    assert len(tasks) == 1
    assert tasks[0]["title"] == "急ぎで見積を送ってください"


def test_ai_extract_tasks_forces_billing_category_per_item(monkeypatch):
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "請求書を発行する", "next_action": "", "due_date": "", "category": "総務"},
        {"title": "会議室を予約する", "next_action": "", "due_date": "", "category": "総務"},
    ]))
    tasks = slack_tasks.ai_extract_tasks(
        "・請求書を発行する\n・会議室を予約する", categories=sfa_db.ADMIN_TASK_CATEGORIES)
    assert tasks[0]["category"] == "経費・請求"
    assert tasks[1]["category"] != "経費・請求"


# ── handle_mention_task（コンサルタスクBot、通常#146の対象） ──

def test_handle_mention_task_asks_before_splitting_into_multiple_tasks(con, monkeypatch):
    """#151(2026-09-02): 複数タスクに分割できる場合、無確認では作成しない。まず
    「分割する/1件のまま登録する」ボタン付きの確認メッセージだけを投稿し、タスクは
    1件も作らない（ユーザー報告: 「＋」区切りの1行が無確認で4件に分割されてしまった）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "要件詰め案件の棚卸と注力案件の掘り起こし・特定", "next_action": "",
         "due_date": "", "category": ""},
        {"title": "開発案件の定義書FMTを整備", "next_action": "", "due_date": "", "category": ""},
    ]))

    tids = slack_tasks.handle_mention_task(con, "C1", "100.0", BULLET_TEXT, "U1")

    assert tids == []
    assert sfa_db.list_tasks(con, admin=False) == []
    assert len(posts) == 1
    _, kwargs = posts[0]
    assert "2件" in kwargs["text"]
    assert "分割しますか" in kwargs["blocks"][0]["text"]["text"]
    assert _split_action_id(kwargs, "yes")
    assert _split_action_id(kwargs, "no")


def test_handle_mention_task_confirm_yes_creates_multiple_tasks(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "要件詰め案件の棚卸と注力案件の掘り起こし・特定", "next_action": "",
         "due_date": "", "category": ""},
        {"title": "開発案件の定義書FMTを整備", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_mention_task(con, "C1", "100.0", BULLET_TEXT, "U1")
    confirm_kwargs = posts[0][1]
    posts.clear()
    _click_split_button(con, confirm_kwargs, "yes")

    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2
    titles = {t["title"] for t in all_tasks}
    assert titles == {"要件詰め案件の棚卸と注力案件の掘り起こし・特定", "開発案件の定義書FMTを整備"}
    for t in all_tasks:
        assert t["assignee"] == "早瀬"
    # 確定後の返信は1回のchat.postMessageにまとまっている
    assert len(posts) == 1
    _, kwargs = posts[0]
    assert "2件" in kwargs["text"]
    action_ids = [el["action_id"] for block in kwargs["blocks"] if block.get("type") == "actions"
                  for el in block["elements"]]
    assert sum(1 for a in action_ids if a.startswith("task_done:")) == 2


def test_handle_mention_task_confirm_no_creates_single_consolidated_task(con, monkeypatch):
    """「1件のまま登録」を選ぶと、元の本文全体をai_extract_task(単数形)で1件に
    再抽出して登録する。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")

    def _fake_call_claude(prompt):
        if "JSON配列" in prompt:
            return json.dumps([
                {"title": "要件詰め案件の棚卸と注力案件の掘り起こし・特定", "next_action": "",
                 "due_date": "", "category": ""},
                {"title": "開発案件の定義書FMTを整備", "next_action": "", "due_date": "", "category": ""},
            ], ensure_ascii=False)
        return json.dumps({"title": "テレンプのAP調達未来像+デモ+ステップ+想定DB整理",
                           "next_action": "", "due_date": "", "category": ""}, ensure_ascii=False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_call_claude)

    slack_tasks.handle_mention_task(con, "C1", "100.0", BULLET_TEXT, "U1")
    confirm_kwargs = posts[0][1]
    posts.clear()
    _click_split_button(con, confirm_kwargs, "no")

    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 1
    assert all_tasks[0]["title"] == "テレンプのAP調達未来像+デモ+ステップ+想定DB整理"


def test_handle_split_decision_ignores_unknown_or_already_used_split_id(con):
    """既に処理済み（pending行が削除済み）のsplit_idでボタンを押しても何も起きない
    （二重クリック対策）。"""
    slack_tasks._handle_split_decision(con, 999999, "yes")
    assert sfa_db.list_tasks(con, admin=False) == []


def test_split_button_click_routes_through_handle_interactive(con, monkeypatch):
    """Slackから届く実際のblock_actionsペイロード形式でも、分割確認ボタンが
    _handle_block_action経由で正しく_handle_split_decisionへルーティングされること。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "タスクA", "next_action": "", "due_date": "", "category": ""},
        {"title": "タスクB", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_mention_task(con, "C1", "700.0", "・タスクA\n・タスクB", "U1")
    action_id = _split_action_id(posts[0][1], "yes")
    payload = {"type": "block_actions", "actions": [{"action_id": action_id}], "response_url": ""}
    slack_tasks.handle_interactive(con, payload)

    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2


def test_handle_mention_task_single_item_keeps_original_reply_format(con, monkeypatch):
    """1件しか抽出されない場合は、従来通りの単一タスク文言・ブロック構成のまま
    （#148導入前との後方互換）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "見積を送る", "next_action": "", "due_date": "", "category": ""},
    ]))

    tids = slack_tasks.handle_mention_task(con, "C1", "100.0", "見積を送る", "U1")
    assert len(tids) == 1
    _, kwargs = posts[0]
    assert kwargs["text"] == "コンサルタスク化しました: 見積を送る"


def test_handle_mention_task_confirmed_multi_tasks_use_distinct_slack_ts_for_dedup(con, monkeypatch):
    """複数タスクは同一slack_tsのまま保存すると重複起票防止ロジックに引っかかって
    2件目以降が作られなくなる。連番サフィックスで区別しつつ、1件目は元のtsのまま
    保つこと（事務タスクの期限確認スレッド照合のため）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "タスクA", "next_action": "", "due_date": "", "category": ""},
        {"title": "タスクB", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_mention_task(con, "C1", "200.0", "・タスクA\n・タスクB", "U1")
    _click_split_button(con, posts[0][1], "yes")

    ts_values = {t["slack_ts"] for t in sfa_db.list_tasks(con, admin=False)}
    assert ts_values == {"200.0", "200.0#1"}


def test_handle_split_decision_double_click_does_not_duplicate_tasks(con, monkeypatch):
    """確認ボタンの二重クリック（Slackの再配信・誤操作）でも、複数タスクが
    重複作成されない（pending行が1回目の処理で削除されるため）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "タスクA", "next_action": "", "due_date": "", "category": ""},
        {"title": "タスクB", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_mention_task(con, "C1", "300.0", "・タスクA\n・タスクB", "U1")
    action_id = _split_action_id(posts[0][1], "yes")
    split_id = int(action_id.split(":", 1)[1])
    slack_tasks._handle_split_decision(con, split_id, "yes")
    slack_tasks._handle_split_decision(con, split_id, "yes")   # 二重クリック
    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2


# ── handle_admin_mention_task（事務タスクBot） ──

def test_handle_admin_mention_task_asks_before_splitting(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "経費精算をする", "next_action": "", "due_date": "", "category": ""},
        {"title": "会議室を予約する", "next_action": "", "due_date": "", "category": ""},
    ]))

    tids = slack_tasks.handle_admin_mention_task(con, "C1", "400.0", "・経費精算をする\n・会議室を予約する", "U1")
    assert tids == []
    assert sfa_db.list_tasks(con, admin=True) == []
    assert len(posts) == 1


def test_handle_admin_mention_task_confirm_yes_creates_multiple_tasks(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "経費精算をする", "next_action": "", "due_date": "", "category": ""},
        {"title": "会議室を予約する", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_admin_mention_task(con, "C1", "400.0", "・経費精算をする\n・会議室を予約する", "U1")
    _click_split_button(con, posts[0][1], "yes")

    all_tasks = sfa_db.list_tasks(con, admin=True)
    assert len(all_tasks) == 2
    titles = {t["title"] for t in all_tasks}
    assert titles == {"経費精算をする", "会議室を予約する"}
    for t in all_tasks:
        assert t["is_admin"] == 1
        assert t["assignee"] == (slack_tasks.DESK_ASSIGNEE or None)
    assert len(posts) == 2   # 確認メッセージ＋確定後の返信


# ── handle_reaction（🎯/📋リアクション起票） ──

def test_handle_reaction_asks_before_splitting_bulleted_message(con, monkeypatch):
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_fetch_message",
                        lambda channel, ts, token=None: {"text": BULLET_TEXT, "user": "U_AUTHOR"})
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "要件詰め案件の棚卸と注力案件の掘り起こし・特定", "next_action": "",
         "due_date": "", "category": ""},
        {"title": "開発案件の定義書FMTを整備", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_reaction(con, {
        "reaction": "dart", "user": "U_REACTOR", "item": {"channel": "C1", "ts": "111.1"},
    })
    assert sfa_db.list_tasks(con, admin=False) == []

    _click_split_button(con, posts[0][1], "yes")
    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2
    titles = {t["title"] for t in all_tasks}
    assert titles == {"要件詰め案件の棚卸と注力案件の掘り起こし・特定", "開発案件の定義書FMTを整備"}


# ── #149/#150(2026-09-02): 期限確認の返信率が低い問題への対応 ──
# #149でいったん「事務タスク化しました」見出しに依頼者メンションを付けたが、ユーザーから
# 「見出しにメンションは不要、文字も小さくてよい。実際に返信してほしい期限確認の一文
# （📅期限は...でよろしいですか？）の方にメンションを付け、そちらを太字・通常サイズに」と
# 修正指示（#150）。見出しはcontextブロック（小さい）、期限確認は section ブロック
# （通常サイズ・太字・メンション付き）という最終形になった。

def test_handle_admin_mention_task_header_is_small_and_unmentioned(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "請求書を発行する", "next_action": "", "due_date": "", "category": ""},
    ]))
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])

    slack_tasks.handle_admin_mention_task(con, "C1", "500.0", "請求書を発行する", "U_REQUESTER")

    _, kwargs = posts[-1]
    header = kwargs["blocks"][0]
    assert header["type"] == "context"
    assert "<@" not in header["elements"][0]["text"]
    assert "事務タスク化しました" in header["elements"][0]["text"]


def test_handle_admin_mention_task_mentions_requester_in_due_confirmation(con, monkeypatch):
    """依頼者（＝メンションした本人）へのメンションは「事務タスク化しました」ではなく、
    期限確認の一文の方に付くこと。名前解決を挟まず、メンションした人の実IDをそのまま使う。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "請求書を発行する", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_admin_mention_task(con, "C1", "500.0", "請求書を発行する", "U_REQUESTER")

    _, kwargs = posts[-1]
    due_block = kwargs["blocks"][1]
    assert due_block["type"] == "section"
    assert "<@U_REQUESTER>" in due_block["text"]["text"]
    assert "でよろしいですか" in due_block["text"]["text"]


def test_handle_admin_mention_task_mentions_requester_only_on_first_task_for_multi(con, monkeypatch):
    """複数タスクの場合、「OK」返信でヒットするのは元tsのままの1件目だけ(#148設計)なので、
    メンションも1件目の期限確認ブロックにのみ付ける。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "経費精算をする", "next_action": "", "due_date": "", "category": ""},
        {"title": "会議室を予約する", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_admin_mention_task(con, "C1", "501.0", "・経費精算をする\n・会議室を予約する", "U_REQUESTER")
    _click_split_button(con, posts[-1][1], "yes")

    _, kwargs = posts[-1]
    due_blocks = [b for b in kwargs["blocks"] if b.get("type") == "section" and "でよろしいですか" in b["text"]["text"]]
    assert len(due_blocks) == 2
    assert "<@U_REQUESTER>" in due_blocks[0]["text"]["text"]
    assert "<@" not in due_blocks[1]["text"]["text"]


def test_handle_reaction_admin_mentions_message_author_in_due_confirmation(con, monkeypatch):
    """📋リアクション起票では、依頼者=元メッセージの投稿者(author_id)を、期限確認の
    一文の方にメンションすること。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "_fetch_message",
                        lambda channel, ts, token=None: {"text": "請求書を発行する", "user": "U_AUTHOR"})
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "請求書を発行する", "next_action": "", "due_date": "", "category": ""},
    ]))

    slack_tasks.handle_reaction(con, {
        "reaction": "clipboard", "user": "U_REACTOR", "item": {"channel": "C1", "ts": "600.1"},
    })
    _, kwargs = posts[-1]
    header = kwargs["blocks"][0]
    assert header["type"] == "context"
    assert "<@" not in header["elements"][0]["text"]
    due_block = kwargs["blocks"][1]
    assert due_block["type"] == "section"
    assert "<@U_AUTHOR>" in due_block["text"]["text"]


# ── sfa_db層: pending_task_splits CRUD ──

def test_pending_task_split_crud_roundtrip(con):
    prefills = [{"title": "A", "next_action": "", "due_date": "", "category": ""},
                {"title": "B", "next_action": "", "due_date": "", "category": ""}]
    split_id = sfa_db.create_pending_task_split(
        con, channel="C1", thread_ts="800.0", text="・A\n・B", prefills=prefills,
        is_admin=True, user_id="U1", token="xoxb-x")
    row = sfa_db.get_pending_task_split(con, split_id)
    assert row["channel"] == "C1"
    assert row["thread_ts"] == "800.0"
    assert row["is_admin"] == 1
    assert row["user_id"] == "U1"
    assert row["token"] == "xoxb-x"
    assert row["prefills"] == prefills

    sfa_db.delete_pending_task_split(con, split_id)
    assert sfa_db.get_pending_task_split(con, split_id) is None


def test_get_pending_task_split_missing_returns_none(con):
    assert sfa_db.get_pending_task_split(con, 999999) is None
