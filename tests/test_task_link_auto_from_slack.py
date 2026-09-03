"""#157(2026-09-04): TaskBotで起票する際、本文にリンクが同時に送られていたら
タスクの「リンク」(task_links)に自動セットする機能の回帰テスト。

対象: @メンション起票(handle_mention_task/handle_admin_mention_task)、
リアクション起票(handle_reaction、通常/事務両方)、複数タスク分割確定後
(_finalize_normal_tasks/_finalize_admin_tasks)。
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
    d = tempfile.mkdtemp(prefix="sfa_task_link_auto_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _fake_single(title="タスク", next_action=""):
    return lambda prompt: json.dumps(
        [{"title": title, "next_action": next_action, "due_date": "", "category": ""}],
        ensure_ascii=False)


# ── _extract_urls ──

def test_extract_urls_handles_slack_link_markup_with_label():
    urls = slack_tasks._extract_urls("資料はこちら <https://example.com/doc|資料>。よろしく")
    assert urls == ["https://example.com/doc"]


def test_extract_urls_handles_slack_bare_link_markup():
    urls = slack_tasks._extract_urls("見てね <https://example.com/x>")
    assert urls == ["https://example.com/x"]


def test_extract_urls_handles_plain_url_without_markup():
    urls = slack_tasks._extract_urls("見てね https://example.com/y お願いします")
    assert urls == ["https://example.com/y"]


def test_extract_urls_strips_trailing_punctuation():
    urls = slack_tasks._extract_urls("参考: https://example.com/z、以上です")
    assert urls == ["https://example.com/z"]


def test_extract_urls_dedupes_and_caps_at_limit():
    text = " ".join(f"https://example.com/{i}" for i in range(10)) + " https://example.com/0"
    urls = slack_tasks._extract_urls(text)
    assert len(urls) == 5
    assert urls[0] == "https://example.com/0"


def test_extract_urls_returns_empty_list_when_no_url():
    assert slack_tasks._extract_urls("リンクはありません") == []
    assert slack_tasks._extract_urls("") == []
    assert slack_tasks._extract_urls(None) == []


# ── handle_mention_task（コンサルタスク） ──

def test_handle_mention_task_with_link_sets_task_link(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_single("資料を確認する"))

    tids = slack_tasks.handle_mention_task(
        con, "C1", "100.0", "資料はこちら <https://example.com/doc|資料> 確認お願いします", "U1")

    assert len(tids) == 1
    links = sfa_db.list_task_links(con, tids[0])
    assert [l["url"] for l in links] == ["https://example.com/doc"]


def test_handle_mention_task_without_link_adds_no_link(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_single("見積を送る"))

    tids = slack_tasks.handle_mention_task(con, "C1", "100.0", "見積を送ってください", "U1")

    assert sfa_db.list_task_links(con, tids[0]) == []


def test_handle_mention_task_duplicate_delivery_does_not_duplicate_link(con, monkeypatch):
    """Slackの再送(同一channel+ts)で二重起票を防ぐ既存仕様(#92)と同様、
    リンクも二重には追加されないこと。"""
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_single("資料を確認する"))

    text = "資料はこちら https://example.com/doc 確認お願いします"
    tids1 = slack_tasks.handle_mention_task(con, "C1", "100.0", text, "U1")
    tids2 = slack_tasks.handle_mention_task(con, "C1", "100.0", text, "U1")

    assert tids1 == tids2
    assert len(sfa_db.list_task_links(con, tids1[0])) == 1


# ── handle_admin_mention_task（事務タスク） ──

def test_handle_admin_mention_task_with_link_sets_task_link(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_single("請求書を確認する"))

    tids = slack_tasks.handle_admin_mention_task(
        con, "C1", "200.0", "請求書はこちら <https://example.com/invoice.pdf> 確認して", "U1")

    assert len(tids) == 1
    links = sfa_db.list_task_links(con, tids[0])
    assert [l["url"] for l in links] == ["https://example.com/invoice.pdf"]


# ── handle_reaction（🎯/📋リアクション起票） ──

def test_handle_reaction_normal_with_link_sets_task_link(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "_fetch_message",
                        lambda channel, ts, token=None: {"text": "資料 <https://example.com/a> 見て"})
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_single("資料を見る"))

    event = {"reaction": "dart", "item": {"channel": "C1", "ts": "300.0"}, "user": "U1"}
    slack_tasks.handle_reaction(con, event)

    tasks = sfa_db.list_tasks(con, admin=False)
    assert len(tasks) == 1
    assert [l["url"] for l in sfa_db.list_task_links(con, tasks[0]["id"])] == ["https://example.com/a"]


def test_handle_reaction_admin_with_link_sets_task_link(con, monkeypatch):
    monkeypatch.setattr(slack_tasks, "_slack_post", lambda method, **kw: {"ok": True})
    monkeypatch.setattr(slack_tasks, "_fetch_message",
                        lambda channel, ts, token=None: {"text": "見積 <https://example.com/b> 承認して",
                                                         "user": "U_AUTHOR"})
    monkeypatch.setattr(slack_tasks, "_message_permalink", lambda channel, ts, token=None: None)
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "notify_task_created", lambda *a, **kw: False)
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_single("見積を承認する"))

    event = {"reaction": "clipboard", "item": {"channel": "C1", "ts": "400.0"}, "user": "U1"}
    slack_tasks.handle_reaction(con, event)

    tasks = sfa_db.list_tasks(con, admin=True)
    assert len(tasks) == 1
    assert [l["url"] for l in sfa_db.list_task_links(con, tasks[0]["id"])] == ["https://example.com/b"]


# ── 複数タスク分割確定後（#151の確認ボタンを経由） ──

def _fake_claude_json_array(items):
    return lambda prompt: json.dumps(items, ensure_ascii=False)


def _split_action_id(kwargs, decision="yes"):
    for block in kwargs["blocks"]:
        if block.get("type") != "actions":
            continue
        for el in block["elements"]:
            if el["action_id"].startswith(f"task_split_{decision}:"):
                return el["action_id"]
    raise AssertionError(f"task_split_{decision}:... のボタンが見つからない")


def test_finalize_normal_tasks_attaches_link_from_full_text_to_all_split_tasks(con, monkeypatch):
    """分割された全タスクに、起票元本文中のURLが付く（#157: どのタスクに属するかまでは
    判定しない単純仕様）。"""
    posts = []
    monkeypatch.setattr(slack_tasks, "_slack_post",
                        lambda method, **kw: (posts.append((method, kw)), {"ok": True})[1])
    monkeypatch.setattr(slack_tasks, "owner_from_slack_user", lambda uid, token=None: "早瀬")
    monkeypatch.setattr(slack_tasks, "_call_claude", _fake_claude_json_array([
        {"title": "Aをやる", "next_action": "", "due_date": "", "category": ""},
        {"title": "Bをやる", "next_action": "", "due_date": "", "category": ""},
    ]))

    text = "参考資料 <https://example.com/ref> あります。\n・Aをやる\n・Bをやる"
    slack_tasks.handle_mention_task(con, "C1", "500.0", text, "U1")
    confirm_kwargs = posts[0][1]
    slack_tasks._handle_split_decision(
        con, int(_split_action_id(confirm_kwargs, "yes").split(":", 1)[1]), "yes")

    all_tasks = sfa_db.list_tasks(con, admin=False)
    assert len(all_tasks) == 2
    for t in all_tasks:
        links = sfa_db.list_task_links(con, t["id"])
        assert [l["url"] for l in links] == ["https://example.com/ref"]
