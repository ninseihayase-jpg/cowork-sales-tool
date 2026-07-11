"""SFA↔Hisho同期整合性チェック diagnose_sync のテスト（Hishoクライアントはモック）。"""
import os
import tempfile

import pytest

from cowork import sfa_db, dev_project_link


class FakeClient:
    """ThemeDBClient.execute(SELECT id, sfa_id FROM dev_projects) を模擬する。"""
    def __init__(self, rows, raise_exc=None):
        self._rows = rows
        self._raise = raise_exc

    def execute(self, sql, params):
        if self._raise:
            raise self._raise
        return {"rows": self._rows}


@pytest.fixture
def con():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    sfa_db.init_db(db)
    c = sfa_db.connect(db)
    yield c
    c.close()


def _setup(con):
    a = sfa_db.upsert_account(con, name="A")
    deal = sfa_db.upsert_deal(con, account_id=a, deal_name="商談1")
    p_ok = sfa_db.upsert_dev_project(con, deal_id=deal, theme="同期済", status="開発中")
    p_unsynced = sfa_db.upsert_dev_project(con, deal_id=deal, theme="未同期", status="開発中")
    p_broken = sfa_db.upsert_dev_project(con, deal_id=deal, theme="リンク切れ", status="開発中")
    con.execute("UPDATE dev_projects SET hisho_id=100 WHERE id=?", (p_ok,))
    con.execute("UPDATE dev_projects SET hisho_id=999 WHERE id=?", (p_broken,))  # Hishoに無い
    con.commit()
    return p_ok, p_unsynced, p_broken


def test_diagnose_detects_all_categories(con):
    p_ok, p_unsynced, p_broken = _setup(con)
    # Hisho: id=100(sfa_id=p_ok, 正常), id=200(sfa_id=555, SFAに無い孤児)
    client = FakeClient([{"id": 100, "sfa_id": p_ok}, {"id": 200, "sfa_id": 555}])
    d = dev_project_link.diagnose_sync(client, con)
    assert d["error"] is None
    assert [x["id"] for x in d["dev_unsynced"]] == [p_unsynced]
    assert [x["id"] for x in d["dev_broken_link"]] == [p_broken]
    assert [x["sfa_id"] for x in d["dev_orphan_hisho"]] == [555]


def test_diagnose_deal_unsynced(con):
    a = sfa_db.upsert_account(con, name="B")
    sfa_db.upsert_deal(con, account_id=a, deal_name="未同期商談")  # theme_id NULL
    client = FakeClient([])
    d = dev_project_link.diagnose_sync(client, con)
    assert any(x["deal_name"] == "未同期商談" for x in d["deal_unsynced"])


def test_diagnose_hisho_error_is_captured(con):
    client = FakeClient([], raise_exc=RuntimeError("Hisho down"))
    d = dev_project_link.diagnose_sync(client, con)
    assert d["error"] == "Hisho down"
