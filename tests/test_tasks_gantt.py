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
