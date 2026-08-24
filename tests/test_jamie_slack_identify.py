"""#98: Jamie文字起こしの商談識別をSlackに移設する運用の回帰テスト。

設計(docs/hearing-ai/DESIGN.md §10、2026-08-19改訂)の3本柱を検証する:
1. Jamie webhook到着時、候補の有無に関わらず個人DM（JAMIE_CANDIDATE_DM_OWNER）に
   「候補ボタン(最大5)＋対象(候補以外)＋対象外(割当不要)」を投稿する（識別①）。
   候補0件でも投稿する（以前は候補0件なら投稿自体をスキップしていたが、それが見逃しの
   温床だったため撤廃）。
2. Slack上のボタン操作で識別・突合が完結する
   （候補割当→既存活動の有無チェック→無ければWeb取込リンクを提示／あれば追記提案→追記。
   対象(候補以外)→Web割当画面へのリンク。対象外(割当不要)→完了扱い(status='not_needed')）。
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


def test_no_candidate_still_posts_with_escape_buttons_only(monkeypatch):
    """論点/内部議論など、商談名に一致しない文字起こしでも個人DMには投稿する
    （2026-08-19改訂：候補0件でも通知し、「対象(候補以外)」「対象外(割当不要)」のみ出す）。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
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
            con, inbox_id=iid, title="社内定例:調達方針すり合わせ", occurred_on="2026-08-15",
            candidates=cands, channel="D0TESTDM")
        assert r == "100.200"
        blocks_json = json.dumps(store["kwargs"]["blocks"])
        assert "jamie_pick_deal" not in blocks_json
        assert "jamie_not_candidate" in blocks_json and "jamie_skip" in blocks_json
        # Webのinboxにはそのまま残る（まだ割当も除外もされていない）
        assert sfa_db.count_inbox_transcripts(con) == 1
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_candidate_match_posts_three_button_types(monkeypatch):
    """商談名に一致する場合、候補ボタン＋「対象(候補以外)」＋「対象外(割当不要)」の3種を投稿する。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="x2",
                                          title="ソラスト 成果報酬コスト削減 定例",
                                          occurred_on="2026-08-15", transcript="議事内容", attendees_json="[]")
        cands = webapp._inbox_candidates("ソラスト 成果報酬コスト削減 定例", [],
                                         sfa_db.list_deals(con, status="open"), [])
        assert cands and cands[0][0] == f"deal:{did}"
        ts = slack_bot.post_jamie_candidate_prompt(
            con, inbox_id=iid, title="ソラスト 成果報酬コスト削減 定例", occurred_on="2026-08-15",
            candidates=cands, channel="D0TESTDM")
        assert ts == "100.200"
        blocks_json = json.dumps(store["kwargs"]["blocks"])
        assert "jamie_pick_deal" in blocks_json
        assert "jamie_not_candidate" in blocks_json
        assert "jamie_skip" in blocks_json
        assert f"{iid}:{did}" in blocks_json
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_candidate_prompt_channel_override_bypasses_dm_resolution(monkeypatch):
    """channel引数を渡すと、DM解決（Slack API呼び出し）を経由せず指定チャネルへ投稿できる
    （#98動作確認プレビュー用: scripts/send_jamie_candidate_dm_preview.py が使う）。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))

        def _fail_if_called():
            raise AssertionError("channelを明示指定した場合はDM解決を呼ばないはず")
        monkeypatch.setattr(slack_bot, "_resolve_jamie_dm_channel", _fail_if_called)

        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
        cands = webapp._inbox_candidates("ソラスト 成果報酬コスト削減 定例", [],
                                         sfa_db.list_deals(con, status="open"), [])
        ts = slack_bot.post_jamie_candidate_prompt(
            con, inbox_id=99, title="ソラスト 成果報酬コスト削減 定例", occurred_on="2026-08-15",
            candidates=cands, channel="D0TESTDM")
        assert ts == "100.200"
        assert store["kwargs"]["channel"] == "D0TESTDM"
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_resolve_jamie_dm_channel_success(monkeypatch):
    """_resolve_jamie_dm_channel: owner_slack_map経由でuser_id解決→conversations.openでDM取得。"""
    from cowork import slack_tasks

    def _fake_slack_get(method, params, token=None):
        assert method == "users.lookupByEmail"
        return {"ok": True, "user": {"id": "U123"}}
    monkeypatch.setattr(slack_tasks, "_slack_get", _fake_slack_get)

    def _fake_slack_post(method, token=None, **kwargs):
        assert method == "conversations.open" and kwargs.get("users") == "U123"
        return {"ok": True, "channel": {"id": "D0REAL"}}
    monkeypatch.setattr(slack_bot, "_slack_post", _fake_slack_post)

    ch = slack_bot._resolve_jamie_dm_channel()
    assert ch == "D0REAL"


def test_resolve_jamie_dm_channel_user_not_found_returns_none(monkeypatch):
    from cowork import slack_tasks
    monkeypatch.setattr(slack_tasks, "_slack_get", lambda *a, **k: {"ok": False})
    assert slack_bot._resolve_jamie_dm_channel() is None


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
        assert "割り当てました" in store["kwargs"]["text"]
        assert f"/hearing/intake?deal={did}&inbox_id={iid}" in store["kwargs"]["text"]
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_pick_not_candidate_gives_web_inbox_link_without_assigning(monkeypatch):
    """「対象(候補以外)」: 割当は行わず、Web取り込みインボックスへのリンクだけ提示する。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="xnc",
                                          title="タイトル一致なし定例", occurred_on="2026-08-15",
                                          transcript="本文", attendees_json="[]")
        slack_bot.handle_interactive(con, {
            "actions": [{"action_id": "jamie_not_candidate", "value": str(iid)}],
            "channel": {"id": "C1"}, "message": {"ts": "1.1"}})
        t = sfa_db.get_intake_transcript(con, iid)
        assert t["status"] == "inbox"   # 割当も除外もされていない
        assert f"/intake-inbox#inbox-{iid}" in store["kwargs"]["text"]
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_skip_marks_not_needed_and_ends(monkeypatch):
    """「対象外(割当不要)」: status='not_needed'にして完了扱いにする（見逃し検知/Web一覧からも除外）。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        iid = sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="xskip",
                                          title="社内定例", occurred_on="2026-08-15",
                                          transcript="本文", attendees_json="[]")
        slack_bot.handle_interactive(con, {
            "actions": [{"action_id": "jamie_skip", "value": str(iid)}],
            "channel": {"id": "C1"}, "message": {"ts": "1.1"}})
        t = sfa_db.get_intake_transcript(con, iid)
        assert t["status"] == "not_needed"
        assert "対象外" in store["kwargs"]["text"]
        # Webの取り込みインボックス一覧(status='inbox'限定)からも消える
        assert sfa_db.count_inbox_transcripts(con) == 0
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


def test_post_jamie_slack_candidates_helper_calls_even_without_candidates(monkeypatch):
    """webapp._post_jamie_slack_candidates: 候補が0件でもslack_bot側を呼ぶ（2026-08-19改訂。
    以前は候補0件ならスキップしていたが、それが見逃しの温床だったため撤廃）。"""
    d, con = _fresh()
    try:
        seen = {}

        def _capture(con_, *, inbox_id, title, occurred_on, candidates):
            seen.update(inbox_id=inbox_id, candidates=candidates)
            return "ts"
        monkeypatch.setattr(slack_bot, "post_jamie_candidate_prompt", _capture)
        webapp._post_jamie_slack_candidates(con, inbox_id=1, title="社内定例:何か", occurred_on="2026-08-15")
        assert seen.get("inbox_id") == 1 and seen.get("candidates") == []
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


def test_candidate_buttons_have_unique_action_ids(monkeypatch):
    """回帰テスト(2026-08-24): 候補ボタンが2件以上あると、同一actions block内で全ボタンが
    同じaction_id("jamie_pick_deal")を共有し、Slackのchat.postMessageがinvalid_blocksで
    拒否して投稿自体が（無言で）消えていた不具合。ボタンごとにaction_idが一意であることを確認する
    （TaskBotのtask_effortボタンでも同種の不具合があった＝cowork/slack_tasks.pyの
    test_task_effort_block_has_unique_action_ids参照）。"""
    d, con = _fresh()
    try:
        store = {}
        monkeypatch.setattr(slack_bot, "_slack_post", _fake_poster(store))
        acc = sfa_db.upsert_account(con, name="複数候補社")
        d1 = sfa_db.upsert_deal(con, account_id=acc, deal_name="複数候補案件A", stage="提案")
        d2 = sfa_db.upsert_deal(con, account_id=acc, deal_name="複数候補案件B", stage="提案")
        candidates = [(f"deal:{d1}", "複数候補社/複数候補案件A"), (f"deal:{d2}", "複数候補社/複数候補案件B")]
        slack_bot.post_jamie_candidate_prompt(
            con, inbox_id=1, title="T", occurred_on="2026-08-15",
            candidates=candidates, channel="D0TESTDM")
        actions_block = next(b for b in store["kwargs"]["blocks"] if b.get("type") == "actions")
        action_ids = [el["action_id"] for el in actions_block["elements"]]
        assert len(action_ids) == len(set(action_ids)), f"action_idが重複している: {action_ids}"
        pick_ids = [a for a in action_ids if a.startswith("jamie_pick_deal")]
        assert len(pick_ids) == 2 and len(set(pick_ids)) == 2
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)
