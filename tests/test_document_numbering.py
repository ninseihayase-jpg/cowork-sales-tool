"""採番Bot（見積書・請求書・契約書番号の自動発行、#160・2026-09-17）の回帰テスト。

背景: Slackで「TOPPANの見積書に採番してもらえますか？」→堀籠さんが手動で番号
（例: 01744Q01）を考えて返信、という運用が続いており、重複採番のリスクがあった。
「InProc社内採番ルール_v1_2609.xlsx」に明文化されたフォーマットに従い、SFA-CRM側で
発行済み番号を一元管理(document_numbers)し、Slack専用チャンネルの採番Botが自然文
リクエストから自動発行する。対象・契約種別・改版元番号が特定できない場合はスレッド内で
1項目ずつ聞き返し、返信を受けて確定する（numbering_requests、slack_tasksの期限確認
フローと同じ「Bot提案→スレッド返信で確定」設計）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest

from cowork import sfa_db, slack_numbering as sn


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_docnum_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def deal_and_account(con):
    acc = con.execute("INSERT INTO accounts(name) VALUES('TOPPAN')").lastrowid
    con.commit()
    deal = sfa_db.upsert_deal(con, account_id=acc, deal_name="TOPPAN印刷案件",
                              stage="提案", status="open")
    return deal, acc


# ── スキーマ ──

def test_schema_has_document_numbers_and_numbering_requests_tables(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(document_numbers)")}
    assert {"doc_type", "entity_kind", "entity_id", "contract_type", "period_key",
            "seq", "revision", "revision_of", "full_number", "issued_by"} <= cols
    cols2 = {r[1] for r in con.execute("PRAGMA table_info(numbering_requests)")}
    assert {"slack_channel", "slack_ts", "doc_type", "is_revision", "revision_of", "status"} <= cols2


def test_full_number_has_unique_constraint(con, deal_and_account):
    deal, _ = deal_and_account
    with pytest.raises(Exception):
        con.execute(
            "INSERT INTO document_numbers (doc_type, entity_kind, entity_id, seq, full_number) "
            "VALUES ('quote','deal',?,1,'DUPTEST')", (deal,))
        con.execute(
            "INSERT INTO document_numbers (doc_type, entity_kind, entity_id, seq, full_number) "
            "VALUES ('quote','deal',?,2,'DUPTEST')", (deal,))


# ── issue_document_number: フォーマット(Excelの具体例と一致) ──

def test_quote_number_format_matches_excel_example(con, deal_and_account):
    deal, _ = deal_and_account
    row = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal,
                                       today=date(2026, 9, 17))
    assert row["fullNumber"] == f"{deal:05d}Q01"


def test_invoice_number_format_matches_excel_example(con, deal_and_account):
    _, acc = deal_and_account
    row = sfa_db.issue_document_number(con, doc_type="invoice", entity_id=acc,
                                       today=date(2026, 9, 17))
    assert row["fullNumber"] == f"{acc:05d}I260901"


def test_contract_number_format_matches_excel_example(con, deal_and_account):
    deal, _ = deal_and_account
    row = sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal,
                                       contract_type="M", today=date(2026, 9, 17))
    assert row["fullNumber"] == f"{deal:05d}CM01"


def test_contract_requires_contract_type(con, deal_and_account):
    deal, _ = deal_and_account
    with pytest.raises(ValueError):
        sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal)


# ── 連番の増加・リセット ──

def test_quote_seq_increments_within_same_deal_and_month(con, deal_and_account):
    deal, _ = deal_and_account
    r1 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 1))
    r2 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 30))
    assert (r1["fullNumber"], r2["fullNumber"]) == (f"{deal:05d}Q01", f"{deal:05d}Q02")


def test_quote_seq_does_not_reset_across_months(con, deal_and_account):
    """見積書番号は本体に発行月を含まない(SFA番号+Q+連番のみ)ため、Excelの「同一発行月内で
    リセット」を文字通り実装すると、月をまたいだ再発行で見た目が完全に同一の番号
    （例: 9月の1件目と10月の1件目が両方"12345Q01"）が生成されてしまう。これは「重複しない
    ように採番したい」という目的と矛盾するため、月をまたいでもリセットせず連番を
    累積させる設計に補正した（Excelの記述との既知の差異。ユーザーへ報告済み）。"""
    deal, _ = deal_and_account
    r1 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 30))
    r2 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 10, 1))
    assert (r1["fullNumber"], r2["fullNumber"]) == (f"{deal:05d}Q01", f"{deal:05d}Q02")


def test_quote_seq_independent_per_deal(con, deal_and_account):
    deal, acc = deal_and_account
    deal2 = sfa_db.upsert_deal(con, account_id=acc, deal_name="別案件", stage="提案", status="open")
    sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 1))
    r2 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal2, today=date(2026, 9, 1))
    assert r2["fullNumber"] == f"{deal2:05d}Q01"  # 別案件なので連番は01から


def test_invoice_seq_resets_next_month(con, deal_and_account):
    _, acc = deal_and_account
    sfa_db.issue_document_number(con, doc_type="invoice", entity_id=acc, today=date(2026, 9, 30))
    r2 = sfa_db.issue_document_number(con, doc_type="invoice", entity_id=acc, today=date(2026, 10, 1))
    assert r2["fullNumber"] == f"{acc:05d}I261001"


def test_contract_seq_independent_per_contract_type(con, deal_and_account):
    deal, _ = deal_and_account
    r1 = sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal, contract_type="M",
                                      today=date(2026, 9, 17))
    r2 = sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal, contract_type="S",
                                      today=date(2026, 9, 17))
    assert (r1["fullNumber"], r2["fullNumber"]) == (f"{deal:05d}CM01", f"{deal:05d}CS01")


def test_contract_seq_does_not_reset_across_years(con, deal_and_account):
    """契約書番号も本体に発行年を含まないため、見積書と同じ理由で年をまたいでもリセット
    しない（累積）設計に補正した。"""
    deal, _ = deal_and_account
    r1 = sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal, contract_type="M",
                                      today=date(2026, 12, 31))
    r2 = sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal, contract_type="M",
                                      today=date(2027, 1, 1))
    assert (r1["fullNumber"], r2["fullNumber"]) == (f"{deal:05d}CM01", f"{deal:05d}CM02")


def test_seq_overflow_raises(con, deal_and_account):
    deal, _ = deal_and_account
    for i in range(99):
        sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 1))
    with pytest.raises(sfa_db.DocumentNumberOverflowError):
        sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 1))


# ── 改版番号(R1,R2…) ──

def test_revision_appends_r1_and_increments(con, deal_and_account):
    deal, _ = deal_and_account
    base = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 17))
    rev1 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, revision_of=base["id"])
    rev2 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, revision_of=rev1["id"])
    assert rev1["fullNumber"] == f"{base['fullNumber']}R1"
    assert rev2["fullNumber"] == f"{base['fullNumber']}R2"
    assert rev2["revisionOf"] == base["id"]  # 改版の改版も元の初回発行を辿る（チェーンをフラットに保つ）


def test_revision_does_not_consume_new_seq(con, deal_and_account):
    """改版発行は新規の連番を消費しない（同一案件・同月で改版後に新規発行しても02になる）。"""
    deal, _ = deal_and_account
    base = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 17))
    sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, revision_of=base["id"])
    r2 = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 17))
    assert r2["fullNumber"] == f"{deal:05d}Q02"


def test_revision_of_invalid_id_raises(con, deal_and_account):
    deal, _ = deal_and_account
    with pytest.raises(ValueError):
        sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, revision_of=999999)


# ── 一覧・取消 ──

def test_list_document_numbers_filters(con, deal_and_account):
    deal, acc = deal_and_account
    sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 17))
    sfa_db.issue_document_number(con, doc_type="invoice", entity_id=acc, today=date(2026, 9, 17))
    assert len(sfa_db.list_document_numbers(con, doc_type="quote")) == 1
    assert len(sfa_db.list_document_numbers(con, entity_kind="account")) == 1


def test_delete_document_number(con, deal_and_account):
    deal, _ = deal_and_account
    row = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal, today=date(2026, 9, 17))
    sfa_db.delete_document_number(con, row["id"])
    assert sfa_db.get_document_number(con, row["id"]) is None


# ── find_account (slack_bot.py) ──

def test_find_account_matches_substring(con, deal_and_account):
    from cowork.slack_bot import find_account
    acc = find_account(con, "TOPPANの請求書をお願いします")
    assert acc is not None and acc["name"] == "TOPPAN"


def test_find_account_no_match_returns_none(con):
    from cowork.slack_bot import find_account
    assert find_account(con, "無関係な文章です") is None


# ── slack_numbering: 数字文字列の抽出(日本語直後でも境界を誤検出しない) ──

def test_number_regex_matches_even_when_immediately_followed_by_japanese_text():
    assert sn._NUMBER_RE.search("12345Q01を改版で採番して") is not None
    assert sn._NUMBER_RE.search("00030I260901R1の訂正版") is not None


# ── parse_numbering_request: キーワードフォールバック(ANTHROPIC_API_KEY無し環境での既定動作) ──

def test_parse_numbering_request_detects_doc_type_by_keyword():
    parsed = sn.parse_numbering_request("TOPPANの見積書に採番してもらえますか？")
    assert parsed["doc_type"] == "quote"


def test_parse_numbering_request_detects_revision_from_embedded_number():
    parsed = sn.parse_numbering_request("12345Q01を改版で採番して")
    assert parsed["is_revision"] is True
    assert parsed["revision_number_hint"] == "12345Q01"


# ── handle_mention_numbering / handle_message_numbering: エンドツーエンドのフロー ──

@pytest.fixture
def posts(monkeypatch):
    sent = []

    def _fake_post(method, token=None, **kw):
        sent.append(kw.get("text"))
        return {"ok": True, "ts": "999.999"}

    monkeypatch.setattr(sn, "_slack_post", _fake_post)
    return sent


def test_mention_issues_immediately_when_fully_specified(con, deal_and_account, posts):
    sn.handle_mention_numbering(con, "C1", "100.001",
                                "TOPPANの見積書に採番してもらえますか？", "U1")
    assert posts[-1].startswith("✅ 見積書番号を発行しました: ")
    assert len(sfa_db.list_document_numbers(con)) == 1


def test_mention_asks_when_entity_unresolved_then_completes_on_reply(con, deal_and_account, posts):
    sn.handle_mention_numbering(con, "C1", "200.001", "見積書に採番して", "U1")
    assert "対象の商談名" in posts[-1]
    assert sfa_db.list_document_numbers(con) == []

    sn.handle_message_numbering(con, {"channel": "C1", "ts": "200.002",
                                      "thread_ts": "200.001", "text": "TOPPAN", "user": "U1"})
    assert posts[-1].startswith("✅ 見積書番号を発行しました: ")
    numbers = sfa_db.list_document_numbers(con)
    assert len(numbers) == 1
    assert numbers[0]["entityId"] == deal_and_account[0]


def test_mention_asks_contract_type_then_completes(con, deal_and_account, posts):
    sn.handle_mention_numbering(con, "C1", "300.001", "TOPPANの契約書に採番して", "U1")
    assert "契約種別" in posts[-1]
    sn.handle_message_numbering(con, {"channel": "C1", "ts": "300.002",
                                      "thread_ts": "300.001", "text": "基本契約でお願いします",
                                      "user": "U1"})
    assert posts[-1].startswith("✅ 契約書番号を発行しました: ")
    assert sfa_db.list_document_numbers(con)[0]["contractType"] == "M"


def test_revision_request_with_valid_number_issues_immediately(con, deal_and_account, posts):
    deal, _ = deal_and_account
    base = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal)
    sn.handle_mention_numbering(con, "C1", "400.001",
                                f"{base['fullNumber']}を改版で採番して", "U1")
    assert posts[-1] == f"✅ 見積書番号を発行しました: {base['fullNumber']}R1"


def test_revision_request_with_unknown_number_asks_then_completes(con, deal_and_account, posts):
    deal, _ = deal_and_account
    base = sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal)
    sn.handle_mention_numbering(con, "C1", "500.001", "見積書を改版で採番して", "U1")
    assert "改版する元の番号" in posts[-1]

    sn.handle_message_numbering(con, {"channel": "C1", "ts": "500.002", "thread_ts": "500.001",
                                      "text": base["fullNumber"], "user": "U1"})
    assert posts[-1] == f"✅ 見積書番号を発行しました: {base['fullNumber']}R1"


def test_revision_inherits_doc_type_entity_without_asking_them(con, deal_and_account, posts):
    """改版発行時はdoc_type/対象/契約種別を個別に尋ねない（元番号から継承するため、
    revision_ofさえ特定できれば発行に必要な情報は揃う設計）。"""
    deal, _ = deal_and_account
    base = sfa_db.issue_document_number(con, doc_type="contract", entity_id=deal, contract_type="N")
    sn.handle_mention_numbering(con, "C1", "600.001", "契約書を改版で採番して", "U1")
    assert "改版する元の番号" in posts[-1]  # doc_type/契約種別は聞かれない

    sn.handle_message_numbering(con, {"channel": "C1", "ts": "600.002", "thread_ts": "600.001",
                                      "text": base["fullNumber"], "user": "U1"})
    assert posts[-1] == f"✅ 契約書番号を発行しました: {base['fullNumber']}R1"


def test_message_without_pending_request_is_ignored(con, posts):
    """該当する保留リクエストが無いスレッドへの返信は無視される（無関係なスレッドで反応しない）。"""
    sn.handle_message_numbering(con, {"channel": "C9", "ts": "900.002",
                                      "thread_ts": "900.001", "text": "TOPPAN", "user": "U1"})
    assert posts == []


def test_overflow_error_replies_with_warning_instead_of_crashing(con, deal_and_account, posts):
    """handle_mention_numbering内部のissue_document_number呼び出しはtodayを渡さず実際の
    現在日時(JST)を使うため、事前投入分もtoday指定なしにして期間キーを一致させる
    （quote/contractは期間でリセットしない設計に補正済みなので、実際にはtoday不一致でも
    影響しないが、意図を明確にするため揃えておく）。"""
    deal, _ = deal_and_account
    for _ in range(99):
        sfa_db.issue_document_number(con, doc_type="quote", entity_id=deal)
    sn.handle_mention_numbering(con, "C1", "700.001",
                                "TOPPANの見積書に採番してもらえますか？", "U1")
    assert "⚠️" in posts[-1]
