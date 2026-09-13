"""社内PJ管理ガント(/deal-issues/gantt)の日次/週次ズーム切替(2026-09-13)の回帰テスト。

ユーザー要望: 「ガントを、日単位/週単位で切り替えられるように。切替た上で、手修正も
引き続き可能な仕様。」

設計: グリッドの列数・_col_of()のオフセット計算・ドラッグ判定(dayColWidth()は実測
clientWidthベース)は一切変えず、日列の最小幅(--ig-daycol-min というCSS変数)だけを
JS側で書き換える「表示密度の切替」として実装した。これにより手修正（ドラッグ/リサイズ）は
ズーム状態に関係なく常に正しく動く（dayColWidth()が実測値ベースのため）。

あわせて、このガント固有のドラッグJSで LABEL_W が220pxとハードコードされていた
（実際のcol_tpl側は260px）既存バグも本修正の過程で発見・修正した。

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
    d = tempfile.mkdtemp(prefix="sfa_ig_zoom_")
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


# ── ズーム切替UI・JSの静的マークアップ ──

def test_zoom_toggle_buttons_present(con):
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert 'onclick="setIgGanttZoom(\'day\')"' in html
    assert 'onclick="setIgGanttZoom(\'week\')"' in html
    assert 'class="ig-zoom-btn active" data-zoom="day"' in html  # 初期状態は日次がactive


def test_set_ig_gantt_zoom_js_function_defined(con):
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert "function setIgGanttZoom(mode)" in html
    assert "--ig-daycol-min" in html
    assert "localStorage.setItem('igGanttZoom', mode)" in html


def test_col_template_and_min_width_reference_same_css_variable(con):
    """日列の最小幅(grid-template-columns)とmin-widthの両方が同じCSS変数を参照していること
    （片方だけ変数化すると、ズーム時にmin-widthが固定のままで1frが余白を埋めてしまい
    実際には列が縮まらなくなる）。"""
    iid = _issue(con, issue="論点B")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-11-20")
    html = webapp.deal_issues_gantt_page(con)
    assert "minmax(var(--ig-daycol-min, 28px), 1fr)" in html
    assert "min-width:calc(260px + " in html
    assert "* var(--ig-daycol-min, 28px))" in html


def test_day_cells_and_labels_carry_data_dow_for_week_boundary_styling(con):
    """週表示で月曜日に区切り罫線を出す・平日以外の日番号を隠すため、day-cell/day-labelの
    両方に曜日(0=月〜6=日)がdata-dowとして載っていること。"""
    iid = _issue(con, issue="論点C")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-14", "2026-09-20")  # 月曜始まり
    html = webapp.deal_issues_gantt_page(con)
    assert 'class="gantt-daycell" data-dow="0"' in html or 'data-dow="0"' in html
    assert re.search(r'class="gantt-daylabel[^"]*" data-dow="\d"', html)


def test_week_mode_css_rules_present(con):
    _issue(con, issue="論点D")
    html = webapp.deal_issues_gantt_page(con)
    assert '.gantt-wrap[data-zoom="week"] .gantt-daylabel:not([data-dow="0"]){color:transparent}' in html
    assert '.gantt-daylabel[data-dow="0"],' in html


# ── LABEL_W不一致バグの修正（ドラッグ判定オフセットが260pxのラベル列幅と一致すること） ──

def test_drag_js_label_width_matches_actual_sidebar_column_width(con):
    """従来 var LABEL_W = 220 とハードコードされており、実際のcol_tpl側(260px)と
    食い違っていたため、ドラッグ/リサイズの日付判定が常に約40px（1日強）ずれる
    不具合があった（2026-09-13発見・修正）。"""
    _issue(con, issue="論点E")
    html = webapp.deal_issues_gantt_page(con)
    assert "var LABEL_W = 260;" in html
    assert "var LABEL_W = 220;" not in html
