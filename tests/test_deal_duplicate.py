"""商談複製(duplicate_deal)のユーザー確定方針の回帰テスト。

確定方針:
- ステージ: 先頭ステージへリセット（複製元のステージは引き継がない）
- 現状メモ: 複製である旨を明示した上で引き継ぐ
- 活動履歴・マイルストーン・ヒアリング結果・論点・Hisho同期状態・Slack紐付け・終了理由・
  次回MSキャッシュは引き継がない（新規案件として真っ白から始める）
- アカウント・担当・案件名（+コピー表記）・金額系・分類属性はそのまま引き継ぐ
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_dup_deal_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _make_source_deal(con):
    acc = sfa_db.upsert_account(con, name="加藤製作所")
    did = sfa_db.upsert_deal(
        con, account_id=acc, deal_name="制御システム部 岸本 昌平 技術アドバイザー",
        stage="クロージング", owner="吉江", sub_owner="田中",
        client_contact="岸本", client_dept="制御システム部",
        value_lumpsum=500, value_recurring=10, client_budget="500万円",
        note="【株式会社加藤製作所】業界:製造業\n■課題: XXX", goal="受注最大化",
        importance="高", cost_stage="診断", approach_value=1.5, fee_rate=20,
    )
    sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-10", body="面談メモ")
    sfa_db.add_deal_milestone(con, did, date="2026-09-05", label="次回アポ", ms_type="アポ")
    con.execute("UPDATE deals SET exhibition_name=?, theme_id=?, slack_notified_date=?, "
                "close_reason=? WHERE id=?", ("〇〇展示会", 1454, "2026-08-01", None, did))
    con.commit()
    return acc, did


def test_stage_resets_to_first_master_stage(con):
    acc, did = _make_source_deal(con)
    new_id = sfa_db.duplicate_deal(con, did)
    new = sfa_db.get_deal(con, new_id)
    first_stage = (sfa_db.get_master_list(con, "deal_stages") or sfa_db.DEAL_STAGES)[0]
    assert new["stage"] == first_stage
    assert sfa_db.get_deal(con, did)["stage"] == "クロージング"  # 元は不変


def test_note_carries_over_with_copy_marker(con):
    acc, did = _make_source_deal(con)
    new_id = sfa_db.duplicate_deal(con, did)
    new = sfa_db.get_deal(con, new_id)
    assert f"SFA#{did}" in new["note"]
    assert "複製です" in new["note"]
    assert "課題: XXX" in new["note"]  # 元の内容も残る


def test_deal_name_gets_copy_suffix_and_status_is_open(con):
    acc, did = _make_source_deal(con)
    new_id = sfa_db.duplicate_deal(con, did)
    new = sfa_db.get_deal(con, new_id)
    assert new["deal_name"] == "制御システム部 岸本 昌平 技術アドバイザー（コピー）"
    assert new["status"] == "open"


def test_simple_attributes_are_carried_over(con):
    acc, did = _make_source_deal(con)
    new_id = sfa_db.duplicate_deal(con, did)
    new = sfa_db.get_deal(con, new_id)
    assert new["account_id"] == acc
    assert new["owner"] == "吉江"
    assert new["sub_owner"] == "田中"
    assert new["client_contact"] == "岸本"
    assert new["client_dept"] == "制御システム部"
    assert new["value_lumpsum"] == 500
    assert new["value_recurring"] == 10
    assert new["client_budget"] == "500万円"
    assert new["goal"] == "受注最大化"
    assert new["importance"] == "高"
    assert new["cost_stage"] == "診断"
    assert new["approach_value"] == 1.5
    assert new["fee_rate"] == 20
    assert new["exhibition_name"] == "〇〇展示会"


def test_deal_specific_history_and_sync_state_are_not_copied(con):
    acc, did = _make_source_deal(con)
    new_id = sfa_db.duplicate_deal(con, did)

    assert sfa_db.list_activities(con, new_id) == []
    assert sfa_db.list_deal_milestones(con, new_id) == []

    new = sfa_db.get_deal(con, new_id)
    assert new["theme_id"] is None
    assert not new.get("close_reason")
    assert not new.get("slack_notified_date")
    assert not new.get("next_milestone_date")
    assert not new.get("next_milestone_label")

    # 元商談の履歴は無傷
    assert len(sfa_db.list_activities(con, did)) == 1
    assert len(sfa_db.list_deal_milestones(con, did)) == 1


def test_duplicate_nonexistent_deal_returns_none(con):
    assert sfa_db.duplicate_deal(con, 999999) is None
