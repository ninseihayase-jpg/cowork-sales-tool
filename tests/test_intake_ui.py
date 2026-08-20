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


def test_inbox_candidate_matches_common_company_name_abbreviation():
    """実事故の回帰確認: 会議タイトル「川崎重工向け...」が、正式社名「川崎重工業株式会社」に
    対して候補として出せなかった不具合の修正。法人格接尾辞の除去＋末尾1文字の省略許容で
    「重工業→重工」のような日本語社名の慣用的な略称を候補に出せること。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="川崎重工業株式会社")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="生産管理部 太田 淳 部長", stage="要件詰め")
        sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="m2",
                                    title="(I) 川崎重工向けデモアプリ集中討議", occurred_on="2026-08-20",
                                    transcript="本文", attendees_json="[]")
        html = _s(webapp.intake_inbox_page(con))
        assert "候補（クリックで選択）" in html
        assert "川崎重工業株式会社" in html.split("候補（クリックで選択）")[1][:300]
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_inbox_candidate_uses_registered_alias_dictionary():
    """住友重工業→住重のような、機械的には導出不能な慣用ニックネームは辞書登録
    (accounts.aliases)経由でのみ候補に出せる。登録前は出ず、登録後は出ることを確認。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="住友重工業株式会社")
        sfa_db.upsert_deal(con, account_id=acc, deal_name="設備投資案件", stage="要件詰め")
        sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="m4",
                                    title="住重様との定例MTG", occurred_on="2026-08-20",
                                    transcript="本文", attendees_json="[]")

        html_before = _s(webapp.intake_inbox_page(con))
        assert "候補（クリックで選択）" not in html_before, "辞書未登録なのに候補が出ている"

        sfa_db.set_account_aliases(con, acc, "住重、住重工")
        html_after = _s(webapp.intake_inbox_page(con))
        assert "候補（クリックで選択）" in html_after
        assert "住友重工業株式会社" in html_after.split("候補（クリックで選択）")[1][:300]
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_inbox_candidates_ranked_by_confidence_exact_before_fuzzy():
    """完全一致(score100)は部分一致(fuzzy)より必ず上位に来ること。"""
    deals = [
        {"id": 1, "account_name": "川崎重工業株式会社", "deal_name": "部分一致のみ", "account_aliases": None},
        {"id": 2, "account_name": "加藤製作所株式会社", "deal_name": "完全一致するはず", "account_aliases": None},
    ]
    cands = webapp._inbox_candidates("加藤製作所株式会社と川崎重工向けの合同会議", [], deals, [])
    assert cands[0][1].startswith("商談: 加藤製作所株式会社")


def test_inbox_candidates_caps_at_five_and_excludes_zero_score():
    """関係のない会社は0点で除外され、5件を超えても上位5件だけになること。"""
    deals = [{"id": i, "account_name": f"サンプル商事{i}株式会社", "deal_name": "", "account_aliases": None}
             for i in range(7)]
    cands = webapp._inbox_candidates("サンプル商事4株式会社との打ち合わせ", [], deals, [])
    assert len(cands) <= 5
    unrelated = [{"id": 99, "account_name": "全く無関係な株式会社", "deal_name": "", "account_aliases": None}]
    assert webapp._inbox_candidates("サンプル商事4株式会社との打ち合わせ", [], unrelated, []) == []


def test_inbox_card_checkbox_and_discard_are_on_header_row():
    """#実事故の回帰確認: チェックボックスがグローバルCSS(input{width:100%})の影響で
    タイトルと同じ行に乗らず折り返していた不具合、および「破棄」がカード最下部の別行に
    あった配置の修正。チェックボックスにwidth:auto、破棄をヘッダ行内に配置。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="テスト社")
        sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="m3",
                                    title="定例会議", occurred_on="2026-08-20",
                                    transcript="本文", attendees_json="[]")
        html = _s(webapp.intake_inbox_page(con))
        assert 'style="width:auto;flex:none;margin:0"' in html  # チェックボックスの折り返し防止
        idx_discard = html.find("破棄")
        idx_assign_btn = html.find("この会議を割り当てる")
        assert 0 < idx_discard < idx_assign_btn, "破棄ボタンがヘッダ行（割り当てフォームより前）にない"
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


def test_deal_memo_review_saves_to_deal_memo_not_activity():
    """社内議論(record_kind=memo)の確認画面は論点メモ同仕様で、活動履歴ではなく商談メモへ導く(#29)。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        deal = {"id": did, "deal_name": "D", "account_name": "ソラスト"}
        st = {"title": "方針すり合わせ", "date": "2026-08-15", "overview": "社内で方針を議論",
              "points": [{"topic": "調達", "detail": "直材優先"}], "decisions": ["A方針"],
              "nextsteps": ["見積更新"], "_ai_ok": True, "_opts": {"record_kind": "memo"}}
        sid = sfa_db.create_hearing_session(con, deal_id=did, source="jamie", template_id=None,
                                            conducted_on="2026-08-15", transcript="x",
                                            structured=st, status="structured")
        html = _s(webapp.deal_memo_review_page(con, deal, sfa_db.get_hearing_session(con, sid)))
        # 論点メモ同仕様の欄（見出し=YYMMDD_タイトル・論点別・決定事項・NextStep）＋商談メモ保存の明示
        assert 'name="note_title"' in html and 'name="points_md"' in html
        assert 'name="decisions"' in html and 'name="nextsteps"' in html
        assert "260815_方針すり合わせ" in html
        assert "商談メモに保存" in html and "活動履歴には記録しません" in html
        assert 'action="/hearing/intake/commit"' in html
        # 論点と同じコピーウィジェット
        assert 'id="issueCopyOut"' in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_intake_page_has_record_kind_classification():
    """取り込み入口で「顧客面談/社内議論」を選べ、面談オプションはトグルで隠せる(#29)。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="ソラスト")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        deal = {"id": did, "deal_name": "D", "account_name": "ソラスト"}
        html = _s(webapp.hearing_intake_page(con, deal))
        assert 'name="record_kind"' in html
        assert 'value="meeting"' in html and 'value="memo"' in html
        assert 'id="meetingOpts"' in html   # 面談オプションはトグル対象
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_inbox_assign_js_is_syntactically_present():
    """割り当て絞り込みJSと確定JSが定義され、想定の関数名を含む（HTMLに埋め込まれる前提）。"""
    assert "function assignFilter" in webapp._INBOX_ASSIGN_JS
    assert "function hearingCommitSubmit" in webapp._HEARING_COMMIT_JS
    assert "mailto" in webapp._HEARING_COMMIT_JS
