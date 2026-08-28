"""社内資料の体系化・層2「検討資料」自動生成機能(docs, #129 2026-08-28)の回帰テスト。

型（見出し構成・順序）はコード側で固定し、AIには各見出しの中身だけを埋めさせる設計。
_call_claude_haiku はネットワーク呼び出しなので、生成ロジックのテストは monkeypatch で
固定応答に差し替える。一時DBのみ使用。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

_FAKE_AI_RESPONSE = """### 背景・課題認識
- 申請フローが紙ベースで手間がかかっている
- 担当者ごとにやり方がバラバラ

### 現状の問題点
- 承認までに平均5営業日かかる

### 提案内容
- SFA上にフォームを新設し電子承認に切り替える

### 期待される効果
- 承認リードタイムを2営業日に短縮

### 実行計画（スケジュール・担当）
（検討中・材料不足）

### リスク・懸念事項
- 移行期間中の運用混乱

### 意思決定を求める事項
- 電子化への切り替え可否の承認
"""


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_docs_gen_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _issue(con):
    return sfa_db.upsert_deal_issue(con, deal_id=None, issue="申請フロー電子化", status="議論中")


# ── sfa_db: docs CRUD ──────────────────────────────────────────────────────

def test_schema_has_docs_table(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(docs)")}
    assert {"kind", "template", "title", "issue_id", "body_html"} <= cols


def test_create_and_get_doc(con):
    iid = _issue(con)
    did = sfa_db.create_doc(con, kind="検討資料", title="テスト資料", body_html="<h1>x</h1>",
                            template="process_change", issue_id=iid)
    doc = sfa_db.get_doc(con, did)
    assert doc["title"] == "テスト資料"
    assert doc["kind"] == "検討資料"
    assert doc["issue_id"] == iid


def test_list_docs_scoped_by_issue_and_newest_first(con):
    i1 = _issue(con)
    i2 = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点Y", status="議論中")
    sfa_db.create_doc(con, kind="検討資料", title="A", body_html="a", issue_id=i1)
    d2 = sfa_db.create_doc(con, kind="検討資料", title="B", body_html="b", issue_id=i1)
    sfa_db.create_doc(con, kind="検討資料", title="C", body_html="c", issue_id=i2)
    docs_i1 = sfa_db.list_docs(con, issue_id=i1)
    assert [d["title"] for d in docs_i1] == ["B", "A"]  # 新しい順
    assert docs_i1[0]["id"] == d2
    assert len(sfa_db.list_docs(con)) == 3  # issue_id未指定は全件


def test_delete_doc(con):
    did = sfa_db.create_doc(con, kind="その他", title="X", body_html="x")
    sfa_db.delete_doc(con, did)
    assert sfa_db.get_doc(con, did) is None


# ── 生成ロジック: パース・簡易md変換 ──────────────────────────────────────

def test_parse_doc_sections_extracts_each_heading_body():
    headings = webapp._DOC_TEMPLATE_SECTIONS["process_change"]
    parsed = webapp._parse_doc_sections(_FAKE_AI_RESPONSE, headings)
    assert "申請フローが紙ベースで手間がかかっている" in parsed["background"]
    assert "承認までに平均5営業日" in parsed["problem"]
    assert "電子承認に切り替える" in parsed["proposal"]
    assert parsed["plan"] == "（検討中・材料不足）"


def test_parse_doc_sections_missing_heading_returns_empty_string():
    parsed = webapp._parse_doc_sections("### 背景・課題認識\n内容だけ", webapp._DOC_TEMPLATE_SECTIONS["process_change"])
    assert parsed["background"] == "内容だけ"
    assert parsed["problem"] == ""  # 出力に無い見出しは空


def test_simple_md_to_html_converts_bullets_and_paragraphs():
    out = webapp._simple_md_to_html("- 箇条書き1\n- 箇条書き2\n\n段落です")
    assert "<ul><li>箇条書き1</li><li>箇条書き2</li></ul>" in out
    assert "<p>段落です</p>" in out


def test_simple_md_to_html_escapes_html_special_chars():
    out = webapp._simple_md_to_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_simple_md_to_html_empty_falls_back_to_material_shortage_message():
    out = webapp._simple_md_to_html("")
    assert "材料不足" in out


# ── 生成パイプライン（AI呼び出しはmonkeypatch） ────────────────────────────

def test_generate_doc_body_html_assembles_fixed_structure(con, monkeypatch):
    """#129: 見出し構成・順序はAIの出力に関わらずコード側の型で固定される。"""
    iid = _issue(con)
    sfa_db.add_issue_material(con, iid, "紙申請の課題についての調査メモ")
    monkeypatch.setattr(webapp, "_call_claude_haiku", lambda *a, **kw: _FAKE_AI_RESPONSE)
    issue = sfa_db.get_deal_issue(con, iid)
    body = webapp._generate_doc_body_html(con, issue, "process_change")
    assert body is not None
    headings = webapp._DOC_TEMPLATE_SECTIONS["process_change"]
    positions = [body.find(h) for _, h in headings]
    assert all(p >= 0 for p in positions)
    assert positions == sorted(positions)  # コード側で固定した順序通りに並ぶ
    assert "電子承認に切り替える" in body


def test_generate_doc_body_html_returns_none_when_no_material(con):
    """検討材料も論点メモも無ければAIを呼ばずNoneを返す（空生成を防ぐ）。"""
    iid = _issue(con)
    issue = sfa_db.get_deal_issue(con, iid)
    assert webapp._generate_doc_body_html(con, issue, "process_change") is None


def test_generate_doc_body_html_returns_none_when_ai_response_empty(con, monkeypatch):
    """AIキー未設定等でAI応答が空の場合はNone（既存のAI機能と同じ優雅な失敗）。"""
    iid = _issue(con)
    sfa_db.add_issue_material(con, iid, "材料あり")
    monkeypatch.setattr(webapp, "_call_claude_haiku", lambda *a, **kw: "")
    issue = sfa_db.get_deal_issue(con, iid)
    assert webapp._generate_doc_body_html(con, issue, "process_change") is None


def test_issue_detail_page_shows_generate_button_and_generated_docs_list(con):
    iid = _issue(con)
    sfa_db.create_doc(con, kind="検討資料", title="既存の資料", body_html="x", issue_id=iid)
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.deal_issue_detail_page(con, issue)
    assert f'/deal-issue/{iid}/doc/generate' in html
    assert "業務改善・社内プロセス変更提案資料を生成" in html
    assert "既存の資料" in html


def test_doc_view_page_wraps_body_without_crm_nav_chrome(con):
    did = sfa_db.create_doc(con, kind="検討資料", title="表示確認資料", body_html="<h1>本文</h1>")
    doc = sfa_db.get_doc(con, did)
    raw = webapp.doc_view_page(con, doc).decode("utf-8")
    assert "<h1>本文</h1>" in raw
    assert "表示確認資料" in raw
    assert "資料庫一覧" in raw
    assert "コンサルタスク" not in raw  # CRMのナビ枠（メニュー項目）を含まない
