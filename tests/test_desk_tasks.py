"""事務員向けタスク（is_admin=1 / requester）の分離・保存・絞り込みの最小検証。

一時DBに対して行い、本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from cowork import sfa_db


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_desk_test_")
    path = str(Path(d) / "desk.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def test_schema_has_admin_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
    assert "is_admin" in cols
    assert "requester" in cols


def test_admin_flag_and_requester_persist(con):
    tid = sfa_db.upsert_task(con, title="交通費精算の入力", is_admin=1,
                             requester="早瀬", category="経費・請求", status="受信箱")
    row = sfa_db.get_task(con, tid)
    assert row["is_admin"] == 1
    assert row["requester"] == "早瀬"
    assert row["category"] == "経費・請求"


def test_list_tasks_admin_filter_separates(con):
    a = sfa_db.upsert_task(con, title="事務A", is_admin=1, requester="田中")
    b = sfa_db.upsert_task(con, title="通常B", is_admin=0)
    c = sfa_db.upsert_task(con, title="通常C")  # is_admin未指定→NULL扱い

    admin_ids = {t["id"] for t in sfa_db.list_tasks(con, admin=True)}
    normal_ids = {t["id"] for t in sfa_db.list_tasks(con, admin=False)}
    all_ids = {t["id"] for t in sfa_db.list_tasks(con)}

    assert admin_ids == {a}
    assert normal_ids == {b, c}           # NULLは通常側に含む（COALESCE）
    assert all_ids == {a, b, c}           # admin=None は両方
    # 交わらない
    assert admin_ids.isdisjoint(normal_ids)


def test_admin_task_categories_defined():
    assert "書類作成" in sfa_db.ADMIN_TASK_CATEGORIES
    assert "その他" in sfa_db.ADMIN_TASK_CATEGORIES
    # 開発系の分類とは別体系
    assert sfa_db.ADMIN_TASK_CATEGORIES != sfa_db.TASK_CATEGORIES
