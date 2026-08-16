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
