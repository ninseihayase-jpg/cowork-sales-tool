"""scripts/backfill_stale_milestones.py の回帰テスト。

修正前（add_activityに自動完了が無かった時代）に生じたはずの滞留データを直接DBへ
組み立てて再現し、検出(find_stale_milestones)・適用(apply_fixes)が正しく動くことを確認する。
"""
from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def backfill_mod():
    spec = importlib.util.spec_from_file_location(
        "backfill_stale_milestones", ROOT / "scripts" / "backfill_stale_milestones.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fresh():
    d = tempfile.mkdtemp(prefix="sfa_bf_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, path


def test_find_stale_milestones_detects_past_ms_with_later_activity(backfill_mod):
    d, path = _fresh()
    try:
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        # 修正前の生SQL的な状態を再現: MS追加後、activityは別途生SQLで直接INSERT
        # （add_activity経由ではないので自動完了が効かず、doneが取り残される）
        sfa_db.add_deal_milestone(con, did, date="2026-08-10", label="初回面談", ms_type="アポ")
        con.execute("INSERT INTO activities (deal_id, type, occurred_on, contact_name, body) "
                    "VALUES (?,?,?,?,?)", (did, "面談", "2026-08-10", "田中", "実施済み"))
        con.commit()

        stale = backfill_mod.find_stale_milestones(con)
        assert len(stale) == 1
        assert stale[0]["deal_id"] == did
        assert stale[0]["ms_date"] == "2026-08-10"
        assert stale[0]["max_activity_date"] == "2026-08-10"
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_find_stale_milestones_ignores_future_ms_and_already_done():
    d, path = _fresh()
    try:
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        con.execute("INSERT INTO activities (deal_id, type, occurred_on, contact_name, body) "
                    "VALUES (?,?,?,?,?)", (did, "面談", "2026-08-10", "田中", "実施済み"))
        con.commit()
        # 未来MS(活動日より後) → 対象外
        sfa_db.add_deal_milestone(con, did, date="2026-08-20", label="次アポ", ms_type="アポ")
        # 既にdone=1 → 対象外
        sfa_db.add_deal_milestone(con, did, date="2026-08-05", label="旧done", ms_type="アポ", done=True)

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backfill_stale_milestones", ROOT / "scripts" / "backfill_stale_milestones.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        stale = m.find_stale_milestones(con)
        assert stale == []
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_apply_fixes_marks_detected_milestones_done(backfill_mod):
    d, path = _fresh()
    try:
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sfa_db.add_deal_milestone(con, did, date="2026-08-01", label="old1", ms_type="アポ")
        sfa_db.add_deal_milestone(con, did, date="2026-08-05", label="old2", ms_type="アポ")
        sfa_db.add_deal_milestone(con, did, date="2026-08-20", label="future", ms_type="アポ")
        con.execute("INSERT INTO activities (deal_id, type, occurred_on, contact_name, body) "
                    "VALUES (?,?,?,?,?)", (did, "面談", "2026-08-10", "田中", "実施済み"))
        con.commit()

        stale = backfill_mod.find_stale_milestones(con)
        assert len(stale) == 2
        n = backfill_mod.apply_fixes(con, stale)
        assert n == 2

        ms = {m["ms_label"]: m["done"] for m in sfa_db.list_deal_milestones(con, did)}
        assert ms["old1"] == 1 and ms["old2"] == 1 and ms["future"] == 0

        # 再実行すると対象0件（冪等）
        stale2 = backfill_mod.find_stale_milestones(con)
        assert stale2 == []
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_report_handles_empty_list(backfill_mod, capsys):
    backfill_mod.report([])
    out = capsys.readouterr().out
    assert "該当なし" in out
