"""次回MSライフサイクル是正（新規タスク・2026-08-17）の回帰テスト。

調査で判明した2つのバグの再現・修正確認:
1. slack_bot.apply_to_db が deal_milestones を経由せず deals.next_milestone_* を直接
   書き換えていたため、後で recompute が走ると古い未完了MSへ静かに巻き戻っていた。
2. /hearing/intake/commit 等、add_deal_milestoneで次のMSを追加するだけの経路では、
   活動日以前の旧MSが未完了のまま残り、MS超過に出続けていた。

sfa_db.add_activity() が occurred_on 以前の未完了MSを自動完了する安全網
（complete_past_milestones）を持つことで、どの経路でも一貫して解消されることを確認する。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cowork import sfa_db, slack_bot


def _fresh():
    d = tempfile.mkdtemp(prefix="sfa_msl_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def test_complete_past_milestones_marks_all_stale_ones_done():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-01", label="old1", ms_type="アポ")
        sfa_db.add_deal_milestone(con, did, date="2026-08-05", label="old2", ms_type="アポ")
        sfa_db.add_deal_milestone(con, did, date="2026-08-20", label="future", ms_type="アポ")
        n = sfa_db.complete_past_milestones(con, did, "2026-08-10")
        assert n == 2
        ms = {m["ms_label"]: m["done"] for m in sfa_db.list_deal_milestones(con, did)}
        assert ms["old1"] == 1 and ms["old2"] == 1 and ms["future"] == 0
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_add_activity_auto_completes_past_milestones():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-10", label="初回面談", ms_type="アポ")
        sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-10",
                            contact_name="田中", body="実施済み")
        ms = sfa_db.list_deal_milestones(con, did)
        assert ms[0]["done"] == 1
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_add_activity_without_occurred_on_does_not_touch_milestones():
    """occurred_on が無い活動追加（現状は無いが将来のため）ではMSに触らない。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-10", label="初回面談", ms_type="アポ")
        sfa_db.add_activity(con, deal_id=did, type="メモ", occurred_on=None, body="雑談メモ")
        ms = sfa_db.list_deal_milestones(con, did)
        assert ms[0]["done"] == 0
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_slack_bot_apply_to_db_does_not_revert_after_recompute_trigger():
    """再現した本命バグ: Slack確定後、MSパネル操作等でrecomputeが走っても巻き戻らないこと。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-10", label="初回面談", ms_type="アポ")

        fields = {"種別": "面談", "活動日": "2026-08-10", "相手": "田中", "内容": "初回面談実施",
                  "次回MS日": "2026-08-22", "次回MSラベル": "見積提示", "次回MS種別": "アポ"}
        slack_bot.apply_to_db(con, fields, did)

        deal = sfa_db.get_deal(con, did)
        assert deal["next_milestone_date"] == "2026-08-22"

        old = [m for m in sfa_db.list_deal_milestones(con, did) if m["ms_date"] == "2026-08-10"][0]
        assert old["done"] == 1, "旧MSが自動完了になっていない"

        # MSパネル操作等でrecomputeが再度走っても、巻き戻らないことを確認
        sfa_db.recompute_deal_next_milestone(con, did)
        deal2 = sfa_db.get_deal(con, did)
        assert deal2["next_milestone_date"] == "2026-08-22", (
            "バグ再発: recompute後に古いMSへ巻き戻った")
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_slack_bot_apply_to_db_activity_uses_add_activity_not_raw_sql():
    """活動履歴が sfa_db.add_activity 経由（＝共通処理が効く経路）で記録されること。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-10", label="初回面談", ms_type="アポ")
        fields = {"種別": "面談", "活動日": "2026-08-10", "相手": "田中", "内容": "実施"}
        slack_bot.apply_to_db(con, fields, did)
        act = dict(con.execute("SELECT * FROM activities WHERE deal_id=?", (did,)).fetchone())
        assert act["type"] == "面談" and act["occurred_on"] == "2026-08-10"
        # add_activity経由なら自動完了が効く
        ms = sfa_db.list_deal_milestones(con, did)
        assert ms[0]["done"] == 1
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_hearing_intake_style_flow_no_longer_leaves_stale_overdue_milestone():
    """/hearing/intake/commit相当（add_activity→add_deal_milestone）で旧MSが自動完了すること。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-10", label="初回面談", ms_type="アポ")

        sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-10",
                            contact_name="田中", body="実施済み")
        sfa_db.add_deal_milestone(con, did, date="2026-08-22", label="見積提示", ms_type="アポ")

        deal = sfa_db.get_deal(con, did)
        assert deal["next_milestone_date"] == "2026-08-22"
        old = [m for m in sfa_db.list_deal_milestones(con, did) if m["ms_date"] == "2026-08-10"][0]
        assert old["done"] == 1
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_slack_override_reply_with_slash_date_normalizes_and_does_not_leave_ms_blank():
    """実事故の再現: Slack返信で「次回MS日: 2026/10/31」(スラッシュ区切り)と上書きすると、
    <input type="date">がISO以外の文字列を表示できず「MSが空」に見える不具合の回帰確認。
    collect_fieldsがISOへ正規化することで、next_milestone_dateが実際に設定されること。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="要件詰め")

        messages = [
            {"ts": "100.0", "bot_id": "B1",
             "text": "ステージ: 要件詰め\n次回MS日: 【記載なし】\n次回MSラベル: 金型管理・BCP相談予定\n次回MS種別: アポ"},
            {"ts": "200.0", "user": "U1",
             "text": "活動日: 2026-08-18\n種別: 面談\n内容: 購買システムAX導入を進行中。\n"
                     "次回MS日: 2026/10/31\n次回MSラベル: 金型管理検討状況ヒアリング\n次回MS種別: タスク"},
            {"ts": "300.0", "user": "U1", "text": "ok"},
        ]
        fields = slack_bot.collect_fields(messages, bot_ts="100.0", confirm_ts="300.0")
        assert fields["次回MS日"] == "2026-10-31", "スラッシュ区切りがISOへ正規化されていない"

        slack_bot.apply_to_db(con, fields, did)
        deal = sfa_db.get_deal(con, did)
        assert deal["next_milestone_date"] == "2026-10-31", (
            "バグ再発: next_milestone_dateが設定されていない（画面上はMS空に見える不具合）")
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_slack_override_reply_with_unparseable_date_is_dropped_not_corrupted():
    """パース不能な日付表記（自由記述）は、そのまま保存して壊れたデータを作るより
    未指定扱い（フィールド無視）にする方が安全。"""
    messages = [
        {"ts": "100.0", "bot_id": "B1", "text": "内容: テスト"},
        {"ts": "200.0", "user": "U1", "text": "次回MS日: 来週あたり"},
        {"ts": "300.0", "user": "U1", "text": "ok"},
    ]
    fields = slack_bot.collect_fields(messages, bot_ts="100.0", confirm_ts="300.0")
    assert "次回MS日" not in fields
