"""重複アカウント検出・統合のテスト（tempfile DBで完結）。"""
import os
import tempfile

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    sfa_db.init_db(db)
    c = sfa_db.connect(db)
    yield c
    c.close()


def test_find_duplicates_groups_by_name(con):
    a1 = sfa_db.upsert_account(con, name="重複社")
    sfa_db.upsert_account(con, name="重複社")   # 同名
    sfa_db.upsert_account(con, name="単独社")    # 単独
    sfa_db.upsert_account(con, name="  重複社 ")  # 前後空白は同一視
    groups = sfa_db.find_duplicate_accounts(con)
    names = {g["name"] for g in groups}
    assert "重複社" in names
    assert "単独社" not in names  # 単独はグループに出ない
    dup = next(g for g in groups if g["name"] == "重複社")
    assert len(dup["accounts"]) == 3


def test_find_duplicates_counts(con):
    a1 = sfa_db.upsert_account(con, name="X社")
    a2 = sfa_db.upsert_account(con, name="X社")
    sfa_db.upsert_deal(con, account_id=a2, deal_name="d1")
    sfa_db.upsert_deal(con, account_id=a2, deal_name="d2")
    g = sfa_db.find_duplicate_accounts(con)[0]
    counts = {a["id"]: a["deal_count"] for a in g["accounts"]}
    assert counts[a2] == 2 and counts[a1] == 0


def test_merge_repoints_and_deletes(con):
    a1 = sfa_db.upsert_account(con, name="M社")
    a2 = sfa_db.upsert_account(con, name="M社")
    sfa_db.upsert_deal(con, account_id=a2, deal_name="移動する商談")
    res = sfa_db.merge_accounts(con, keep_id=a1, drop_ids=[a2])
    assert res["moved_deals"] == 1 and res["dropped"] == 1
    # a2は削除、商談はa1へ
    assert [r["id"] for r in sfa_db.list_accounts(con)] == [a1]
    row = con.execute("SELECT account_id FROM deals WHERE deal_name='移動する商談'").fetchone()
    assert row["account_id"] == a1
    assert sfa_db.find_duplicate_accounts(con) == []


def test_merge_ignores_keep_in_drop_ids(con):
    a1 = sfa_db.upsert_account(con, name="Y社")
    a2 = sfa_db.upsert_account(con, name="Y社")
    # keep_idがdrop_idsに混ざっていても残す側は消えない
    res = sfa_db.merge_accounts(con, keep_id=a1, drop_ids=[a1, a2])
    assert res["dropped"] == 1
    assert a1 in [r["id"] for r in sfa_db.list_accounts(con)]
