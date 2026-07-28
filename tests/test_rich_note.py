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
