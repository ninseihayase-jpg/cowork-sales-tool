"""Delivery（受注後・納品）アサイン計画のDB層テスト（#75）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
検証: 提案到達での自動起票 / 見込み(提案)・確定(受注)の振り分け /
      クローズ済み非受注の除外 / アサインブロックの週展開・合算 / ベース工数合算。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_delivery_test_")
    db = str(Path(d) / "t.db")
    sfa_db.init_db(db)
    conn = sfa_db.connect(db)
    try:
        yield conn
    finally:
        conn.close()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def acc_id(con):
    aid = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    con.commit()
    return aid


def _deal(con, acc_id, stage, status="open", name="D"):
    return sfa_db.upsert_deal(con, account_id=acc_id, deal_name=name, stage=stage, status=status)


# ---- 自動起票（提案到達以降） ----

def test_ensure_delivery_only_from_proposal(con, acc_id):
    d_req = _deal(con, acc_id, "要件詰め")
    d_prop = _deal(con, acc_id, "提案")
    assert sfa_db.ensure_delivery_on_stage(con, d_req, "要件詰め") is None      # 提案未満は起票しない
    assert sfa_db.ensure_delivery_on_stage(con, d_prop, "提案") is not None     # 提案で起票
    # 二重起票しない
    assert sfa_db.ensure_delivery_on_stage(con, d_prop, "提案") is None
    assert len(sfa_db.list_deliveries(con, deal_id=d_prop)) == 1
    assert len(sfa_db.list_deliveries(con, deal_id=d_req)) == 0


def test_delivery_title_defaults_to_deal_name(con, acc_id):
    did = _deal(con, acc_id, "受注", name="納品対象案件")
    sfa_db.ensure_delivery_on_stage(con, did, "受注")
    dv = sfa_db.list_deliveries(con, deal_id=did)[0]
    assert dv["title"] == "納品対象案件"


# ---- アサイン集計（見込み/確定・除外・週展開） ----

def test_compute_load_forecast_committed_and_exclusion(con, acc_id):
    d_prop = _deal(con, acc_id, "提案", name="見込み案件")
    d_won = _deal(con, acc_id, "受注", name="確定案件")
    d_lost = _deal(con, acc_id, "提案", status="closed", name="失注案件")
    for did in (d_prop, d_won, d_lost):
        sfa_db.ensure_delivery_on_stage(con, did, "提案")
    dv = {d["deal_id"]: d["id"] for d in sfa_db.list_deliveries(con)}
    W0 = "2026-07-27"  # 月曜
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_prop], owner="早瀬",
                                   from_week=W0, to_week="2026-08-10", fte_pct=50)
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_won], owner="早瀬",
                                   from_week=W0, to_week="2026-08-03", fte_pct=80)
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_lost], owner="早瀬",
                                   from_week=W0, to_week="2026-08-03", fte_pct=100)
    load = sfa_db.compute_delivery_load(con, start_week=W0, n_weeks=4)
    cell = load["cells"]["早瀬"][W0]
    assert cell["actual"]["forecast"] == 50    # 提案案件のみ（失注案件は除外）
    assert cell["actual"]["committed"] == 80   # 受注案件
    # billingはfte_billing未指定なら実想定と同値
    assert cell["billing"]["forecast"] == 50
    # 範囲外の週は0（案件は8/10まで/8/3まで）
    assert "2026-08-17" not in load["cells"]["早瀬"]


def test_base_workload_sum_and_upsert(con):
    sfa_db.upsert_base_workload(con, "早瀬", "営業", 30)
    sfa_db.upsert_base_workload(con, "早瀬", "管理", 10)
    assert sfa_db.base_workload_by_owner(con)["早瀬"] == 40
    # 同一(owner,function)はupsertで上書き（重複行にしない）
    sfa_db.upsert_base_workload(con, "早瀬", "営業", 25)
    assert sfa_db.base_workload_by_owner(con)["早瀬"] == 35
    assert len(sfa_db.list_base_workload(con, owner="早瀬")) == 2


def test_delivery_grid_expansion(con, acc_id):
    did = _deal(con, acc_id, "受注")
    sfa_db.ensure_delivery_on_stage(con, did, "受注")
    dvid = sfa_db.list_deliveries(con, deal_id=did)[0]["id"]
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="中島",
                                   from_week="2026-07-27", to_week="2026-08-10", fte_pct=60)
    grid = sfa_db.delivery_grid(con, dvid)
    assert grid["weeks"] == ["2026-07-27", "2026-08-03", "2026-08-10"]
    assert grid["cells"]["中島"]["2026-08-03"]["actual"] == 60


def test_billing_and_update(con, acc_id):
    did = _deal(con, acc_id, "受注")
    sfa_db.ensure_delivery_on_stage(con, did, "受注")
    dvid = sfa_db.list_deliveries(con, deal_id=did)[0]["id"]
    aid = sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬",
                                         from_week="2026-07-27", to_week="2026-07-27",
                                         fte_pct=80, fte_billing=100)
    load = sfa_db.compute_delivery_load(con, start_week="2026-07-27", n_weeks=1)
    c = load["cells"]["早瀬"]["2026-07-27"]
    assert c["actual"]["committed"] == 80 and c["billing"]["committed"] == 100
    # 編集: 実想定/請求/メンバーを更新
    sfa_db.update_delivery_assignment(con, aid, owner="中島", from_week="2026-07-27",
                                      to_week="2026-07-27", fte_pct=40, fte_billing=60, note="改")
    b = sfa_db.list_delivery_assignments(con, dvid)[0]
    assert b["owner"] == "中島" and b["fte_pct"] == 40 and b["fte_billing"] == 60 and b["note"] == "改"


def test_delivery_cascade_delete(con, acc_id):
    did = _deal(con, acc_id, "受注")
    sfa_db.ensure_delivery_on_stage(con, did, "受注")
    dvid = sfa_db.list_deliveries(con, deal_id=did)[0]["id"]
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬",
                                   from_week="2026-07-27", to_week="2026-07-27", fte_pct=10)
    sfa_db.delete_delivery(con, dvid)
    assert sfa_db.list_deliveries(con, deal_id=did) == []
    assert sfa_db.list_delivery_assignments(con, dvid) == []
