"""sfa_db.py のCRUD関数の往復検証。

すべて一時ディレクトリ内のSQLiteファイルに対して行い、本番DB(cowork_sfa.db)には
一切触れない。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def tmp_dir():
    """テストごとに独立した一時ディレクトリ（tempfile使用）。"""
    d = tempfile.mkdtemp(prefix="sfa_db_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    return str(tmp_dir / "test_sfa.db")


@pytest.fixture
def con(db_path):
    """スキーマ作成済みの一時DBへの接続。テストごとに破棄する。"""
    sfa_db.init_db(db_path)
    conn = sfa_db.connect(db_path)
    yield conn
    conn.close()


# ---- accounts ----

def test_upsert_account_insert_then_list(con):
    new_id = sfa_db.upsert_account(con, id=None, name="株式会社サンプル", industry="商社・卸売",
                                    company_size="1000億未満", note="メモ1")
    assert isinstance(new_id, int)
    rows = sfa_db.list_accounts(con)
    assert len(rows) == 1
    assert rows[0]["id"] == new_id
    assert rows[0]["name"] == "株式会社サンプル"
    assert rows[0]["industry"] == "商社・卸売"
    assert rows[0]["company_size"] == "1000億未満"
    assert rows[0]["note"] == "メモ1"


def test_upsert_account_update_existing(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="更新前")
    updated_id = sfa_db.upsert_account(con, id=acc_id, name="更新後", industry="金融・証券・保険",
                                        company_size="5000億以上", note="更新メモ")
    assert updated_id == acc_id
    rows = sfa_db.list_accounts(con)
    assert len(rows) == 1  # 新規行が増えていない(UPDATEになっている)こと
    assert rows[0]["name"] == "更新後"
    assert rows[0]["industry"] == "金融・証券・保険"
    assert rows[0]["company_size"] == "5000億以上"
    assert rows[0]["note"] == "更新メモ"


# ---- deals ----

def test_upsert_deal_insert_and_get(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="A社")
    deal_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="新規商談",
                                  stage="初回アポ実施", owner="吉江")
    assert isinstance(deal_id, int)
    deal = sfa_db.get_deal(con, deal_id)
    assert deal is not None
    assert deal["deal_name"] == "新規商談"
    assert deal["stage"] == "初回アポ実施"
    assert deal["owner"] == "吉江"
    assert deal["account_name"] == "A社"
    # 注意: upsert_deal()はDEAL_FIELDS全カラムを常にINSERTするため、statusを
    # 明示指定しない場合はスキーマのDEFAULT 'open'は適用されずNULLになる
    # (list_deals()側がstatus IS NULLをopen扱いするため実害はないが、
    # get_deal()の生の戻り値としてはNoneになる点に注意)。
    assert deal["status"] is None


def test_upsert_deal_update_existing(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="B社")
    deal_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="商談1", stage="提案")
    updated_id = sfa_db.upsert_deal(con, id=deal_id, account_id=acc_id, deal_name="商談1(改)",
                                     stage="クロージング")
    assert updated_id == deal_id
    deal = sfa_db.get_deal(con, deal_id)
    assert deal["deal_name"] == "商談1(改)"
    assert deal["stage"] == "クロージング"


def test_list_deals_status_open_filter(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="C社")
    open_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="オープン商談")
    closed_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="クローズ商談",
                                    status="closed")

    open_deals = sfa_db.list_deals(con, status="open")
    open_ids = {d["id"] for d in open_deals}
    assert open_id in open_ids
    assert closed_id not in open_ids

    closed_deals = sfa_db.list_deals(con, status="closed")
    closed_ids = {d["id"] for d in closed_deals}
    assert closed_id in closed_ids
    assert open_id not in closed_ids

    all_deals = sfa_db.list_deals(con, status=None)
    all_ids = {d["id"] for d in all_deals}
    assert open_id in all_ids and closed_id in all_ids


# ---- dev_projects ----

def test_upsert_dev_project_and_list_get(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="D社")
    deal_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="開発商談")
    other_deal_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="別商談")

    proj_id = sfa_db.upsert_dev_project(
        con, id=None, deal_id=deal_id, theme="ダッシュボード開発", status="開発中",
        stage="PoC", budget_confirmed="〇", resolution="〇", difficulty="易",
    )
    sfa_db.upsert_dev_project(con, id=None, deal_id=other_deal_id, theme="別案件")

    proj = sfa_db.get_dev_project(con, proj_id)
    assert proj is not None
    assert proj["theme"] == "ダッシュボード開発"
    assert proj["deal_name"] == "開発商談"
    # compute_dev_order_potential: 予算確認〇・解像度〇・難易度易 → 高
    assert proj["order_potential"] == "高"

    filtered = sfa_db.list_dev_projects(con, deal_id=deal_id)
    assert len(filtered) == 1
    assert filtered[0]["id"] == proj_id

    all_projects = sfa_db.list_dev_projects(con)
    assert len(all_projects) == 2


def test_upsert_dev_project_update_existing(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="E社")
    deal_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="商談E")
    proj_id = sfa_db.upsert_dev_project(con, id=None, deal_id=deal_id, theme="テーマ1",
                                         budget_confirmed="×")
    proj = sfa_db.get_dev_project(con, proj_id)
    assert proj["order_potential"] == "低"  # 予算確認×なら常に低

    updated_id = sfa_db.upsert_dev_project(con, id=proj_id, deal_id=deal_id, theme="テーマ1改",
                                            budget_confirmed="〇", resolution="△", difficulty="中")
    assert updated_id == proj_id
    proj2 = sfa_db.get_dev_project(con, proj_id)
    assert proj2["theme"] == "テーマ1改"
    assert proj2["order_potential"] == "中"  # 解像度△のため高にはならない


# ---- deal_issues / memos ----

def test_deal_issue_common_and_deal_scoped(con):
    acc_id = sfa_db.upsert_account(con, id=None, name="F社")
    deal_id = sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="商談F")

    common_id = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="商談共通の論点",
                                          status="議論中")
    scoped_id = sfa_db.upsert_deal_issue(con, id=None, deal_id=deal_id, issue="商談固有の論点",
                                          status="議論中")

    all_issues = sfa_db.list_deal_issues(con)
    ids = {i["id"] for i in all_issues}
    assert common_id in ids and scoped_id in ids
    common_row = next(i for i in all_issues if i["id"] == common_id)
    assert common_row["deal_id"] is None

    scoped_only = sfa_db.list_deal_issues(con, deal_id=deal_id)
    assert {i["id"] for i in scoped_only} == {scoped_id}

    fetched_common = sfa_db.get_deal_issue(con, common_id)
    assert fetched_common["issue"] == "商談共通の論点"
    assert fetched_common["deal_id"] is None


def test_deal_issue_memos_add_list_delete(con):
    issue_id = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="論点X")

    memo1_id = sfa_db.add_deal_issue_memo(con, issue_id=issue_id, body="メモ1", author="早瀬")
    memo2_id = sfa_db.add_deal_issue_memo(con, issue_id=issue_id, body="メモ2", author="吉江")

    memos = sfa_db.list_deal_issue_memos(con, issue_id)
    assert [m["id"] for m in memos] == [memo1_id, memo2_id]
    assert memos[0]["body"] == "メモ1"
    assert memos[0]["author"] == "早瀬"

    sfa_db.delete_deal_issue_memo(con, memo1_id)
    remaining = sfa_db.list_deal_issue_memos(con, issue_id)
    assert [m["id"] for m in remaining] == [memo2_id]


def test_delete_deal_issue_cascades_memos(con):
    issue_id = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="削除予定の論点")
    sfa_db.add_deal_issue_memo(con, issue_id=issue_id, body="メモA")

    sfa_db.delete_deal_issue(con, issue_id)

    assert sfa_db.get_deal_issue(con, issue_id) is None
    # ON DELETE CASCADEでメモも消えていること(foreign_keys=ONが有効な場合)
    assert sfa_db.list_deal_issue_memos(con, issue_id) == []


# ---- masters ----

def test_get_master_list_default(con):
    assert sfa_db.get_master_list(con, "owners") == sfa_db.OWNERS
    assert sfa_db.get_master_list(con, "deal_stages") == sfa_db.DEAL_STAGES
    # 未知のキーは MASTER_KEYS.get(key, []) にフォールバック
    assert sfa_db.get_master_list(con, "no_such_key") == []


def test_set_master_list_then_get(con):
    sfa_db.set_master_list(con, "owners", ["山田", "鈴木"])
    assert sfa_db.get_master_list(con, "owners") == ["山田", "鈴木"]

    # 同じキーで再設定すると上書きされる(UPSERT)こと
    sfa_db.set_master_list(con, "owners", ["佐藤"])
    assert sfa_db.get_master_list(con, "owners") == ["佐藤"]
    count = con.execute("SELECT count(*) FROM masters WHERE key='owners'").fetchone()[0]
    assert count == 1


def test_get_master_list_falls_back_on_broken_json(con):
    # masters テーブルに直接不正JSONをINSERTしてクラッシュしないことを確認する
    con.execute(
        "INSERT INTO masters(key, values_json) VALUES (?, ?)",
        ("industries", "{not valid json"),
    )
    con.commit()
    result = sfa_db.get_master_list(con, "industries")
    assert result == sfa_db.INDUSTRIES  # デフォルトにフォールバック


# ---- backup_db ----

def test_backup_db_creates_generation_and_skips_same_day(tmp_dir, db_path):
    # init_db()実行時点でファイルが作られる
    sfa_db.init_db(db_path)

    dest1 = sfa_db.backup_db(db_path)
    assert dest1 is not None
    backups_dir = Path(db_path).parent / "backups"
    assert backups_dir.is_dir()
    dest1_path = Path(dest1)
    assert dest1_path.exists()
    assert dest1_path.parent == backups_dir

    # バックアップ先が正しいsqlite DBとして開けること(スキーマがコピーされている)
    bcon = sqlite3.connect(dest1)
    try:
        tables = {r[0] for r in bcon.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "deals" in tables
    finally:
        bcon.close()

    mtime_before = dest1_path.stat().st_mtime

    # 商談を1件追加してから同日中に再度バックアップ→スキップされ、内容は更新されない
    con = sfa_db.connect(db_path)
    acc_id = sfa_db.upsert_account(con, id=None, name="バックアップ確認用")
    sfa_db.upsert_deal(con, id=None, account_id=acc_id, deal_name="バックアップ後の商談")
    con.close()

    dest2 = sfa_db.backup_db(db_path)
    assert dest2 == dest1  # 同日分は同じパスを返す(=スキップ)
    assert Path(dest2).stat().st_mtime == mtime_before  # 上書きされていない

    # 新しい商談はバックアップに含まれていない(スキップされた証拠)
    bcon2 = sqlite3.connect(dest2)
    try:
        cnt = bcon2.execute("SELECT count(*) FROM deals").fetchone()[0]
        assert cnt == 0
    finally:
        bcon2.close()


def test_backup_db_returns_none_when_source_missing(tmp_dir):
    missing_path = str(tmp_dir / "does_not_exist.db")
    assert sfa_db.backup_db(missing_path) is None
