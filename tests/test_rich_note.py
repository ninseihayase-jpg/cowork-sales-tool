"""OneNote風リッチメモ(#70)のサニタイズとスキーマの検証。

_sanitize_rich_html は contenteditable 由来のHTMLを許可タグ/属性のみに整形する
（public repo・本番相当データのためXSS対策が要）。一時DBのみ使用。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


def test_rich_notes_table_exists():
    d = tempfile.mkdtemp(prefix="sfa_rn_")
    try:
        path = str(Path(d) / "rn.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(rich_notes)")}
        assert {"kind", "entity_id", "title", "body"} <= cols
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("raw, expected", [
    ("<h3>見出し</h3><p>本文</p>", "<h3>見出し</h3><p>本文</p>"),
    ("<b>a</b><br><b>b</b>", "<b>a</b><br><b>b</b>"),
    # チェックリストの class/data-checked は保持
    ('<ul class="cl"><li data-checked="1">done</li><li data-checked="0">todo</li></ul>',
     '<ul class="cl"><li data-checked="1">done</li><li data-checked="0">todo</li></ul>'),
    # 安全なリンクは target/rel を付与して保持
    ('<a href="https://ok.com">ok</a>',
     '<a href="https://ok.com" rel="noopener" target="_blank">ok</a>'),
])
def test_sanitizer_keeps_allowed(raw, expected):
    assert webapp._sanitize_rich_html(raw) == expected


@pytest.mark.parametrize("raw", [
    "<script>alert(1)</script>",
    '<div onclick="evil()" style="color:red">x</div>',
    '<a href="javascript:evil()">x</a>',
    '<iframe src="http://evil"></iframe>',
])
def test_sanitizer_strips_dangerous(raw):
    out = webapp._sanitize_rich_html(raw)
    # 実行系・イベント属性・危険スキームは一切残らない
    for bad in ("<script", "onerror", "onclick", "javascript:", "<iframe", "style="):
        assert bad not in out


def test_sanitizer_image_allowed_but_safe():
    """画像は許可するが、危険属性(onerror等)・不正src(javascript:)は除去。data URI/幅は保持。"""
    # data URI + width は保持
    o = webapp._sanitize_rich_html('<img src="data:image/png;base64,iVBORw0KGg=" width="360">')
    assert 'src="data:image/png;base64,iVBORw0KGg="' in o and 'width="360"' in o
    # onerror 等のイベント属性は除去
    assert "onerror" not in webapp._sanitize_rich_html('<img src="data:image/png;base64,AAAA" onerror="alert(1)">')
    # javascript: src は破棄（imgタグは残るがsrcは付かない）
    assert "javascript" not in webapp._sanitize_rich_html('<img src="javascript:alert(1)">')
    # width は範囲clamp（>2000→2000）
    assert 'width="2000"' in webapp._sanitize_rich_html('<img src="data:image/jpeg;base64,BBBB" width="9999">')
    # 画像のみのノートは空扱いにせず保持する
    assert "<img" in webapp._sanitize_rich_html('<img src="data:image/png;base64,CCCC">')


def test_sanitizer_empty_like_becomes_blank():
    assert webapp._sanitize_rich_html("<br><div></div>&nbsp;") == ""
    assert webapp._sanitize_rich_html("") == ""


def test_sanitizer_keeps_collapse_and_checklist_attrs():
    src = '<ul><li data-collapsed="1"><ul class="cl"><li data-checked="1">x</li></ul></li></ul>'
    out = webapp._sanitize_rich_html(src)
    assert 'data-collapsed="1"' in out
    assert 'class="cl"' in out and 'data-checked="1"' in out


def test_sanitizer_per_line_checklist_mixed_with_bullets():
    # 行ごと（li.cl）のチェックボックスと通常ブレットが1つのリストに混在できる
    src = '<ul><li class="cl" data-checked="1">done</li><li>bullet</li><li class="cl" data-checked="0">todo</li></ul>'
    out = webapp._sanitize_rich_html(src)
    assert 'class="cl"' in out and 'data-checked="1"' in out and 'data-checked="0"' in out
    assert "<li>bullet</li>" in out  # 通常ブレット行はそのまま残る


def test_rich_note_preview_strips_tags():
    assert webapp._rich_note_preview("<h3>方針</h3><ul><li>予算</li></ul>") == "方針 予算"
    assert webapp._rich_note_preview("") == ""
    long = "<p>" + ("あ" * 100) + "</p>"
    assert webapp._rich_note_preview(long, limit=10).endswith("…")


def test_chip_and_button_are_kind_scoped():
    # チップは kind+id で rnOpen を呼ぶ（内容は出さない・常に一定表示）
    dchip = webapp._rich_note_chip("deal", 5)
    assert "rnOpen(&#39;deal&#39;,5)" in dchip and "📝 商談ノート" in dchip
    ichip = webapp._rich_note_chip("issue", 9)
    assert "rnOpen(&#39;issue&#39;,9)" in ichip and "論点メモ" in ichip
    # 一覧ボタンは kind/id を data 属性に持ち、記入有無で on が付く
    on = webapp._rich_note_btn("issue", 7, True)
    off = webapp._rich_note_btn("htmpl", 8, False)
    assert 'data-kind="issue"' in on and 'data-id="7"' in on and "rn-trg on" in on
    assert 'data-kind="htmpl"' in off and "rn-trg on" not in off


def test_rich_notes_crud_multi_and_entity_ids():
    d = tempfile.mkdtemp(prefix="sfa_rn_")
    try:
        path = str(Path(d) / "rn.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        a = sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="要件", body="<h3>x</h3>")
        b = sfa_db.create_rich_note(con, kind="deal", entity_id=did, title="", body="<ul><li>y</li></ul>")
        notes = sfa_db.list_rich_notes(con, "deal", did)
        assert [n["id"] for n in notes] == [a, b]           # sort_order順
        assert notes[0]["title"] == "要件" and (notes[1]["title"] in (None, ""))  # 無題
        assert sfa_db.rich_note_entity_ids(con, "deal") == {did}
        assert sfa_db.rich_note_entity_ids(con, "issue") == set()  # kindで分離
        sfa_db.update_rich_note(con, b, title="議事", body="<p>z</p>")
        assert sfa_db.get_rich_note(con, b)["title"] == "議事"
        sfa_db.delete_rich_note(con, a)
        assert [n["id"] for n in sfa_db.list_rich_notes(con, "deal", did)] == [b]
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_legacy_deal_rich_note_migrated_to_rich_notes():
    # 旧 deals.rich_note（単一メモ）は init_db 実行時に rich_notes へ移行される（冪等）
    d = tempfile.mkdtemp(prefix="sfa_rn_")
    try:
        path = str(Path(d) / "rn.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="旧社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="旧D", stage="提案")
        con.execute("UPDATE deals SET rich_note=? WHERE id=?", ("<h3>旧メモ</h3>", did))
        con.commit()
        con.close()
        sfa_db.init_db(path)  # 2回目のinitで移行が走る
        con = sfa_db.connect(path)
        notes = sfa_db.list_rich_notes(con, "deal", did)
        assert len(notes) == 1 and "旧メモ" in (notes[0]["body"] or "")
        sfa_db.init_db(path)  # 冪等: 3回目でも重複しない
        con2 = sfa_db.connect(path)
        assert len(sfa_db.list_rich_notes(con2, "deal", did)) == 1
        con.close(); con2.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rich_note_roundtrip_via_column():
    d = tempfile.mkdtemp(prefix="sfa_rn_")
    try:
        path = str(Path(d) / "rn.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
        clean = webapp._sanitize_rich_html("<h3>x</h3><script>bad()</script>")
        con.execute("UPDATE deals SET rich_note=? WHERE id=?", (clean, did))
        con.commit()
        assert sfa_db.get_deal(con, did)["rich_note"] == "<h3>x</h3>"
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
