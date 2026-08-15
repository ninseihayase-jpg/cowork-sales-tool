"""#29 取り込み/整形の運用改善4点のUI回帰テスト（レンダリングHTMLのマーカーで検証）。

1. インボックス割り当ての「種別→検索→リスト」UI
2. 整形確認の自由記述欄がクリック拡大(#55共通: taExpand/taShrink)
3. ヒアリング確定でメール素案をmailto起動（hearingCommitSubmit + window.open('mailto...')）
4. 「NextStepをタスク起票」チェックボックスのレイアウト（width:auto でラベル整列）
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cowork import sfa_db, webapp


def _s(x):
    return x.decode() if isinstance(x, (bytes, bytearray)) else x


def _fresh():
    d = tempfile.mkdtemp(prefix="sfa_iu_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def test_inbox_assign_has_type_search_list_ui():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
        sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")
        sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="m1",
                                    title="面談", occurred_on="2026-08-14",
                                    transcript="本文", attendees_json="[]")
        html = _s(webapp.intake_inbox_page(con))
        assert 'name="ttype"' in html            # 種別セレクト
        assert 'class="assign-q"' in html         # 検索ボックス
        assert "assignFilter" in html             # 絞り込みJS
        assert 'data-type="deal"' in html and 'data-type="issue"' in html
        assert 'data-s=' in html                  # 検索キー（小文字ラベル）
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_hearing_review_expand_mailto_and_checkbox():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        sid = sfa_db.create_hearing_session(
            con, deal_id=did, source="jamie", template_id=None, conducted_on="2026-08-14",
            transcript="t", structured={"items": [{"label": "予算", "answer": "x"}],
                                         "overview": "o", "nextsteps": ["n1"],
                                         "email_draft": "メール本文", "_ai_ok": True},
            status="structured")
        deal = {"id": did, "deal_name": "D", "account_name": "ソラスト"}
        html = _s(webapp.hearing_review_page(con, deal, sfa_db.get_hearing_session(con, sid)))
        # (3) 自由記述欄のクリック拡大
        assert "taExpand(this)" in html and "taShrink(this)" in html
        # (4) 確定でmailto起動（フォームはhearingCommitSubmit、埋め込みJSにmailto/window.open）
        assert "hearingCommitSubmit(this)" in html
        assert 'data-acct="ソラスト"' in html
        assert "window.open" in html and "mailto" in html
        # (1) チェックボックスは width:auto でラベル整列
        assert 'name="make_tasks"' in html and "width:auto" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_issue_review_prose_fields_expand():
    d, con = _fresh()
    try:
        iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")
        st = {"title": "T", "date": "2026-08-14", "overview": "o",
              "points": [{"topic": "a", "detail": "b"}], "decisions": ["d"],
              "nextsteps": ["n"], "_ai_ok": True}
        html = _s(webapp.issue_review_page(con, sfa_db.get_deal_issue(con, iid), st))
        # 全体像・決定事項・NextStep の3欄にクリック拡大が付く
        assert html.count("taExpand(this)") >= 3
        # 見出しは YYMMDD_タイトル
        assert "260814_T" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_hearing_review_is_options_aware():
    """商談取り込みの確認画面が取り込みオプション(_opts)に従い、必要な欄だけ出す（#29再設計）。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="要件詰め")
        deal = {"id": did, "deal_name": "D", "account_name": "ソラスト"}
        base = {"items": [{"label": "予算", "answer": "x"}], "overview": "o",
                "nextsteps": ["n1"], "email_draft": "メール本文", "_ai_ok": True}
        # 現状更新ON / ヒアリングOFF / メールOFF
        st1 = dict(base, _opts={"make_hearing": False, "update_state": True,
                                "make_mail": False, "act_type": "面談", "contact_name": "岩崎"})
        sid1 = sfa_db.create_hearing_session(con, deal_id=did, source="jamie", template_id=None,
                                             conducted_on="2026-08-15", transcript="x",
                                             structured=st1, status="structured")
        h1 = _s(webapp.hearing_review_page(con, deal, sfa_db.get_hearing_session(con, sid1)))
        assert 'name="act_type"' in h1 and 'name="stage"' in h1 and 'name="ms_label"' in h1 and 'name="state_note"' in h1
        assert 'name="email_draft"' not in h1        # メールOFF
        assert "ヒアリング項目" not in h1              # ヒアリングOFF
        assert "活動履歴＋現状更新" in h1
        # 全部ON
        st2 = dict(base, _opts={"make_hearing": True, "update_state": True,
                                "make_mail": True, "act_type": "面談", "contact_name": ""})
        sid2 = sfa_db.create_hearing_session(con, deal_id=did, source="jamie", template_id=None,
                                             conducted_on="2026-08-15", transcript="x",
                                             structured=st2, status="structured")
        h2 = _s(webapp.hearing_review_page(con, deal, sfa_db.get_hearing_session(con, sid2)))
        assert "ヒアリング項目" in h2 and 'name="email_draft"' in h2 and 'name="stage"' in h2
        assert "活動履歴＋ヒアリング＋現状更新＋メール" in h2
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_inbox_assign_js_is_syntactically_present():
    """割り当て絞り込みJSと確定JSが定義され、想定の関数名を含む（HTMLに埋め込まれる前提）。"""
    assert "function assignFilter" in webapp._INBOX_ASSIGN_JS
    assert "function hearingCommitSubmit" in webapp._HEARING_COMMIT_JS
    assert "mailto" in webapp._HEARING_COMMIT_JS
