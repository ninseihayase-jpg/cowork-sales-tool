"""マーケ施策診断ツールの「事業種別」をSFA本体のbusiness_type_l1/l2マスタと連動させる
修正(2026-09-16)の回帰テスト。

ユーザー報告:「マーケ診断の『事業種別』ってどこから引っ張ってきてる？修正したい」→
調査の結果、旧・独自の固定5区分（調達SCM/他AX/IT/コスト削減/その他）が2箇所
（診断タブ・戦略マップタブのフィルタ）にハードコードされており、SFA本体で実際に
使われている事業種別L1マスタ（コスト削減/コンサルティング/AI導入/他）とは
「コスト削減」以外ほぼ一致していなかった。

確定方針: SFA本体のbusiness_type_l1/l2マスタをそのまま使う（L1+L2の2段）。既存の
保存済み診断が持つ旧区分値は自動変換しない（ユーザーが個別に選び直す運用）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_mktg_bizl1l2_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


# ── スキーマ・マイグレーション ──

def test_schema_has_biz_type_l2_column(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(mktg_diagnostics)")}
    assert "biz_type_l2" in cols


def test_init_db_migrates_legacy_mktg_diagnostics_without_biz_type_l2():
    """本番再現: biz_type_l2列の無い旧mktg_diagnosticsに対しinit_dbが失敗せず列を追加する
    （過去に全く同じ原因のバグ(#93系, parent_id等)があったため、この形の回帰テストを必須にする）。"""
    import sqlite3
    d = tempfile.mkdtemp(prefix="sfa_mktg_legacy_")
    try:
        path = str(Path(d) / "t.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE mktg_diagnostics(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tool_name TEXT NOT NULL, biz_type TEXT NOT NULL DEFAULT 'その他', "
            "priority INTEGER NOT NULL DEFAULT 0, sel1_json TEXT NOT NULL, sel2_json TEXT NOT NULL, "
            "top_methods_json TEXT NOT NULL, total_matched INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        con.execute(
            "INSERT INTO mktg_diagnostics(tool_name, biz_type, sel1_json, sel2_json, top_methods_json) "
            "VALUES ('旧ツール','調達SCM','{}','{}','[]')")
        con.commit()
        con.close()

        sfa_db.init_db(path)   # 例外なく完了すること
        sfa_db.init_db(path)   # 冪等

        con2 = sfa_db.connect(path)
        cols = {r[1] for r in con2.execute("PRAGMA table_info(mktg_diagnostics)")}
        assert "biz_type_l2" in cols
        row = con2.execute(
            "SELECT tool_name, biz_type, biz_type_l2 FROM mktg_diagnostics WHERE tool_name='旧ツール'"
        ).fetchone()
        assert row[0] == "旧ツール"
        assert row[1] == "調達SCM"  # 旧値は自動変換されず残る
        assert row[2] is None
        con2.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── sfa_db層 CRUD ──

def test_create_mktg_diagnostic_with_biz_type_l2(con):
    row = sfa_db.create_mktg_diagnostic(
        con, tool_name="テストツール", biz_type="コスト削減", biz_type_l2="コスト診断(有償)",
        priority=False, sel1={}, sel2={}, top_methods=[], total_matched=0)
    assert row["bizType"] == "コスト削減"
    assert row["bizTypeL2"] == "コスト診断(有償)"


def test_create_mktg_diagnostic_without_biz_type_l2_defaults_to_none(con):
    row = sfa_db.create_mktg_diagnostic(
        con, tool_name="テストツール", biz_type="他", priority=False,
        sel1={}, sel2={}, top_methods=[], total_matched=0)
    assert row["bizTypeL2"] is None


def test_list_mktg_diagnostics_includes_biz_type_l2(con):
    created = sfa_db.create_mktg_diagnostic(
        con, tool_name="テストツール2", biz_type="AI導入", biz_type_l2="AI開発(軽)",
        priority=False, sel1={}, sel2={}, top_methods=[], total_matched=0)
    rows = sfa_db.list_mktg_diagnostics(con)
    match = next(r for r in rows if r["id"] == created["id"])
    assert match["bizTypeL2"] == "AI開発(軽)"


def test_seed_diagnostics_have_null_biz_type_l2(con):
    """旧HTML版由来のプリロードデータ(_SEED_MKTG_DIAGNOSTICS)はL2を持たないため、
    biz_type_l2はNoneのまま（自動推定・自動変換はしない）。"""
    rows = sfa_db.list_mktg_diagnostics(con)
    assert rows  # 初期シードで何件か入っているはず
    assert all(r["bizTypeL2"] is None for r in rows)


# ── ページ描画 ──

def test_page_renders_dynamic_l1_options_matching_master(con):
    html = webapp.mktg_sim_page(con)
    l1_list = sfa_db.get_master_list(con, "business_type_l1") or list(sfa_db.BUSINESS_TYPE_L1)
    for l1 in l1_list:
        assert f'<option value="{l1}"' in html
    # 旧・独自区分（調達SCM等）はもう静的に埋め込まれていないこと
    assert '<option value="調達SCM">調達SCM事業</option>' not in html
    assert '<option value="他AX">他AX事業</option>' not in html


def test_page_renders_biz_l2_select_for_default_l1(con):
    html = webapp.mktg_sim_page(con)
    l1_list = sfa_db.get_master_list(con, "business_type_l1") or list(sfa_db.BUSINESS_TYPE_L1)
    default_l1 = l1_list[0]
    l2_list = sfa_db.business_type_l2_of(con, default_l1)
    assert 'id="sel-biz-l2"' in html
    if l2_list:
        assert f'<option value="{l2_list[0]}"' in html


def test_strategy_filter_uses_dynamic_l1_list_not_hardcoded(con):
    html = webapp.mktg_sim_page(con)
    assert 'id="strategy-biz-filter"' in html
    assert '<option value="調達SCM">調達SCM事業</option>' not in html.split('id="strategy-biz-filter"')[1][:500]


def test_page_injects_business_type_tree_json_for_l1_l2_cascade(con):
    html = webapp.mktg_sim_page(con)
    assert "const BIZ_L2_MAP=" in html
    assert "const BIZ_L1_LIST=" in html
    assert "function rebuildBizL2Select(" in html


def test_page_no_longer_uses_hardcoded_biz_dot_or_biz_order(con):
    """名前ごとの固定色マップ(BIZ_DOT)・固定並び順(BIZ_ORDER)は、事業種別が可変になった
    ことで廃止し、インデックスベースのbizStyle()に統一されていること。"""
    html = webapp.mktg_sim_page(con)
    assert "const BIZ_DOT=" not in html
    assert "const BIZ_ORDER=" not in html
    assert "function bizStyle(" in html


def test_page_no_longer_has_hardcoded_biz_tag_css_classes(con):
    html = webapp.mktg_sim_page(con)
    # 実際のCSSルールとして出力されていないことを確認する（説明コメント内の言及は対象外）。
    assert ".biz-tag-調達SCM{" not in html
    assert ".biz-tag-その他{" not in html
    assert 'class="hm-biz-tag biz-tag-' not in html  # 旧: クラス名にbizType値を直接埋め込む方式


def test_saved_diagnostic_with_legacy_biz_type_falls_back_gracefully(con):
    """旧区分(調達SCM等)を持つ既存診断でも、ページ描画自体は例外にならないこと
    （表示上はフォールバック色になるだけで、致命的エラーにはしない）。"""
    sfa_db.create_mktg_diagnostic(
        con, tool_name="旧区分ツール", biz_type="調達SCM", priority=False,
        sel1={}, sel2={}, top_methods=[], total_matched=0)
    html = webapp.mktg_sim_page(con)
    assert "旧区分ツール" in html
