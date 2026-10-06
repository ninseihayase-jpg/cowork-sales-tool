"""商談を「アカウントなしで登録できない仕様に」した修正の回帰テスト(2026-10-06)。

背景: 新規商談フォームは「account_id<select>」と「新規アカウントを追加」チェックボックス
+ new_account_name<input>の2通りでアカウントを指定できる。従来は
`acc_req = "required" if deal.get("id") else ""` により新規作成時のみrequiredを外しており、
チェックボックスonchangeのtoggleNewAcc()でしかrequiredを付け直していなかったため、
チェックボックスに一度も触れずに送信すると、アカウント未選択のまま商談が登録できてしまって
いた（実例: ユーザー報告のスクリーンショットでアカウント列が空の商談行）。
account_id<select>は常にrequiredとし、「新規アカウントを追加」チェック時だけ
toggleNewAcc()がJS側でrequiredを外す（既存の挙動のまま）よう修正した。
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
    d = tempfile.mkdtemp(prefix="sfa_deal_acc_req_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def test_new_deal_account_select_is_required_by_default():
    d, con = _fresh()
    try:
        html = _s(webapp.deal_form(con, None))
        assert 'id="acc_id_sel"' in html
        # account_id<select>がrequired属性付きで出力されること（チェックボックスに触れなくても
        # ブラウザのHTML5バリデーションでブロックされる）。
        sel_tag = html.split('id="acc_id_sel"', 1)[1].split(">", 1)[0]
        assert "required" in sel_tag
        # 「新規アカウントを追加」チェック時にrequiredを外すJSが存在すること（既存動作の確認）。
        assert 'document.getElementById("acc_id_sel").required=!chk.checked;' in html
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_existing_deal_account_select_is_still_required():
    """既存商談編集時は従来からrequiredだった。新規作成側を修正しても退行しないことの確認。"""
    d, con = _fresh()
    try:
        acc = sfa_db.upsert_account(con, name="A社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X案件", stage="提案")
        html = _s(webapp.deal_form(con, sfa_db.get_deal(con, did)))
        sel_tag = html.split('id="acc_id_sel"', 1)[1].split(">", 1)[0]
        assert "required" in sel_tag
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)
