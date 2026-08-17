"""scripts/backfill_intake_transcript_links.py の回帰テスト。

intake_transcript_id導入(2026-08-17)より前に作られた既存データ（NULLのまま）を、
「文字起こし1件・未紐づけ対象行1件」の場合のみ確実な1:1として自動検出・適用できること、
曖昧（2件以上）な場合は自動判定せずスキップすることを検証する。
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
        "backfill_intake_transcript_links", ROOT / "scripts" / "backfill_intake_transcript_links.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_bf_intake_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    try:
        yield conn
    finally:
        conn.close()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def acc_id(con):
    return con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid


def test_unique_transcript_and_unique_note_links_automatically(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="提案")
    itid = sfa_db.add_intake_transcript(con, kind="deal", entity_id=did, source="paste", transcript="t")
    # intake_transcript_id導入前の作成を再現（NULLのまま）
    nid = sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="旧メモ", body="B")
    aid = sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-08-01", body="x")

    result = sfa_db.find_intake_transcript_backfill_candidates(con)
    tables = {(item["table"], item["row_id"]) for item in result["apply"]}
    assert ("rich_notes", nid) in tables
    assert ("activities", aid) in tables
    assert not result["ambiguous"]

    n = sfa_db.apply_intake_transcript_backfill(con, result["apply"])
    assert n == 2
    assert sfa_db.get_rich_note(con, nid)["intake_transcript_id"] == itid
    assert sfa_db.get_activity(con, aid)["intake_transcript_id"] == itid

    # 冪等: 再実行すると対象0件
    result2 = sfa_db.find_intake_transcript_backfill_candidates(con)
    assert result2["apply"] == []


def test_multiple_transcripts_are_ambiguous_and_skipped(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D2", stage="提案")
    sfa_db.add_intake_transcript(con, kind="deal", entity_id=did, source="paste", transcript="t1")
    sfa_db.add_intake_transcript(con, kind="deal", entity_id=did, source="paste", transcript="t2")
    nid = sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="メモ", body="B")

    result = sfa_db.find_intake_transcript_backfill_candidates(con)
    assert result["apply"] == []
    assert any(a["entity_id"] == did for a in result["ambiguous"])
    assert sfa_db.get_rich_note(con, nid)["intake_transcript_id"] is None


def test_multiple_unlinked_notes_are_ambiguous_and_skipped(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D3", stage="提案")
    sfa_db.add_intake_transcript(con, kind="deal", entity_id=did, source="paste", transcript="t")
    n1 = sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="メモ1", body="B1")
    n2 = sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="メモ2", body="B2")

    result = sfa_db.find_intake_transcript_backfill_candidates(con)
    assert result["apply"] == []
    assert any(a["entity_id"] == did for a in result["ambiguous"])
    assert sfa_db.get_rich_note(con, n1)["intake_transcript_id"] is None
    assert sfa_db.get_rich_note(con, n2)["intake_transcript_id"] is None


def test_issue_kind_backfills_rich_note_only(con):
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")
    itid = sfa_db.add_intake_transcript(con, kind="issue", entity_id=iid, source="paste", transcript="t")
    nid = sfa_db.create_rich_note(con, kind="issue", entity_id=iid, title="論点メモ", body="B")

    result = sfa_db.find_intake_transcript_backfill_candidates(con)
    assert len(result["apply"]) == 1
    n = sfa_db.apply_intake_transcript_backfill(con, result["apply"])
    assert n == 1
    assert sfa_db.get_rich_note(con, nid)["intake_transcript_id"] == itid


def test_already_linked_rows_are_not_reconsidered(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D4", stage="提案")
    itid = sfa_db.add_intake_transcript(con, kind="deal", entity_id=did, source="paste", transcript="t")
    sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="既に紐づけ済み", body="B",
                            intake_transcript_id=itid)
    result = sfa_db.find_intake_transcript_backfill_candidates(con)
    assert result["apply"] == []
    assert result["ambiguous"] == []


def test_report_handles_empty_result(backfill_mod, capsys):
    backfill_mod.report({"apply": [], "ambiguous": []})
    out = capsys.readouterr().out
    assert "該当なし" in out
