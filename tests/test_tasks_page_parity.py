"""通常タスク看板(/tasks)への事務タスク機能移植(#93)の回帰テスト。

移植対象: Slackパーマリンク紐付け・繰り返し発生設定パネル・緊急度フィルタの5種化・
最優先ピンのみフィルタ・上部集計ボックス・担当フィルタの「受信箱(未割当のみ)」選択肢・
削除済みタスクの確認・復活ビュー。
依頼者(requester)入力・ボード埋め込み起票フォーム・請求分類一括修正・全削除ルートは
事務タスク専用のまま移植不要（ユーザー確定）。
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_tasks_parity_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_tasks_page_renders_slack_link_and_add_button(con):
    with_link = sfa_db.upsert_task(con, title="リンクあり", slack_permalink="https://slack.com/archives/C1/p1")
    without_link = sfa_db.upsert_task(con, title="リンクなし")
    html = webapp.tasks_page(con)
    assert "🔗 Slack" in html
    assert "🔗 リンク追加" in html
    assert f"tcSlack({with_link})" in html
    assert f"tcSlack({without_link})" in html


def test_tasks_page_renders_recur_ui(con):
    """desk_tasks_pageのtest_desk_page_renders_recur_uiと対になる通常タスク側の確認。"""
    tid = sfa_db.upsert_task(con, title="月次レポート作成")
    sfa_db.set_task_recur(con, tid, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    html = webapp.tasks_page(con)
    assert "繰り返し発生" in html
    assert f"tcRecurPanel({tid}" in html
    assert 'class="tc-rec-pin on"' in html
    assert f"tcRecurOff({tid}" in html


def test_recurring_duplication_already_works_for_normal_tasks(con):
    """duplicate_due_recurring_tasksはis_admin不問の共通ロジック（#93調査で確認済み）。
    通常タスク(is_admin=0)でも複製が実際に発生することの回帰確認。"""
    tid = sfa_db.upsert_task(con, title="週次棚卸し", is_admin=0)
    sfa_db.set_task_recur(con, tid, is_recurring=True, recur_freq="weekly", recur_dup_day=2)  # 水曜
    wed = date(2026, 7, 22)
    new_ids = sfa_db.duplicate_due_recurring_tasks(con, today=wed)
    assert len(new_ids) == 1
    dup = sfa_db.get_task(con, new_ids[0])
    assert dup["is_admin"] in (0, None)
    assert "週次棚卸し" in dup["title"]


def test_urgency_filter_has_five_buckets_like_desk(con):
    # 実行日を基準にした相対日付で組む（tasks_pageは内部で_today_jst()＝実行時の実日付を使うため、
    # ハードコードした日付だと日をまたいだ瞬間にoverdue/today/tomorrowの分類がズレて壊れる）。
    today = webapp._today_jst()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_s = today.isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    sfa_db.upsert_task(con, title="超過タスク", due_date=yesterday)
    sfa_db.upsert_task(con, title="今日タスク", due_date=today_s)
    sfa_db.upsert_task(con, title="明日タスク", due_date=tomorrow)
    sfa_db.upsert_task(con, title="期限なしタスク")
    sfa_db.upsert_task(con, title="保留タスク", due_date=yesterday, status="保留")

    def titles(urgency):
        html = webapp.tasks_page(con, urgency=urgency)
        return {t: (t in html) for t in
                ("超過タスク", "今日タスク", "明日タスク", "期限なしタスク", "保留タスク")}

    # 保留中は期限アラート(overdue/today/tomorrow)の対象外
    r = titles("overdue")
    assert r["超過タスク"] and not r["保留タスク"] and not r["今日タスク"]
    r = titles("today")
    assert r["今日タスク"] and not r["超過タスク"]
    r = titles("tomorrow")
    assert r["明日タスク"] and not r["今日タスク"]
    r = titles("nodue")
    assert r["期限なしタスク"] and not r["超過タスク"]
    r = titles("hold")
    assert r["保留タスク"] and not r["超過タスク"]


def test_pinned_only_filter_and_aggregate_count(con):
    pinned_id = sfa_db.upsert_task(con, title="最優先タスク")
    normal_id = sfa_db.upsert_task(con, title="通常タスク")
    con.execute("UPDATE tasks SET pinned=1 WHERE id=?", (pinned_id,))
    con.commit()

    all_html = webapp.tasks_page(con)
    assert '最優先ピン <b data-agg-count="pinned">1</b>' in all_html

    pinned_html = webapp.tasks_page(con, pinned=True)
    assert "最優先タスク" in pinned_html
    assert "通常タスク" not in pinned_html


def test_assignee_inbox_only_filter(con):
    assigned = sfa_db.upsert_task(con, title="割当済み", assignee="早瀬")
    unassigned = sfa_db.upsert_task(con, title="未割当")
    html = webapp.tasks_page(con, assignee="__none__")
    assert "受信箱（未割当のみ）" in html  # フィルタのoption自体
    assert "未割当" in html
    assert "割当済み" not in html


def test_deleted_tasks_view_and_restore_link(con):
    tid = sfa_db.upsert_task(con, title="消したタスク")
    sfa_db.delete_task(con, tid)
    normal_html = webapp.tasks_page(con)
    assert "消したタスク" not in normal_html
    assert '/tasks?deleted=1' in normal_html

    deleted_html = webapp.tasks_page(con, deleted=True)
    assert "消したタスク" in deleted_html
    assert f'/task/{tid}/restore' in deleted_html
    assert 'value="/tasks?deleted=1"' in deleted_html

    sfa_db.restore_task(con, tid)
    restored_html = webapp.tasks_page(con)
    assert "消したタスク" in restored_html
