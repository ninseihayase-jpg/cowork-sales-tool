"""コンサルタスクのガントチャートビュー(#93拡張)の回帰テスト。

ユーザー確定仕様(2026-08-19):
- 工数感(軽/中/重/超重) -> 所要日数(1/2/5/10営業日、すべて営業日換算)
- 開始日 = 期日から所要日数ぶん営業日逆算（当日/作成日より前でもよい）
- 大分類 = プロジェクト × カテゴリー。グループ間の順序は合計所要日数が多い順。
- グループ内の順序は開始日が古い順（今日以前は一律）→期日が近い順
- 工数感/期日が未設定のタスクは、件数と一覧を明示してガントから除外する（黙って消さない）
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_gantt_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_task_gantt_range_effort_to_business_days():
    assert sfa_db.TASK_EFFORT_DAYS == {"軽": 1, "中": 2, "重": 5, "超重": 10}
    # 2026-08-20(木)から中(2営業日)逆算 -> 2026-08-18(火)
    start, end = sfa_db.task_gantt_range("2026-08-20", "中")
    assert (start, end) == ("2026-08-18", "2026-08-20")


def test_task_gantt_range_missing_inputs():
    assert sfa_db.task_gantt_range(None, "軽") == (None, None)
    start, end = sfa_db.task_gantt_range("2026-08-20", None)
    assert start is None and end == "2026-08-20"
    assert sfa_db.task_gantt_range("2026-08-20", "不正値") == (None, "2026-08-20")


def test_gantt_page_empty_state(con):
    html = webapp.tasks_gantt_page(con)
    assert "まだありません" in html


def test_gantt_groups_ordered_by_total_business_days_desc(con):
    today = webapp._today_jst()
    far = (today + timedelta(days=30)).isoformat()
    # 大分類A: PJ甲×開発、2件で計7営業日(重5+中2)
    sfa_db.upsert_task(con, title="設計書作成", project="PJ甲", category="開発", due_date=far, effort_level="重")
    sfa_db.upsert_task(con, title="レビュー対応", project="PJ甲", category="開発", due_date=far, effort_level="中")
    # 大分類B: PJ乙×調査・検証、1件で計10営業日(超重)。合計はBの方が多い。
    sfa_db.upsert_task(con, title="市場調査", project="PJ乙", category="調査・検証", due_date=far, effort_level="超重")

    html = webapp.tasks_gantt_page(con)
    pos_a = html.find("PJ甲")
    pos_b = html.find("PJ乙")
    assert pos_b != -1 and pos_a != -1
    assert pos_b < pos_a, "合計所要日数が多いグループ(PJ乙=10営業日)が先に出るべき"
    assert "7営業日" in html
    assert "10営業日" in html


def test_gantt_within_group_sorts_by_start_then_due(con):
    """開始日が今日以前(=同列扱い)のタスク同士は、期日が近い順に並ぶこと。"""
    today = webapp._today_jst()
    near_due = (today + timedelta(days=1)).isoformat()
    far_due = (today + timedelta(days=5)).isoformat()
    # どちらも工数感の逆算で開始日が今日以前になるように大きめの工数感を選ぶ
    sfa_db.upsert_task(con, title="遠い期日タスク", project="P", category="C",
                       due_date=far_due, effort_level="重")
    sfa_db.upsert_task(con, title="近い期日タスク", project="P", category="C",
                       due_date=near_due, effort_level="軽")
    html = webapp.tasks_gantt_page(con)
    assert html.find("近い期日タスク") < html.find("遠い期日タスク")


def test_gantt_excludes_completed_tasks(con):
    today = webapp._today_jst().isoformat()
    sfa_db.upsert_task(con, title="完了済みタスク", project="P", category="C",
                       due_date=today, effort_level="軽", status="完了")
    html = webapp.tasks_gantt_page(con)
    assert "完了済みタスク" not in html


def test_gantt_lists_tasks_missing_effort_or_due_without_silently_dropping(con):
    sfa_db.upsert_task(con, title="期日だけ設定", due_date="2026-09-01")
    sfa_db.upsert_task(con, title="工数感だけ設定", effort_level="中")
    html = webapp.tasks_gantt_page(con)
    assert "期日だけ設定" in html
    assert "工数感だけ設定" in html
    assert "2件" in html


def test_gantt_bar_opens_floating_editor_instead_of_navigating(con):
    """#124（2026-08-28）: バー/タイトルをクリックすると看板へ遷移せず、その場のフローティング
    ポップアップで編集できる（gtOpenTask）。GANTT_TASKSに編集対象フィールドが埋め込まれる。"""
    today = webapp._today_jst().isoformat()
    tid = sfa_db.upsert_task(con, title="リンク確認タスク", project="P", category="C",
                             due_date=today, effort_level="軽", assignee="早瀬")
    html = webapp.tasks_gantt_page(con)
    assert f"onclick=\"return gtOpenTask({tid})\"" in html
    assert f"/tasks#tc-{tid}" not in html
    assert f'"{tid}": ' in html or f'"{tid}":' in html  # GANTT_TASKS JSONにこのタスクが含まれる
    assert "function gtOpenTask" in html and "function gtField" in html
    assert "id=\"gtBackdrop\"" in html and "id=\"gtPop\"" in html


def test_gantt_shows_at_least_three_weeks_and_fills_width(con):
    """ユーザー要望2026-08-25: 横軸の日付が3週間くらいは表示され、画面いっぱいを使って
    可視性を高める。タスクの実期間が短くても最低21日(3週間)分の列を確保し、
    列幅は固定pxではなくminmax(...,1fr)で画面幅まで伸びるようにする。"""
    today = webapp._today_jst().isoformat()
    sfa_db.upsert_task(con, title="短期タスク", due_date=today, effort_level="軽")  # 1営業日のみ
    html = webapp.tasks_gantt_page(con)
    import re
    m = re.search(r"repeat\((\d+), minmax\(28px, 1fr\)\)", html)
    assert m, "minmax(28px,1fr)の列テンプレートが見つからない"
    assert int(m.group(1)) >= 21
    assert "width:100%" in html or "width: 100%" in html


def test_gantt_group_tabs_render_with_active_state(con):
    """#124: 「作業種別ごと」「紐づけ単位」タブが両方出て、現在のgroup_byが強調表示される。"""
    html_type = webapp.tasks_gantt_page(con, group_by="type")
    assert 'href="/tasks/gantt?group=type"' in html_type
    assert 'href="/tasks/gantt?group=link"' in html_type
    assert "作業種別ごと" in html_type and "紐づけ単位" in html_type

    html_link = webapp.tasks_gantt_page(con, group_by="link")
    assert "background:#4f46e5;color:#fff" in html_link  # どちらかのタブがアクティブ表示される


def test_gantt_link_grouping_orders_delivery_deal_issue_then_unlinked(con):
    """#124: 紐づけ単位モードは、単位のグループをDelivery→商談→論点→紐づけ無し、の順に並べる。
    グループ内タスクは着手日の早い順（=期日の逆算で開始日が早いものが上）。"""
    today = webapp._today_jst()
    far = (today + timedelta(days=30)).isoformat()
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="商談X", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="DeliveryX")
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")

    sfa_db.upsert_task(con, title="論点タスク", due_date=far, effort_level="軽",
                       link_type="issue", link_id=iid)
    sfa_db.upsert_task(con, title="商談タスク", due_date=far, effort_level="軽",
                       link_type="deal", link_id=did)
    sfa_db.upsert_task(con, title="Deliveryタスク", due_date=far, effort_level="軽",
                       link_type="delivery", link_id=dvid)
    sfa_db.upsert_task(con, title="紐づけ無しタスク", due_date=far, effort_level="軽")

    html = webapp.tasks_gantt_page(con, group_by="link")
    pos_delivery = html.find("🚚")
    pos_deal = html.find("🤝")
    pos_issue = html.find("📌")
    pos_none = html.find("（紐づけ無し）")
    assert -1 not in (pos_delivery, pos_deal, pos_issue, pos_none)
    assert pos_delivery < pos_deal < pos_issue < pos_none


def test_gantt_link_grouping_within_group_sorted_by_start_date(con):
    """紐づけ単位モードでも、グループ内は開始日が古い順（従来の並び順と同じロジック）。"""
    today = webapp._today_jst()
    near = (today + timedelta(days=5)).isoformat()
    far = (today + timedelta(days=25)).isoformat()
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="商談Y", stage="受注")
    sfa_db.upsert_task(con, title="遠い方", due_date=far, effort_level="軽",
                       link_type="deal", link_id=did)
    sfa_db.upsert_task(con, title="近い方", due_date=near, effort_level="軽",
                       link_type="deal", link_id=did)
    html = webapp.tasks_gantt_page(con, group_by="link")
    assert html.find("近い方") < html.find("遠い方")


def test_gantt_day_axis_labels_every_day(con):
    """#126: 日付ラベルは月曜/月初だけでなく毎日記載する（月初のみ月/日、他は日のみ）。"""
    today = webapp._today_jst()
    far = (today + timedelta(days=10)).isoformat()
    sfa_db.upsert_task(con, title="日付ラベル確認", due_date=far, effort_level="軽")
    html = webapp.tasks_gantt_page(con)
    import re
    labels = re.findall(r'class="gantt-daylabel[^"]*"[^>]*>([^<]*)</div>', html)
    assert labels, "日付ラベルのセルが見つからない"
    assert all(lbl.strip() != "" for lbl in labels), "全ての日に日付が入っていること"


# ── #152(2026-09-03): バーのドラッグ移動・リサイズ ──
# ユーザー要望: ガント画面でスケジュールをドラッグ&幅変更で修正できるようにしたい。
# 幅変更は日付線にスナップし、ドラッグ中は変更後の日付が見えるようにする。
# tasks.gantt_start_date（新規カラム）に手動調整後の開始日を保存し、以後は
# 自動計算（期日からの営業日逆算・容量スケジュール）より優先する。

def test_schema_has_gantt_start_date_column(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
    assert "gantt_start_date" in cols


def test_gantt_bar_has_drag_and_resize_attributes(con):
    today = webapp._today_jst().isoformat()
    tid = sfa_db.upsert_task(con, title="ドラッグ確認タスク", project="P", category="C",
                             due_date=today, effort_level="軽", assignee="早瀬")
    html = webapp.tasks_gantt_page(con)
    assert f'data-tid="{tid}"' in html
    assert 'draggable="true"' in html
    assert 'class="gt-grip gt-grip-l"' in html
    assert 'class="gt-grip gt-grip-r"' in html
    assert f"onclick=\"return gtBarClick(event,{tid})\"" in html
    assert "function gtBarClick" in html
    assert "GANTT_MIN_DATE" in html and "GANTT_NUM_DAYS" in html
    assert "gt-drag-preview" in html


def test_gantt_start_date_override_takes_precedence_over_effort_level(con):
    """gantt_start_date（ドラッグ移動/リサイズで保存される値）が設定されていれば、
    effort_levelからの自動計算より優先してバー開始位置に使われる。"""
    today = webapp._today_jst()
    due = (today + timedelta(days=10)).isoformat()
    manual_start = (today + timedelta(days=3)).isoformat()
    tid = sfa_db.upsert_task(con, title="手動調整タスク", due_date=due, effort_level="軽")
    con.execute("UPDATE tasks SET gantt_start_date=? WHERE id=?", (manual_start, tid))
    con.commit()
    html = webapp.tasks_gantt_page(con)
    assert f'data-start="{manual_start}"' in html
    assert f'data-end="{due}"' in html


def test_gantt_without_override_still_uses_effort_level_calc(con):
    """gantt_start_date未設定のタスクは従来通りtask_gantt_rangeの計算結果を使う（回帰確認）。"""
    tid = sfa_db.upsert_task(con, title="自動計算タスク", due_date="2026-08-20", effort_level="中")
    html = webapp.tasks_gantt_page(con)
    assert 'data-start="2026-08-18"' in html
    assert 'data-end="2026-08-20"' in html
    assert str(tid) in html


def test_task_field_route_accepts_gantt_start_date(con, monkeypatch, tmp_path):
    """POST /task/{id}/field がgantt_start_dateを受け付けて保存すること。"""
    import base64
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    con.close()  # 別プロセス相当のサーバー用に新しいDBファイルで検証する
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con2, title="X", due_date="2026-09-10", effort_level="軽")
    con2.close()

    monkeypatch.setattr(webapp, "SFA_BASIC_USER", "u")
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        token = base64.b64encode(b"u:p").decode()
        body = urllib.parse.urlencode({"field": "gantt_start_date", "value": "2026-09-05"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/task/{tid}/field", data=body,
            headers={"Authorization": f"Basic {token}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.getcode() == 200
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT gantt_start_date FROM tasks WHERE id=?", (tid,)).fetchone()
    con3.close()
    assert row["gantt_start_date"] == "2026-09-05"
