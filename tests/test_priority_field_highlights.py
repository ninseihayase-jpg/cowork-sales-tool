"""優先入力項目ハイライト機能の拡張（2026-09-26）: 商談/Delivery(全項目)/社内PJ/アカウントの
5画面ぶんのテスト。Delivery v1(#134由来3項目)の既存テストはtest_delivery.pyにある。

検証観点: 既定(全項目ON)で未入力ならハイライト / 設定でOFFにすると消える / 条件付き項目
(成果報酬有無・経費請求有無・外注先の有無)は条件を満たす時だけ / 段階ゲート(クロージング/受注)の
有無 / 集合レベル項目(次回MS・活動履歴・体制・アサイン・検収額・責任者)はセクション単位。
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
    d = tempfile.mkdtemp(prefix="sfa_hl_test_")
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


# ── 商談 ────────────────────────────────────────────────────────────

def test_deal_form_highlights_blank_fields_by_default(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D")
    deal = sfa_db.get_deal(con, did)
    html = webapp.deal_form(con, deal)
    for field_id in ("dealStage", "dealOwner", "dealSubOwner", "biz_l1", "biz_l2",
                      "dealLeadPattern", "dealValueLumpsum"):
        seg = html.split(f'id="{field_id}"')[1][:200]
        assert "background:#fef3c7" in seg, f"{field_id} should be highlighted when blank"
    assert "background:#fef3c7" in html.split('id="msRows"')[0][-400:], \
        "次回マイルストンのエディタ全体が黄色くハイライトされるはず"
    assert 'id="activity" style="border:1px solid #fde68a;background:#fffbeb"' in html


def test_deal_form_respects_disabled_candidates(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D")
    sfa_db.set_master_list(con, "required_field_highlights_deal", ["owner"])
    deal = sfa_db.get_deal(con, did)
    html = webapp.deal_form(con, deal)
    seg_owner = html.split('id="dealOwner"')[1][:200]
    assert "background:#fef3c7" in seg_owner
    seg_stage = html.split('id="dealStage"')[1][:200]
    assert "background:#fef3c7" not in seg_stage, "OFFにしたキーはハイライトされないはず"


def test_deal_form_clears_highlight_when_filled(con, acc_id):
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D", stage="要件詰め",
                              owner="早瀬", sub_owner="早瀬", business_type_l1="コンサル・AX",
                              business_type_l2="生成AI", lead_pattern="紹介", value_lumpsum=100)
    sfa_db.add_deal_milestone(con, did, date="2026-10-01", label="次回定例")
    sfa_db.add_activity(con, deal_id=did, type="電話", occurred_on="2026-09-01", body="架電")
    deal = sfa_db.get_deal(con, did)
    html = webapp.deal_form(con, deal)
    for field_id in ("dealStage", "dealOwner", "dealSubOwner", "biz_l1", "biz_l2",
                      "dealLeadPattern", "dealValueLumpsum"):
        seg = html.split(f'id="{field_id}"')[1][:200]
        assert "background:#fef3c7" not in seg, f"{field_id} should not be highlighted once filled"
    assert 'id="activity" style="border:1px solid #fde68a;background:#fffbeb"' not in html


def test_deal_form_milestone_needs_both_date_and_label(con, acc_id):
    """日付だけ・ラベルだけの行では「入った行」とみなさない（両方揃って初めて非空扱い）。"""
    did = sfa_db.upsert_deal(con, account_id=acc_id, deal_name="D")
    sfa_db.add_deal_milestone(con, did, date="2026-10-01", label=None)
    deal = sfa_db.get_deal(con, did)
    html = webapp.deal_form(con, deal)
    assert "background:#fef3c7" in html.split('id="msRows"')[0][-400:]


# ── Delivery（全項目拡張） ──────────────────────────────────────────

def _deal(con, acc_id, stage, status="open", name="D"):
    return sfa_db.upsert_deal(con, account_id=acc_id, deal_name=name, stage=stage, status=status)


def test_delivery_expected_impact_exempt_from_stage_gate(con, acc_id):
    """想定インパクトは成果報酬比率と同枠。見込み段階でも成果報酬有無=有かつ未入力ならハイライトする。"""
    did = _deal(con, acc_id, "提案")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, performance_fee="有")
    html = webapp.delivery_form(con, dvid)
    assert "var DV_STAGE_GATE_OK = false;" in html
    seg = html.split('id="dvExpectedImpact"')[1][:200]
    assert "background:#fef3c7" in seg
    # 成果報酬有無=無なら見込み段階どころか未入力でもハイライトしない
    sfa_db.update_delivery(con, dvid, performance_fee="無")
    html2 = webapp.delivery_form(con, dvid)
    seg2 = html2.split('id="dvExpectedImpact"')[1][:200]
    assert "background:#fef3c7" not in seg2


def test_delivery_cost_monthly_requires_vendor_and_stage_gate(con, acc_id):
    """外注費/月額は外注先ありのときだけ、かつ#134と同じ段階ゲート（クロージング/受注）に従う。"""
    did_open = _deal(con, acc_id, "提案")
    dv_open = sfa_db.create_delivery(con, deal_id=did_open, title="X")
    sfa_db.update_delivery(con, dv_open, cost_vendor="外注先A")
    html_open = webapp.delivery_form(con, dv_open)
    seg_open = html_open.split('id="dvCostMonthly"')[1][:200]
    assert "background:#fef3c7" not in seg_open, "見込み段階では外注費/月額はハイライトしないはず"

    did_closing = _deal(con, acc_id, "クロージング", name="Y")
    dv_closing = sfa_db.create_delivery(con, deal_id=did_closing, title="Y")
    html_no_vendor = webapp.delivery_form(con, dv_closing)
    seg_no_vendor = html_no_vendor.split('id="dvCostMonthly"')[1][:200]
    assert "background:#fef3c7" not in seg_no_vendor, "外注先が空なら対象外のはず"

    sfa_db.update_delivery(con, dv_closing, cost_vendor="外注先B")
    html_vendor = webapp.delivery_form(con, dv_closing)
    seg_vendor = html_vendor.split('id="dvCostMonthly"')[1][:200]
    assert "background:#fef3c7" in seg_vendor, "クロージング段階＋外注先ありで未入力ならハイライトするはず"


def test_delivery_expense_billing_note_requires_yes_and_stage_gate(con, acc_id):
    did = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.update_delivery(con, dvid, expense_billing="有")
    html = webapp.delivery_form(con, dvid)
    seg = html.split('id="dvExpenseBillingNote"')[1][:200]
    assert "background:#fef3c7" in seg
    sfa_db.update_delivery(con, dvid, expense_billing="不明(要確認)")
    html2 = webapp.delivery_form(con, dvid)
    seg2 = html2.split('id="dvExpenseBillingNote"')[1][:200]
    assert "background:#fef3c7" not in seg2, "経費請求有無=有 以外はハイライト対象外のはず"


def test_delivery_billing_method_and_recipient_highlight_with_stage_gate(con, acc_id):
    did = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    html = webapp.delivery_form(con, dvid)
    seg_method = html.split('id="dvBillingMethod"')[1][:300]
    assert "background:#fef3c7" in seg_method
    seg_recipient = html.split('id="dvBillingRecipient"')[1][:300]
    assert "background:#fef3c7" in seg_recipient
    sfa_db.update_delivery(con, dvid, billing_method="請求書", billing_recipient="経理部")
    html2 = webapp.delivery_form(con, dvid)
    assert "background:#fef3c7" not in html2.split('id="dvBillingMethod"')[1][:300]
    assert "background:#fef3c7" not in html2.split('id="dvBillingRecipient"')[1][:300]


def test_delivery_collection_level_sections_highlight_when_empty(con, acc_id):
    """責任者/体制/アサイン/検収額はセクション単位のハイライト（クロージング以降のみ）。"""
    did = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    html = webapp.delivery_form(con, dvid)
    assert "border:1px solid #fde68a;background:#fffbeb" in html.split("責任者・担当者</h3>")[0][-200:]
    assert "border:1px solid #fde68a;background:#fffbeb" in html.split("体制（役割別の目標稼働率）</h3>")[0][-200:]
    assert "border:1px solid #fde68a;background:#fffbeb" in html.split("アサイン（役割")[0][-200:]
    assert "border:1px solid #fde68a;background:#fffbeb;border-radius:8px" in html.split("月別入金計画")[0][-260:]

    sfa_db.add_delivery_role(con, delivery_id=dvid, role="PM", fte_billing=100, fte_pct=100)
    sfa_db.update_delivery(con, dvid, responsible_owner="早瀬")
    sfa_db.set_delivery_receipt(con, dvid, "2026-10", 100)
    blocks = sfa_db.list_delivery_assignments(con, dvid)
    if blocks:
        sfa_db.update_delivery_assignment(con, blocks[0]["id"], owner="早瀬",
                                           from_week=blocks[0].get("from_week") or "2026-10-05",
                                           to_week=blocks[0].get("to_week") or "2026-10-05",
                                           fte_pct=100)
    html2 = webapp.delivery_form(con, dvid)
    assert "border:1px solid #fde68a;background:#fffbeb" not in html2.split("責任者・担当者</h3>")[0][-200:]
    assert "border:1px solid #fde68a;background:#fffbeb" not in html2.split("体制（役割別の目標稼働率）</h3>")[0][-200:]
    assert "border:1px solid #fde68a;background:#fffbeb;border-radius:8px" not in html2.split("月別入金計画")[0][-260:]


def test_delivery_disabling_candidate_removes_new_field_highlights(con, acc_id):
    did = _deal(con, acc_id, "受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title="X")
    sfa_db.set_master_list(con, "required_field_highlights_delivery",
                            ["performance_fee_ratio", "fee_amount", "expense_billing"])  # v1のみON
    html = webapp.delivery_form(con, dvid)
    assert "background:#fef3c7" not in html.split('id="dvBillingMethod"')[1][:300]
    assert "background:#fef3c7" not in html.split('id="dvBillingRecipient"')[1][:300]
    assert "border:1px solid #fde68a;background:#fffbeb" not in html.split("責任者・担当者</h3>")[0][-200:]


# ── 社内PJ（3レンダリング箇所） ─────────────────────────────────────

def test_deal_issue_form_new_highlights_responsible_and_due_date(con, acc_id):
    did = _deal(con, acc_id, "要件詰め")
    html = webapp.deal_issue_form(con, issue=None, deal_id=did)
    seg_resp = html.split('id="diResponsible"')[1][:300]
    assert "background:#fef3c7" in seg_resp
    seg_due = html.split('id="diDueDate"')[1][:300]
    assert "background:#fef3c7" not in seg_due, "新規作成時は+7日の既定値が入るため未入力扱いにしないはず"


def test_deal_issue_form_edit_highlights_when_blank(con, acc_id):
    did = _deal(con, acc_id, "要件詰め")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点A", status="議論中")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.deal_issue_form(con, issue=issue, deal_id=did)
    assert "background:#fef3c7" in html.split('id="diResponsible"')[1][:300]
    assert "background:#fef3c7" in html.split('id="diDueDate"')[1][:300]

    sfa_db.upsert_deal_issue(con, id=iid, deal_id=did, issue="論点A", status="議論中",
                              responsible="早瀬", due_date="2026-10-10")
    issue2 = sfa_db.get_deal_issue(con, iid)
    html2 = webapp.deal_issue_form(con, issue=issue2, deal_id=did)
    assert "background:#fef3c7" not in html2.split('id="diResponsible"')[1][:300]
    assert "background:#fef3c7" not in html2.split('id="diDueDate"')[1][:300]


def test_deal_issue_detail_page_highlights_when_blank(con, acc_id):
    did = _deal(con, acc_id, "要件詰め")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点A", status="議論中")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.deal_issue_detail_page(con, issue)
    assert "background:#fef3c7" in html.split('id="diResponsible"')[1][:300]
    assert "background:#fef3c7" in html.split('id="diDueDate"')[1][:300]


def test_deal_issue_inline_table_highlights_in_deal_form(con, acc_id):
    """deal_form内の社内PJ一覧テーブル（行ごとのインライン編集セル）。"""
    did = _deal(con, acc_id, "要件詰め")
    sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点A", status="議論中")
    deal = sfa_db.get_deal(con, did)
    html = webapp.deal_form(con, deal)
    assert "background:#fef3c7" in html, "社内PJテーブルの未入力セルがハイライトされるはず"


def test_deal_issue_disabling_due_date_leaves_responsible_alone(con, acc_id):
    did = _deal(con, acc_id, "要件詰め")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点A", status="議論中")
    sfa_db.set_master_list(con, "required_field_highlights_deal_issues", ["responsible"])
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.deal_issue_form(con, issue=issue, deal_id=did)
    assert "background:#fef3c7" in html.split('id="diResponsible"')[1][:300]
    assert "background:#fef3c7" not in html.split('id="diDueDate"')[1][:300]


# ── アカウント ───────────────────────────────────────────────────────

def test_account_form_highlights_blank_industry_and_size(con):
    html = webapp.account_form(con, acc={})
    assert "background:#fef3c7" in html.split('id="accIndustry"')[1][:300]
    assert "background:#fef3c7" in html.split('id="accCompanySize"')[1][:300]


def test_account_form_clears_when_filled(con):
    aid = sfa_db.upsert_account(con, name="テスト社", industry="IT・通信", company_size="中堅")
    acc = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    html = webapp.account_form(con, acc=dict(acc))
    assert "background:#fef3c7" not in html.split('id="accIndustry"')[1][:300]
    assert "background:#fef3c7" not in html.split('id="accCompanySize"')[1][:300]


def test_account_form_respects_disabled_candidate(con):
    sfa_db.set_master_list(con, "required_field_highlights_accounts", ["industry"])
    html = webapp.account_form(con, acc={})
    assert "background:#fef3c7" in html.split('id="accIndustry"')[1][:300]
    assert "background:#fef3c7" not in html.split('id="accCompanySize"')[1][:300]
