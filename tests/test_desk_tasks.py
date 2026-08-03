"""事務員向けタスク（is_admin=1 / requester）の分離・保存・絞り込みの最小検証。

一時DBに対して行い、本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_desk_test_")
    path = str(Path(d) / "desk.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_schema_has_admin_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
    assert "is_admin" in cols
    assert "requester" in cols


def test_admin_flag_and_requester_persist(con):
    tid = sfa_db.upsert_task(con, title="交通費精算の入力", is_admin=1,
                             requester="早瀬", category="経費・請求", status="受信箱")
    row = sfa_db.get_task(con, tid)
    assert row["is_admin"] == 1
    assert row["requester"] == "早瀬"
    assert row["category"] == "経費・請求"


def test_list_tasks_admin_filter_separates(con):
    a = sfa_db.upsert_task(con, title="事務A", is_admin=1, requester="田中")
    b = sfa_db.upsert_task(con, title="通常B", is_admin=0)
    c = sfa_db.upsert_task(con, title="通常C")  # is_admin未指定→NULL扱い

    admin_ids = {t["id"] for t in sfa_db.list_tasks(con, admin=True)}
    normal_ids = {t["id"] for t in sfa_db.list_tasks(con, admin=False)}
    all_ids = {t["id"] for t in sfa_db.list_tasks(con)}

    assert admin_ids == {a}
    assert normal_ids == {b, c}           # NULLは通常側に含む（COALESCE）
    assert all_ids == {a, b, c}           # admin=None は両方
    # 交わらない
    assert admin_ids.isdisjoint(normal_ids)


def test_admin_task_categories_defined():
    assert "書類作成" in sfa_db.ADMIN_TASK_CATEGORIES
    assert "その他" in sfa_db.ADMIN_TASK_CATEGORIES
    # 開発系の分類とは別体系
    assert sfa_db.ADMIN_TASK_CATEGORIES != sfa_db.TASK_CATEGORIES


def test_admin_intake_with_assignee_auto_triages_to_未着手(con):
    """Slack起票は既定担当あみ＋期限3営業日後が入るため、受信箱を経ず未着手へ上がる。"""
    from cowork.slack_tasks import create_task_from_fields
    tid = create_task_from_fields(con, title="請求書送付", requester="早瀬",
                                  assignee="あみ", is_admin=1, ai_category=False)
    row = sfa_db.get_task(con, tid)
    assert row["status"] == "未着手"          # 担当＋（既定）期限が揃う→自動整理
    assert (row["due_date"] or "").strip()     # 期限は既定3営業日後で埋まる


def test_admin_intake_without_assignee_stays_受信箱(con):
    """担当未割当の受付は受信箱に留まる（誰がやるか未定のものを溜める）。"""
    from cowork.slack_tasks import create_task_from_fields
    tid = create_task_from_fields(con, title="要トリアージ", requester="早瀬",
                                  assignee=None, is_admin=1, ai_category=False)
    row = sfa_db.get_task(con, tid)
    assert row["status"] == "受信箱"


# ── 繰り返し発生（定期複製） ────────────────────────────────────────────────
from datetime import date


def test_schema_has_recur_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
    assert {"is_recurring", "recur_freq", "recur_dup_day", "recur_last_period"} <= cols


def test_set_task_recur_persists_and_clears(con):
    tid = sfa_db.upsert_task(con, title="月末締め作業", is_admin=1, status="完了")
    sfa_db.set_task_recur(con, tid, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    row = sfa_db.get_task(con, tid)
    assert row["is_recurring"] == 1
    assert row["recur_freq"] == "monthly"
    assert row["recur_dup_day"] == 20
    # OFFにしたら頻度・複製日はクリアされる
    sfa_db.set_task_recur(con, tid, is_recurring=False)
    row = sfa_db.get_task(con, tid)
    assert row["is_recurring"] == 0
    assert row["recur_freq"] is None
    assert row["recur_dup_day"] is None


def test_set_task_recur_invalid_freq_falls_back_monthly(con):
    tid = sfa_db.upsert_task(con, title="X", is_admin=1)
    sfa_db.set_task_recur(con, tid, is_recurring=True, recur_freq="yearly", recur_dup_day=1)
    assert sfa_db.get_task(con, tid)["recur_freq"] == "monthly"


def test_monthly_duplicate_creates_suffixed_card(con):
    tmpl = sfa_db.upsert_task(con, title="月末締め作業", is_admin=1, requester="早瀬",
                              assignee="あみ", category="経費・請求", status="完了")
    sfa_db.set_task_recur(con, tmpl, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    new_ids = sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 20))
    assert len(new_ids) == 1
    dup = sfa_db.get_task(con, new_ids[0])
    assert dup["title"] == "月末締め作業 7月分"
    assert dup["requester"] == "早瀬"
    assert dup["category"] == "経費・請求"
    # 複製カードは通常カード（繰り返しOFF）
    assert (dup["is_recurring"] or 0) == 0
    # 事務タスクは既定期限が入り担当も揃うので受付ルールで未着手へ
    assert dup["status"] == "未着手"
    assert (dup["due_date"] or "").strip()
    # テンプレ側は完了のまま（戻さない）＋期間キー記録
    tmpl_row = sfa_db.get_task(con, tmpl)
    assert tmpl_row["status"] == "完了"
    assert tmpl_row["recur_last_period"] == "2026-07"


def test_monthly_duplicate_is_idempotent(con):
    tmpl = sfa_db.upsert_task(con, title="請求書発行", is_admin=1, assignee="あみ", status="完了")
    sfa_db.set_task_recur(con, tmpl, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    first = sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 20))
    second = sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 21))  # 同一月内の再実行
    assert len(first) == 1
    assert len(second) == 0
    # 通常カード（テンプレ以外）は1件のみ
    n = con.execute("SELECT COUNT(*) FROM tasks WHERE COALESCE(is_recurring,0)=0").fetchone()[0]
    assert n == 1


def test_monthly_next_period_duplicates_again(con):
    tmpl = sfa_db.upsert_task(con, title="月末締め作業", is_admin=1, assignee="あみ", status="完了")
    sfa_db.set_task_recur(con, tmpl, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 20))
    aug = sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 8, 20))  # 翌月
    assert len(aug) == 1
    assert sfa_db.get_task(con, aug[0])["title"] == "月末締め作業 8月分"


def test_monthly_not_due_before_dup_day(con):
    tmpl = sfa_db.upsert_task(con, title="月次レポート", is_admin=1, assignee="あみ", status="完了")
    sfa_db.set_task_recur(con, tmpl, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    assert sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 19)) == []


def test_monthly_dup_day_clamped_to_month_end(con):
    """複製日31日でも2月は末日(28/29)で発火する。"""
    tmpl = sfa_db.upsert_task(con, title="末日タスク", is_admin=1, assignee="あみ", status="完了")
    sfa_db.set_task_recur(con, tmpl, is_recurring=True, recur_freq="monthly", recur_dup_day=31)
    new_ids = sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 2, 28))
    assert len(new_ids) == 1
    assert sfa_db.get_task(con, new_ids[0])["title"] == "末日タスク 2月分"


def test_weekly_duplicate_suffix_and_idempotent(con):
    tmpl = sfa_db.upsert_task(con, title="週次棚卸し", is_admin=1, assignee="あみ", status="完了")
    # 水曜(2)に複製
    sfa_db.set_task_recur(con, tmpl, is_recurring=True, recur_freq="weekly", recur_dup_day=2)
    wed = date(2026, 7, 22)  # 水曜（その週の月曜=7/20）
    first = sfa_db.duplicate_due_recurring_tasks(con, today=wed)
    assert len(first) == 1
    assert sfa_db.get_task(con, first[0])["title"] == "週次棚卸し 7/20週分"
    # 同一週の木曜で再実行 → 増えない
    thu = date(2026, 7, 23)
    assert sfa_db.duplicate_due_recurring_tasks(con, today=thu) == []
    # 翌週の水曜 → また複製
    nxt = sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 29))
    assert len(nxt) == 1
    assert sfa_db.get_task(con, nxt[0])["title"] == "週次棚卸し 7/27週分"


def test_recurring_template_not_duplicated_when_off(con):
    sfa_db.upsert_task(con, title="ただの完了タスク", is_admin=1, status="完了")
    assert sfa_db.duplicate_due_recurring_tasks(con, today=date(2026, 7, 20)) == []


def test_desk_page_renders_recur_ui(con):
    """desk_tasks_page が繰り返しUI（ヘッダの🔁アイコン＋ドロップダウン設定パネル）を描画できる（スモーク）。
    繰り返し発生はピン★の左のアイコンで表示し、ON時は着色（tc-rec-pin on）。"""
    from cowork import webapp
    tid = sfa_db.upsert_task(con, title="月末締め作業", is_admin=1, assignee="あみ")
    sfa_db.set_task_recur(con, tid, is_recurring=True, recur_freq="monthly", recur_dup_day=20)
    html = webapp.desk_tasks_page(con)
    assert "繰り返し発生" in html
    assert f"tcRecurPanel({tid}" in html          # ヘッダアイコンのクリックでパネル開閉
    assert 'class="tc-rec-pin on"' in html         # ON状態は着色アイコン
    assert f"tcRecurOff({tid}" in html             # ON時は繰り返しOFFボタン
    assert "🔁" in html
