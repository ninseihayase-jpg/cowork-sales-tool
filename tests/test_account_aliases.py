"""アカウント略称の一括登録機能(取り込みインボックス候補検出の辞書)の回帰テスト。

「住友重工業→住重」のような、アルゴリズムでは導出できない慣用ニックネームは
accounts.aliasesへの手動登録でのみ拾える。この列を安全に管理できることを確認する:
- 一括登録画面(/account-aliases)からの保存で正しく反映される
- 通常のアカウント編集(/account/save、aliases未指定)では既存の登録を消さない
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_acc_alias_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_set_account_aliases_persists_and_blank_clears(con):
    acc = sfa_db.upsert_account(con, name="住友重工業株式会社")
    sfa_db.set_account_aliases(con, acc, "住重、住重工")
    row = con.execute("SELECT aliases FROM accounts WHERE id=?", (acc,)).fetchone()
    assert row["aliases"] == "住重、住重工"

    sfa_db.set_account_aliases(con, acc, "")
    row2 = con.execute("SELECT aliases FROM accounts WHERE id=?", (acc,)).fetchone()
    assert row2["aliases"] is None


def test_normal_account_edit_does_not_clobber_aliases(con):
    """/account/save 相当のupsert_account(id=..., aliases未指定)が既存のaliasesを消さないこと
    （実装時に見つけた本物のリグレッション経路: aliasesにデフォルト値を持たせると
    通常の編集フォームからの保存で毎回クリアされてしまう）。"""
    acc = sfa_db.upsert_account(con, name="住友重工業株式会社")
    sfa_db.set_account_aliases(con, acc, "住重")
    sfa_db.upsert_account(con, id=acc, name="住友重工業株式会社", industry="製造業")
    row = con.execute("SELECT aliases, industry FROM accounts WHERE id=?", (acc,)).fetchone()
    assert row["aliases"] == "住重"
    assert row["industry"] == "製造業"


def test_list_deals_exposes_account_aliases(con):
    acc = sfa_db.upsert_account(con, name="住友重工業株式会社")
    sfa_db.set_account_aliases(con, acc, "住重")
    sfa_db.upsert_deal(con, account_id=acc, deal_name="設備投資案件", stage="要件詰め")
    deals = sfa_db.list_deals(con, status="open")
    assert deals[0]["account_aliases"] == "住重"


def test_account_aliases_page_renders_existing_values(con):
    acc = sfa_db.upsert_account(con, name="住友重工業株式会社")
    sfa_db.set_account_aliases(con, acc, "住重、住重工")
    html = webapp.account_aliases_page(con)
    assert "住友重工業株式会社" in html
    assert f'name="aids[]" value="{acc}"' in html
    assert 'value="住重、住重工"' in html
