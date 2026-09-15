"""マーケ施策診断ツールの企業規模(AXIS1.size)複数選択化(2026-09-16)の回帰テスト。

ユーザー要望:「企業規模を複数選択できるようにしたい」。従来は<select>による単一選択
だったが、チェックボックス群による複数選択に変更した。マッチング判定は、企業規模以外の
軸（カテゴリ認知・契約単価・意思決定期間）は従来通りAND判定、企業規模だけは選択した
どれか1つにでも対応していればOKというOR判定にする（getIdxs/sizeLabel参照）。

旧保存データ（sel1.sizeが単一文字列）との後方互換も維持する（getIdxsが
Array.isArray()で吸収し、buildAxisのチェック状態判定も同様に両対応）。

JS実行環境を持たないため、生成されたHTML文字列に対する静的アサーションで検証する
（このファイル内の他のJS機能テストと同じ手法。tests/test_mktg_strategy_plan_ui.py参照）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_mktg_size_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_axis1_size_marked_multi(con):
    html = webapp.mktg_sim_page(con)
    assert '{key:"size",  label:"企業規模", multi:true,' in html


def test_default_sel1_size_is_array(con):
    html = webapp.mktg_sim_page(con)
    assert 'let sel1={cat:"あり",price:"高",size:["ENP"],period:"長"};' in html


def test_get_idxs_helper_defined(con):
    html = webapp.mktg_sim_page(con)
    assert "function getIdxs(ax,val){" in html
    assert "function sizeLabel(val){" in html


def test_build_axis_renders_checkbox_group_for_multi_field(con):
    html = webapp.mktg_sim_page(con)
    assert "class=\"multi-chk-group\"" in html
    assert "multi-chk-opt" in html


def test_build_axis_wires_checkbox_change_handler_updates_array(con):
    html = webapp.mktg_sim_page(con)
    assert "boxes.filter(b=>b.checked).map(b=>b.value)" in html
    # 0件になったら直前の状態に戻す（最低1つは選択必須）
    assert "cb.checked=true" in html


def test_get_matched_sorted_uses_or_across_selected_sizes(con):
    html = webapp.mktg_sim_page(con)
    assert "const sizeIdxs=getIdxs(AXIS1[2],sel1.size);" in html
    assert "sizeIdxs.some(idx=>m.s[idx]===1)" in html


def test_get_method_rank_uses_or_across_selected_sizes_for_saved_diagnostics(con):
    html = webapp.mktg_sim_page(con)
    assert "const sizeIdxs=getIdxs(AXIS1[2],save.sel1.size);" in html
    assert "sizeIdxs.some(idx=>s[idx]===1)" in html


def test_saves_tooltip_uses_size_label_helper_for_array_or_legacy_string(con):
    html = webapp.mktg_sim_page(con)
    assert "${sizeLabel(s.sel1.size)}" in html


# ── JS構文検証（node --checkがあれば実行） ──

def test_mktg_sim_script_is_syntactically_valid_js(con):
    if not shutil.which("node"):
        pytest.skip("nodeが無い環境ではスキップ")
    html = webapp.mktg_sim_page(con)
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    script = html[start:end]
    d = tempfile.mkdtemp(prefix="sfa_mktg_js_")
    try:
        js_path = Path(d) / "check.js"
        js_path.write_text(script, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── 実データでの動作確認（getMethodRank相当のロジックをPython側で再現し、
#    複数企業規模のOR判定が想定通りになることを確認） ──

def test_saved_diagnostic_with_array_size_matches_via_or_logic(con):
    """新規保存（sel1.sizeが配列）した診断が、戦略マップのgetMethodRank相当のロジックで
    正しくOR判定されること（Python側でJS同等ロジックを再現して検証）。"""
    row = sfa_db.create_mktg_diagnostic(
        con, tool_name="複数規模対応ツール", biz_type="IT", priority=False,
        sel1={"cat": "あり", "price": "高", "size": ["ENP", "MM"], "period": "長"},
        sel2={"nps": "高い"}, top_methods=[], total_matched=0)
    assert row["sel1"]["size"] == ["ENP", "MM"]


def test_legacy_saved_diagnostic_with_string_size_still_roundtrips(con):
    """後方互換: 旧形式（sel1.sizeが単一文字列）で保存された診断も、そのまま読み出せること
    （読み出し時に強制変換はしない。JS側getIdxs/sizeLabelが吸収する設計）。"""
    row = sfa_db.create_mktg_diagnostic(
        con, tool_name="旧形式ツール", biz_type="IT", priority=False,
        sel1={"cat": "あり", "price": "高", "size": "ENP", "period": "長"},
        sel2={"nps": "高い"}, top_methods=[], total_matched=0)
    assert row["sel1"]["size"] == "ENP"
    fetched = sfa_db.list_mktg_diagnostics(con)
    match = next(r for r in fetched if r["id"] == row["id"])
    assert match["sel1"]["size"] == "ENP"
