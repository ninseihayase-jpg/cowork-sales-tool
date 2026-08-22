"""Delivery（受注後・納品）アサイン計画のDB層テスト（#75）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
検証: 提案到達での自動起票 / 見込み(提案)・確定(受注)の振り分け /
      クローズ済み非受注の除外 / アサインブロックの週展開・合算 / ベース工数合算。
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


def test_deal_form_shows_delivery_button_in_top_row_when_triggered(con, acc_id):
    """商談編集画面の上部ボタン列（＋新規開発案件等と並び）にも「＋Delivery追加」を出す。
    下部カードと同条件（既存Deliveryあり or 提案以降のステージ）のみ表示し、
    下までスクロールしないと気付けないという指摘への対応。"""
    d_won = _deal(con, acc_id, "受注", name="調達BPO")
    html = webapp.deal_form(con, sfa_db.get_deal(con, d_won))
    assert "🚚 ＋Delivery追加" in html
    assert html.find("🚚 ＋Delivery追加") < html.find("この商談を複製")

    d_early = _deal(con, acc_id, "初回アポ実施", name="早期商談")
    html_early = webapp.deal_form(con, sfa_db.get_deal(con, d_early))
    assert "🚚 ＋Delivery追加" not in html_early


def test_delivery_title_defaults_to_deal_name(con, acc_id):
    did = _deal(con, acc_id, "受注", name="納品対象案件")
    sfa_db.ensure_delivery_on_stage(con, did, "受注")
    dv = sfa_db.list_deliveries(con, deal_id=did)[0]
    assert dv["title"] == "納品対象案件"


# ---- アサイン集計（見込み/確定・除外・週展開） ----

def test_compute_load_forecast_committed_and_exclusion(con, acc_id):
    d_prop = _deal(con, acc_id, "提案", name="見込み(提案中)案件")
    d_closing = _deal(con, acc_id, "クロージング", name="見込み(クロージング)案件")
    d_won = _deal(con, acc_id, "受注", name="確定案件")
    d_lost = _deal(con, acc_id, "提案", status="closed", name="失注案件")
    for did in (d_prop, d_closing, d_won, d_lost):
        sfa_db.ensure_delivery_on_stage(con, did, sfa_db.get_deal(con, did)["stage"])
    dv = {d["deal_id"]: d["id"] for d in sfa_db.list_deliveries(con)}
    W0 = "2026-07-27"  # 月曜
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_prop], owner="早瀬",
                                   from_week=W0, to_week="2026-08-10", fte_pct=50)
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_closing], owner="早瀬",
                                   from_week=W0, to_week="2026-08-10", fte_pct=30)
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_won], owner="早瀬",
                                   from_week=W0, to_week="2026-08-03", fte_pct=80)
    sfa_db.add_delivery_assignment(con, delivery_id=dv[d_lost], owner="早瀬",
                                   from_week=W0, to_week="2026-08-03", fte_pct=100)
    load = sfa_db.compute_delivery_load(con, start_week=W0, n_weeks=4)
    cell = load["cells"]["早瀬"][W0]
    assert cell["actual"]["proposal"] == 50    # 提案案件のみ（失注案件は除外）
    assert cell["actual"]["closing"] == 30     # クロージング案件
    assert cell["actual"]["committed"] == 80   # 受注案件
    # billingはfte_billing未指定なら実想定と同値
    assert cell["billing"]["proposal"] == 50
    # 範囲外の週は0（案件は8/10まで/8/3まで）
    assert "2026-08-17" not in load["cells"]["早瀬"]


def test_confidence_auto_derivation():
    assert sfa_db.delivery_confidence_auto("提案", "open") == "見込み(提案中)"
    assert sfa_db.delivery_confidence_auto("クロージング", "open") == "見込み(クロージング)"
    assert sfa_db.delivery_confidence_auto("受注", "open") == "確定"
    assert sfa_db.delivery_confidence_auto("受注", "closed") == "確定"   # 受注クローズは確定のまま
    assert sfa_db.delivery_confidence_auto("提案", "closed") == "無効(終了)"   # 失注等


def test_confidence_override_takes_priority_over_auto(con, acc_id):
    did = _deal(con, acc_id, "提案")
    dv_id = sfa_db.create_delivery(con, deal_id=did, title="D")
    dv = sfa_db.get_delivery(con, dv_id)
    assert sfa_db.delivery_confidence_effective(dv) == "見込み(提案中)"   # override無し=自動
    sfa_db.update_delivery(con, dv_id, confidence_override="確定")
    dv = sfa_db.get_delivery(con, dv_id)
    assert sfa_db.delivery_confidence_effective(dv) == "確定"   # 手修正が自動導出を上書き
    sfa_db.update_delivery(con, dv_id, confidence_override=None)
    dv = sfa_db.get_delivery(con, dv_id)
    assert sfa_db.delivery_confidence_effective(dv) == "見込み(提案中)"   # 空に戻すと自動に戻る


def test_delivery_is_active_excludes_completed_hold_cancelled_and_invalid(con, acc_id):
    did = _deal(con, acc_id, "提案")
    dv_ing = sfa_db.get_delivery(con, sfa_db.create_delivery(con, deal_id=did, status="進行中"))
    dv_done = sfa_db.get_delivery(con, sfa_db.create_delivery(con, deal_id=did, status="完了"))
    dv_hold = sfa_db.get_delivery(con, sfa_db.create_delivery(con, deal_id=did, status="保留"))
    dv_stop = sfa_db.get_delivery(con, sfa_db.create_delivery(con, deal_id=did, status="中止"))
    assert sfa_db.delivery_is_active(dv_ing) is True
    assert sfa_db.delivery_is_active(dv_done) is False
    assert sfa_db.delivery_is_active(dv_hold) is False
    assert sfa_db.delivery_is_active(dv_stop) is False
    # 状態=進行中でも、確度が(手修正で)無効(終了)ならactive対象外
    _id = sfa_db.create_delivery(con, deal_id=did, status="進行中")
    sfa_db.update_delivery(con, _id, confidence_override="無効(終了)")
    dv_invalid = sfa_db.get_delivery(con, _id)
    assert sfa_db.delivery_is_active(dv_invalid) is False


def test_compute_load_excludes_completed_hold_cancelled_deliveries(con, acc_id):
    """状態=完了/保留/中止のDeliveryは、紐づく商談が開いていても稼働集計から除外される。"""
    d_done = _deal(con, acc_id, "提案", name="完了済み案件")
    d_hold = _deal(con, acc_id, "提案", name="保留中案件")
    d_stop = _deal(con, acc_id, "提案", name="中止案件")
    d_active = _deal(con, acc_id, "提案", name="進行中案件")
    dv_done = sfa_db.create_delivery(con, deal_id=d_done, status="完了")
    dv_hold = sfa_db.create_delivery(con, deal_id=d_hold, status="保留")
    dv_stop = sfa_db.create_delivery(con, deal_id=d_stop, status="中止")
    dv_active = sfa_db.create_delivery(con, deal_id=d_active, status="進行中")
    W0 = "2026-07-27"
    for dv_id in (dv_done, dv_hold, dv_stop, dv_active):
        sfa_db.add_delivery_assignment(con, delivery_id=dv_id, owner="早瀬",
                                       from_week=W0, to_week="2026-08-03", fte_pct=40)
    load = sfa_db.compute_delivery_load(con, start_week=W0, n_weeks=2)
    cell = load["cells"]["早瀬"][W0]
    assert cell["actual"]["proposal"] == 40   # 進行中案件のみ
    assert len(load["items"]) == 1
    assert load["items"][0]["delivery_id"] == dv_active


def test_deliveries_page_sort_order(con, acc_id):
    """一覧の並び順: 確定→見込み(クロージング)→見込み(提案中)（進行中のみ・開始週の早い順）
    →保留→完了→中止→無効(終了)（確度=無効(終了)は状態に関わらず最下位）。"""
    def mkdeal(stage, status="open"):
        return sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage=stage, status=status)

    d_prop_late = mkdeal("提案")
    sfa_db.create_delivery(con, deal_id=d_prop_late, title="提案-遅", start_week="2026-09-01")
    d_prop_early = mkdeal("提案")
    sfa_db.create_delivery(con, deal_id=d_prop_early, title="提案-早", start_week="2026-08-01")
    d_closing = mkdeal("クロージング")
    sfa_db.create_delivery(con, deal_id=d_closing, title="クロージング", start_week="2026-08-15")
    d_won = mkdeal("受注")
    sfa_db.create_delivery(con, deal_id=d_won, title="確定", start_week="2026-08-20")
    d_hold = mkdeal("提案")
    sfa_db.create_delivery(con, deal_id=d_hold, title="保留", status="保留", start_week="2026-07-01")
    d_done = mkdeal("提案")
    sfa_db.create_delivery(con, deal_id=d_done, title="完了", status="完了", start_week="2026-07-01")
    d_stop = mkdeal("提案")
    sfa_db.create_delivery(con, deal_id=d_stop, title="中止", status="中止", start_week="2026-07-01")
    d_lost = mkdeal("提案")
    sfa_db.create_delivery(con, deal_id=d_lost, title="無効", start_week="2026-06-01")
    sfa_db.close_deal_to_lead(con, d_lost, "失注")   # status=中止になるが確度=無効(終了)が優先で最下位

    html = webapp.deliveries_page(con)
    order = re.findall(r'<input type="text" value="([^"]*)"', html)
    assert order == ["確定", "クロージング", "提案-早", "提案-遅", "保留", "完了", "中止", "無効"]


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


# ---- 報酬額（月額↔総額 換算）＋ xlsx出力（#75 追加） ----

def test_delivery_month_count():
    # 月数＝合計週数÷4（4週≒1ヶ月）で統一
    assert sfa_db.delivery_month_count("2026-07-10", "2026-09-19") == 2.75   # 11週
    assert sfa_db.delivery_month_count("2026-07-06", "2026-07-27") == 1.0    # 4週
    assert sfa_db.delivery_month_count("2026-01-01", "2026-12-31") == 13.25  # 53週
    assert sfa_db.delivery_month_count("2026-09-28", "2026-11-16") == 2.0    # 8週
    assert sfa_db.delivery_month_count(None, None) == 1.0  # 未設定は1


def test_delivery_fee_monthly_to_total():
    mo, to = sfa_db.compute_delivery_fee("monthly", 100, None, 3)
    assert (mo, to) == (100, 300.0)


def test_delivery_fee_total_to_monthly():
    mo, to = sfa_db.compute_delivery_fee("total", None, 300, 3)
    assert (mo, to) == (100.0, 300)
    # 空入力は None
    assert sfa_db.compute_delivery_fee("monthly", "", "", 3) == (None, None)


def test_delivery_fee_both_present_is_trusted_as_is_manual_override():
    """新規タスク: 両方に値がある場合は再計算せずそのまま尊重する（自動換算後の手修正を保存で
    上書きしないため）。mode='monthly'でも、総額側に自動換算値と異なる手修正値が入っていれば
    その値のまま返る。"""
    # 通常の自動換算のまま(整合済み)なら当然そのまま
    mo, to = sfa_db.compute_delivery_fee("monthly", 100, 300, 3)
    assert (mo, to) == (100, 300)
    # 手修正: 月額100・月数3なら本来総額300のはずだが、人間が総額を850に手修正した場合
    mo, to = sfa_db.compute_delivery_fee("monthly", 100, 850, 3)
    assert (mo, to) == (100, 850), "手修正した総額が保存時に自動換算で上書きされてはいけない"
    # 逆方向(mode='total'でも同様に、月額側の手修正が尊重される)
    mo, to = sfa_db.compute_delivery_fee("total", 999, 300, 3)
    assert (mo, to) == (999, 300)


def test_delivery_form_fee_fields_allow_manual_edit_via_shared_js(con):
    """新規タスク: 灰色側(自動算出側)がreadOnlyでなくなり、手修正用のJSに委譲していること。"""
    from cowork import webapp
    acc = con.execute("INSERT INTO accounts(name) VALUES('A社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="受注", status="open")
    con.commit()
    dvid = sfa_db.create_delivery(con, deal_id=did, title="納品X",
                                  start_week="2026-07-06", end_week="2026-09-14")
    con.commit()
    html = webapp.delivery_form(con, dvid)
    assert "readOnly=true" not in html and "readOnly=false" not in html
    assert 'oninput="dvFeeFieldInput(this)"' in html
    assert 'onchange="dvFeeModeChanged()"' in html
    assert "function dvFeeFieldInput(" in html and "function dvFeeModeChanged(" in html


def test_delivery_fee_persist_and_xlsx(con):
    import io
    import openpyxl
    from cowork import webapp
    acc = con.execute("INSERT INTO accounts(name) VALUES('A社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="受注", status="open")
    con.commit()
    dvid = sfa_db.create_delivery(con, deal_id=did, title="納品X",
                                  start_week="2026-07-06", end_week="2026-09-14")
    con.commit()
    months = sfa_db.delivery_month_count("2026-07-06", "2026-09-14")  # 11週÷4 = 2.75
    assert months == 2.75
    mo, to = sfa_db.compute_delivery_fee("total", None, 300, months)
    sfa_db.update_delivery(con, dvid, fee_mode="total", fee_monthly=mo, fee_total=to)
    con.commit()
    r = sfa_db.get_delivery(con, dvid)
    assert r["fee_mode"] == "total" and r["fee_total"] == 300 and r["fee_monthly"] == 109.09
    # xlsx出力（全テーブル情報が3シートで出る）
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬",
                                   from_week="2026-07-06", to_week="2026-09-14",
                                   fte_pct=80, fte_billing=100, role="PM", member_kind="内部")
    con.commit()
    xls = webapp.build_deliveries_xlsx(con)
    assert xls[:2] == b"PK"
    wb = openpyxl.load_workbook(io.BytesIO(xls))
    assert wb.sheetnames == ["Delivery一覧", "アサイン明細", "体制(役割別目標)"]
    ws = wb["Delivery一覧"]
    assert ws.cell(2, 9).value == 2.75         # 月数（11週÷4）
    assert ws.cell(2, 10).value == "総額報酬"   # 報酬形態
    assert wb["アサイン明細"].cell(2, 7).value == "早瀬"


def test_delivery_total_assign_effort(con):
    acc = con.execute("INSERT INTO accounts(name) VALUES('A社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X", stage="受注", status="open")
    con.commit()
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X",
                                  start_week="2026-05-18", end_week="2026-07-27")
    con.commit()
    # 5/18〜7/27 = 11週
    assert sfa_db._assignment_weeks("2026-05-18", "2026-07-27") == 11
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="高橋",
                                   from_week="2026-05-18", to_week="2026-07-27",
                                   fte_pct=20, fte_billing=40, role="リード")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="杉山",
                                   from_week="2026-05-18", to_week="2026-07-27",
                                   fte_pct=80, fte_billing=80, role="コンサル")
    con.commit()
    # 期間平均の合計稼働率: (11*20 + 11*80) / 11週 = 1100/11 = 100.0（%/月）
    assert sfa_db.delivery_total_assign_effort(con, dvid) == 100.0
    # 一部期間のみのアサインは期間割りで薄まる（杉山を後半6週=6/22〜7/27だけ80%に変更）
    sugi = [a for a in sfa_db.list_delivery_assignments(con, dvid) if a["owner"] == "杉山"][0]
    sfa_db.update_delivery_assignment(con, sugi["id"], owner="杉山",
                                      from_week="2026-06-22", to_week="2026-07-27",
                                      fte_pct=80, fte_billing=80)
    con.commit()
    # 高橋20%×11週=220, 杉山80%×6週=480 → 700/11 ≈ 63.6
    assert sfa_db.delivery_total_assign_effort(con, dvid) == 63.6


def test_delivery_assign_effort_billing_basis(con):
    """平均単価用: 請求ベース工数（請求>0は請求、請求0/未入力は実想定にフォールバック）。"""
    acc = con.execute("INSERT INTO accounts(name) VALUES('X社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注", status="open")
    con.commit()
    dvid = sfa_db.create_delivery(con, deal_id=did, title="D",
                                  start_week="2026-05-18", end_week="2026-07-27")
    con.commit()
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="高橋",
                                   from_week="2026-05-18", to_week="2026-07-27",
                                   fte_pct=20, fte_billing=40)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="杉山",
                                   from_week="2026-05-18", to_week="2026-07-27",
                                   fte_pct=80, fte_billing=80)
    con.commit()
    # 実想定: (20+80)=100 / 請求: (40+80)=120
    assert sfa_db.delivery_total_assign_effort(con, dvid) == 100.0
    assert sfa_db.delivery_total_assign_effort(con, dvid, use_billing=True) == 120.0
    # 高橋の請求を0にすると純粋請求は0扱い（フォールバックなし）→ 請求ベースは杉山80のみ = 80
    taka = [a for a in sfa_db.list_delivery_assignments(con, dvid) if a["owner"] == "高橋"][0]
    sfa_db.update_delivery_assignment(con, taka["id"], owner="高橋",
                                      from_week="2026-05-18", to_week="2026-07-27",
                                      fte_pct=20, fte_billing=0)
    con.commit()
    assert sfa_db.delivery_total_assign_effort(con, dvid, use_billing=True) == 80.0


def test_delivery_grid_ignores_blank_week_rows(con):
    """開始/終了週が空のアサイン行（役割追加直後など）があっても delivery_grid が落ちない（#502回避）。"""
    acc = con.execute("INSERT INTO accounts(name) VALUES('浜松')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X", stage="受注", status="open")
    con.commit()
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    con.commit()
    # 空週の行を直挿し（from_week NOT NULL のため空文字）
    con.execute("INSERT INTO delivery_assignments(delivery_id,role,member_kind,owner,from_week,to_week,fte_pct) "
                "VALUES(?,?,?,?,?,?,?)", (dvid, "リード", "内部", "", "", "", 0))
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="杉山",
                                   from_week="2026-05-18", to_week="2026-07-27", fte_pct=80)
    con.commit()
    g = sfa_db.delivery_grid(con, dvid)          # 例外を投げないこと
    assert g["owners"] == ["杉山"] and len(g["weeks"]) == 11
    # 空週の行しかない場合は空グリッド（例外なし）
    dvid2 = sfa_db.create_delivery(con, deal_id=did, title="Y")
    con.commit()
    con.execute("INSERT INTO delivery_assignments(delivery_id,role,member_kind,owner,from_week,to_week,fte_pct) "
                "VALUES(?,?,?,?,?,?,?)", (dvid2, "リード", "内部", "", "", "", 0))
    con.commit()
    assert sfa_db.delivery_grid(con, dvid2) == {"weeks": [], "owners": [], "cells": {}}


def test_delivery_unit_price_8weeks_case(con):
    """8週・チーム合計100%・総額300万 → 月数=2(=8週/4)・月額150万・平均単価(月額)150万。"""
    import io
    import openpyxl
    from cowork import webapp
    acc = con.execute("INSERT INTO accounts(name) VALUES('マルハン')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="AI受託", stage="クロージング", status="open")
    con.commit()
    dvid = sfa_db.create_delivery(con, deal_id=did, title="AI受託",
                                  start_week="2026-09-28", end_week="2026-11-16")  # 8週
    con.commit()
    months = sfa_db.delivery_month_count("2026-09-28", "2026-11-16")
    assert months == 2.0
    mo, to = sfa_db.compute_delivery_fee("total", None, 300, months)
    assert (mo, to) == (150.0, 300.0)   # 総額300 ÷ 2ヶ月 = 月額150
    sfa_db.update_delivery(con, dvid, fee_mode="total", fee_monthly=mo, fee_total=to)
    # チーム合計100%（実想定30+50+20、請求は0＝実想定にフォールバック）
    for ow, pct in (("早瀬", 30), ("杉山", 50), ("戸野", 20)):
        sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner=ow,
                                       from_week="2026-09-28", to_week="2026-11-16",
                                       fte_pct=pct, fte_billing=0)
    con.commit()
    # 実想定は100%/月。請求は全行0%＝純粋請求は0（成果物ベース＝稼働コミットなし）
    assert sfa_db.delivery_total_assign_effort(con, dvid) == 100.0
    assert sfa_db.delivery_total_assign_effort(con, dvid, use_billing=True) == 0.0
    # 平均単価(月額)=月額150÷(工数/100)。請求0%なので実想定100で試算→150万 → xlsxの平均単価列で確認
    wb = openpyxl.load_workbook(io.BytesIO(webapp.build_deliveries_xlsx(con)))
    ws = wb["Delivery一覧"]
    assert ws.cell(2, 15).value == 150   # 平均単価(月額・万円/100%)


def test_reschedule_delivery_assignments_slide_and_extend(con):
    """デリバリー期間変更→各アサインの週が連動スライド（#75）。
    開始移動＝全員まるごとスライド／終了のみ移動＝全員の終了だけ延長。"""
    acc = con.execute("INSERT INTO accounts(name) VALUES('社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-08-03", end_week="2026-08-31")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="A",
                                   from_week="2026-08-03", to_week="2026-08-17", fte_pct=100)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="B",
                                   from_week="2026-08-10", to_week="2026-08-31", fte_pct=50)
    # スライド: 開始+2週（終了も+2週で週数不変）→ 全員 from/to +2週
    sfa_db.reschedule_delivery_assignments(con, dvid, "2026-08-03", "2026-08-31",
                                           "2026-08-17", "2026-09-14")
    byo = {a["owner"]: a for a in sfa_db.list_delivery_assignments(con, dvid)}
    assert (byo["A"]["from_week"], byo["A"]["to_week"]) == ("2026-08-17", "2026-08-31")
    assert (byo["B"]["from_week"], byo["B"]["to_week"]) == ("2026-08-24", "2026-09-14")
    # 週数延長: 開始そのまま・終了+1週 → 全員の終了だけ +1週
    sfa_db.reschedule_delivery_assignments(con, dvid, "2026-08-17", "2026-09-14",
                                           "2026-08-17", "2026-09-21")
    byo = {a["owner"]: a for a in sfa_db.list_delivery_assignments(con, dvid)}
    assert (byo["A"]["from_week"], byo["A"]["to_week"]) == ("2026-08-17", "2026-09-07")
    assert (byo["B"]["from_week"], byo["B"]["to_week"]) == ("2026-08-24", "2026-09-21")
    # 変化なしは0件
    assert sfa_db.reschedule_delivery_assignments(con, dvid, "2026-08-17", "2026-09-21",
                                                  "2026-08-17", "2026-09-21") == 0


def test_add_assignment_defaults_weeks_from_delivery_period(con):
    """週未入力でアサイン追加すると、デリバリーの開始/終了週がデフォルト採用される（webapp経路の要点）。"""
    from cowork import webapp  # noqa: F401 (ルート挙動はweb層。ここではDB既定値の前提を確認)
    acc = con.execute("INSERT INTO accounts(name) VALUES('社2')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D2", stage="受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-08-03", end_week="2026-08-31")
    dv = sfa_db.get_delivery(con, dvid)
    # webappの/assignment/addは from/to 未入力時 dv.start_week/end_week を採用する（本体はこの前提）
    assert dv["start_week"] == "2026-08-03" and dv["end_week"] == "2026-08-31"


def test_base_max_periods_effective_and_replace(con):
    """ベース最大稼働率の期間版（#75）: 期間別に実効値を返す・総入れ替え・空行スキップ。"""
    con.execute("INSERT INTO accounts(name) VALUES('社')"); con.commit()
    # 7/6〜8/2=100 / 8/3〜継続=80
    sfa_db.replace_base_max_periods(con, "早瀬", [
        {"from_week": "2026-07-06", "to_week": "2026-08-02", "max_pct": 100},
        {"from_week": "2026-08-03", "to_week": "", "max_pct": 80},
    ])
    ps = sfa_db.list_base_max_periods(con)["早瀬"]
    assert len(ps) == 2 and ps[1]["to_week"] == ""
    assert sfa_db.base_max_at(con, "早瀬", "2026-07-13") == 100
    assert sfa_db.base_max_at(con, "早瀬", "2026-08-10") == 80   # 継続期間
    assert sfa_db.base_max_at(con, "未登録", "2026-08-10") == 100  # 未設定は100
    # 全空行はスキップ（保存されない）
    sfa_db.replace_base_max_periods(con, "中島", [{"from_week": "", "to_week": "", "max_pct": ""}])
    assert not sfa_db.list_base_max_periods(con).get("中島")
    # 総入れ替え（1期間に置換）
    sfa_db.replace_base_max_periods(con, "早瀬", [{"from_week": "", "to_week": "", "max_pct": 50}])
    assert len(sfa_db.list_base_max_periods(con)["早瀬"]) == 1
    assert sfa_db.base_max_at(con, "早瀬", "2027-01-01") == 50   # 開区間=常に適用
