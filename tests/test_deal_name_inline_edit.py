"""商談編集画面: 案件名を画面上部の固定エリアで編集できるようにした機能の回帰テスト(2026-09-10)。

ユーザー要望: 「案件名を上部固定エリアで編集できるような仕様に」。
以前は案件名の<input>がフォーム本体（画面下部）にしか無く、確認するたびにスクロールが
必要だった。固定保存バー（_save_bar）のタイトル部分に案件名の編集用inputを追加し、
HTML5のform属性で本来のdealForm（フォーム本体は別位置）へ送信できるようにした。
名前重複（同じname="deal_name"のinputが2つ）を避けるため、既存商談編集時は
フォーム本体側の案件名inputを外し、案内文に差し替える（新規商談作成時は上部に
SFA番号がまだ無いため、従来どおりフォーム本体のinputのまま）。
一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cowork import sfa_db, webapp


def _s(x):
    return x.decode() if isinstance(x, (bytes, bytearray)) else x


def _fresh():
    d = tempfile.mkdtemp(prefix="sfa_dnie_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def test_existing_deal_has_editable_deal_name_in_save_bar():
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="浜松ホトニクス")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="通信費(iPhone)成果報酬コスト削減",
                                 stage="初回アポ実施")
        html = _s(webapp.deal_form(con, sfa_db.get_deal(con, did)))
        assert f'form="dealForm"' in html
        assert 'name="deal_name" form="dealForm"' in html
        assert 'value="通信費(iPhone)成果報酬コスト削減"' in html
        assert f"SFA#{did}" in html
        assert "浜松ホトニクス" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_existing_deal_body_form_does_not_duplicate_deal_name_input():
    """名前重複によるフォーム送信の不整合を防ぐため、既存商談編集時はフォーム本体側の
    案件名inputが無く（画面上部のみに一本化）、案内文に差し替わっていること。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="A社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X案件", stage="提案")
        html = _s(webapp.deal_form(con, sfa_db.get_deal(con, did)))
        assert html.count('name="deal_name"') == 1  # 上部の1箇所のみ
        assert "画面上部で編集できます" in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_new_deal_still_has_deal_name_input_in_form_body():
    """新規商談作成時はまだSFA番号が無く上部固定エリアの見出し自体が出ないため、
    従来どおりフォーム本体に案件名inputがあること（回帰防止）。"""
    d, con = _fresh()
    try:
        html = _s(webapp.deal_form(con, None))
        assert html.count('name="deal_name"') == 1
        assert 'form="dealForm"' not in html or 'name="deal_name" form="dealForm"' not in html
        assert "画面上部で編集できます" not in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_save_bar_title_html_overrides_plain_title():
    """_save_bar()のtitle_htmlパラメータが、title(プレーンテキスト)より優先されること
    （他の呼び出し元に影響しない後方互換の確認）。"""
    html = webapp._save_bar("f1", title="無視されるはず", title_html='<span id="custom">カスタム</span>')
    assert 'id="custom"' in html
    assert "無視されるはず" not in html


def test_save_bar_plain_title_still_works_without_title_html():
    """title_htmlを渡さない既存呼び出し（他フォーム）が引き続き動作すること（後方互換）。"""
    html = webapp._save_bar("f1", title="プレーンタイトル")
    assert '<span class="sb-title">プレーンタイトル</span>' in html
