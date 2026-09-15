"""社内PJ管理ガント(/deal-issues/gantt) カテゴリ帯・PJ見出し行のsticky列崩れ修正(2026-09-15)。

ユーザー報告（画像付き、2026-09-15）: 「相変わらず右スライドの挙動がおかしい」——
2026-09-13の修正(min-width計算追加)ではタスク行(.gantt-lbl, grid-column:1のみ)の
sticky崩れは直ったが、カテゴリ帯見出し（🗂 ...）とPJ見出し行（📌 PJ名＋＋ボタン）は
そもそも grid-column:1 / -1（行全体の幅）でsticky指定されていたため直っていなかった。
要素が既にスクロール可能な全幅を占めている場合、position:stickyは可動範囲を持てず
実質無効になり、右スクロール時にラベル文字列が読めなくなる（画像452で実際に発生）。

修正: 帯の背景色・罫線は全幅の非sticky装飾divに分離し、実際に読ませたいラベル文字
（🗂 カテゴリ名、📌 PJ名＋＋ボタン）はgrid-column:1のみのsticky divに寄せることで、
タスク行と同じ仕組みで左に固定表示されるようにした。

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
    d = tempfile.mkdtemp(prefix="sfa_ig_sticky_hdr_")
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


# ── カテゴリ帯見出し（🗂 ...） ──

def test_category_header_label_is_pinned_to_column_1_not_full_width(con):
    _issue(con, acc_name="A社", deal_name="甲", issue="論点1")
    html = webapp.deal_issues_gantt_page(con)
    # ラベル文字を含むdivはgrid-column:1のみ（フル幅ではない）
    m = re.search(
        r'<div class="gantt-lbl grp" style="grid-row:\d+;grid-column:1;[^"]*">'
        r'🗂 A社 / 甲（1件）</div>', html)
    assert m, "カテゴリ帯ラベルがgrid-column:1のstickyな構造で見つからない"


def test_category_header_has_separate_full_width_background_div(con):
    """帯の背景色（視覚的な区切り）は全幅の非stickyな装飾divとして別に存在すること
    （ラベル自体はcolumn:1に閉じ込めるため、帯の見た目は別レイヤーで維持する）。"""
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert re.search(
        r'<div style="grid-row:\d+;grid-column:1 / -1;background:#f1f5f9"></div>', html)


def test_category_header_no_longer_spans_full_width_as_sticky(con):
    """従来の「grid-column:1/-1のgantt-lbl grp」（sticky無効化バグの原因）が
    もう出力されないこと。"""
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert 'class="gantt-lbl grp" style="grid-row:2;grid-column:1 / -1"' not in html
    assert not re.search(r'class="gantt-lbl grp" style="grid-row:\d+;grid-column:1 / -1"', html)


# ── PJ見出し行（📌 PJ名＋＋ボタン） ──

def test_pj_header_label_is_pinned_to_column_1(con):
    iid = _issue(con, issue="ステップなしの社内PJ")
    html = webapp.deal_issues_gantt_page(con)
    m = re.search(
        r'<div class="gantt-lbl" style="grid-row:\d+;grid-column:1;[^"]*">'
        r'<a href="/deal-issue/' + str(iid) + r'"[^>]*>📌ステップなしの社内PJ', html)
    assert m, "PJ見出しラベル(📌+PJ名)がgrid-column:1のsticky構造で見つからない"


def test_pj_header_add_button_still_present_inside_sticky_label(con):
    iid = _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert f"igShowInlineAdd('ig-mainadd-{iid}')" in html
    # ＋ボタンはPJ名と同じsticky divの中にあること（別行に分離されていない）
    m = re.search(
        r'<div class="gantt-lbl" style="grid-row:\d+;grid-column:1;[^"]*">.*?📌.*?'
        r"igShowInlineAdd\('ig-mainadd-" + str(iid) + r"'\).*?</div>", html, re.DOTALL)
    assert m


def test_pj_header_has_separate_full_width_background_div(con):
    _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert re.search(
        r'<div style="grid-row:\d+;grid-column:1 / -1;background:#fafbfc;'
        r'border-top:1px solid #e2e8f0"></div>', html)


def test_pj_header_no_longer_spans_full_width_with_content(con):
    """従来の「PJ名+＋ボタンを含むgrid-column:1/-1のdiv」がもう出力されないこと。"""
    iid = _issue(con, issue="論点B")
    html = webapp.deal_issues_gantt_page(con)
    assert not re.search(
        r'<div style="grid-row:\d+;grid-column:1 / -1;background:#fafbfc;'
        r'border-top:1px solid #e2e8f0;padding:4px 8px[^"]*">\s*<a href="/deal-issue/' + str(iid),
        html)


def test_pj_name_truncates_with_ellipsis_inside_sticky_column(con):
    """260px固定幅のsticky列に収まりきらない長いPJ名でも、リンク自体に
    ellipsis truncationが指定されており、レイアウトが壊れないこと。"""
    _issue(con, issue="非常に長い社内PJ名前がここに入るケースのテスト用サンプル文字列")
    html = webapp.deal_issues_gantt_page(con)
    assert "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0" in html
