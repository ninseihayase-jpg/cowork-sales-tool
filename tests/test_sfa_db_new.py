"""2026-07 追加機能の回帰テスト（次回MS種別・終了理由タグ・MS超過抽出・週次数字パック）。

すべて一時DBに対して行い、本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest

from cowork import sfa_db
from cowork import weekly_report as wr


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_db_new_test_")
    db = str(Path(d) / "t.db")
    sfa_db.init_db(db)
    conn = sfa_db.connect(db)
    try:
        yield conn
    finally:
        conn.close()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def acc_id(con):
    aid = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    con.commit()
    return aid


# ---- 次回MS種別 ----

def test_next_milestone_type_roundtrip(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="提案",
                             next_milestone_type="タスク", status="open")
    d = sfa_db.get_deal(con, did)
    assert d["next_milestone_type"] == "タスク"
    # list_deals も種別を返す（Slack通知が参照する）
    row = next(x for x in sfa_db.list_deals(con, status="open") if x["id"] == did)
    assert row["next_milestone_type"] == "タスク"


def test_next_ms_types_constant(con):
    assert sfa_db.NEXT_MS_TYPES == ["アポ", "タスク"]


# ---- 終了理由タグ ----

def test_close_reason_design(con):
    # close_reason は DEAL_FIELDS に含めない（部分更新のNULL上書き事故回避）
    assert "close_reason" not in sfa_db.DEAL_FIELDS
    assert len(sfa_db.CLOSE_REASONS) == 5
    assert "自社都合で撤退" in sfa_db.CLOSE_REASONS


def test_close_reason_columns_exist(con, acc_id):
    dcols = {r[1] for r in con.execute("PRAGMA table_info(deals)")}
    lcols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    assert "close_reason" in dcols
    assert "lost_reason" in lcols


# ---- MS超過抽出 ----

def test_list_overdue_deals(con, acc_id):
    mk = lambda **k: sfa_db.upsert_deal(con, account_id=acc_id, **k)
    mk(deal_name="超過古", stage="提案", owner="早瀬", next_milestone_date="2026-07-01", status="open")
    mk(deal_name="超過当日", stage="要件詰め", owner="中島", next_milestone_date="2026-07-13", status="open")
    mk(deal_name="未来", stage="提案", owner="早瀬", next_milestone_date="2099-01-01", status="open")
    mk(deal_name="MS無", stage="初回アポ実施", owner="早瀬", status="open")
    mk(deal_name="closed超過", stage="失注", owner="早瀬", next_milestone_date="2026-07-01", status="closed")
    con.commit()

    got = [d["deal_name"] for d in sfa_db.list_overdue_deals(con, today="2026-07-13")]
    assert set(got) == {"超過古", "超過当日"}        # 未来/MS無/closed は除外
    assert got[0] == "超過古"                          # 昇順（最も遅れている順）
    assert [d["deal_name"] for d in sfa_db.list_overdue_deals(con, owner="中島", today="2026-07-13")] == ["超過当日"]


# ---- 週次スナップショット / 数字パック ----

def test_weekly_snapshot_roundtrip(con):
    sfa_db.save_weekly_snapshot(con, "2026-07-06", {"open_deals": 30, "pipeline_lump": 5000})
    sfa_db.save_weekly_snapshot(con, "2026-07-06", {"open_deals": 31})   # upsert（上書き）
    snap = sfa_db.get_weekly_snapshot(con, "2026-07-06")
    assert snap["open_deals"] == 31 and snap["pipeline_lump"] == 5000
    assert sfa_db.list_snapshot_weeks(con) == ["2026-07-06"]


def test_exhibition_funnel_valid_total(con, acc_id):
    mk = lambda **k: sfa_db.upsert_deal(con, account_id=acc_id, lead_pattern="Exh.", **k)
    d1 = mk(deal_name="有望", stage="提案", status="open")
    d2 = mk(deal_name="ニーズ無", stage="失注", status="closed")
    mk(deal_name="受注", stage="受注", status="open")
    con.execute("UPDATE deals SET close_reason='ニーズなし' WHERE id=?", (d2,))
    for dt in ("2026-07-07", "2026-07-09"):
        con.execute("INSERT INTO activities(deal_id,type,occurred_on) VALUES(?,?,?)", (d1, "面談", dt))
    con.commit()
    exh = wr._exhibition_funnel(con)
    assert exh["total"] == 3
    assert exh["no_need"] == 1
    assert exh["valid_total"] == 2          # 総数 - ニーズなし
    assert exh["first_meeting"] == 1 and exh["second_meeting"] == 1 and exh["won"] == 1


def test_backfill_lists(con, acc_id):
    # ①未タグ次回MS: MSありかつ種別無しのopenのみ
    d1 = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="MS未タグ", stage="提案",
                            next_milestone_date="2026-07-20", status="open")
    sfa_db.upsert_deal(con, account_id=acc_id, deal_name="MS済", stage="提案",
                       next_milestone_date="2026-07-20", next_milestone_type="アポ", status="open")
    sfa_db.upsert_deal(con, account_id=acc_id, deal_name="MS無", stage="提案", status="open")
    # ②未分類クローズ: closedかつclose_reason無し
    c1 = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="失注未分類", stage="失注", status="closed")
    c2 = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="失注済", stage="失注", status="closed")
    con.execute("UPDATE deals SET close_reason='失注' WHERE id=?", (c2,))
    # ③未分類lostリード
    con.execute("INSERT INTO leads(name,company,lead_status) VALUES('L1','X','lost')")
    con.execute("INSERT INTO leads(name,company,lead_status,lost_reason) VALUES('L2','Y','lost','キャンセル')")
    con.commit()

    assert [d["id"] for d in sfa_db.list_untyped_milestone_deals(con)] == [d1]
    assert [d["id"] for d in sfa_db.list_unclassified_closed_deals(con)] == [c1]
    assert [l["name"] for l in sfa_db.list_unclassified_lost_leads(con)] == ["L1"]


def test_bulk_tag_appt_by_label(con, acc_id):
    mk = lambda **k: sfa_db.upsert_deal(con, account_id=acc_id, **k)
    a = mk(deal_name="対象", next_milestone_date="2026-07-20", next_milestone_label="初回アポ 13:00", status="open")
    mk(deal_name="当日以前", next_milestone_date="2026-07-13", next_milestone_label="初回アポ", status="open")
    mk(deal_name="別ラベル", next_milestone_date="2026-07-20", next_milestone_label="状況フォロー", status="open")
    mk(deal_name="既にタスク", next_milestone_date="2026-07-20", next_milestone_label="初回アポ",
       next_milestone_type="タスク", status="open")
    mk(deal_name="クローズ", next_milestone_date="2026-07-20", next_milestone_label="初回アポ", status="closed")
    con.commit()

    n = sfa_db.bulk_tag_appt_by_label(con, after_date="2026-07-13")
    assert n == 1                                              # 対象のみ
    assert sfa_db.get_deal(con, a)["next_milestone_type"] == "アポ"
    # 既にタスクのものは上書きされない
    row = [d for d in sfa_db.list_deals(con, status="open") if d["deal_name"] == "既にタスク"][0]
    assert row["next_milestone_type"] == "タスク"


def test_dev_project_tools_crud(con, acc_id):
    deal = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="提案", status="open")
    dp = sfa_db.upsert_dev_project(con, deal_id=deal, theme="テーマ", status="開発中", stage="プロト")
    t1 = sfa_db.add_dev_project_tool(con, dev_project_id=dp, url="https://a.example", label="A")
    sfa_db.add_dev_project_tool(con, dev_project_id=dp, url="https://b.example")
    assert [t["url"] for t in sfa_db.list_dev_project_tools(con, dp)] == ["https://a.example", "https://b.example"]
    # 一括取得（N+1回避）
    batch = sfa_db.list_dev_project_tools_for(con, [dp, 999999])
    assert len(batch[dp]) == 2 and 999999 not in batch
    # 削除
    sfa_db.delete_dev_project_tool(con, t1)
    assert [t["url"] for t in sfa_db.list_dev_project_tools(con, dp)] == ["https://b.example"]
    # 主リンク(tool_url)は dev_projects 側のまま＝Hisho同期契約は不変
    assert "tool_url" in sfa_db.DEV_PROJECT_FIELDS if hasattr(sfa_db, "DEV_PROJECT_FIELDS") else True


def test_weekly_numbers_and_wow(con, acc_id):
    # 先週スナップショット（前週比の基準）
    sfa_db.save_weekly_snapshot(con, "2026-06-29",
                                {"open_deals": 1, "pipeline_lump": 300, "pipeline_recurring": 0, "leads_active": 0})
    d1 = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="今週商談", stage="提案",
                            value_lumpsum=500, status="open")
    con.execute("INSERT INTO activities(deal_id,type,occurred_on) VALUES(?,?,?)", (d1, "面談", "2026-07-08"))
    con.commit()

    wr.record_snapshot(con, as_of=date(2026, 7, 12))          # 今週分を記録
    r = wr.compute_weekly_numbers(con, as_of=date(2026, 7, 12))
    assert r["flow"]["meetings"] == 1
    assert r["stock"]["open_deals"] == 1 and r["stock"]["pipeline_lump"] == 500
    assert r["wow"]["available"] is True
    assert r["wow"]["open_deals"] == 0            # 今週1 - 先週1
    assert r["wow"]["pipeline_lump"] == 200       # 500 - 300
