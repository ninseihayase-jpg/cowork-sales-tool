"""#148(2026-09-02): TaskBot(@メンション)へ箇条書きで複数の依頼を書いた場合、
1件のタスクに統合されてしまう不具合の回帰テスト。

ユーザー報告: 「・要件詰め案件の棚卸、注力案件の掘り起こし・特定」「・開発案件の定義書FMTを整備」
という2行の箇条書きをメンションしたら、1件のタスク（両方を要約統合したタイトル）しか
起票されなかった。原因は`ai_extract_task`が「1件抽出」前提のプロンプト・単一dict戻り値
だったこと。`ai_extract_tasks`（複数形）を新設し、箇条書き/改行ごとに複数タスクへ分割する。
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

def test_handle_mention_task_creates_multiple_tasks_from_bullets(con, monkeypatch):
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

    assert len(tids) == 2
    titles = {sfa_db.get_task(con, t)["title"] for t in tids}
    assert titles == {"要件詰め案件の棚卸と注力案件の掘り起こし・特定", "開発案件の定義書FMTを整備"}
    for t in tids:
        assert sfa_db.get_task(con, t)["assignee"] == "早瀬"
    # 返信は1回のchat.postMessageにまとまっている（メッセージ数が依頼数に比例して増えない）
    assert len(posts) == 1
    _, kwargs = posts[0]
    assert "2件" in kwargs["text"]
    action_ids = [el["action_id"] for block in kwargs["blocks"] if block.get("type") == "actions"
                  for el in block["elements"]]
    # 各タスクの完了/開始/進捗ボタンが両方分含まれている
    assert sum(1 for a in action_ids if a.startswith("task_done:")) == 2


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


def test_handle_mention_task_multi_tasks_use_distinct_slack_ts_for_dedup(con, monkeypatch):
    """複数タスクは同一slack_tsのまま保存すると重複起票防止ロジックに引っかかって
    2件目以降が作られなくなる。連番サフィックスで区別しつつ、1件目は元のtsのまま
    保つこと（事務タスクの期限確認スレッド照合のため）。"""
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "タスクA", "next_action": "", "due_date": "", "category": ""},
        {"title": "タスクB", "next_action": "", "due_date": "", "category": ""},
    ]))

    tids = slack_tasks.handle_mention_task(con, "C1", "200.0", "・タスクA\n・タスクB", "U1")
    rows = [sfa_db.get_task(con, t) for t in tids]
    ts_values = {r["slack_ts"] for r in rows}
    assert ts_values == {"200.0", "200.0#1"}


def test_handle_mention_task_retry_does_not_duplicate_multi_tasks(con, monkeypatch):
    """同一メッセージ(同一slack_ts)からの多重配信でも、複数タスクが重複作成されない
    （各タスクごとの重複防止判定が効くこと）。"""
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "タスクA", "next_action": "", "due_date": "", "category": ""},
        {"title": "タスクB", "next_action": "", "due_date": "", "category": ""},
    ]))

    tids1 = slack_tasks.handle_mention_task(con, "C1", "300.0", "・タスクA\n・タスクB", "U1")
    tids2 = slack_tasks.handle_mention_task(con, "C1", "300.0", "・タスクA\n・タスクB", "U1")
    assert tids1 == tids2
    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2


# ── handle_admin_mention_task（事務タスクBot） ──

def test_handle_admin_mention_task_creates_multiple_tasks(con, monkeypatch):
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
    assert len(tids) == 2
    titles = {sfa_db.get_task(con, t)["title"] for t in tids}
    assert titles == {"経費精算をする", "会議室を予約する"}
    for t in tids:
        row = sfa_db.get_task(con, t)
        assert row["is_admin"] == 1
        assert row["assignee"] == (slack_tasks.DESK_ASSIGNEE or None)
    assert len(posts) == 1


# ── handle_reaction（🎯/📋リアクション起票） ──

def test_handle_reaction_creates_multiple_tasks_from_bulleted_message(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: {"ok": True})
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
    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2
    titles = {t["title"] for t in all_tasks}
    assert titles == {"要件詰め案件の棚卸と注力案件の掘り起こし・特定", "開発案件の定義書FMTを整備"}
