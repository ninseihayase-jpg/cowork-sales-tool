"""社内PJ「進捗報告」機能(2026-09-13)のDB層(sfa_db)回帰テスト。

既存の「社内PJメモ」(rich_notes, kind='issue')とは別建てで、編集した日付でverが
更新されていく定型レポート。②サマリー〜⑥次回予定の本文は、行ラベル/プレースホルダーを
毎回サーバ側でフレッシュに描画できるよう、セクションごとに個別の列(<key>_html)として
持つ（1本のtable HTMLとしては持たない。理由はsfa_db.py内SCHEMAコメント参照）。

バージョニング規則（ユーザー確定）:
- 過去ver(report_date<今日)は読み取り専用。
- 「編集」操作(open_progress_report_for_edit)は常に最新verに対して行われる。最新verの
  report_dateが今日ならそのまま返す（＝同日内の再編集は上書き、新verを作らない）。
  今日でなければ、最新verの内容を引き継いだ新verが今日日付で自動生成される。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_progress_report_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _issue(con, issue="論点A"):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X", status="open")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue=issue)


# ── スキーマ ──

def test_schema_has_progress_reports_table_with_all_section_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(deal_issue_progress_reports)")}
    assert {"issue_id", "report_date", "status_signal", "purpose_tag"} <= cols
    for key in sfa_db.PROGRESS_REPORT_SECTION_KEYS:
        assert f"{key}_html" in cols


def test_progress_report_section_keys_cover_all_ten_rows():
    """①ヘッダー(status/purpose)を除く②〜⑥の全セクション(リスク6区分を含む)が揃っていること。"""
    assert sfa_db.PROGRESS_REPORT_SECTION_KEYS == [
        "summary", "progress", "decision", "risk_schedule", "risk_budget",
        "risk_quality", "risk_external", "risk_compliance", "risk_other", "next_steps",
    ]


def test_schema_enforces_unique_issue_and_report_date(con):
    """同一issue×同一report_dateは1行のみ（バージョニング規則の整合性を担保する安全網）。"""
    iid = _issue(con)
    sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        sfa_db.create_progress_report(con, iid, report_date="2026-09-06")


# ── CRUD基本 ──

def test_create_and_get_progress_report(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(
        con, iid, report_date="2026-08-30", status_signal="yellow", purpose_tag="discuss",
        sections={"summary": "<div>順調です</div>", "decision": "<div>論点：〇〇</div>"})
    assert row["issue_id"] == iid
    assert row["report_date"] == "2026-08-30"
    assert row["status_signal"] == "yellow"
    assert row["purpose_tag"] == "discuss"
    assert row["summary_html"] == "<div>順調です</div>"
    assert row["decision_html"] == "<div>論点：〇〇</div>"
    assert row["progress_html"] == ""  # 渡さなかったセクションは空文字のまま
    fetched = sfa_db.get_progress_report(con, row["id"])
    assert fetched == row


def test_create_progress_report_defaults_all_sections_empty_when_no_sections_given(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    for key in sfa_db.PROGRESS_REPORT_SECTION_KEYS:
        assert row[f"{key}_html"] == ""


def test_get_progress_report_missing_returns_none(con):
    assert sfa_db.get_progress_report(con, 999999) is None


def test_list_progress_reports_orders_newest_first(con):
    iid = _issue(con)
    sfa_db.create_progress_report(con, iid, report_date="2026-08-16")
    sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    sfa_db.create_progress_report(con, iid, report_date="2026-08-30")
    rows = sfa_db.list_progress_reports(con, iid)
    assert [r["report_date"] for r in rows] == ["2026-09-06", "2026-08-30", "2026-08-16"]


def test_list_progress_reports_scoped_to_issue(con):
    i1 = _issue(con, issue="論点A")
    i2 = _issue(con, issue="論点B")
    sfa_db.create_progress_report(con, i1, report_date="2026-09-01")
    sfa_db.create_progress_report(con, i2, report_date="2026-09-01")
    assert len(sfa_db.list_progress_reports(con, i1)) == 1
    assert len(sfa_db.list_progress_reports(con, i2)) == 1


def test_get_latest_progress_report(con):
    iid = _issue(con)
    sfa_db.create_progress_report(con, iid, report_date="2026-08-30")
    latest = sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    assert sfa_db.get_latest_progress_report(con, iid)["id"] == latest["id"]


def test_get_latest_progress_report_none_when_empty(con):
    iid = _issue(con)
    assert sfa_db.get_latest_progress_report(con, iid) is None


def test_update_progress_report_partial_section_does_not_clobber_other_sections(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(
        con, iid, report_date="2026-09-06", status_signal="green", purpose_tag="share",
        sections={"summary": "元のサマリー", "progress": "元の進捗"})
    sfa_db.update_progress_report(con, row["id"], sections={"summary": "更新後のサマリー"})
    fetched = sfa_db.get_progress_report(con, row["id"])
    assert fetched["summary_html"] == "更新後のサマリー"
    assert fetched["progress_html"] == "元の進捗"  # 触っていないセクションは残る
    assert fetched["status_signal"] == "green"
    assert fetched["purpose_tag"] == "share"


def test_update_progress_report_status_and_purpose_independent_of_sections(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(con, iid, report_date="2026-09-06",
                                        sections={"summary": "元"})
    sfa_db.update_progress_report(con, row["id"], status_signal="red", purpose_tag="decide")
    fetched = sfa_db.get_progress_report(con, row["id"])
    assert fetched["status_signal"] == "red"
    assert fetched["purpose_tag"] == "decide"
    assert fetched["summary_html"] == "元"


def test_update_progress_report_ignores_unknown_section_keys(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    sfa_db.update_progress_report(con, row["id"], sections={"not_a_real_section": "x"})
    # 例外にならず、既知の列も一切変わらないこと
    fetched = sfa_db.get_progress_report(con, row["id"])
    assert fetched["summary_html"] == ""


def test_update_progress_report_without_args_is_noop(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(con, iid, report_date="2026-09-06", sections={"summary": "元"})
    sfa_db.update_progress_report(con, row["id"])
    assert sfa_db.get_progress_report(con, row["id"])["summary_html"] == "元"


def test_delete_progress_report(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    sfa_db.delete_progress_report(con, row["id"])
    assert sfa_db.get_progress_report(con, row["id"]) is None


def test_deleting_issue_cascades_progress_reports(con):
    iid = _issue(con)
    row = sfa_db.create_progress_report(con, iid, report_date="2026-09-06")
    con.execute("DELETE FROM deal_issues WHERE id=?", (iid,))
    con.commit()
    assert sfa_db.get_progress_report(con, row["id"]) is None


# ── バージョニング規則: open_progress_report_for_edit ──

def test_open_for_edit_creates_first_version_when_none_exists(con):
    iid = _issue(con)
    report = sfa_db.open_progress_report_for_edit(
        con, iid, today="2026-08-30", default_sections={"summary": "雛形サマリー"})
    assert report["report_date"] == "2026-08-30"
    assert report["summary_html"] == "雛形サマリー"
    assert report["status_signal"] == "green"
    assert report["purpose_tag"] == "share"
    assert len(sfa_db.list_progress_reports(con, iid)) == 1


def test_open_for_edit_same_day_returns_existing_version_without_creating_new_one(con):
    """同日内の再編集は上書き（新verは作らない）。"""
    iid = _issue(con)
    first = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    sfa_db.update_progress_report(con, first["id"], sections={"summary": "編集済み本文"})
    reopened = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    assert reopened["id"] == first["id"]
    assert reopened["summary_html"] == "編集済み本文"
    assert len(sfa_db.list_progress_reports(con, iid)) == 1


def test_open_for_edit_new_day_forks_new_version_carrying_forward_content(con):
    """最新verの日付が今日でなければ、その内容を引き継いだ新verが今日日付で自動生成される。"""
    iid = _issue(con)
    aug30 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30")
    sfa_db.update_progress_report(con, aug30["id"], status_signal="yellow", purpose_tag="decide",
                                  sections={"summary": "8/30時点の内容", "next_steps": "次週予定"})
    sep06 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    assert sep06["id"] != aug30["id"]
    assert sep06["report_date"] == "2026-09-06"
    assert sep06["summary_html"] == "8/30時点の内容"  # 前verの内容を引き継ぐ
    assert sep06["next_steps_html"] == "次週予定"
    assert sep06["status_signal"] == "yellow"
    assert sep06["purpose_tag"] == "decide"
    # 過去verは変更されず残る（読み取り専用の履歴として保持）
    aug30_after = sfa_db.get_progress_report(con, aug30["id"])
    assert aug30_after["summary_html"] == "8/30時点の内容"
    assert aug30_after["report_date"] == "2026-08-30"
    assert len(sfa_db.list_progress_reports(con, iid)) == 2


def test_open_for_edit_does_not_fork_repeatedly_on_repeated_calls_same_new_day(con):
    """新しい日付での「編集」を複数回押しても、フォークは1回だけ（2回目以降は既存の
    今日日付verをそのまま返す＝上記の同日内挙動と合流する）。"""
    iid = _issue(con)
    sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30")
    first_sep06 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    second_sep06 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    assert first_sep06["id"] == second_sep06["id"]
    assert len(sfa_db.list_progress_reports(con, iid)) == 2


def test_open_for_edit_past_versions_remain_untouched_after_multiple_forks(con):
    """複数世代を経ても、各過去verの内容がそれぞれのタイミングの状態のまま保持されること
    （フォークは常に「引き継ぎ元をコピーして新verを作る」だけで、古いverを書き換えない）。"""
    iid = _issue(con)
    v1 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-16")
    sfa_db.update_progress_report(con, v1["id"], sections={"summary": "8/16の内容"})
    v2 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30")
    sfa_db.update_progress_report(con, v2["id"], sections={"summary": "8/30の内容"})
    v3 = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    sfa_db.update_progress_report(con, v3["id"], sections={"summary": "9/6の内容"})

    assert sfa_db.get_progress_report(con, v1["id"])["summary_html"] == "8/16の内容"
    assert sfa_db.get_progress_report(con, v2["id"])["summary_html"] == "8/30の内容"
    assert sfa_db.get_progress_report(con, v3["id"])["summary_html"] == "9/6の内容"
    rows = sfa_db.list_progress_reports(con, iid)
    assert [r["report_date"] for r in rows] == ["2026-09-06", "2026-08-30", "2026-08-16"]


def test_open_for_edit_default_sections_only_used_when_no_prior_version(con):
    """default_sectionsは「1件も無い時の初期値」としてのみ使う。既存verがあれば
    その内容が優先され、default_sectionsは無視される。"""
    iid = _issue(con)
    sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30",
                                         default_sections={"summary": "初回用の雛形"})
    sfa_db.update_progress_report(
        con, sfa_db.get_latest_progress_report(con, iid)["id"], sections={"summary": "実際の内容"})
    forked = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06",
                                                  default_sections={"summary": "無視されるはずの雛形"})
    assert forked["summary_html"] == "実際の内容"
