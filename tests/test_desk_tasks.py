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


def test_admin_slack_dedup_concurrent(tmp_path):
    """同一Slackメッセージ(channel+ts)への near-simultaneous な複数起票でも1件に収れんする。
    SELECT→INSERT を _CREATE_LOCK で直列化した回帰テスト（実事故: 1依頼が3枚起票の再発防止）。
    件名を変えて呼ぶ＝AI抽出が毎回違う件名を返す状況を模擬。"""
    import threading
    from cowork import slack_tasks as st
    path = str(tmp_path / "dedup.db")
    sfa_db.init_db(path)
    N = 5
    results: list[int] = []
    barrier = threading.Barrier(N)

    def worker(i: int):
        c = sfa_db.connect(path)
        try:
            barrier.wait()  # 5スレッドの発火をそろえて競合させる
            tid = st.create_task_from_fields(
                c, title=f"請求書作成 案{i}", is_admin=1, ai_category=False,
                slack_channel="C123", slack_ts="1699999999.000100", requester="土屋")
            results.append(tid)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    c = sfa_db.connect(path)
    n = c.execute(
        "SELECT COUNT(*) FROM tasks WHERE slack_channel='C123' AND slack_ts='1699999999.000100' "
        "AND COALESCE(is_admin,0)=1").fetchone()[0]
    c.close()
    assert n == 1, f"重複起票が発生: {n}件"
    assert len(set(results)) == 1, f"戻りidが割れている: {results}"


def test_notify_task_done(con, monkeypatch):
    """完了通知: 起票元スレッドへ返信・Slack登録者のみメンション・未登録は注記・
    双方未登録＆スレッド無しは #OPSへメンションなし投稿、を検証（Slack呼び出しはモック）。"""
    from cowork import slack_tasks as st
    posts = []
    monkeypatch.setattr(st, "_slack_post", lambda method, token=None, **kw: (posts.append((method, kw)) or {"ok": True}))
    monkeypatch.setattr(st, "_slack_user_id_for", lambda name, token=None: {"早瀬": "U_HAYASE"}.get((name or "").strip()))

    # (1) 起票元スレッドあり／依頼者=登録・担当者=未登録
    tid = sfa_db.upsert_task(con, title="月末締め作業", is_admin=1, requester="早瀬", assignee="あみ",
                             slack_channel="C1", slack_ts="111.222", status="対応中")
    assert st.notify_task_done(con, tid, token="xoxb-test") is True
    method, kw = posts[-1]
    assert method == "chat.postMessage"
    assert kw["channel"] == "C1" and kw.get("thread_ts") == "111.222"
    assert "<@U_HAYASE>" in kw["text"]                 # 依頼者=早瀬をメンション
    assert "あみ" in kw["text"] and "見つからない" in kw["text"]   # 担当者=未登録の注記
    assert f"tc-{tid}" in kw["text"]                   # 対象カードリンク

    # (2) スレッド無し＆双方未登録 → OPSチャネルへメンションなし投稿
    monkeypatch.setattr(st, "SLACK_OPS_CHANNEL_ID", "C_OPS")
    tid2 = sfa_db.upsert_task(con, title="X", is_admin=1, requester="不明A", assignee="不明B", status="対応中")
    posts.clear()
    assert st.notify_task_done(con, tid2, token="xoxb-test") is True
    _, kw2 = posts[-1]
    assert kw2["channel"] == "C_OPS" and "thread_ts" not in kw2
    assert "<@" not in kw2["text"]                     # 双方未登録＝メンションなし

    # (3) 非事務タスク(is_admin=0)は通知しない
    posts.clear()
    tid3 = sfa_db.upsert_task(con, title="通常", is_admin=0, status="対応中")
    assert st.notify_task_done(con, tid3, token="xoxb-test") is False
    assert not posts


def test_notify_task_created_dm(con, monkeypatch):
    """起票時DM: 担当者へ簡潔DM（推定緊急度＋依頼者/タスク/期日＋Slackリンク）。
    緊急度はHaiku（モック）で推定。担当未解決/非事務はスキップ。"""
    from cowork import slack_tasks as st
    posts = []
    monkeypatch.setattr(st, "_slack_post", lambda method, token=None, **kw: (posts.append((method, kw)) or {"ok": True}))
    monkeypatch.setattr(st, "_slack_user_id_for", lambda name, token=None: {"あみ": "U_AMI"}.get((name or "").strip()))
    monkeypatch.setattr(st, "_call_claude", lambda prompt: '{"level":"高","reason":"明日締切の請求"}')

    tid = sfa_db.upsert_task(con, title="請求書作成＆提出", is_admin=1, requester="早瀬",
                             assignee="あみ", due_date="2026-08-05",
                             slack_permalink="https://slack.example/archives/C/p1", status="受信箱")
    assert st.notify_task_created(con, tid, source_text="明日まで 請求書 作成", token="xoxb") is True
    method, kw = posts[-1]
    assert method == "chat.postMessage" and kw["channel"] == "U_AMI"     # 担当あみへDM
    txt = kw["text"]
    assert "緊急度: 高" in txt and "🔴" in txt and "明日締切の請求" in txt  # 推定緊急度
    assert "早瀬" in txt and "請求書作成＆提出" in txt and "2026-08-05" in txt  # 依頼者/タスク/期日
    assert "https://slack.example/archives/C/p1" in txt                  # Slackリンク(起票元優先)
    assert txt.count("\n") <= 2   # 3行以内＝簡潔

    # 担当がSlack未解決 → 送らない
    posts.clear()
    tid2 = sfa_db.upsert_task(con, title="X", is_admin=1, assignee="不明", status="受信箱")
    assert st.notify_task_created(con, tid2, token="xoxb") is False and not posts

    # 非事務(is_admin=0) → 送らない
    posts.clear()
    tid3 = sfa_db.upsert_task(con, title="通常", is_admin=0, assignee="あみ", status="受信箱")
    assert st.notify_task_created(con, tid3, token="xoxb") is False and not posts


def test_estimate_urgency_fallback(monkeypatch):
    """Haiku応答が壊れていても中にフォールバックする。"""
    from cowork import slack_tasks as st
    monkeypatch.setattr(st, "_call_claude", lambda prompt: "ごめん、わかりません")  # JSON無し
    level, reason = st._estimate_urgency("何か", due_date=None, category=None)
    assert level == "中"
