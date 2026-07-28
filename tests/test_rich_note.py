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


def test_deals_has_rich_note_column():
    d = tempfile.mkdtemp(prefix="sfa_rn_")
    try:
        path = str(Path(d) / "rn.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(deals)")}
        assert "rich_note" in cols
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
    '<img src=x onerror=alert(1)>',
    '<div onclick="evil()" style="color:red">x</div>',
    '<a href="javascript:evil()">x</a>',
    '<iframe src="http://evil"></iframe>',
])
def test_sanitizer_strips_dangerous(raw):
    out = webapp._sanitize_rich_html(raw)
    # 実行系・イベント属性・危険スキームは一切残らない
    for bad in ("<script", "onerror", "onclick", "javascript:", "<iframe", "<img", "style="):
        assert bad not in out


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


def test_chip_is_constant_and_hides_content():
    # 保存バーのチップは常に一定表示（内容・プレビューを出さない）
    filled = webapp._rich_note_chip(5, "<h3>社外秘の方針</h3>")
    empty = webapp._rich_note_chip(6, "")
    assert "社外秘の方針" not in filled            # 内容は表示しない
    assert filled.replace("5", "N") == empty.replace("6", "N")  # id以外は同一表示
    assert 'rnOpen(5)' in filled and "📝 商談ノート" in filled
    # 一覧の📝ボタンは記入有無で状態が変わる
    assert 'class="rn-trg on"' in webapp._rich_note_btn(7, True)
    assert 'class="rn-trg"' in webapp._rich_note_btn(8, False)


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
