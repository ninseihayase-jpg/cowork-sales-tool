"""上部ナビメニュー再編(2026-09-05)の回帰テスト。

ユーザー指示: 「デモ開発」(旧/dev-projects)・「開発」(旧開発要件一覧)・「社内PJ」(旧論点)への
改名、メニュー項目の絵文字アイコン全廃、区切り(日常/社内/タスク/クライアント/他)での並び替え。
デモ開発は「開発」ページ内のタブへ、社内PJガントは「社内PJ」ページ内のタブへ統合し、
トップナビの項目数を絞った。一時DBのみ使用。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_nav_menu_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _nav_html():
    return webapp.render("<div></div>").decode("utf-8")


# ── ナビバー: 区切り・並び順・改名・絵文字撤廃 ──

def test_nav_has_no_emoji_on_top_level_items(con):
    html = _nav_html()
    # 絵文字を伴っていた旧ラベルが完全に消えていること（部分文字列として拾われないよう
    # 絵文字+スペース+旧テキストの組み合わせで確認）
    for old in ["🚚 Delivery", "🧩 開発要件一覧", "📊 社内PJガント", "📄 資料庫",
                "🔀 業務フロー", "🎯 マーケ診断", "✅ コンサルタスク", "🗂 事務タスク",
                "📥 取り込み", "⚙ 管理"]:
        assert old not in html, f"絵文字付きの旧ラベルが残っている: {old}"


def test_nav_new_labels_present(con):
    html = _nav_html()
    for label in ["商談", "Delivery", "開発", "社内PJ", "業務フロー",
                  "コンサルタスク", "事務タスク", "アカウント", "ヒアリング",
                  "リード", "マーケ診断", "メール", "資料庫", "取り込み", "管理"]:
        assert label in html


def test_nav_dev_projects_and_gantt_not_toplevel(con):
    """デモ開発(/dev-projects)・社内PJガント(/deal-issues/gantt)は、統合先ページ内の
    タブに折りたたまれ、トップナビの独立項目としては存在しないこと。"""
    html = _nav_html()
    assert 'href="/dev-projects"' not in html
    assert 'href="/deal-issues/gantt"' not in html
    assert 'href="/dev-requirements"' in html
    assert 'href="/deal-issues"' in html


def test_nav_section_order(con):
    """区切り(nav-sep)で仕切られた区分の順序: 日常→社内→タスク→クライアント→他。
    各区分の先頭項目の出現順で判定する。"""
    html = _nav_html()
    order_markers = ['>商談<', '>社内PJ<', '>コンサルタスク<', '>アカウント<', '>メール<']
    positions = [html.find(m) for m in order_markers]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)


def test_nav_right_side_pills_keep_emoji(con):
    """右端固定リンク(InProc dashboard/SFA dashboard/週次レポート)は改名対象外・
    絵文字もそのまま残ること（ユーザー確認済み: ロゴ削除は各メニュー項目の絵文字のみ）。"""
    html = _nav_html()
    assert "📊 SFA dashboard" in html
    assert "📰 週次レポート" in html


# ── サブタブ統合: 開発（開発要件一覧⇔デモ開発） ──

def test_dev_requirements_page_has_subtab_to_dev_projects(con):
    html = webapp.dev_requirements_page(con)
    assert 'href="/dev-projects"' in html
    assert "デモ開発" in html
    assert "開発要件一覧" in html


def test_dev_projects_list_page_has_subtab_to_dev_requirements(con):
    html = webapp.dev_projects_list_page(con)
    assert 'href="/dev-requirements"' in html
    assert "デモ開発" in html


# ── サブタブ統合: 社内PJ（一覧⇔ガント） ──

def test_deal_issues_list_page_has_subtab_to_gantt(con):
    html = webapp.deal_issues_list_page(con)
    assert 'href="/deal-issues/gantt"' in html
    assert "ガント" in html


def test_deal_issues_gantt_page_has_subtab_to_list(con):
    html = webapp.deal_issues_gantt_page(con)
    assert 'href="/deal-issues"' in html
    assert "一覧" in html


# ── _subtab_strip ヘルパー単体 ──

def test_subtab_strip_marks_active_tab():
    html = webapp._subtab_strip([("A", "/a", True), ("B", "/b", False)])
    assert 'href="/a"' in html and 'href="/b"' in html
    # active=Trueの方が白背景(#fff)で強調されていること
    idx_a = html.find('href="/a"')
    idx_b = html.find('href="/b"')
    seg_a = html[idx_a:html.find("</a>", idx_a)]
    seg_b = html[idx_b:html.find("</a>", idx_b)]
    assert "background:#fff" in seg_a
    assert "background:#fff" not in seg_b
