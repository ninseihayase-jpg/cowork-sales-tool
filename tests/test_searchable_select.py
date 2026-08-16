"""長大なselect(数十件以上)を「入力して絞り込み」できるようにする共通コンポーネントの回帰テスト。

対象は新規商談フォーム(リード引用/アカウント選択)・Delivery手動追加・新規ヒアリング(商談/リード)
・開発案件フォーム(dpDealSelect)・論点フォーム(diDealSelect)。全て共通JS filterSelectOptions に
委譲していること、既存のdpFilterDeals/diFilterDealsも重複実装をやめて委譲していることを確認する。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cowork import sfa_db, webapp


def _s(x):
    return x.decode() if isinstance(x, (bytes, bytearray)) else x


def _fresh():
    d = tempfile.mkdtemp(prefix="sfa_ss_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def test_shared_filter_function_is_defined_once_globally():
    """共通コアfilterSelectOptionsがPAGEテンプレート(全ページ共通)に1回だけ定義されている。"""
    html = _s(webapp.render("<div></div>"))
    assert html.count("function filterSelectOptions(") == 1


def test_deal_form_account_and_lead_selects_use_shared_filter():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_lead(con, name="山田", company="テスト商事", assigned_to="早瀬", source="web")
        html = webapp.deal_form(con, None)
        assert 'id="acc_id_sel_q"' in html and "accIdFilter" in html
        assert "filterSelectOptions('acc_id_sel', 'acc_id_sel_q')" in html
        assert 'id="lead_ref_q"' in html and "leadRefFilter" in html
        assert "filterSelectOptions('lead_ref', 'lead_ref_q')" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_deliveries_page_new_deal_select_uses_shared_filter():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        html = _s(webapp.deliveries_page(con))
        assert 'id="dvNewDealFilter"' in html and 'id="dvNewDealSelect"' in html
        assert "filterSelectOptions('dvNewDealSelect', 'dvNewDealFilter')" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_hearing_new_page_target_select_uses_shared_filter():
    d, con = _fresh()
    try:
        sfa_db.save_hearing_template(con, name="標準", items=[{"label": "予算", "type": "text"}])
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        html = webapp.hearing_new_page(con)
        assert 'id="hnTargetFilter"' in html and 'id="hnTargetSelect"' in html
        assert "filterSelectOptions('hnTargetSelect', 'hnTargetFilter')" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_dev_project_form_deal_select_delegates_to_shared_filter():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        html = webapp.dev_project_form(con, None, deal_id=None)
        assert "function dpFilterDeals() { filterSelectOptions('dpDealSelect', 'dpDealFilter'); }" in html
        # 旧実装（各ページに複製していたループ）が残っていないこと
        assert "o.text.includes(q)" not in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_deal_issue_form_deal_select_delegates_to_shared_filter():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        html = webapp.deal_issue_form(con, None, deal_id=None)
        assert "function diFilterDeals() { filterSelectOptions('diDealSelect', 'diDealFilter'); }" in html
        assert "o.text.includes(q)" not in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)
