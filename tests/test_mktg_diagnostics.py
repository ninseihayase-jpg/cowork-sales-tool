"""マーケ施策診断ツール(/mktg-sim)のDB層回帰テスト(2026-09-06)。

事業名・事業種別に全角英数字（IMEの全角固定入力等で紛れ込みやすい）が入っていても、
保存時・表示時の両方で半角に正規化されること（ユーザー要望「英数字をすべて半角に修正」）。
一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_mktg_diag_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _create(con, tool_name="テストツール", biz_type="調達SCM"):
    return sfa_db.create_mktg_diagnostic(
        con, tool_name=tool_name, biz_type=biz_type, priority=True,
        sel1={"cat": "あり"}, sel2={"nps": "高い"}, top_methods=[1, 2, 3], total_matched=3)


# ── 全角英数字→半角正規化 ──

def test_to_halfwidth_alnum_converts_letters_and_digits():
    assert sfa_db._to_halfwidth_alnum("ＡＩエージェント２０２６") == "AIエージェント2026"


def test_to_halfwidth_alnum_leaves_japanese_and_fullwidth_symbols_untouched():
    """全角記号・日本語はそのまま維持し、英数字だけを変換すること。"""
    s = "調達（ＳＣＭ）／購買"
    assert sfa_db._to_halfwidth_alnum(s) == "調達（SCM）／購買"


def test_to_halfwidth_alnum_none_passthrough():
    assert sfa_db._to_halfwidth_alnum(None) is None
    assert sfa_db._to_halfwidth_alnum("") == ""


def test_create_mktg_diagnostic_normalizes_tool_name_and_biz_type_on_save(con):
    row = _create(con, tool_name="サプライチェーンＡＩ", biz_type="調達ＳＣＭ")
    assert row["toolName"] == "サプライチェーンAI"
    assert row["bizType"] == "調達SCM"


def test_list_mktg_diagnostics_normalizes_legacy_fullwidth_data_on_read(con):
    """保存時の正規化より前に入った既存データ（DB直接値が全角のまま）でも、
    表示時に半角へ揃うこと（DB本体は書き換えず、読み出し時に変換する設計）。"""
    con.execute(
        "INSERT INTO mktg_diagnostics "
        "(tool_name, biz_type, priority, sel1_json, sel2_json, top_methods_json, total_matched) "
        "VALUES (?,?,?,?,?,?,?)",
        ("既存ツールＡＩ", "調達ＳＣＭ", 0, "{}", "{}", "[]", 0))
    con.commit()
    rows = sfa_db.list_mktg_diagnostics(con)
    assert rows[0]["toolName"] == "既存ツールAI"
    assert rows[0]["bizType"] == "調達SCM"


def test_create_and_list_mktg_diagnostic_roundtrip(con):
    """init_db()は初期データとしていくつかのmktg_diagnosticsをseedするため、
    件数の絶対値ではなく作成した行が一覧に含まれることで検証する。"""
    before = len(sfa_db.list_mktg_diagnostics(con))
    created = _create(con)
    rows = sfa_db.list_mktg_diagnostics(con)
    assert len(rows) == before + 1
    row = next(r for r in rows if r["id"] == created["id"])
    assert row["toolName"] == "テストツール"
    assert row["priority"] is True
    assert row["topMethods"] == [1, 2, 3]


def test_delete_mktg_diagnostic(con):
    before = len(sfa_db.list_mktg_diagnostics(con))
    row = _create(con)
    sfa_db.delete_mktg_diagnostic(con, row["id"])
    rows = sfa_db.list_mktg_diagnostics(con)
    assert len(rows) == before
    assert all(r["id"] != row["id"] for r in rows)


# ── 実行対象プラン（戦略マップのマス選択の保存。2026-09-13） ──
# 「どの事業でどの打ち手を実施するか」を戦略マップ上でクリック選択し、全体を1つの名前で
# 保存できるようにする機能のDB層。セルは診断id(mktg_diagnostics.id)と手法名の
# ペアで表す（METHODS配列の並び順に依存しないよう、手法名を安定キーとして使う）。

def _selections():
    return [{"diagnosticId": 1, "method": "SEO / オウンドブログ"},
            {"diagnosticId": 1, "method": "導入事例（Case Study）"},
            {"diagnosticId": 2, "method": "ABM"}]


def test_create_and_list_mktg_strategy_plan_roundtrip(con):
    before = len(sfa_db.list_mktg_strategy_plans(con))
    saved = sfa_db.create_mktg_strategy_plan(con, name="260913_マーケ施策実行対象", selections=_selections())
    rows = sfa_db.list_mktg_strategy_plans(con)
    assert len(rows) == before + 1
    row = next(r for r in rows if r["id"] == saved["id"])
    assert row["name"] == "260913_マーケ施策実行対象"
    assert row["selections"] == _selections()
    assert row["savedAt"]  # YYYY/MM/DD形式の日付が入っていること


def test_create_mktg_strategy_plan_defaults_untitled_name_rejected_upstream(con):
    """DB層自体は空文字名も受け付ける（空文字拒否はルート側の責務）。"""
    saved = sfa_db.create_mktg_strategy_plan(con, name="", selections=[])
    assert saved["name"] == ""
    assert saved["selections"] == []


def test_delete_mktg_strategy_plan(con):
    before = len(sfa_db.list_mktg_strategy_plans(con))
    saved = sfa_db.create_mktg_strategy_plan(con, name="削除対象プラン", selections=_selections())
    sfa_db.delete_mktg_strategy_plan(con, saved["id"])
    rows = sfa_db.list_mktg_strategy_plans(con)
    assert len(rows) == before
    assert all(r["id"] != saved["id"] for r in rows)


def test_list_mktg_strategy_plans_ordered_newest_first(con):
    first = sfa_db.create_mktg_strategy_plan(con, name="1件目", selections=[])
    second = sfa_db.create_mktg_strategy_plan(con, name="2件目", selections=[])
    rows = sfa_db.list_mktg_strategy_plans(con)
    ids = [r["id"] for r in rows]
    assert ids.index(second["id"]) < ids.index(first["id"])
