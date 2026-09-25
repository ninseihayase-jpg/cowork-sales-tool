"""Delivery（受注後・納品）アサイン計画のDB層テスト（#75）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
検証: 提案到達での自動起票 / 見込み(提案)・確定(受注)の振り分け /
      クローズ済み非受注の除外 / アサインブロックの週展開・合算 / ベース工数合算。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import date
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


def test_create_delivery_accepts_confidence_override_at_creation(con, acc_id):
    """新規Delivery起票時に確度（確定/見込み等）を指定できる（ユーザー要望2026-08-23）。
    不正値・省略時は自動導出(None)にフォールバックする。"""
    d = _deal(con, acc_id, "提案")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="X", confidence_override="確定")
    dv = sfa_db.get_delivery(con, dvid)
    assert dv["confidence_override"] == "確定"
    assert sfa_db.delivery_confidence_effective(dv) == "確定"

    dvid2 = sfa_db.create_delivery(con, deal_id=d, title="Y", confidence_override="でたらめ")
    assert sfa_db.get_delivery(con, dvid2)["confidence_override"] is None

    dvid3 = sfa_db.create_delivery(con, deal_id=d, title="Z")
    assert sfa_db.get_delivery(con, dvid3)["confidence_override"] is None


def test_deal_form_and_deliveries_page_creation_forms_include_confidence_select(con, acc_id):
    d = _deal(con, acc_id, "受注", name="調達BPO")
    html = webapp.deal_form(con, sfa_db.get_deal(con, d))
    assert html.count('name="confidence_override"') == 2  # 上部ボタン列＋下部カードの両方

    dp_html = webapp.deliveries_page(con)
    assert 'name="confidence_override"' in dp_html


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


def test_confidence_auto_shows_pre_proposal_when_deal_regresses(con, acc_id):
    """ユーザー報告(2026-09-17):「商談を『要件詰め』に戻したのにDeliveryに反映されない」。
    既にDeliveryが起票された後、商談が提案未満（要件詰め/初回アポ実施/保留中）に差し戻されても、
    従来は"見込み(提案中)"のまま何も変化しなかった（提案未満を区別するバケットが無かった）。
    ユーザー確定事項: バッジ表示のみを区別する新区分"見込み(提案前)"を追加。集計上の扱い
    （_DELIVERY_CONFIDENCE_BUCKET）・稼働集計除外・並び順（_DELIVERY_ACTIVE_CONF_RANK）は
    "見込み(提案中)"と一切変えない。"""
    assert sfa_db.delivery_confidence_auto("要件詰め", "open") == "見込み(提案前)"
    assert sfa_db.delivery_confidence_auto("初回アポ実施", "open") == "見込み(提案前)"
    assert sfa_db.delivery_confidence_auto("保留中", "open") == "見込み(提案前)"

    did = _deal(con, acc_id, "提案")
    dv_id = sfa_db.create_delivery(con, deal_id=did, title="D")
    assert sfa_db.delivery_confidence_effective(sfa_db.get_delivery(con, dv_id)) == "見込み(提案中)"
    con.execute("UPDATE deals SET stage=? WHERE id=?", ("要件詰め", did))  # 商談を提案未満へ差し戻す
    con.commit()
    assert sfa_db.delivery_confidence_effective(sfa_db.get_delivery(con, dv_id)) == "見込み(提案前)"


def test_pre_proposal_confidence_bucket_unchanged_but_sort_rank_below_proposal(con, acc_id):
    """集計バケット（_DELIVERY_CONFIDENCE_BUCKET、稼働集計除外に影響）は"見込み(提案中)"と
    同じままだが、一覧の並び順ランク（webapp._DELIVERY_ACTIVE_CONF_RANK）は"見込み(提案前)"の
    案件が"見込み(提案中)"の案件より一覧で必ず下に来るよう分離されている（2026-09-24〜。
    以前は同ランクで開始週のみに依存しており、提案前の案件が提案中より上に来る不具合があった）。"""
    from cowork import webapp
    assert (sfa_db._DELIVERY_CONFIDENCE_BUCKET["見込み(提案前)"]
            == sfa_db._DELIVERY_CONFIDENCE_BUCKET["見込み(提案中)"])
    assert (webapp._DELIVERY_ACTIVE_CONF_RANK["見込み(提案前)"]
            > webapp._DELIVERY_ACTIVE_CONF_RANK["見込み(提案中)"])


def test_delivery_list_sorts_reverted_pre_proposal_below_still_in_proposal(con, acc_id):
    """ユーザー報告2026-09-24: 提案中→提案前に差し戻した案件が、開始週が早いだけで一覧上
    提案中の案件より上に来てしまう不具合の回帰テスト。開始週を意図的に「提案前の方が早い」
    ように設定し、それでも並び順では提案中が上に来ることを確認する。"""
    from cowork import webapp
    d_back = _deal(con, acc_id, "要件詰め", name="差し戻し案件")  # 見込み(提案前)を自動導出
    dv_back = sfa_db.create_delivery(con, deal_id=d_back, status="進行中", start_week="2026-06-01")
    d_prop = _deal(con, acc_id, "提案", name="提案中案件")  # 見込み(提案中)
    dv_prop = sfa_db.create_delivery(con, deal_id=d_prop, status="進行中", start_week="2026-09-01")
    dv_back_full = sfa_db.get_delivery(con, dv_back)
    dv_prop_full = sfa_db.get_delivery(con, dv_prop)
    lbl_back, _ = webapp._delivery_confidence(dv_back_full["deal_stage"], dv_back_full["deal_status"],
                                              dv_back_full.get("confidence_override"))
    lbl_prop, _ = webapp._delivery_confidence(dv_prop_full["deal_stage"], dv_prop_full["deal_status"],
                                              dv_prop_full.get("confidence_override"))
    assert lbl_back == "見込み(提案前)" and lbl_prop == "見込み(提案中)"
    key_back = webapp._delivery_sort_key(dv_back_full, lbl_back)
    key_prop = webapp._delivery_sort_key(dv_prop_full, lbl_prop)
    assert key_prop < key_back  # 提案中が提案前より必ず上（開始週が逆でも）


def test_compute_load_still_counts_pre_proposal_delivery_same_as_proposal(con, acc_id):
    """稼働集計は"見込み(提案前)"でも除外されない（バッジ表示だけの区別であり、集計上は
    "見込み(提案中)"と同じ扱いのまま。除外したいなら状態(status)を保留/中止にする運用は変わらない）。"""
    did = _deal(con, acc_id, "要件詰め", name="差し戻し案件")
    dv_id = sfa_db.create_delivery(con, deal_id=did, status="進行中")
    W0 = "2026-07-27"
    sfa_db.add_delivery_assignment(con, delivery_id=dv_id, owner="早瀬", role="担当",
                                   member_kind="内部", from_week=W0, to_week=W0, fte_pct=50)
    load = sfa_db.compute_delivery_load(con, start_week=W0, n_weeks=1)
    assert load["cells"]["早瀬"][W0]["actual"]["proposal"] == 50


def test_compute_delivery_load_includes_deliveries_meta_for_productivity(con, acc_id):
    """2026-09-21〜: Hisho側で週別売上・生産性を算出できるよう、delivery_idごとのfee_total
    （fee_mode='monthly'でも解決済み）・start_week/end_weekを deliveries_meta として返す。"""
    did = _deal(con, acc_id, "受注", status="open")
    dv_id = sfa_db.create_delivery(con, deal_id=did, title="X", status="進行中")
    sfa_db.update_delivery(con, dv_id, fee_mode="monthly", fee_monthly=100,
                            start_week="2026-06-01", end_week="2026-06-22")
    W0 = "2026-06-01"
    sfa_db.add_delivery_assignment(con, delivery_id=dv_id, owner="早瀬", from_week=W0, to_week=W0, fte_pct=50)
    load = sfa_db.compute_delivery_load(con, start_week=W0, n_weeks=1)
    meta = load["deliveries_meta"][dv_id]
    assert meta["fee_total"] == 100.0  # 月額100万×1ヶ月(4週)分に解決済み
    assert meta["start_week"] == "2026-06-01"
    assert meta["end_week"] == "2026-06-22"


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


# ---- 日ベースの日程入力 × 週ベースの集計（#180、2026-09-18） ----
# ユーザー要望: 「deliveryの日程を、日ベースで設定できるようにして。一方、集計はすべて週単位で。
# （金曜から始まるプロジェクトでも、50%稼働であれば、その週は50%稼働、と単純に見做す）」
# → 開始/終了は月曜に限らない任意の日付を許容し、週集計側(_assignment_weeks/compute_delivery_load等)
# で月曜にスナップしてから週数を数える。部分週は按分せず単純に1週として数える。

def test_assignment_weeks_counts_partial_week_from_non_monday_start():
    # 2026-09-18は金曜(週の月曜=09-14)。2026-09-24は翌週木曜(週の月曜=09-21)。
    # →09-14週と09-21週の2週にまたがる。
    assert sfa_db._assignment_weeks("2026-09-18", "2026-09-24") == 2
    # 同じ週の中に収まる場合（金曜開始・同じ週の日曜終了）は1週。
    assert sfa_db._assignment_weeks("2026-09-18", "2026-09-20") == 1
    # 月曜スナップ後に既に週が揃っている既存データ（月曜〜月曜）は従来どおり。
    assert sfa_db._assignment_weeks("2026-07-27", "2026-08-10") == 3


def test_delivery_month_count_with_friday_start():
    # 金曜開始でも、その週を1週として単純にカウントする（按分しない）。
    assert sfa_db.delivery_month_count("2026-09-18", "2026-09-24") == 0.5   # 2週÷4


def test_compute_delivery_load_treats_partial_week_as_full_for_friday_start(con, acc_id):
    """金曜から始まる案件で稼働率50%の場合、その週(月曜始まり)は単純に50%稼働とみなす
    （部分週の按分をしない、というユーザー方針#180に基づく）。"""
    did = _deal(con, acc_id, "提案", name="金曜開始案件")
    dv_id = sfa_db.create_delivery(con, deal_id=did, status="進行中")
    sfa_db.add_delivery_assignment(con, delivery_id=dv_id, owner="早瀬", role="担当",
                                   member_kind="内部", from_week="2026-09-18", to_week="2026-09-18",
                                   fte_pct=50)
    W0 = "2026-09-14"  # 2026-09-18(金)が属する週の月曜
    load = sfa_db.compute_delivery_load(con, start_week=W0, n_weeks=1)
    assert load["cells"]["早瀬"][W0]["actual"]["proposal"] == 50


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


def test_delivery_form_discussion_notes_card_moved_to_top(con):
    """ユーザー要望(2026-09-16):「個別Deliveryページの議論メモを上部に移動させて」。
    従来は月別入金計画・削除ボタンより下（ページ最下部）にあったため、常にスクロールが
    必要だった。ヘッダー(タイトル/複製ボタン)の直後・基礎情報フォームより前に移動した。"""
    acc = con.execute("INSERT INTO accounts(name) VALUES('A社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件Z", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="納品Z")
    html = webapp.delivery_form(con, dvid)
    note_pos = html.index("議論メモ")
    duplicate_btn_pos = html.index("このDeliveryを複製")
    base_info_pos = html.index("基礎情報")
    assign_pos = html.index("アサイン（役割")
    assert duplicate_btn_pos < note_pos < base_info_pos < assign_pos
    # divの開閉が壊れていないこと（カード分割時の閉じ忘れ/重複クローズ検知）
    assert html.count("<div") == html.count("</div>")


def test_delivery_form_role_add_row_appears_above_existing_roles(con):
    """#117: 役割追加の入力欄は体制エリアの一番上（既存の役割一覧より前）に出す
    （ユーザー要望2026-08-28: 役割数が増えると追加欄が下に流れて見えづらいため）。"""
    acc = con.execute("INSERT INTO accounts(name) VALUES('A社')").lastrowid
    con.commit()
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件Y", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="納品Y")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="テストロール123", fte_billing=100, fte_pct=100)
    html = webapp.delivery_form(con, dvid)
    add_form_pos = html.index(f'/delivery/{dvid}/role/add')
    existing_role_pos = html.index("テストロール123")
    assert add_form_pos < existing_role_pos


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
    assert wb.sheetnames == ["Delivery一覧", "アサイン明細", "体制(役割別目標)", "月別入金計画"]
    ws = wb["Delivery一覧"]
    assert ws.cell(2, 9).value == 2.75         # 月数（11週÷4）
    assert ws.cell(2, 12).value == "総額報酬"   # 報酬形態（事業種別L1/L2列が2列追加され列位置が後ろへ移動）
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
    assert ws.cell(2, 17).value == 150   # 平均単価(月額・万円/100%)（事業種別L1/L2列の追加で列位置が後ろへ移動）


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


def test_delivery_cost_fields_persist_and_convert(con, acc_id):
    """外注費（ユーザー要望2026-08-23）: 報酬額と同じ月額/総額の相互換算＋外注先名の記録。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")  # 4週=1.0ヶ月
    sfa_db.update_delivery(con, dvid, cost_mode="monthly", cost_monthly=50, cost_total=50,
                            cost_vendor="A社")
    dv = sfa_db.get_delivery(con, dvid)
    assert dv["cost_vendor"] == "A社"
    assert sfa_db.delivery_display_costs(dv) == (50, 50)

    # 総額モードで片方だけ入力→月数から補完される
    sfa_db.update_delivery(con, dvid, cost_mode="total", cost_monthly=None, cost_total=200)
    dv2 = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_display_costs(dv2) == (200.0, 200)  # 200/1.0ヶ月=200/月


def test_delivery_profit_is_fee_minus_cost(con, acc_id):
    """想定利益＝報酬額－外注費。外注費未入力なら報酬額そのまま。両方未入力ならNone。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")
    dv0 = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_profit(dv0) == (None, None)

    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=150, fee_total=150)
    dv1 = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_profit(dv1) == (150, 150)  # 外注費未入力=0扱い

    sfa_db.update_delivery(con, dvid, cost_mode="monthly", cost_monthly=60, cost_total=60)
    dv2 = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_profit(dv2) == (90, 90)


def test_delivery_form_renders_cost_fields_and_profit_display(con, acc_id):
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=150, fee_total=150,
                            cost_mode="monthly", cost_monthly=60, cost_total=60, cost_vendor="B社")
    html = webapp.delivery_form(con, dvid)
    assert 'name="cost_vendor"' in html and 'value="B社"' in html
    assert 'name="cost_mode"' in html
    assert 'id="dvCostMonthly"' in html and 'id="dvCostTotal"' in html
    assert 'id="dvProfitMonthly"' in html and 'id="dvProfitTotal"' in html


def test_delivery_month_range_extends_by_cycle(con, acc_id):
    """delivery_month_range: 開始週〜終了週の月初リスト＋extra_months分の延長（ユーザー要望2026-08-23:
    月別入金計画。検収額と支払いサイクルから入金月を算出するための月範囲展開）。"""
    dv = {"start_week": "2026-09-07", "end_week": "2026-11-30"}
    assert sfa_db.delivery_month_range(dv) == ["2026-09", "2026-10", "2026-11"]
    assert sfa_db.delivery_month_range(dv, extra_months=2) == \
        ["2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]
    assert sfa_db.delivery_month_range({}) == []


def test_delivery_receipt_set_and_delete(con, acc_id):
    """set_delivery_receipt: 保存・上書き・空入力での削除（未入力に戻す）。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 100)
    assert sfa_db.list_delivery_receipts(con, dvid)[0]["amount"] == 100
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 150)  # 上書き
    rows = sfa_db.list_delivery_receipts(con, dvid)
    assert len(rows) == 1 and rows[0]["amount"] == 150
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", "")  # 空入力で削除
    assert sfa_db.list_delivery_receipts(con, dvid) == []
    sfa_db.set_delivery_receipt(con, dvid, "不正な月", 100)  # 不正な月は無視
    assert sfa_db.list_delivery_receipts(con, dvid) == []


def test_delivery_cashflow_shifts_receipts_by_payment_cycle(con, acc_id):
    """delivery_cashflow: 検収額をpayment_cycle_months分ずらして入金額を算出。既定は翌月(1)。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 100)
    sfa_db.set_delivery_receipt(con, dvid, "2026-10", 50)
    cf = sfa_db.delivery_cashflow(con, dvid)
    assert cf["receipts"] == {"2026-09": 100, "2026-10": 50}
    assert cf["payments"] == {"2026-10": 100, "2026-11": 50}  # 既定サイクル=1ヶ月後
    assert "2026-11" in cf["months"]  # 入金月も月一覧に含まれる（テーブル表示のため）

    sfa_db.update_delivery(con, dvid, payment_cycle_months=0)  # 検収月内に入金
    cf2 = sfa_db.delivery_cashflow(con, dvid)
    assert cf2["payments"] == {"2026-09": 100, "2026-10": 50}

    sfa_db.update_delivery(con, dvid, payment_cycle_months=2)  # 検収月+2ヶ月後・年跨ぎ確認は別途
    cf3 = sfa_db.delivery_cashflow(con, dvid)
    assert cf3["payments"] == {"2026-11": 100, "2026-12": 50}


def test_delivery_form_renders_cashflow_table(con, acc_id):
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 100)
    html = webapp.delivery_form(con, dvid)
    assert 'id="dvCashflow"' in html
    assert "2026/09" in html and "2026/10" in html  # _fmt_month表示
    assert 'onchange="dvSetCycle(' in html
    assert f'onchange="dvReceiptSet({dvid},\'2026-09\',this.value)"' in html


def test_delivery_business_type_inherits_from_deal_by_default(con, acc_id):
    """事業種別L1/L2はデフォルトで紐づく商談を継承（ユーザー要望2026-08-23）。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注",
                           business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    dv = sfa_db.get_delivery(con, dvid)
    assert dv["deal_business_type_l1"] == "コスト削減"
    assert sfa_db.delivery_business_type_effective(dv) == ("コスト削減", "コスト診断(無償)")


def test_delivery_business_type_override_takes_precedence(con, acc_id):
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注",
                           business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.update_delivery(con, dvid, business_type_l1_override="コンサルティング")
    dv = sfa_db.get_delivery(con, dvid)
    l1, l2 = sfa_db.delivery_business_type_effective(dv)
    assert l1 == "コンサルティング"


def test_delivery_form_renders_business_type_override_selects(con, acc_id):
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注",
                           business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    html = webapp.delivery_form(con, dvid)
    assert 'name="business_type_l1_override"' in html
    assert 'name="business_type_l2_override"' in html
    # 2026-08-27: 「自動（現在: X）」→「X（自動: 商談を継承）」に表記順を変更（値が先頭に出る）
    assert "コスト削減（自動: 商談を継承）" in html
    assert "コスト診断(無償)（自動: 商談を継承）" in html


def test_delivery_form_owner_roles_box_is_display_only(con, acc_id):
    """2026-08-29: 責任者・担当者は体制の上に表示専用ボックスとして配置し、
    値の入力はここでは行わない（下のアサイン行のチェックボックスで指定する）。
    未設定時は「未設定」と表示される。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    html = webapp.delivery_form(con, dvid)
    assert "責任者・担当者" in html
    box = webapp._delivery_owner_roles_box_html(sfa_db.get_delivery(con, dvid))
    assert "責任者" in box and "担当者" in box
    assert "未設定" in box
    assert "<select" not in box  # 直接入力用のセレクトは無い（自動表示のみ）


def test_delivery_form_assignment_row_has_responsible_and_handling_checkboxes(con, acc_id):
    """2026-08-29: 責任者/担当者はアサイン行のチェックボックスから指定する。
    既にresponsible_owner/handling_ownerに一致するアサインは初期状態でチェック済みになる。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-07",
                                   to_week="2026-09-14", role="コンサルタント", fte_pct=50)
    sfa_db.update_delivery(con, dvid, responsible_owner="早瀬", handling_owner="早瀬")
    html = webapp.delivery_form(con, dvid)
    assert 'data-role-field="responsible_owner"' in html
    assert 'data-role-field="handling_owner"' in html
    assert 'data-role-field="responsible_owner" checked' in html
    assert 'data-role-field="handling_owner" checked' in html
    assert "責任者: <b>早瀬</b>" in html
    assert "担当者: <b>早瀬</b>" in html




def test_delivery_form_includes_billing_fields_and_collapsible_effort_box(con, acc_id):
    """#121: 基礎情報に請求方法/請求期日/請求送付先・経費請求有無/メモを追加。
    総アサイン工数等のボックスは<details>で既定折りたたみにする。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.update_delivery(con, dvid, billing_method=sfa_db.DELIVERY_BILLING_METHODS[1],
                           billing_recipient="経理部佐藤さん、PF提出", expense_billing="無")
    html = webapp.delivery_form(con, dvid)
    assert 'name="billing_method"' in html
    assert f'<option value="{sfa_db.DELIVERY_BILLING_METHODS[1]}" selected>' in html
    assert 'name="billing_recipient"' in html and "経理部佐藤さん、PF提出" in html
    assert 'name="expense_billing"' in html
    assert 'name="expense_billing_note"' in html
    assert "<details" in html and "総アサイン工数" in html
    # 既定値(当月末日)がマスタ選択肢として選択されている（自由入力欄は隠れている）
    assert 'id="dvBillingDueOther"' in html and "display:none" in html


def test_delivery_form_base_info_autosaves_without_save_button(con, acc_id):
    """#132: 基礎情報(#dvBaseForm)は保存ボタン押し忘れ対策として、フィールド変更で自動保存される。
    (1) 保存の要否をユーザーに教える案内文言、(2) JS側の自動保存関数、(3) 責任者/担当者の
    選択肢をアサイン変更に追従させる関数、が出力に含まれること。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    html = webapp.delivery_form(con, dvid)
    assert "自動保存されます" in html
    assert 'id="dvBaseSaveStatus"' in html
    assert "function dvBaseAutoSave" in html
    assert "function dvOwnerRolesRender" in html
    assert 'id="dvOwnerRolesBox"' in html


def test_delivery_form_weeks_field_is_readonly_auto_calculated(con, acc_id):
    """2026-09-22〜: 週数(hdrWeeks)は開始日・終了日・対象外期間から自動計算する表示専用フィールド
    になった（3カレンダーでの直接選択に置き換え、タイプ入力から終了日を逆算するhdrCalcEnd()は廃止）。
    readonlyで、ユーザー操作による自動保存トリガーの対象にはならない。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    html = webapp.delivery_form(con, dvid)
    assert 'id="hdrWeeks"' in html
    assert "readonly" in html
    assert "hdrCalcEnd" not in html  # 廃止された旧関数が残っていないこと


def test_delivery_form_save_button_is_beside_base_info_heading(con, acc_id):
    """2026-08-29: 保存ボタンは「基礎情報」見出しの横に配置し、確度/事業種別L1/L2は同じ行に
    横並びで表示する（ユーザー要望）。保存ボタンは見出し直下でform属性経由でdvBaseFormに紐づく。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    html = webapp.delivery_form(con, dvid)
    assert '基礎情報' in html
    heading_idx = html.index("基礎情報")
    button_idx = html.index('<button class="btn" form="dvBaseForm"')
    form_idx = html.index('<form id="dvBaseForm"')
    assert heading_idx < button_idx < form_idx  # 保存ボタンは見出しの直後・フォーム本体より前
    assert '必須' in html  # 先に基礎情報を入力するよう促す注意喚起


def test_delivery_form_billing_due_other_shown_when_custom_value(con, acc_id):
    """請求期日にマスタ外のカスタム文言が入っている場合は「他」を選択状態にし、
    自由入力欄にその値を出す（隠さない）。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.update_delivery(con, dvid, billing_due="毎月10日")
    html = webapp.delivery_form(con, dvid)
    assert '<option value="__other__" selected>他</option>' in html
    assert 'value="毎月10日"' in html


def test_build_deliveries_xlsx_includes_business_type_columns(con, acc_id):
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注",
                           business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_deliveries_xlsx(con)))
    ws = wb["Delivery一覧"]
    hdr = [c.value for c in ws[1]]
    assert "事業種別L1" in hdr and "事業種別L2" in hdr
    row = [c.value for c in ws[2]]
    row_dict = dict(zip(hdr, row))
    assert row_dict["ID"] == dvid
    assert row_dict["事業種別L1"] == "コスト削減"
    assert row_dict["事業種別L2"] == "コスト診断(無償)"


def test_build_deliveries_xlsx_includes_cashflow_sheet(con, acc_id):
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 100)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_deliveries_xlsx(con)))
    assert "月別入金計画" in wb.sheetnames
    ws = wb["月別入金計画"]
    assert ws.cell(row=1, column=1).value == "Delivery ID"
    rows = [tuple(r) for r in ws.iter_rows(min_row=2, values_only=True)]
    assert any(r[0] == dvid and r[3] == "2026-09" and r[4] == 100 for r in rows)


def _ym(n: int) -> str:
    """今月からnヶ月後の'YYYY-MM'（テストはその日実行される日付に依存しないよう相対計算）。"""
    today = webapp._today_jst()
    y, m = sfa_db._add_months_ym(today.year, today.month, n)
    return f"{y:04d}-{m:02d}"


def _ml(ym: str) -> str:
    return f"{ym[2:4]}/{ym[5:7]}"


def test_payment_schedule_xlsx_combines_receipt_and_payment_rows_with_filterable_flag(con, acc_id):
    """#115（2026-08-28修正）: 検収/入金は別ファイルではなく同一xlsx・同一シートに同居させ、
    先頭列「検収/入金」の値でExcel側のフィルタ機能から絞り込めるようにする。
    支払いサイト・責任者/担当者・請求関連・アサインN・月列(今月〜+18ヶ月固定・未登録月は0)も検証。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="A社支援", status="進行中")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-07",
                                   to_week="2026-09-14", role="コンサルタント", fte_pct=50)
    sfa_db.update_delivery(con, dvid, payment_cycle_months=1, responsible_owner="早瀬",
                           billing_method=sfa_db.DELIVERY_BILLING_METHODS[0], billing_due="翌月1日",
                           billing_recipient="経理部佐藤さん、PF提出", expense_billing="有",
                           expense_billing_note="交通費のみ")
    m0, m1, m2 = _ym(0), _ym(1), _ym(2)
    sfa_db.set_delivery_receipt(con, dvid, m0, 100)
    sfa_db.set_delivery_receipt(con, dvid, m1, 200)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    assert ws.title == "入金予定表"
    hdr = [c.value for c in ws[1]]
    assert hdr[:18] == ["確度", "検収/入金", "#", "クライアント", "案件", "事業種別L1", "事業種別L2",
                        "状態", "開始週", "終了週", "支払いサイト", "責任者", "担当者", "請求方法",
                        "請求期日", "請求送付先", "経費請求有無", "経費請求メモ"]
    assert hdr[18] == "アサイン1"
    assert _ml(m0) in hdr and _ml(m1) in hdr and _ml(m2) in hdr
    assert _ml(_ym(18)) in hdr   # 今月+18ヶ月後まで含む
    assert _ml(_ym(19)) not in hdr  # +19ヶ月後は含まない(固定19ヶ月分)

    rows = [dict(zip(hdr, [c.value for c in ws[r]])) for r in (2, 3)]
    receipt_row = next(r for r in rows if r["検収/入金"] == "検収")
    payment_row = next(r for r in rows if r["検収/入金"] == "入金")
    assert receipt_row["#"] == dvid and receipt_row["案件"] == "A社支援"
    assert receipt_row["確度"] == "確定"  # stage=受注→自動判定は「確定」
    assert receipt_row["支払いサイト"] == 1
    assert receipt_row["責任者"] == "早瀬" and receipt_row["担当者"] == "na"  # #137: 空欄は"na"に統一
    assert receipt_row["請求方法"] == sfa_db.DELIVERY_BILLING_METHODS[0]
    assert receipt_row["請求期日"] == "翌月1日"
    assert receipt_row["請求送付先"] == "経理部佐藤さん、PF提出"
    assert receipt_row["経費請求有無"] == "有" and receipt_row["経費請求メモ"] == "交通費のみ"
    assert receipt_row["アサイン1"] == "早瀬"
    assert receipt_row[_ml(m0)] == 100 and receipt_row[_ml(m1)] == 200
    assert receipt_row[_ml(m2)] == 0  # 登録の無い月は0
    assert payment_row[_ml(m0)] == 0  # 検収月自体には入金額は出ない
    assert payment_row[_ml(m1)] == 100 and payment_row[_ml(m2)] == 200  # 1ヶ月後に入金
    assert ws.auto_filter.ref == f"A1:{openpyxl.utils.get_column_letter(len(hdr))}3"


def test_payment_schedule_xlsx_uses_meiryo_ui_10pt_font(con, acc_id):
    """#137: フォントはメイリオ UI 10ptに統一する（ヘッダ・データともに）。"""
    d = _deal(con, acc_id, "受注")
    sfa_db.create_delivery(con, deal_id=d, title="A社支援")
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    assert ws["A1"].font.name == "Meiryo UI" and ws["A1"].font.size == 10 and ws["A1"].font.bold
    assert ws["A2"].font.name == "Meiryo UI" and ws["A2"].font.size == 10 and not ws["A2"].font.bold


def test_payment_schedule_xlsx_blank_fields_shown_as_na_except_amounts(con, acc_id):
    """#137: 金額(月)列以外の空欄はすべて"na"に統一する。金額列は0のまま。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="")  # 未入力の空文字
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    hdr = [c.value for c in ws[1]]
    row = dict(zip(hdr, [c.value for c in ws[2]]))
    assert row["案件"] == "na"
    assert row["開始週"] == "na" and row["終了週"] == "na"
    assert row["責任者"] == "na" and row["担当者"] == "na"
    assert row["請求方法"] == "na" and row["請求送付先"] == "na"
    assert row["経費請求有無"] == "na" and row["経費請求メモ"] == "na"
    assert row[_ml(_ym(0))] == 0  # 金額列は"na"にせず0のまま


def test_payment_schedule_xlsx_includes_business_type_columns(con, acc_id):
    """#137: 事業種別L1/L2もカラムとして出力する（override優先、無ければ商談から継承）。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注",
                           business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="A社支援")
    sfa_db.update_delivery(con, dvid, business_type_l1_override="コンサルティング")
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    hdr = [c.value for c in ws[1]]
    row = dict(zip(hdr, [c.value for c in ws[2]]))
    assert row["事業種別L1"] == "コンサルティング"  # override優先
    assert row["事業種別L2"] == "コスト診断(無償)"  # override無し→商談から継承


def test_payment_schedule_xlsx_sorted_by_confidence_then_start_week_including_done(con, acc_id):
    """#137: 並び順は確度順(完了含む) × 開始週の早い順。一覧ページの並び(_delivery_sort_key)とは異なり、
    状態=完了のDeliveryも他と同じ確度基準で並べる（下段に沈めない）。"""
    d1 = _deal(con, acc_id, "提案")  # 見込み(提案中)
    dv1 = sfa_db.create_delivery(con, deal_id=d1, title="提案中・遅い開始", start_week="2026-10-05")
    d2 = _deal(con, acc_id, "受注")  # 確定
    dv2 = sfa_db.create_delivery(con, deal_id=d2, title="確定・完了済み・早い開始", start_week="2026-09-07")
    sfa_db.update_delivery(con, dv2, status="完了")
    d3 = _deal(con, acc_id, "受注")  # 確定
    dv3 = sfa_db.create_delivery(con, deal_id=d3, title="確定・遅い開始", start_week="2026-09-14")
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    hdr = [c.value for c in ws[1]]
    ids_in_order = []
    for r in range(2, ws.max_row + 1):
        row = dict(zip(hdr, [c.value for c in ws[r]]))
        if row["#"] not in ids_in_order:
            ids_in_order.append(row["#"])
    # 確定(完了含む)が先、確定内は開始週の早い順。見込み(提案中)は最後。
    assert ids_in_order == [dv2, dv3, dv1]


def test_payment_schedule_xlsx_multiple_assignees_get_own_columns(con, acc_id):
    """アサインは1人1列(アサインN)。列数は全案件を通じた最大人数に揃え、少ない案件は空欄で埋める。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="複数アサイン案件")
    sfa_db.set_delivery_receipt(con, dvid, _ym(0), 50)
    for owner in ("早瀬", "中島", "吉江"):
        sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner=owner, from_week="2026-09-07",
                                       to_week="2026-09-14", role="コンサルタント", fte_pct=30)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    hdr = [c.value for c in ws[1]]
    assert ["アサイン1", "アサイン2", "アサイン3"] == hdr[18:21]
    row = dict(zip(hdr, [c.value for c in ws[2]]))
    # sorted()の文字コード順（五十音順ではない）: 中島(4E2D) < 吉江(5409) < 早瀬(65E9)
    assert row["アサイン1"] == "中島" and row["アサイン2"] == "吉江" and row["アサイン3"] == "早瀬"


def test_payment_schedule_xlsx_includes_deliveries_with_no_amount_registered(con, acc_id):
    """#121（2026-08-28）: 検収/入金の登録が無い案件も、月列0埋めの行として出力する
    （以前はスキップしていたが、予定が未入力の案件も一覧できるよう変更）。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="登録なし案件")
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    assert ws.max_row == 3  # ヘッダ+検収行+入金行
    hdr = [c.value for c in ws[1]]
    rows = [dict(zip(hdr, [c.value for c in ws[r]])) for r in (2, 3)]
    assert {r["検収/入金"] for r in rows} == {"検収", "入金"}
    assert all(r["#"] == dvid for r in rows)
    assert all(r[_ml(_ym(0))] == 0 for r in rows)  # 登録が無い月は0埋め


def test_payment_schedule_xlsx_includes_past_months_for_completed_deliveries(con, acc_id):
    """ユーザー報告(2026-09-18):「入金予定表Excelが当月以降の金額のみ出力される。完了した
    案件含め、全案件・全月の実績を出力してほしい」。月列が今月〜+18ヶ月の固定窓だったため、
    完了済み案件の過去の検収/入金実績（窓の外）が黙って欠落していた不具合の回帰確認。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="完了済み案件", status="完了")
    m_past2, m_past1 = _ym(-24), _ym(-13)  # 固定19ヶ月窓の外側の過去月
    sfa_db.set_delivery_receipt(con, dvid, m_past2, 300)
    sfa_db.set_delivery_receipt(con, dvid, m_past1, 400)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(webapp.build_delivery_payment_schedule_xlsx(con)))
    ws = wb.active
    hdr = [c.value for c in ws[1]]
    assert _ml(m_past2) in hdr and _ml(m_past1) in hdr, "過去の実績月が列に出力されていない"
    rows = [dict(zip(hdr, [c.value for c in ws[r]])) for r in range(2, ws.max_row + 1)]
    receipt_row = next(r for r in rows if r["#"] == dvid and r["検収/入金"] == "検収")
    assert receipt_row[_ml(m_past2)] == 300 and receipt_row[_ml(m_past1)] == 400
    # payment_cycle_months未設定(既定1ヶ月)なので入金は検収の翌月
    payment_row = next(r for r in rows if r["#"] == dvid and r["検収/入金"] == "入金")
    m_past2_pay = _ym(-23)
    assert payment_row[_ml(m_past2_pay)] == 300


def test_deliveries_page_renders_full_width_and_wider_columns(con, acc_id):
    """#118: Delivery一覧は画面幅いっぱいに表示（main-wide）し、状態セレクト等が
    潰れて見えなくならないよう最低幅を確保する（ユーザー報告2026-08-28: 状態が潰れている）。"""
    d = _deal(con, acc_id, "受注")
    sfa_db.create_delivery(con, deal_id=d, title="案件Z")
    html = webapp.render(webapp.deliveries_page(con), wide=True).decode("utf-8")
    assert '<main class="main-wide">' in html
    assert 'min-width:104px' in html  # 状態セレクトの最低幅
    assert 'width:220px' in html  # 案件名入力欄

    html_default = webapp.render("<div>x</div>").decode("utf-8")
    assert '<main class="">' in html_default  # 他ページは従来通り(1440px上限)のまま


# ── Delivery複製（ユーザー要望2026-08-27） ────────────────────────────────

def test_duplicate_delivery_copies_plan_fields_but_not_execution_data(con, acc_id):
    """deal_duplicateと同じ思想: 体制(目標役割)・報酬/外注費設定は引き継ぐが、
    実行済みのアサイン実績・検収実額・確度の手動固定は引き継がず、真っ白から始める。"""
    d = _deal(con, acc_id, "受注")
    src_id = sfa_db.create_delivery(con, deal_id=d, title="A社支援", start_week="2026-09-07",
                                    end_week="2026-10-04", status="完了",
                                    overview="概要テキスト", confidence_override="確定")
    sfa_db.update_delivery(con, src_id, fee_mode="monthly", fee_monthly=100, cost_mode="monthly",
                           cost_monthly=20, cost_vendor="外注先X", payment_cycle_months=2,
                           business_type_l1_override="コスト削減", business_type_l2_override="診断")
    sfa_db.add_delivery_role(con, delivery_id=src_id, role="リード", fte_billing=15, fte_pct=5)
    sfa_db.add_delivery_role(con, delivery_id=src_id, role="コンサルタント", fte_billing=50, fte_pct=30)
    sfa_db.add_delivery_assignment(con, delivery_id=src_id, owner="高橋", from_week="2026-09-07",
                                   to_week="2026-09-14", role="コンサルタント", fte_pct=50)
    sfa_db.update_delivery(con, src_id, responsible_owner="高橋", handling_owner="高橋",
                           billing_method=sfa_db.DELIVERY_BILLING_METHODS[0], billing_due="翌月1日",
                           billing_recipient="経理部佐藤さん", expense_billing="有",
                           expense_billing_note="交通費のみ",
                           performance_fee="有", performance_fee_ratio=15.0)
    sfa_db.set_delivery_receipt(con, src_id, "2026-09", 100)

    new_id = sfa_db.duplicate_delivery(con, src_id)
    assert new_id and new_id != src_id
    new = sfa_db.get_delivery(con, new_id)
    assert new["title"] == "A社支援（コピー）"
    assert new["deal_id"] == d
    assert new["status"] == "進行中"  # 真っ白から始める
    assert new["confidence_override"] is None  # 手動固定は引き継がない→自動導出に戻る
    assert new["overview"] == "概要テキスト"
    assert new["fee_mode"] == "monthly" and new["fee_monthly"] == 100
    assert new["cost_vendor"] == "外注先X"
    assert new["payment_cycle_months"] == 2
    assert new["business_type_l1_override"] == "コスト削減"
    # 請求関連は計画情報として引き継ぐ
    assert new["billing_method"] == sfa_db.DELIVERY_BILLING_METHODS[0]
    assert new["billing_due"] == "翌月1日"
    assert new["billing_recipient"] == "経理部佐藤さん"
    assert new["expense_billing"] == "有" and new["expense_billing_note"] == "交通費のみ"
    assert new["performance_fee"] == "有" and new["performance_fee_ratio"] == 15.0
    # 責任者/担当者はアサインリスト由来のため、アサインが空の複製直後は引き継がない
    assert new["responsible_owner"] is None and new["handling_owner"] is None

    new_roles = sfa_db.list_delivery_roles(con, new_id)
    assert {(r["role"], r["fte_billing"], r["fte_pct"]) for r in new_roles} == {
        ("リード", 15.0, 5.0), ("コンサルタント", 50.0, 30.0)}

    # アサイン実績・検収実額はコピーしない
    assert sfa_db.list_delivery_assignments(con, new_id) == []
    assert sfa_db.list_delivery_receipts(con, new_id) == []
    # 元Deliveryは変更されない
    assert sfa_db.list_delivery_assignments(con, src_id) != []


def test_duplicate_delivery_missing_source_returns_none(con):
    assert sfa_db.duplicate_delivery(con, 999999) is None


def test_delivery_confidence_and_business_type_auto_option_label_shows_value_first(con, acc_id):
    """自動判定の選択肢は「自動（現在: X）」ではなく「X（自動判定）」の順で表示する
    （ユーザー要望2026-08-27: カッコの内外が逆で分かりにくいとの指摘）。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="案件X", stage="受注", status="open")
    con.commit()
    dv = sfa_db.get_delivery(con, sfa_db.create_delivery(con, deal_id=did, title="納品X"))

    html = webapp.delivery_form(con, dv["id"])
    assert "自動（現在" not in html
    assert "確定（自動判定）" in html  # stage=受注→自動判定は「確定」が先頭に出る

    l1_opts = webapp._delivery_biz_l1_opts(con, dv)
    l2_opts = webapp._delivery_biz_l2_opts(con, dv)
    assert "自動（商談を継承" not in l1_opts and "自動（商談を継承" not in l2_opts
    assert "（自動: 商談を継承）" in l1_opts and "（自動: 商談を継承）" in l2_opts


def test_delivery_missing_requirements_empty_while_deal_stage_before_closing(con, acc_id):
    """#134: 見込み段階（クロージング未満）では、必須項目が何も入っていなくても
    警告を出さない（ユーザー確定仕様: 起票直後から出すと警告過多になるため）。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="提案")
    dvid = sfa_db.create_delivery(con, deal_id=did)
    dv = sfa_db.get_delivery(con, dvid)
    assert webapp._delivery_missing_requirements(con, dv) == []


def test_delivery_missing_requirements_lists_gaps_once_deal_reaches_closing(con, acc_id):
    """#134: 商談がクロージング以降になると、未入力の必須項目をラベルで列挙する。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="クロージング")
    dvid = sfa_db.create_delivery(con, deal_id=did)
    dv = sfa_db.get_delivery(con, dvid)
    missing = webapp._delivery_missing_requirements(con, dv)
    assert "報酬形態・報酬額" in missing
    assert "責任者" in missing
    assert "請求方法・請求期日・請求送付先" in missing
    assert "体制" in missing
    assert "アサイン" in missing
    assert "検収額" in missing
    assert "経費請求有無" in missing


def test_delivery_missing_requirements_empty_once_all_fields_filled(con, acc_id):
    """#134: 必須項目を全て埋めると、クロージング以降でも警告が出なくなる。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="クロージング")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week="2026-09-07", end_week="2026-10-04")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="コンサルタント", fte_billing=100, fte_pct=100)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-07",
                                   to_week="2026-10-04", role="コンサルタント", fte_pct=100)
    sfa_db.update_delivery(
        con, dvid, fee_mode="monthly", fee_monthly=100, fee_total=100,
        responsible_owner="早瀬", billing_method=sfa_db.DELIVERY_BILLING_METHODS[0],
        billing_recipient="経理部佐藤さん", expense_billing="有", payment_cycle_months=1)
    sfa_db.set_delivery_receipt(con, dvid, "2026-09", 50)
    dv = sfa_db.get_delivery(con, dvid)
    assert webapp._delivery_missing_requirements(con, dv) == []


def test_delivery_form_shows_missing_requirements_banner_only_once_closing(con, acc_id):
    """#134: 個別編集画面に、未入力の必須項目を列挙する警告バナーが出る
    （見込み段階では出ない）。"""
    did_early = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D1", stage="提案")
    dvid_early = sfa_db.create_delivery(con, deal_id=did_early)
    assert "未入力の必須項目" not in webapp.delivery_form(con, dvid_early)

    did_late = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D2", stage="クロージング")
    dvid_late = sfa_db.create_delivery(con, deal_id=did_late)
    html_late = webapp.delivery_form(con, dvid_late)
    assert "未入力の必須項目" in html_late
    assert "責任者" in html_late


def test_deliveries_page_shows_warning_badge_for_missing_requirements(con, acc_id):
    """#134: Delivery一覧にも、必須項目未入力のDeliveryに警告バッジ(⚠️)を出す。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="クロージング")
    sfa_db.create_delivery(con, deal_id=did)
    html = webapp.deliveries_page(con)
    assert "⚠️" in html
    assert "未入力の必須項目" in html


def test_delivery_form_renders_performance_fee_fields_next_to_fee(con, acc_id):
    """#138: 基礎情報の報酬額の横に成果報酬有無/比率を入力できる。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.update_delivery(con, dvid, performance_fee="有", performance_fee_ratio=12.5)
    html = webapp.delivery_form(con, dvid)
    assert 'name="performance_fee"' in html
    assert 'id="dvPerfFeeRatio"' in html and 'name="performance_fee_ratio"' in html
    assert '<option value="有" selected>有</option>' in html
    assert 'value="12.5"' in html
    assert "function dvPerfFeeChanged" in html


def test_delivery_performance_fee_total_is_impact_times_ratio(con, acc_id):
    """delivery_performance_fee_total()＝想定インパクト×成果報酬比率÷100。
    成果報酬有無≠有、または想定インパクト・比率のどちらか未入力なら0。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.update_delivery(con, dvid, performance_fee="有", performance_fee_ratio=10, expected_impact=400)
    dv = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_performance_fee_total(dv) == 40.0
    sfa_db.update_delivery(con, dvid, performance_fee="無")
    dv = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_performance_fee_total(dv) == 0.0


def test_delivery_weekly_productivity_adds_performance_fee_to_fixed_fee_at_last_staffed_week(con, acc_id):
    """ユーザー要望2026-09-24: 固定報酬(報酬額/月額・総額)は従来通り契約期間へ週按分し、
    成果報酬(想定インパクト×比率)はそれとは別建てで稼働最終週にだけ全額を上乗せする
    （置き換えではなく加算）。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=d)
    sfa_db.update_delivery(con, dvid, fee_mode="total", fee_total=200, cost_mode="total", cost_total=0,
                            expected_expense_total=0, performance_fee="有", performance_fee_ratio=10,
                            expected_impact=400, start_week="2026-06-01", end_week="2026-06-22")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                   to_week="2026-06-15", fte_pct=100)
    weeks = ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22"]
    prod = sfa_db.delivery_weekly_productivity(con, dvid, weeks)
    # 固定報酬200を4週で均等按分＝週50、成果報酬(400×10%=40)は稼働最終週(6/15)にだけ加算。
    assert prod["weekly_margin"]["2026-06-01"] == 50.0
    assert prod["weekly_margin"]["2026-06-08"] == 50.0
    assert prod["weekly_margin"]["2026-06-15"] == 90.0  # 50(固定按分) + 40(成果報酬)
    assert prod["weekly_margin"]["2026-06-22"] == 50.0  # 稼働は6/15までだが契約は6/22まで按分対象
    assert prod["cum_margin"]["2026-06-22"] == 240.0  # 200(固定) + 40(成果報酬)


def test_delivery_save_route_keeps_expected_expense_auto_tracking_unless_expense_manual_flag_set(
        monkeypatch, tmp_path):
    """ユーザー報告(2026-09-24):「総額1500で5%なら経費75のはずが157.5のまま」。原因は
    想定経費(expected_expense_total)がNULL=自動5%というパターンなのに、フォーム全体の
    自動保存が毎回その時点の表示値をそのまま永続化してしまい、一度でも保存されると
    以後は総額が変わっても追従しなくなっていたこと（fee_manual/cost_manualと同じ不具合の型）。
    expense_manualフラグが立っていない限り、送信された想定経費の値に関わらずNULL保存され、
    総額側の変更に追従し続けることを確認する。"""
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv_exp.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, title="D")
    con2.close()

    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", "u")
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def _post(extra):
        base = {
            "title": "D", "start_week": "2026-10-01", "end_week": "2026-12-25",
            "status": "進行中", "overview": "", "fee_mode": "total", "fee_monthly": "",
            "fee_total": "1500", "cost_mode": "total", "cost_monthly": "", "cost_total": "0",
            "cost_vendor": "", "confidence_override": "", "business_type_l1_override": "",
            "business_type_l2_override": "", "billing_method": "", "billing_due_sel": "",
            "billing_due_other": "", "billing_recipient": "", "expense_billing": "",
            "expense_billing_note": "", "performance_fee": "無", "performance_fee_ratio": "",
            "expected_impact": "", "excluded_periods": "", "fee_manual": "0", "cost_manual": "0",
        }
        base.update(extra)
        headers = {"Cookie": f"sfa_session={webapp._make_session_token()}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode(base).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/delivery/{dvid}/save",
            data=body, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)

    try:
        # 1回目保存: 想定経費は未修正（expense_manual=0）。フォーム側が計算した157.5等の
        # 値をたとえ一緒に送っても、サーバはNULLとして保存し自動5%追従を維持するべき。
        _post({"expected_expense_total": "157.5", "expense_manual": "0"})
        con3 = sfa_db.connect(db_path)
        dv = sfa_db.get_delivery(con3, dvid)
        con3.close()
        assert dv.get("expected_expense_total") is None, "自動追従のはずがNULLでなく固定保存された"
        assert sfa_db.delivery_expected_expense_total(dv) == 75.0  # 1500×5%

        # 総額を変えても追従し続ける（手修正していないため）。
        _post({"fee_total": "1000", "expected_expense_total": "75.0", "expense_manual": "0"})
        con4 = sfa_db.connect(db_path)
        dv2 = sfa_db.get_delivery(con4, dvid)
        con4.close()
        assert dv2.get("expected_expense_total") is None
        assert sfa_db.delivery_expected_expense_total(dv2) == 50.0  # 1000×5%

        # ユーザーが想定経費を直接手修正(expense_manual=1)した場合は、その値をそのまま保持する。
        _post({"fee_total": "1000", "expected_expense_total": "30.0", "expense_manual": "1"})
        con5 = sfa_db.connect(db_path)
        dv3 = sfa_db.get_delivery(con5, dvid)
        con5.close()
        assert dv3.get("expected_expense_total") == 30.0
        assert dv3.get("expense_manual") == 1
        # 手修正後は、無関係な他フィールドの保存でも上書きされない。
        _post({"fee_total": "2000", "expected_expense_total": "30.0", "expense_manual": "1"})
        con6 = sfa_db.connect(db_path)
        dv4 = sfa_db.get_delivery(con6, dvid)
        con6.close()
        assert dv4.get("expected_expense_total") == 30.0
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_delivery_missing_requirements_flags_performance_fee_ratio_regardless_of_stage(con, acc_id):
    """#138: 成果報酬有無=有なのに比率が未入力の場合、商談の段階（見込みでも）に関わらず
    必須項目として警告する（他の#134項目とは異なりクロージング以降縛りなし）。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="提案")  # 見込み段階
    dvid = sfa_db.create_delivery(con, deal_id=did)
    sfa_db.update_delivery(con, dvid, performance_fee="有")
    dv = sfa_db.get_delivery(con, dvid)
    assert "成果報酬比率" in webapp._delivery_missing_requirements(con, dv)

    sfa_db.update_delivery(con, dvid, performance_fee_ratio=10.0)
    dv2 = sfa_db.get_delivery(con, dvid)
    assert "成果報酬比率" not in webapp._delivery_missing_requirements(con, dv2)

    sfa_db.update_delivery(con, dvid, performance_fee="無", performance_fee_ratio=None)
    dv3 = sfa_db.get_delivery(con, dvid)
    assert "成果報酬比率" not in webapp._delivery_missing_requirements(con, dv3)  # 「無」なら不要


def test_deliveries_page_renders_business_type_filter_selects_and_data_attrs(con, acc_id):
    """#139: 事業種別L1/L2でもフィルタできるよう、専用セレクトと各行のdata属性を出力する。"""
    d = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="受注",
                           business_type_l1="コスト削減", business_type_l2="コスト診断(無償)")
    sfa_db.create_delivery(con, deal_id=d, title="A社支援")
    html = webapp.deliveries_page(con)
    assert 'id="dvBizL1F"' in html and 'id="dvBizL2F"' in html
    assert "全事業種別L1" in html and "全事業種別L2" in html
    assert '<option value="コスト削減">コスト削減</option>' in html
    assert '<option value="コスト診断(無償)">コスト診断(無償)</option>' in html
    assert 'data-bizl1="コスト削減"' in html
    assert 'data-bizl2="コスト診断(無償)"' in html
    assert "dvBizL1F" in html and "dvBizL2F" in html  # filterDeliveries()内で参照されていること


# ── #168: 体制の役割削除でアサインも連動削除・役割のドラッグ並び替え ──

def test_delete_delivery_role_also_deletes_matching_assignments(con, acc_id):
    """役割を体制から削除すると、同じdelivery×roleのアサイン行も削除される
    （以前は残っていたため、体制に無い役割のアサインだけが残り整合が取れなくなっていた）。
    削除しない別役割のアサインは残る。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="D")
    r_lead = sfa_db.add_delivery_role(con, delivery_id=dvid, role="リード", fte_billing=100, fte_pct=100)
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="コンサルタント", fte_billing=50, fte_pct=60)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-14",
                                   to_week="2026-11-02", fte_pct=100, fte_billing=100, role="リード")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="山端", from_week="2026-09-14",
                                   to_week="2026-11-02", fte_pct=60, fte_billing=50, role="コンサルタント")
    assert len(sfa_db.list_delivery_assignments(con, dvid)) == 2

    sfa_db.delete_delivery_role(con, r_lead)

    remaining = sfa_db.list_delivery_assignments(con, dvid)
    assert len(remaining) == 1
    assert remaining[0]["role"] == "コンサルタント"
    assert [r["role"] for r in sfa_db.list_delivery_roles(con, dvid)] == ["コンサルタント"]


def test_delete_delivery_role_clears_responsible_owner_if_referenced(con, acc_id):
    """削除される役割のアサイン行に紐づくメンバーが責任者/担当者に指定されていた場合、
    参照切れにならないようクリアする（既存の/assignment/delete同様のロジック、#168）。"""
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="D")
    r_lead = sfa_db.add_delivery_role(con, delivery_id=dvid, role="リード")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-14",
                                   to_week="2026-11-02", fte_pct=100, role="リード")
    sfa_db.update_delivery(con, dvid, responsible_owner="早瀬", handling_owner="早瀬")

    sfa_db.delete_delivery_role(con, r_lead)

    dv = sfa_db.get_delivery(con, dvid)
    assert dv.get("responsible_owner") is None
    assert dv.get("handling_owner") is None


def test_delete_delivery_role_leaves_other_owner_refs_untouched(con, acc_id):
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="D")
    r_lead = sfa_db.add_delivery_role(con, delivery_id=dvid, role="リード")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="コンサルタント")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-09-14",
                                   to_week="2026-11-02", fte_pct=100, role="リード")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="山端", from_week="2026-09-14",
                                   to_week="2026-11-02", fte_pct=60, role="コンサルタント")
    sfa_db.update_delivery(con, dvid, responsible_owner="山端")

    sfa_db.delete_delivery_role(con, r_lead)

    assert sfa_db.get_delivery(con, dvid).get("responsible_owner") == "山端"


def test_delivery_roles_ordered_by_sort_order_and_new_role_appended_last(con, acc_id):
    d = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=d, title="D")
    r1 = sfa_db.add_delivery_role(con, delivery_id=dvid, role="リード")
    r2 = sfa_db.add_delivery_role(con, delivery_id=dvid, role="コンサルタント")
    assert [r["id"] for r in sfa_db.list_delivery_roles(con, dvid)] == [r1, r2]

    sfa_db.reorder_delivery_roles(con, dvid, [r2, r1])
    assert [r["id"] for r in sfa_db.list_delivery_roles(con, dvid)] == [r2, r1]

    r3 = sfa_db.add_delivery_role(con, delivery_id=dvid, role="PM")
    assert [r["id"] for r in sfa_db.list_delivery_roles(con, dvid)] == [r2, r1, r3]


def test_reorder_delivery_roles_ignores_ids_from_other_delivery(con, acc_id):
    """他Deliveryの役割IDが混入しても無視する（不正操作対策）。"""
    d1 = _deal(con, acc_id, "受注", name="D1")
    d2 = _deal(con, acc_id, "受注", name="D2")
    dv1 = sfa_db.create_delivery(con, deal_id=d1, title="D1")
    dv2 = sfa_db.create_delivery(con, deal_id=d2, title="D2")
    r1 = sfa_db.add_delivery_role(con, delivery_id=dv1, role="リード")
    r2 = sfa_db.add_delivery_role(con, delivery_id=dv1, role="コンサルタント")
    other = sfa_db.add_delivery_role(con, delivery_id=dv2, role="他Deliveryの役割")

    sfa_db.reorder_delivery_roles(con, dv1, [r2, other, r1])

    assert [r["id"] for r in sfa_db.list_delivery_roles(con, dv1)] == [r2, r1]
    assert sfa_db.get_delivery(con, dv2) is not None  # 他Deliveryは無傷


def test_role_delete_route_via_http_removes_matching_assignment(monkeypatch, tmp_path):
    """/delivery/{id}/role/{rid}/delete がアサイン連動削除まで行うことをHTTP経由でも確認する。"""
    import base64
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, title="D")
    rid = sfa_db.add_delivery_role(con2, delivery_id=dvid, role="リード")
    sfa_db.add_delivery_assignment(con2, delivery_id=dvid, owner="早瀬", from_week="2026-09-14",
                                   to_week="2026-11-02", fte_pct=100, role="リード")
    con2.close()

    user, pw = "u", "p"
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", user)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        headers = {"Cookie": f"sfa_session={webapp._make_session_token()}"}
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/delivery/{dvid}/role/{rid}/delete",
            data=b"", headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    assert sfa_db.list_delivery_assignments(con3, dvid) == []
    assert sfa_db.list_delivery_roles(con3, dvid) == []


def test_assignment_update_route_saves_owner_even_without_dates(monkeypatch, tmp_path):
    """ユーザー報告(2026-09-18):「担当を入れても保存されない」「アサイン日程表も出てこない」。
    原因: /delivery/{id}/assignment/{id}/updateが開始日/終了日の両方が入力されていないと
    保存自体を丸ごとスキップしていたため、日程未定のまま担当・役割・稼働率だけ先に決めて
    おく（体制欄から役割だけ作った直後によくある状態）という使い方で、担当を入れても
    保存されないように見えていた。日付が空でも他フィールドは保存されることを確認する。"""
    import base64
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv3.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, title="D")
    # 体制から役割だけ作った直後を再現: 日付は空文字のアサイン行。
    aid2 = sfa_db.add_delivery_assignment(con2, delivery_id=dvid, owner="", from_week="",
                                          to_week="", fte_pct=0, role="PM")
    con2.close()

    user, pw = "u", "p"
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", user)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        headers = {"Cookie": f"sfa_session={webapp._make_session_token()}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode({
            "role": "PM", "member_kind": "内部", "owner_sel": "早瀬",
            "from_week": "", "to_week": "", "fte_pct": "10", "fte_billing": "", "note": "",
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/delivery/{dvid}/assignment/{aid2}/update",
            data=body, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    row = next(r for r in sfa_db.list_delivery_assignments(con3, dvid) if r["id"] == aid2)
    assert row["owner"] == "早瀬", "日付未入力を理由に担当(owner)が保存されていない"
    assert row["fte_pct"] == 10.0
    assert row["from_week"] == "" and row["to_week"] == ""  # 日付は入力していないので空のまま


def test_roles_reorder_route_via_http(monkeypatch, tmp_path):
    import base64
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv2.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, title="D")
    r1 = sfa_db.add_delivery_role(con2, delivery_id=dvid, role="リード")
    r2 = sfa_db.add_delivery_role(con2, delivery_id=dvid, role="コンサルタント")
    con2.close()

    user, pw = "u", "p"
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", user)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        headers = {"Cookie": f"sfa_session={webapp._make_session_token()}",
                  "Content-Type": "application/x-www-form-urlencoded"}
        body = f"order={r2},{r1}".encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/delivery/{dvid}/roles/reorder",
            data=body, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.getcode() == 204
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    assert [r["id"] for r in sfa_db.list_delivery_roles(con3, dvid)] == [r2, r1]


# ---- 週別売上・累計生産性（2026-09-21〜） ----

def test_delivery_weekly_productivity_dilutes_when_actual_extends_past_contract_period(con, acc_id):
    """契約期間(start_week〜end_week)は3週・総額300万→週100万。アサインは契約期間+1週(6/22)まで
    実稼働あり（Delivery期間の前後に実稼働があるケース）。6/22週は売上0のまま稼働だけ分母に乗るため、
    累計生産性は6/15週の800万/100%（月換算）から6/22週で600万/100%へ薄まる（2026-09-22〜:
    生産性は「月100%稼働あたり単価」＝売上×400÷稼働率(%週)。100%で4週=1ヶ月働けば月額報酬と
    一致する自己整合性チェックのため×4した。従来は%週のままで割っており月額報酬の1/4だった）。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_total=300, fee_mode="total", expected_expense_total=0,
                            start_week="2026-06-01", end_week="2026-06-15")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                    to_week="2026-06-22", fte_pct=50)
    grid = sfa_db.delivery_grid(con, dvid)
    assert grid["weeks"] == ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22"]

    prod = sfa_db.delivery_weekly_productivity(con, dvid, grid["weeks"])
    assert prod["fee_total"] == 300.0
    assert prod["weekly_revenue"] == {
        "2026-06-01": 100.0, "2026-06-08": 100.0, "2026-06-15": 100.0, "2026-06-22": 0.0,
    }
    assert prod["cum_revenue"]["2026-06-22"] == 300.0  # 契約期間終了後は売上が増えない
    assert prod["cum_workload"] == {
        "2026-06-01": 50.0, "2026-06-08": 100.0, "2026-06-15": 150.0, "2026-06-22": 200.0,
    }
    assert prod["productivity"]["2026-06-01"] == 800.0
    assert prod["productivity"]["2026-06-15"] == 800.0
    assert prod["productivity"]["2026-06-22"] == 600.0  # 契約後の稼働で薄まる


def test_delivery_weekly_productivity_none_when_no_workload_yet(con, acc_id):
    """稼働が始まっていない週はproductivity=None（0除算にしない）。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_total=100, fee_mode="total",
                            start_week="2026-06-01", end_week="2026-06-08")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                    to_week="2026-06-08", fte_pct=0)
    grid = sfa_db.delivery_grid(con, dvid)
    prod = sfa_db.delivery_weekly_productivity(con, dvid, grid["weeks"])
    assert all(v is None for v in prod["productivity"].values())


def test_delivery_weekly_productivity_resolves_monthly_fee_mode(con, acc_id):
    """fee_mode='monthly'（月額入力）だとfee_total列は空のことがあるため、delivery_display_fees
    と同じ換算（月額×月数）で解決する。月額100万・期間4週(=1ヶ月)→総額100万。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=100,
                            start_week="2026-06-01", end_week="2026-06-22")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                    to_week="2026-06-22", fte_pct=50)
    grid = sfa_db.delivery_grid(con, dvid)
    prod = sfa_db.delivery_weekly_productivity(con, dvid, grid["weeks"])
    assert prod["fee_total"] == 100.0


def test_delivery_form_renders_revenue_and_productivity_rows(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_total=300, fee_mode="total", expected_expense_total=0,
                            start_week="2026-06-01", end_week="2026-06-15")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                    to_week="2026-06-15", fte_pct=50)
    html = webapp.delivery_form(con, dvid)
    assert "週別限界利益" in html
    assert "累計生産性" in html
    assert "100万" in html  # 週別限界利益のセル（想定経費0%指定なので売上と同額）


def test_delivery_weekly_productivity_returns_non_cumulative_weekly_figures(con, acc_id):
    """週別売上/週別生産性・累計売上/累計生産性・累計稼働率/週別稼働率の3行表示（2026-09-22）用に、
    非累計（その週単体）のweekly_workload/weekly_productivityも返す。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_total=200, fee_mode="total", expected_expense_total=0,
                            start_week="2026-06-01", end_week="2026-06-08")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                    to_week="2026-06-08", fte_pct=50)
    grid = sfa_db.delivery_grid(con, dvid)
    prod = sfa_db.delivery_weekly_productivity(con, dvid, grid["weeks"])
    assert prod["weekly_workload"] == {"2026-06-01": 50.0, "2026-06-08": 50.0}
    # 週別生産性＝週別限界利益(想定経費0%指定なので売上と同額)100万×400÷週別稼働率50%(%週)
    # = 800万/100%（月100%稼働換算・非累計）。
    assert prod["weekly_productivity"] == {"2026-06-01": 800.0, "2026-06-08": 800.0}


def test_delivery_form_renders_three_row_layout(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_total=200, fee_mode="total",
                            start_week="2026-06-01", end_week="2026-06-08")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-01",
                                    to_week="2026-06-08", fte_pct=50)
    html = webapp.delivery_form(con, dvid)
    assert "週別限界利益/累計限界利益" in html
    assert "累計生産性/累計稼働率" in html
    assert "週別生産性/週別稼働率" in html


def test_delivery_form_renders_excluded_period_calendar_widget(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    html = webapp.delivery_form(con, dvid)
    assert 'id="dvExclCal"' in html
    assert "dvExclCalRender" in html


# ---- 対象外期間（盆休み等・#189） ----

def _excl_json(*pairs):
    return json.dumps([{"from": f, "to": t} for f, t in pairs])


def test_delivery_period_weight_full_interior_week_ignores_incidental_holiday():
    """契約期間に完全に含まれ、対象外期間とも重ならない週は、たまたま祝日（例: 2026-09-21敬老の日）
    があっても常に1.0のまま（通常週の重みが祝日の有無でぶれないようにする設計）。"""
    w = sfa_db._delivery_period_weight("2026-09-21", "2026-06-29", "2026-12-31", [])
    assert w == 1.0


def test_delivery_period_weight_boundary_week_excludes_holidays_from_business_days():
    """境界週（開始日が週の途中）は、対象外期間が無くても営業日ベースで按分される。
    2026-09-22(火)開始の週は、週内に祝日(9/21成人の日は範囲外、9/23秋分の日)があるため
    実働可能な営業日は火・木・金の3日=0.6週分になる。"""
    w = sfa_db._delivery_period_weight("2026-09-21", "2026-09-22", "2026-12-31", [])
    assert w == 0.6


def test_delivery_effective_weeks_matches_60_business_days_example():
    """ユーザー報告(2026-09-22)の実例: 6/29(月)開始・9/30(水)終了の暦14週のうち、
    実働は6/29-30(2日)・盆休み明けまで・9/28-30(3日)で、契約上の実質合意は60営業日=12週。
    対象外期間として7/1〜7/3（境界週の残り3日）と8/10〜8/16（盆休み1週間）を登録すると、
    有効週数の合計がちょうど12.0になる（0.4+11×1.0+0+0.6=12.0）。"""
    excl = [(date(2026, 7, 1), date(2026, 7, 3)), (date(2026, 8, 10), date(2026, 8, 16))]
    eff = sfa_db._delivery_effective_weeks("2026-06-29", "2026-09-30", excl)
    assert eff == 12.0


def test_delivery_month_count_uses_effective_weeks_when_excluded_periods_given():
    excl = [(date(2026, 7, 1), date(2026, 7, 3)), (date(2026, 8, 10), date(2026, 8, 16))]
    months = sfa_db.delivery_month_count("2026-06-29", "2026-09-30", excl)
    assert months == 3.0  # 12.0有効週 / 4 = 3.0ヶ月


def test_delivery_month_count_unaffected_when_no_excluded_periods():
    """対象外期間が無ければ、境界週があっても従来通りの単純な暦週数のまま（後方互換）。"""
    months = sfa_db.delivery_month_count("2026-06-29", "2026-09-30", None)
    assert months == 3.5  # 14暦週 / 4 = 3.5ヶ月（従来ロジックのまま）


def test_delivery_display_fees_resolves_600_not_700_with_excluded_periods(con, acc_id):
    """ユーザー報告(2026-09-22): 14暦週(6/29~9/30)のうち盆休み等で実質2週ぶんが対象外なのに、
    従来は14週まるごとで月数換算されるため月額200万が総額700万にされてしまっていた。
    対象外期間を登録すると、有効週数=12.0/4=3.0ヶ月で総額600万に正しく換算される。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    excl = _excl_json(("2026-07-01", "2026-07-03"), ("2026-08-10", "2026-08-16"))
    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=200,
                            start_week="2026-06-29", end_week="2026-09-30", excluded_periods=excl)
    dv = sfa_db.get_delivery(con, dvid)
    assert sfa_db.delivery_display_fees(dv) == (200.0, 600.0)


def test_delivery_weekly_productivity_prorates_revenue_and_workload_by_business_day_weight(con, acc_id):
    """継続的に毎週同じ稼働(40%)が続くケースでも、稼働累計が暦14週ぶん(560)には積み上がらず、
    有効週数12週ぶん(480)に正しく収まる（ユーザー報告2026-09-22の核心要件）。
    週別売上の合計は必ず案件総額と一致する。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    excl = _excl_json(("2026-07-01", "2026-07-03"), ("2026-08-10", "2026-08-16"))
    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=200,
                            start_week="2026-06-29", end_week="2026-09-30", excluded_periods=excl)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-29",
                                    to_week="2026-09-30", fte_pct=40)
    grid = sfa_db.delivery_grid(con, dvid)
    prod = sfa_db.delivery_weekly_productivity(con, dvid, grid["weeks"])
    assert prod["fee_total"] == 600.0
    assert len(grid["weeks"]) == 14
    # 境界週(6/29)は0.4、盆休み週(8/10)は0.0、境界週(9/28)は0.6、他は1.0で按分。
    assert prod["weekly_revenue"]["2026-06-29"] == 20.0
    assert prod["weekly_revenue"]["2026-08-10"] == 0.0
    assert prod["weekly_revenue"]["2026-09-28"] == 30.0
    assert prod["weekly_revenue"]["2026-07-06"] == 50.0
    assert sum(prod["weekly_revenue"].values()) == 600.0  # 分割方法によらず合計は必ず総額に一致
    final_workload = list(prod["cum_workload"].values())[-1]
    assert final_workload == 480.0  # 40%×12有効週（14週×40%=560にはならない）


def test_delivery_weekly_productivity_flat_split_unchanged_without_excluded_periods(con, acc_id):
    """対象外期間が無いDeliveryは、従来通りのフラットな均等配分のまま変わらない（後方互換）。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, fee_total=300, fee_mode="total",
                            start_week="2026-06-01", end_week="2026-06-15")
    grid = sfa_db.delivery_grid(con, dvid)
    # アサイン無しでも契約期間の週は生成されないため、直接weeksを渡して按分だけ検証する。
    prod = sfa_db.delivery_weekly_productivity(con, dvid, ["2026-06-01", "2026-06-08", "2026-06-15"])
    assert prod["weekly_revenue"] == {"2026-06-01": 100.0, "2026-06-08": 100.0, "2026-06-15": 100.0}


def test_delivery_form_marks_excluded_period_header(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    excl = _excl_json(("2026-07-01", "2026-07-03"))
    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=200,
                            start_week="2026-06-29", end_week="2026-07-13", excluded_periods=excl)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-29",
                                    to_week="2026-07-13", fte_pct=40)
    html = webapp.delivery_form(con, dvid)
    assert "対象外期間" in html
    assert "2026-07-01" in html  # hidden inputの初期値（HTMLエスケープされたJSON配列内）
    assert "(0.4)" in html  # 6/29週の有効週数マーク（4/5=0.4）


def test_compute_delivery_load_deliveries_meta_includes_excluded_periods(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    excl = _excl_json(("2026-07-01", "2026-07-03"))
    sfa_db.update_delivery(con, dvid, fee_mode="monthly", fee_monthly=200,
                            start_week="2026-06-29", end_week="2026-07-13", excluded_periods=excl)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", from_week="2026-06-29",
                                    to_week="2026-07-13", fte_pct=40)
    load = sfa_db.compute_delivery_load(con, start_week="2026-06-29", n_weeks=4)
    meta = load["deliveries_meta"][dvid]
    assert meta["excluded_periods"] == [{"from": "2026-07-01", "to": "2026-07-03"}]


# ---- 役割マスタ・選択制（2026-09-22） ----

def test_delivery_roles_master_has_default_seven_roles(con):
    assert sfa_db.get_master_list(con, "delivery_roles") == [
        "プロジェクトマネジャー", "リードコンサルタント", "ジュニアコンサルタント",
        "リードエンジニア", "エンジニア", "内部アドバイザー", "外部アドバイザー",
    ]


def test_delivery_roles_master_dynamically_reflected(con, acc_id):
    """マスタ設定(delivery_roles)を変更すると、体制・アサインの役割<select>の選択肢に動的に反映される。"""
    sfa_db.set_master_list(con, "delivery_roles", ["カスタム役割A", "カスタム役割B"])
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    html = webapp.delivery_form(con, dvid)
    assert "カスタム役割A" in html
    assert "カスタム役割B" in html
    assert "プロジェクトマネジャー" not in html  # デフォルトは上書きされ、もう選択肢に出ない


def test_delivery_form_role_select_marks_master_role_selected(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="リードコンサルタント")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", role="リードコンサルタント",
                                    from_week="2026-06-01", to_week="2026-06-08", fte_pct=50)
    html = webapp.delivery_form(con, dvid)
    assert '<option value="リードコンサルタント" selected>リードコンサルタント</option>' in html


def test_delivery_form_role_select_preserves_legacy_free_text_value(con, acc_id):
    """マスタ導入前の自由記述時代の役割値（マスタに存在しない）は、選択済みの追加選択肢として
    残る（黙って空欄化/別の値に変わらない。張り替えは利用者が明示的に選び直す）。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="謎の旧役割")
    html = webapp.delivery_form(con, dvid)
    assert '<option value="謎の旧役割" selected>謎の旧役割（旧値・要見直し）</option>' in html


def test_delivery_role_add_form_lists_all_master_roles(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    html = webapp.delivery_form(con, dvid)
    for role in sfa_db.DELIVERY_ROLES:
        assert f'<option value="{role}">{role}</option>' in html


def test_delivery_role_name_taken_detects_duplicate_within_same_delivery(con, acc_id):
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="ジュニアコンサルタント")
    assert sfa_db.delivery_role_name_taken(con, dvid, "ジュニアコンサルタント") is True
    assert sfa_db.delivery_role_name_taken(con, dvid, "エンジニア") is False


def test_delivery_role_name_taken_excludes_self_on_update(con, acc_id):
    """自分自身の行を編集する場合（役割名を変えずに他フィールドだけ保存等）は重複扱いしない。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    rid = sfa_db.add_delivery_role(con, delivery_id=dvid, role="ジュニアコンサルタント")
    assert sfa_db.delivery_role_name_taken(con, dvid, "ジュニアコンサルタント", exclude_role_id=rid) is False


def test_delivery_role_name_taken_scoped_per_delivery(con, acc_id):
    """役割名の一意性は同一Delivery内のみでのチェックで、別Deliveryの同名役割とは衝突しない。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid1 = sfa_db.create_delivery(con, deal_id=did, title="X")
    dvid2 = sfa_db.create_delivery(con, deal_id=did, title="Y")
    sfa_db.add_delivery_role(con, delivery_id=dvid1, role="ジュニアコンサルタント")
    assert sfa_db.delivery_role_name_taken(con, dvid2, "ジュニアコンサルタント") is False


def test_resolve_delivery_role_name_auto_numbers_duplicates_instead_of_blocking(con, acc_id):
    """ユーザー要望2026-09-24: 複数ジュニアコンサルタント等、同じ役割を複数人に個別の目標稼働率
    付きで割り当てたいケースに対応するため、重複追加はブロックせず自動採番する。1件目は無番号の
    まま、2件目を追加した時点で1件目が「役割1」へ自動リネームされ、今回は「役割2」になる。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")

    r1 = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント")
    assert r1 == "ジュニアコンサルタント"  # 1件目は無番号のまま
    sfa_db.add_delivery_role(con, delivery_id=dvid, role=r1)

    r2 = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント")
    assert r2 == "ジュニアコンサルタント2"
    sfa_db.add_delivery_role(con, delivery_id=dvid, role=r2)
    assert sorted(r["role"] for r in sfa_db.list_delivery_roles(con, dvid)) == [
        "ジュニアコンサルタント1", "ジュニアコンサルタント2"]  # 1件目は自動でリネームされた

    r3 = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント")
    assert r3 == "ジュニアコンサルタント3"

    # 他の役割は無関係に無番号のまま
    assert sfa_db.resolve_delivery_role_name_for_save(con, dvid, "PM") == "PM"


def test_resolve_delivery_role_name_renames_linked_assignments_too(con, acc_id):
    """役割↔アサインは役割名の文字列一致で対応付ける設計のため、自動採番で既存役割行を
    リネームする際は、紐づくアサイン行のroleも一緒に追従させないとリンクが切れる。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="ジュニアコンサルタント")
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, owner="早瀬", role="ジュニアコンサルタント",
                                   from_week="2026-01-05", to_week="2026-01-11", fte_pct=100)

    sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント")  # 2件目追加相当

    assert [r["role"] for r in sfa_db.list_delivery_roles(con, dvid)] == ["ジュニアコンサルタント1"]
    assert [a["role"] for a in sfa_db.list_delivery_assignments(con, dvid)] == ["ジュニアコンサルタント1"]


def test_resolve_delivery_role_name_does_not_reuse_gap_after_delete(con, acc_id):
    """欠番（例: 「役割1」を削除）があっても番号を詰めない。過去の週次レポート等が文字列で
    役割を参照している可能性があり、番号の使い回しは事故のもと。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    for _ in range(3):
        role = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント")
        sfa_db.add_delivery_role(con, delivery_id=dvid, role=role)
    rid1 = next(r["id"] for r in sfa_db.list_delivery_roles(con, dvid) if r["role"] == "ジュニアコンサルタント1")
    sfa_db.delete_delivery_role(con, rid1)

    r4 = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント")
    assert r4 == "ジュニアコンサルタント4"


def test_resolve_delivery_role_name_bumps_on_explicit_numbered_collision(con, acc_id):
    """役割リネーム欄で既存の連番役割そのものを選び直した場合（例: PMを「ジュニアコンサルタント2」
    にリネーム）も、文字通り重複するならブロックせず次の番号へずらす。自分自身への無変更保存
    （リネームなし）では採番し直さない。"""
    did = _deal(con, acc_id, "受注", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="ジュニアコンサルタント1")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="ジュニアコンサルタント2")
    rpm = sfa_db.add_delivery_role(con, delivery_id=dvid, role="PM")

    resolved = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント2", exclude_role_id=rpm)
    assert resolved == "ジュニアコンサルタント3"

    r1 = next(r["id"] for r in sfa_db.list_delivery_roles(con, dvid) if r["role"] == "ジュニアコンサルタント1")
    noop = sfa_db.resolve_delivery_role_name_for_save(con, dvid, "ジュニアコンサルタント1", exclude_role_id=r1)
    assert noop == "ジュニアコンサルタント1"


def test_delivery_role_add_route_auto_numbers_instead_of_blocking(monkeypatch, tmp_path):
    """/delivery/{id}/role/add へ同じ役割名を2回POSTしても、以前のような409/flashブロックではなく
    自動採番で2件とも保存されることをHTTPルート経由で確認する（画面のフォームが実際に送る形）。"""
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv_role.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, title="D")
    con2.close()

    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", "u")
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def _post_role_add():
        headers = {"Cookie": f"sfa_session={webapp._make_session_token()}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode({"role": "ジュニアコンサルタント", "fte_billing": "100", "fte_pct": "100"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/delivery/{dvid}/role/add",
            data=body, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)

    try:
        _post_role_add()
        _post_role_add()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    roles = sorted(r["role"] for r in sfa_db.list_delivery_roles(con3, dvid))
    assignments = sorted(a["role"] for a in sfa_db.list_delivery_assignments(con3, dvid))
    con3.close()
    assert roles == ["ジュニアコンサルタント1", "ジュニアコンサルタント2"], "ブロックされず自動採番で2件とも保存されるべき"
    assert assignments == ["ジュニアコンサルタント1", "ジュニアコンサルタント2"], \
        "役割追加時に自動生成されるアサイン行のroleも採番後の名前と一致するべき"


def test_assign_planning_plan_crud_round_trips_nested_plan_json(con):
    """アサインプランニング（2026-09-25）: マーケ診断の戦略マップと同じ、丸ごとJSONで保存/復元
    できる仕組み。ネストしたシナリオ構造が壊れず往復することを確認する。"""
    plan = {
        "stageScope": 1,
        "deliveryOrder": [3, 1, 2],
        "included": {"1": True, "2": False, "3": True},
        "scenarios": [
            {"no": 1, "name": "シナリオ1", "data": {"1": {"roles": [{"role": "PM", "fte_billing": 50,
                                                                     "fte_pct": 50, "sort_order": 0}],
                                                            "assignments": [{"role": "PM", "member_kind": "内部",
                                                                              "owner": "早瀬", "from_week": "2026-10-05",
                                                                              "to_week": "2026-12-28",
                                                                              "fte_billing": 50, "fte_pct": 50,
                                                                              "note": ""}]}}},
            {"no": 2, "name": "壮関を失注した場合", "data": {}},
        ],
    }
    saved = sfa_db.create_assign_planning_plan(con, name="10月想定", plan=plan)
    assert saved["name"] == "10月想定"
    assert saved["plan"] == plan
    assert saved["id"] > 0

    listed = sfa_db.list_assign_planning_plans(con)
    assert len(listed) == 1
    assert listed[0]["plan"]["scenarios"][1]["name"] == "壮関を失注した場合"
    assert listed[0]["plan"]["deliveryOrder"] == [3, 1, 2]

    sfa_db.delete_assign_planning_plan(con, saved["id"])
    assert sfa_db.list_assign_planning_plans(con) == []


def test_assign_planning_page_route_lists_deliveries_by_stage_scope(monkeypatch, tmp_path):
    """/assign-planningが実際のHTTPルート経由で200を返し、確定/クロージング/提案中の各Delivery
    （期間設定済みのもの）を埋め込みJSONに含むことを確認する。見込み(提案前)・無効(終了)や
    期間未設定のDeliveryは対象外。"""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv_ap.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")

    did1 = sfa_db.upsert_deal(con2, account_id=aid, deal_name="確定", stage="受注")
    dvid1 = sfa_db.create_delivery(con2, deal_id=did1, title="確定D")
    sfa_db.update_delivery(con2, dvid1, start_week="2026-10-05", end_week="2026-12-28")

    did2 = sfa_db.upsert_deal(con2, account_id=aid, deal_name="提案前差し戻し", stage="要件詰め")
    dvid2 = sfa_db.create_delivery(con2, deal_id=did2, title="提案前D")
    sfa_db.update_delivery(con2, dvid2, start_week="2026-10-05", end_week="2026-12-28")

    did3 = sfa_db.upsert_deal(con2, account_id=aid, deal_name="期間未設定", stage="受注")
    sfa_db.create_delivery(con2, deal_id=did3, title="期間未設定D")  # start_week/end_week無し

    con2.close()

    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", "u")
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/assign-planning",
            headers={"Cookie": f"sfa_session={webapp._make_session_token()}"})
        body = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    m = re.search(r"var AP_DELIVERIES = (\[.*?\]);\n", body)
    assert m, "埋め込みJSONが見つからない"
    deliveries = json.loads(m.group(1))
    titles = {d["title"] for d in deliveries}
    assert "確定D" in titles
    assert "提案前D" not in titles, "見込み(提案前)は対象外のはず"
    assert "期間未設定D" not in titles, "開始/終了週が無いDeliveryは対象外のはず"


def test_assign_planning_page_sorts_assignments_by_role_order(con, acc_id):
    """アサインプランニング（2026-09-26改修）: アサインの並び順は体制の役割順に合わせる
    （delivery_form()の既存ソート方式と同じ）。DB上のアサイン挿入順が体制と逆でも、
    埋め込みJSONでは体制の役割順に並び替わっていること。"""
    did = _deal(con, acc_id, "クロージング", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, start_week="2026-10-05", end_week="2026-12-28")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="プロジェクトマネジャー")
    sfa_db.add_delivery_role(con, delivery_id=dvid, role="リードコンサルタント")
    # わざと体制と逆順でアサインを追加する
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, role="リードコンサルタント", owner="山端",
                                   from_week="2026-10-05", to_week="2026-12-28", fte_pct=50)
    sfa_db.add_delivery_assignment(con, delivery_id=dvid, role="プロジェクトマネジャー", owner="早瀬",
                                   from_week="2026-10-05", to_week="2026-12-28", fte_pct=10)

    html = webapp.assign_planning_page(con)
    m = re.search(r"var AP_DELIVERIES = (\[.*?\]);\n", html)
    deliveries = json.loads(m.group(1))
    dv_json = next(d for d in deliveries if d["id"] == dvid)
    assert [a["role"] for a in dv_json["assignments"]] == ["プロジェクトマネジャー", "リードコンサルタント"], \
        "アサインの並び順が体制の役割順になっていない"


def test_assign_planning_plan_routes_create_and_delete_do_not_touch_real_assignments(monkeypatch, tmp_path):
    """/assign-planning-plan/create・/delete が実DBのdelivery_roles/delivery_assignmentsに
    一切書き込まない（シミュレーション専用）ことをHTTPルート経由で確認する。"""
    import threading
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    db_path = str(tmp_path / "srv_ap2.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con2, name="テスト社")
    did = sfa_db.upsert_deal(con2, account_id=aid, deal_name="D", stage="受注")
    dvid = sfa_db.create_delivery(con2, deal_id=did, title="D")
    sfa_db.update_delivery(con2, dvid, start_week="2026-10-05", end_week="2026-12-28")
    sfa_db.add_delivery_role(con2, delivery_id=dvid, role="PM", fte_billing=50, fte_pct=50)
    con2.close()

    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", "u")
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", "p")
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        headers = {"Cookie": f"sfa_session={webapp._make_session_token()}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        plan = {"stageScope": 1, "deliveryOrder": [dvid], "included": {str(dvid): True},
                "scenarios": [{"no": 1, "name": "シナリオ1",
                               "data": {str(dvid): {"roles": [{"role": "PM(シミュレーション)",
                                                                "fte_billing": 999, "fte_pct": 999,
                                                                "sort_order": 0}],
                                                     "assignments": []}}}]}
        body = urllib.parse.urlencode({
            "name": "テストプラン", "plan_json": json.dumps(plan, ensure_ascii=False),
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/assign-planning-plan/create",
            data=body, headers=headers, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        plan_id = resp["id"]
        assert resp["plan"]["scenarios"][0]["data"][str(dvid)]["roles"][0]["role"] == "PM(シミュレーション)"

        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/assign-planning-plan/{plan_id}/delete",
            data=b"", headers=headers, method="POST")
        urllib.request.urlopen(req2, timeout=10)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    real_roles = sfa_db.list_delivery_roles(con3, dvid)
    assert len(real_roles) == 1
    assert real_roles[0]["role"] == "PM", "実際のdelivery_rolesは書き換わっていないはず"
    assert sfa_db.list_assign_planning_plans(con3) == [], "delete後はプランテーブルも空のはず"


def test_required_field_highlight_respects_stage_gate_client_side_too(con, acc_id):
    """ユーザー報告(2026-09-25):「優先入力が機能していない」。原因はサーバ側の初回描画では
    #134と同じ段階ゲート（クロージング/受注になるまでは報酬形態・報酬額/経費請求有無は対象外）を
    正しく適用していたのに、クライアント側のdvFeeRecalc()/dvExpenseBillingChanged()が段階を見ずに
    「トグルON＋未入力」だけでハイライトしてしまい、見込み段階のDeliveryでもページ読み込み直後の
    再計算で誤って黄色くなっていた。DV_STAGE_GATE_OKフラグでJS側も段階を判定するよう修正。"""
    did = _deal(con, acc_id, "提案", status="open")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    # fee_monthly/fee_total/expense_billingは未入力のまま（=対象になり得る状態）
    html_early = webapp.delivery_form(con, dvid)
    assert "var DV_STAGE_GATE_OK = false;" in html_early
    assert 'id="dvFeeMonthly" name="fee_monthly"\n                       style="width:110px"' in html_early, \
        "見込み段階では#fef3c7がinlineで付いてはいけない"

    sfa_db.upsert_deal(con, account_id=acc_id, deal_name="Y", stage="受注")
    did2 = _deal(con, acc_id, "受注", status="open")
    dvid2 = sfa_db.create_delivery(con, deal_id=did2, title="Y")
    html_closing = webapp.delivery_form(con, dvid2)
    assert "var DV_STAGE_GATE_OK = true;" in html_closing
    assert "background:#fef3c7" in html_closing.split('id="dvFeeMonthly"')[1][:200], \
        "クロージング/受注段階で未入力なら#fef3c7がinlineで付くはず"


def test_assign_planning_checklist_checkbox_css_prevents_global_width_override(con):
    """ユーザー報告(2026-09-26):「対象Delivery選択が壊れている」。原因はページ全体の共通CSS
    `input,select,textarea{width:100%}`がモーダル内のチェックボックスにも適用され、
    flexレイアウト内でチェックボックスの計算幅が900px超まで肥大化し、隣の.ap-titleが
    幅0になって案件名が見えなくなっていた。`.ap-checklist-row input[type=checkbox]`に
    明示的なwidth:auto上書きが入っていることを確認する回帰テスト。"""
    html = webapp.assign_planning_page(con)
    idx = html.index(".ap-checklist-row .ap-drag")
    assert "input[type=checkbox]{width:auto" in html[idx:idx + 300]


def test_assign_planning_default_delivery_order_pushes_already_past_items_last(con, acc_id):
    """ユーザー指摘(2026-09-26)。初期表示順のロジック(apCompareDeliveries)がJSに存在すること
    を確認する（実際の並び順内容は下のtest_assign_planning_default_order_sorts_by_biz_end_confでも
    検証する）。"""
    html = webapp.assign_planning_page(con)
    assert "function apCompareDeliveries(a, b)" in html
    assert "AP_DELIVERIES.slice().sort(apCompareDeliveries)" in html


def test_assign_planning_default_order_sorts_by_biz_end_confidence(con, acc_id):
    """ユーザー要望(2026-09-27): 対象Delivery選択の既定並び順を「事業種別L1L2順×終了日の早い順×
    確度順」に変更。事業種別マスタのL1→L2登録順・終了日昇順・確度(確定<クロージング<提案中)の
    3段階で並ぶことをJSに埋め込まれたデータから確認する。"""
    sfa_db.set_business_type_tree(con, {"AI導入": ["AI開発(軽)", "AI開発(重)"], "コスト削減": ["コスト診断(無償)"]})

    def _delivery(title, l1, l2, end_week, stage="受注"):
        did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name=title, stage=stage, status="open",
                                  business_type_l1=l1, business_type_l2=l2)
        dvid = sfa_db.create_delivery(con, deal_id=did, title=title)
        sfa_db.update_delivery(con, dvid, start_week="2026-10-05", end_week=end_week)
        return dvid

    # 事業種別が後(コスト削減)でも終了日が早い案件は「コスト削減」グループ扱いのまま先に来ない
    # （L1L2が最優先の並び替えキーであること）ことを確認するため、意図的に登録順をバラす。
    id_cost = _delivery("Z", "コスト削減", "コスト診断(無償)", "2026-10-26")
    id_ai_late = _delivery("Y", "AI導入", "AI開発(重)", "2026-12-28")
    id_ai_early = _delivery("X", "AI導入", "AI開発(軽)", "2026-11-30")

    html = webapp.assign_planning_page(con)
    m = re.search(r"var AP_DELIVERIES = (\[.*?\]);\n", html)
    deliveries = json.loads(m.group(1))
    ordered_ids = [d["id"] for d in sorted(
        deliveries,
        key=lambda d: (0 if d["bizL1"] == "AI導入" else 1,
                        ["AI開発(軽)", "AI開発(重)"].index(d["bizL2"]) if d["bizL1"] == "AI導入" else 0,
                        d["endWeek"]))]
    assert ordered_ids == [id_ai_early, id_ai_late, id_cost], \
        "AI導入グループ(L1が先)がまとまり、その中でL2順→終了日順になっているはず"


def test_assign_planning_default_order_pushes_completed_items_to_bottom_first(con, acc_id):
    """ユーザー要望(2026-09-27):「当週時点で完了している案件は強制的に順番を一番下に」。
    事業種別L1L2×終了日×確度の3段階ソートより前に、apDefaultIncluded()（=当週時点で完了して
    いないか）で完了済み/未完了をまず二分していることを確認する（キーの優先順位の回帰テスト）。"""
    html = webapp.assign_planning_page(con)
    fn = html.split("function apCompareDeliveries(a, b){")[1].split("\n}")[0]
    assert "apDefaultIncluded(a)" in fn and "apDefaultIncluded(b)" in fn
    assert fn.index("apDefaultIncluded(a)") < fn.index("apBizRank(a)"), \
        "完了済み判定が事業種別判定より先（最優先キー）になっていないといけない"
