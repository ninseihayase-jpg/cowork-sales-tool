"""#98: Jamie文字起こしの商談識別をSlackに移設する運用の回帰テスト。

設計(docs/hearing-ai/DESIGN.md §10)の3本柱を検証する:
1. Jamie webhook到着時、商談候補があればSlackに候補ボタンを投稿する（識別①）。
   候補が無い（＝論点/内部議論など）場合は投稿せず、Web `/intake-inbox` に委ねる。
2. Slack上のボタン操作で識別・突合が完結する
   （割当→既存活動の有無チェック→無ければ確定時に統合、あれば追記提案→追記）。
3. `@NegoCollection`確定（apply_to_db）が、割当済み・未消化のJamie全文を
   自動で本文に採用し、Slackの入力を強調として追記する（Jamie先攻ケース）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from cowork import sfa_db, slack_bot, webapp


def _fresh():
    d = tempfile.mkdtemp(prefix="sfa_jsi_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def _fake_poster(store):
    def _post(method, token=None, **kwargs):
        store["method"] = method
        store["kwargs"] = kwargs
        return {"ok": True, "ts": "100.200"}
    return _post


def test_no_candidate_skips_slack_post(monkeypatch):
    """論点/内部議論など、商談名に一致しない文字起こしはSlackに投稿されない。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        monkeypatch.setattr(slack_bot, "SALES_CHANNEL_ID", "C0AT55W40ET")
        sfa_db.upsert_deal(con, account_id=sfa_db.upsert_account(con, name="ソラスト"),
                           deal_name="成果報酬コスト削減", stage="提案")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x1",
                                          title="社内定例:調達方針すり合わせ",
                                          occurred_on="2026-08-15", transcript="内部議論",
                                          attendees_json="[]")
        cands = webapp._inbox_candidates("社内定例:調達方針すり合わせ", [],
                                         sfa_db.list_deals(con, status="open"), [])
        assert cands == []
        r = slack_bot.post_jamie_candidate_prompt(
            con, inbox_id=iid, title="社内定例:調達方針すり合わせ", occurred_on="2026-08-15", candidates=cands)
        assert r is None
        assert not store  # 投稿されていない
        # Webのinboxにはそのまま残る
        assert sfa_db.count_inbox_transcripts(con) == 1
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_candidate_match_posts_buttons_with_skip_option(monkeypatch):
    """商談名に一致する場合はSlackに候補＋「対象外」ボタンを投稿する。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        monkeypatch.setattr(slack_bot, "SALES_CHANNEL_ID", "C0AT55W40ET")
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x2",
                                          title="ソラスト 成果報酬コスト削減 定例",
                                          occurred_on="2026-08-15", transcript="議事内容", attendees_json="[]")
        cands = webapp._inbox_candidates("ソラスト 成果報酬コスト削減 定例", [],
                                         sfa_db.list_deals(con, status="open"), [])
        assert cands and cands[0][0] == f"deal:{did}"
        ts = slack_bot.post_jamie_candidate_prompt(
            con, inbox_id=iid, title="ソラスト 成果報酬コスト削減 定例", occurred_on="2026-08-15", candidates=cands)
        assert ts == "100.200"
        blocks_json = json.dumps(store["kwargs"]["blocks"])
        assert "jamie_pick_deal" in blocks_json and "jamie_skip" in blocks_json
        assert f"{iid}:{did}" in blocks_json
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_pick_deal_without_existing_activity_just_assigns(monkeypatch):
    """Jamie先攻: 既存活動が無ければ割当のみで、確定時の統合を待つ旨を表示する。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x3",
                                          title="D定例", occurred_on="2026-08-15",
                                          transcript="全文A", attendees_json="[]")
        slack_bot.handle_interactive(con, {
            "actions": [{"action_id": "jamie_pick_deal", "value": f"{iid}:{did}"}],
            "channel": {"id": "C1"}, "message": {"ts": "1.1"}})
        t = sfa_db.get_intake_transcript(con, iid)
        assert t["status"] == "assigned" and t["kind"] == "deal" and t["entity_id"] == did
        assert "確定する際にJamie全文を取り込みます" in store["kwargs"]["text"]
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_pick_deal_with_existing_activity_prompts_append(monkeypatch):
    """Slack先攻: 既に同日の活動があれば「追記しますか」を提示する。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-15",
                            contact_name="田中", body="Slackで確定した内容")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x4",
                                          title="D定例", occurred_on="2026-08-15",
                                          transcript="全文B", attendees_json="[]")
        slack_bot.handle_interactive(con, {
            "actions": [{"action_id": "jamie_pick_deal", "value": f"{iid}:{did}"}],
            "channel": {"id": "C1"}, "message": {"ts": "1.1"}})
        assert "追記しますか" in store["kwargs"]["text"]
        blocks_json = json.dumps(store["kwargs"]["blocks"])
        assert "jamie_append_yes" in blocks_json and "jamie_append_no" in blocks_json
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_append_yes_merges_body_and_consumes_transcript(monkeypatch):
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        aid = sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-15",
                                  contact_name="田中", body="Slack確定分")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x5",
                                          title="D定例", occurred_on="2026-08-15",
                                          transcript="Jamie全文C", attendees_json="[]")
        slack_bot.handle_interactive(con, {
            "actions": [{"action_id": "jamie_append_yes", "value": f"{iid}:{aid}"}],
            "channel": {"id": "C1"}, "message": {"ts": "1.1"}})
        act = dict(con.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone())
        assert "Slack確定分" in act["body"] and "Jamie全文C" in act["body"]
        t = sfa_db.get_intake_transcript(con, iid)
        assert t["status"] == "saved"
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_append_no_consumes_without_changing_body(monkeypatch):
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        aid = sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-15",
                                  contact_name="田中", body="Slack確定分")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x6",
                                          title="D定例", occurred_on="2026-08-15",
                                          transcript="Jamie全文D", attendees_json="[]")
        slack_bot.handle_interactive(con, {
            "actions": [{"action_id": "jamie_append_no", "value": str(iid)}],
            "channel": {"id": "C1"}, "message": {"ts": "1.1"}})
        act = dict(con.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone())
        assert act["body"] == "Slack確定分"
        t = sfa_db.get_intake_transcript(con, iid)
        assert t["status"] == "saved"
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_apply_to_db_merges_assigned_jamie_transcript_and_consumes_it(monkeypatch):
    """@NegoCollection確定(apply_to_db)時、割当済み・未消化のJamie全文があれば統合し消化する。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x7",
                                          title="D定例", occurred_on="2026-08-15",
                                          transcript="Jamie全文E", attendees_json="[]")
        sfa_db.assign_inbox_transcript(con, iid, kind="deal", entity_id=did)
        slack_bot.apply_to_db(con, {"種別": "面談", "活動日": "2026-08-15",
                                    "相手": "田中", "内容": "Slackの強調ポイント"}, did)
        act = dict(con.execute("SELECT * FROM activities WHERE deal_id=?", (did,)).fetchone())
        assert "Jamie全文E" in act["body"] and "【Slack強調】" in act["body"]
        assert "Slackの強調ポイント" in act["body"]
        t = sfa_db.get_intake_transcript(con, iid)
        assert t["status"] == "saved"
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_apply_to_db_without_jamie_transcript_unaffected():
    """割当済みJamie全文が無い通常のSlack確定は、これまで通り内容のみで記録される。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        slack_bot.apply_to_db(con, {"種別": "面談", "活動日": "2026-08-15",
                                    "相手": "田中", "内容": "通常の確定内容"}, did)
        act = dict(con.execute("SELECT * FROM activities WHERE deal_id=?", (did,)).fetchone())
        assert act["body"] == "通常の確定内容"
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_post_jamie_slack_candidates_helper_noop_without_candidates(monkeypatch):
    """webapp._post_jamie_slack_candidates: 候補が無ければslack_bot側を一切呼ばない。"""
    d, con = _fresh()
    try:
        called = {"n": 0}

        def _fail_if_called(*a, **kw):
            called["n"] += 1
            return None
        monkeypatch.setattr(slack_bot, "post_jamie_candidate_prompt", _fail_if_called)
        webapp._post_jamie_slack_candidates(con, inbox_id=1, title="社内定例:何か", occurred_on="2026-08-15")
        assert called["n"] == 0
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_post_jamie_slack_candidates_helper_calls_slack_bot_when_matched(monkeypatch):
    """webapp._post_jamie_slack_candidates: 候補があればslack_bot.post_jamie_candidate_promptを呼ぶ。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        seen = {}

        def _capture(con_, *, inbox_id, title, occurred_on, candidates):
            seen.update(inbox_id=inbox_id, title=title, occurred_on=occurred_on, candidates=candidates)
            return "ts"
        monkeypatch.setattr(slack_bot, "post_jamie_candidate_prompt", _capture)
        webapp._post_jamie_slack_candidates(con, inbox_id=42, title="ソラスト D 定例", occurred_on="2026-08-15")
        assert seen.get("inbox_id") == 42 and seen.get("candidates")
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)
