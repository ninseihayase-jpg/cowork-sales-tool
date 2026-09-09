"""@NegoCollection Slack bot（cowork/slack_bot.py）の交渉フロー改善(2026-09-09)回帰テスト。

ユーザーからの実際のSlackスレッドを見ての指摘に基づく3点の修正:
1. 次回MS未設定の商談を確認すると本文に文字通り「None」が出ていた表示バグの修正
   （dict.get(key, default)はNULL値の時にdefaultへフォールバックしない実装ミス）。
2. 次回MS（日付/ラベル/種別）を会話から読み取れなかった場合、投稿者を@メンションした
   別メッセージで個別に確認を促し、「確定」時にもこの3項目が空だと保存をブロックする
   （テンプレート本文の小さな注記だけでは見落とされ、次回MSが空欄のままDB更新される
   実事故が繰り返し起きたため）。ステージは「変更なし(-)」の明示回答を有効とする。
3. 一度確定済み(completed)のスレッドに再メンションした際、新規メッセージだけで商談を
   再マッチングして失敗すると毎回SFA番号の再入力を要求していた問題を修正。直前に
   確定済みの商談idをフォールバックとして引き継ぐ。

一時DBのみ使用。本番DB(cowork_sfa.db)・実際のSlack APIには一切触れない
（_slack_post/_slack_get/get_thread_messagesはすべてmonkeypatchする）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, slack_bot


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_nego_flow_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _deal(con, account_name="A社", deal_name="X案件", **kw):
    acc = sfa_db.upsert_account(con, name=account_name)
    return sfa_db.upsert_deal(con, account_id=acc, deal_name=deal_name, status="open", **kw)


def _sent_messages(monkeypatch):
    """post_message()経由のchat.postMessage呼び出しをすべて記録する。"""
    sent = []

    def _fake_post(method, token=None, **kwargs):
        if method == "chat.postMessage":
            sent.append(kwargs)
            return {"ok": True, "ts": f"bot_ts_{len(sent)}"}
        return {"ok": True}

    monkeypatch.setattr(slack_bot, "_slack_post", _fake_post)
    return sent


AI_FILLED = {
    "activity_date": "2026-09-08", "activity_type": "面談", "contact_name": "宮澤",
    "activity_content": "テスト内容です。", "stage_update": "提案",
    "next_milestone_date": "2026-10-01", "next_milestone_label": "次回打合せ",
    "next_milestone_type": "アポ", "memo_addition": None,
}


# ── draft_template: 「None」漏れバグ修正 ──

def test_draft_template_does_not_leak_none_for_unset_next_ms(monkeypatch, con):
    monkeypatch.setattr(slack_bot, "_call_claude", lambda prompt: json.dumps(AI_FILLED))
    deal = {"id": 1, "deal_name": "X", "stage": "要件詰め",
            "next_milestone_date": None, "next_milestone_label": None,
            "next_milestone_type": None, "note": None}
    text, _ = slack_bot.draft_template("会話内容", deal, con)
    assert "None" not in text


# ── draft_template: 必須項目の未読取検知(missing_required) ──

def test_draft_template_reports_missing_when_ai_returns_null(monkeypatch, con):
    parsed = {**AI_FILLED, "stage_update": None, "next_milestone_date": None,
              "next_milestone_label": None, "next_milestone_type": None}
    monkeypatch.setattr(slack_bot, "_call_claude", lambda prompt: json.dumps(parsed))
    deal = {"id": 1, "deal_name": "X", "stage": "要件詰め"}
    _, missing = slack_bot.draft_template("会話内容", deal, con)
    assert missing == ["次回MS日", "次回MSラベル", "次回MS種別", "ステージ"]


def test_draft_template_treats_literal_kisai_nashi_as_missing_too(monkeypatch):
    """Claudeのプロンプト仕様上、不明時は文字列「【記載なし】」も許容されている
    （null限定ではない）ため、これも未入力(missing_required)として検知できること。"""
    parsed = {**AI_FILLED, "next_milestone_date": "【記載なし】",
              "next_milestone_label": "【記載なし】", "next_milestone_type": "【記載なし】"}
    monkeypatch.setattr(slack_bot, "_call_claude", lambda prompt: json.dumps(parsed))
    _, missing = slack_bot.draft_template("会話内容", {"id": 1, "deal_name": "X"}, None)
    assert set(missing) == {"次回MS日", "次回MSラベル", "次回MS種別"}


def test_draft_template_no_missing_when_ai_fills_everything(monkeypatch, con):
    monkeypatch.setattr(slack_bot, "_call_claude", lambda prompt: json.dumps(AI_FILLED))
    deal = {"id": 1, "deal_name": "X", "stage": "要件詰め"}
    _, missing = slack_bot.draft_template("会話内容", deal, con)
    assert missing == []


# ── handle_message(pending確定): 次回MS未入力での確定ブロック ──

def test_confirm_blocked_when_next_ms_fields_missing(monkeypatch, con):
    did = _deal(con)
    slack_bot.save_pending_thread(con, "t1", "C1", did, "bot1", state="pending")
    sent = _sent_messages(monkeypatch)
    monkeypatch.setattr(slack_bot, "_bot_user_id", "BUID")

    template_text = (
        "【SFA更新テンプレート】\n内容: 打合せを実施。\n"
        "ステージ: -\n次回MS日: -\n次回MSラベル: -\n次回MS種別: -\n"
    )
    monkeypatch.setattr(slack_bot, "get_thread_messages", lambda channel, ts: [
        {"ts": "bot1", "bot_id": "B1", "text": template_text},
        {"ts": "confirm1", "user": "U1", "text": "確定"},
    ])

    event = {"channel": "C1", "text": "確定", "ts": "confirm1", "thread_ts": "t1", "user": "U1"}
    slack_bot.handle_message(event, con)

    assert sfa_db.list_activities(con, did) == []
    assert any("次回MSが未入力のため確定できません" in m.get("text", "") for m in sent)
    row = slack_bot.get_pending_thread(con, "t1")
    assert row["state"] == "pending"  # completedへ進んでいない


def test_confirm_succeeds_when_next_ms_fields_filled(monkeypatch, con):
    did = _deal(con)
    slack_bot.save_pending_thread(con, "t2", "C1", did, "bot1", state="pending")
    sent = _sent_messages(monkeypatch)
    monkeypatch.setattr(slack_bot, "_bot_user_id", "BUID")

    template_text = (
        "【SFA更新テンプレート】\n内容: 打合せを実施。\n"
        "ステージ: -\n次回MS日: 2026-10-01\n次回MSラベル: 次回打合せ\n次回MS種別: アポ\n"
    )
    monkeypatch.setattr(slack_bot, "get_thread_messages", lambda channel, ts: [
        {"ts": "bot1", "bot_id": "B1", "text": template_text},
        {"ts": "confirm2", "user": "U1", "text": "確定"},
    ])

    event = {"channel": "C1", "text": "確定", "ts": "confirm2", "thread_ts": "t2", "user": "U1"}
    slack_bot.handle_message(event, con)

    assert len(sfa_db.list_activities(con, did)) == 1
    assert any("SFA DB を更新しました" in m.get("text", "") for m in sent)
    row = slack_bot.get_pending_thread(con, "t2")
    assert row["state"] == "completed"
    deal_after = sfa_db.get_deal(con, did)
    assert deal_after["next_milestone_date"] == "2026-10-01"
    assert deal_after["next_milestone_label"] == "次回打合せ"


def test_confirm_allows_explicit_stage_no_change(monkeypatch, con):
    """ステージは「変更なし」の明示回答(-)でも確定をブロックしないこと
    （次回MS3項目とは異なる扱い。ユーザー確定要件）。"""
    did = _deal(con, stage="要件詰め")
    slack_bot.save_pending_thread(con, "t3", "C1", did, "bot1", state="pending")
    sent = _sent_messages(monkeypatch)
    monkeypatch.setattr(slack_bot, "_bot_user_id", "BUID")

    template_text = (
        "【SFA更新テンプレート】\n内容: 打合せを実施。\n"
        "ステージ: -\n次回MS日: 2026-10-05\n次回MSラベル: 検討継続\n次回MS種別: タスク\n"
    )
    monkeypatch.setattr(slack_bot, "get_thread_messages", lambda channel, ts: [
        {"ts": "bot1", "bot_id": "B1", "text": template_text},
        {"ts": "confirm3", "user": "U1", "text": "確定"},
    ])
    event = {"channel": "C1", "text": "確定", "ts": "confirm3", "thread_ts": "t3", "user": "U1"}
    slack_bot.handle_message(event, con)

    assert len(sfa_db.list_activities(con, did)) == 1
    deal_after = sfa_db.get_deal(con, did)
    assert deal_after["stage"] == "要件詰め"  # 変更されていない


# ── handle_mention: completed→再メンションで商談を引き継ぐ ──

def test_re_mention_after_completed_reuses_prior_deal_when_new_text_has_no_match(monkeypatch, con):
    did = _deal(con, account_name="加藤製作所株式会社", deal_name="生産管理システム")
    # 直前に確定済み(completed)のスレッド状態を再現
    slack_bot.save_pending_thread(con, "t4", "C1", did, "bot_old", state="pending")
    slack_bot.mark_completed(con, "t4")

    sent = _sent_messages(monkeypatch)
    monkeypatch.setattr(slack_bot, "_bot_user_id", "BUID")
    # 新規メッセージは会社名を含まない技術メモのみ（find_deal()では再マッチングできない）
    monkeypatch.setattr(slack_bot, "get_thread_messages", lambda channel, ts: [
        {"ts": "old1", "user": "U1", "text": "以前のメッセージ"},
        {"ts": "bot_old", "bot_id": "B1", "text": "以前のテンプレート"},
        {"ts": "new1", "user": "U1", "text": "図面フォーマットについて追記します。"},
    ])

    event = {"channel": "C1", "ts": "mention1", "thread_ts": "t4", "user": "U1"}
    slack_bot.handle_mention(event, con)

    # 「既存の商談が見つかりませんでした」ではなく、直前の商談での確認メッセージが出ること
    assert not any("既存の商談が見つかりませんでした" in m.get("text", "") for m in sent)
    assert any(f"SFA#{did}" in m.get("text", "") for m in sent)
    row = slack_bot.get_pending_thread(con, "t4")
    assert row["deal_id"] == did
    assert row["state"] == "identifying"


def test_get_open_deal_by_id_returns_none_for_closed_deal(con):
    did = _deal(con)
    con.execute("UPDATE deals SET status='closed_won' WHERE id=?", (did,))
    con.commit()
    assert slack_bot._get_open_deal_by_id(con, did) is None


def test_get_open_deal_by_id_returns_deal_for_open_deal(con):
    did = _deal(con)
    row = slack_bot._get_open_deal_by_id(con, did)
    assert row is not None and row["id"] == did
