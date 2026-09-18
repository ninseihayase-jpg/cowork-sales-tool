"""社内PJ管理ガント(/deal-issues/gantt)の行間隙間/日付行固定/初期表示位置(2026-09-18)の回帰テスト。

ユーザー報告（画像付き）:
「メインタスクとサブタスクが離れて表示されるから、くっつくように。全体的にスキマが大きくて
見づらい。日付行を固定表示して、かつデフォルトで当日の1週間前を一番左にあわせて表示するように。
（いまは一番古い8/1から常に表示されていて、直近のタスクがわかりづらい）」

原因: タスク追加用の予約行(_ig_add_row_html、既定display:none)がgrid-auto-rows:26pxに
より非表示でも26pxの空白行として描画され、メインタスク→サブタスクの間・PJ見出し→
最初のメインタスクの間に常に1行分の隙間ができていた。
1回目の修正（grid-template-rows + 予約行だけminmax(0,26px)）は実機で0に収縮せず
隙間が残った（ユーザー再報告2026-09-18「変わらず、メインタスクの下に変なスキマ行がある」）。
2回目の修正: 予約行は常に0pxで生成し、「＋」クリック時にJS(_igSetRowHeight)がその
行番号だけ26pxへ明示的に書き換える確実な方式に変更した（他の行は26px固定で変わらない）。

併せて日付行(.gantt-daylabel/.gantt-corner)にposition:sticky;top:0を付与し、
初期表示のスクロール位置を「当日の1週間前」に合わせるJSを追加した。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_ig_rowgap_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _issue(con, acc_name="A社", deal_name="X", issue="論点A"):
    acc = sfa_db.upsert_account(con, name=acc_name)
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name=deal_name, status="open")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue=issue)


def _grid_template_rows(html: str) -> list[str]:
    m = re.search(r'class="gantt-grid" style="[^"]*grid-template-rows:([^;"]+);', html)
    assert m, "grid-template-rowsが見つからない"
    return m.group(1).strip().split(" ")


# ── 予約行(add-wrap)の高さ収縮（既定0px） ──

def test_main_add_reserved_row_is_zero_between_pj_header_and_first_main_task(con):
    """PJ見出し行の直後（メインタスク追加の予約行）は既定0pxで、メインタスクが
    PJ見出しにくっついて見える。"""
    _issue(con, issue="論点A")
    sfa_db.create_deal_issue_subitem(con, sfa_db.list_deal_issues(con)[0]["id"],
                                     "メインタスク1", "2026-09-10", "2026-09-20")
    html = webapp.deal_issues_gantt_page(con)
    rows = _grid_template_rows(html)
    # row1=日付行, row2=カテゴリ帯, row3=PJ見出し, row4=メインタスク追加予約行(0px対象),
    # row5=メインタスク本体。
    assert rows[3] == "0px", f"予約行が0pxになっていない: {rows}"
    assert rows[4] == "26px"


def test_sub_add_reserved_row_is_zero_between_main_and_first_subtask_or_next_main(con):
    """メインタスクの直後（サブタスク追加の予約行）も既定0px＝サブタスクや次の
    メインタスクがくっついて見える（ユーザー報告の核心）。"""
    iid = _issue(con, issue="論点A")
    main_id = sfa_db.create_deal_issue_subitem(con, iid, "メインタスク1", "2026-09-10", "2026-09-20")
    sfa_db.create_deal_issue_subitem(con, iid, "サブタスク1", "2026-09-12", "2026-09-15",
                                     parent_id=main_id)
    html = webapp.deal_issues_gantt_page(con)
    rows = _grid_template_rows(html)
    # row1=日付行,row2=カテゴリ帯,row3=PJ見出し,row4=メイン追加予約行,row5=メインタスク,
    # row6=サブ追加予約行(0px対象),row7=サブタスク本体。
    assert rows[5] == "0px", f"サブタスク直前の予約行が0pxになっていない: {rows}"
    assert rows[6] == "26px"


def test_two_childless_main_tasks_back_to_back_have_zero_gap_row(con):
    """ユーザー報告の再現ケース: 子を持たないメインタスクが連続する場合も、間の
    サブタスク追加予約行は0px（子の有無に関わらず必ず挿入される行のため要確認）。"""
    iid = _issue(con, issue="マーケ")
    sfa_db.create_deal_issue_subitem(con, iid, "マーケ手法の洗い出し", "2026-09-01", "2026-09-30")
    sfa_db.create_deal_issue_subitem(con, iid, "ファンドABM準備", "2026-09-01", "2026-09-30")
    html = webapp.deal_issues_gantt_page(con)
    rows = _grid_template_rows(html)
    # row5=タスク1, row6=間の予約行(0px), row7=タスク2
    assert rows[4] == "26px"
    assert rows[5] == "0px", f"連続するメインタスク間の予約行が0pxになっていない: {rows}"
    assert rows[6] == "26px"


def test_reserved_rows_still_exist_for_inline_add_toggle(con):
    """収縮対象でも行自体（予約枠）は残っており、「＋」クリック時のインライン追加UIは
    従来通り動作すること（.ig-add-wrap自体は削除しない）。行番号はdata-row-noで
    JSに伝えられ、開閉時にその行だけ26px/0pxへ書き換えられる。"""
    iid = _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert f'data-issue-id="{iid}"' in html
    assert "ig-add-wrap" in html
    assert "display:none" in html
    assert re.search(r'data-row-no="\d+"', html)


def test_js_toggle_functions_rewrite_specific_row_height(con):
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert "function _igSetRowHeight(rowNo, height)" in html
    assert "_igSetRowHeight(parseInt(wrap.dataset.rowNo,10),'26px')" in html
    assert "_igSetRowHeight(parseInt(wrap.dataset.rowNo,10),'0px')" in html


def test_content_rows_unaffected_by_row_collapse_fix(con):
    """実データを持つ行（日付行・カテゴリ帯・PJ見出し・タスク本体）は従来通り26px固定
    のままで、意図せず縮んでいないこと。"""
    iid = _issue(con, issue="論点A")
    sfa_db.create_deal_issue_subitem(con, iid, "メインタスク1", "2026-09-10", "2026-09-20")
    html = webapp.deal_issues_gantt_page(con)
    rows = _grid_template_rows(html)
    assert rows[0] == "26px"  # 日付行
    assert rows[1] == "26px"  # カテゴリ帯
    assert rows[2] == "26px"  # PJ見出し
    assert rows[4] == "26px"  # メインタスク本体


# ── 日付行の固定表示（sticky） ──

def test_day_label_and_corner_cells_are_sticky_top(con):
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert ".gantt-daylabel{position:sticky;top:0" in html
    assert ".gantt-corner{position:sticky;top:0;left:0" in html
    assert 'class="gantt-lbl grp gantt-corner"' in html


def test_tasks_gantt_page_also_has_sticky_day_labels(con):
    """コンサルタスクガント（tasks_gantt_page）も同じCSSを共有しており、同様にsticky。"""
    html = webapp.tasks_gantt_page(con)
    assert ".gantt-daylabel{position:sticky;top:0" in html


# ── 初期表示位置（当日の1週間前を左端に） ──

def test_default_scroll_positions_one_week_before_today(con):
    _issue(con, issue="論点A")
    sfa_db.create_deal_issue_subitem(con, sfa_db.list_deal_issues(con)[0]["id"],
                                     "メインタスク1", "2026-01-01", "2026-01-05")
    html = webapp.deal_issues_gantt_page(con)
    assert "target.setDate(target.getDate() - 7)" in html
    assert "wrap.scrollLeft = offsetDays * dayW" in html


def test_tasks_gantt_page_default_scroll_uses_same_logic(con):
    html = webapp.tasks_gantt_page(con)
    assert "target.setDate(target.getDate() - 7)" in html
    assert "wrap.scrollLeft = offsetDays * dayW" in html
