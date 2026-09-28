"""/api/delivery_load の事業種別L1/L2マスタ配信に関する回帰テスト。

ユーザー指摘(2026-09-28)「hisho dashboardの事業種別が、SFA側と一致していないと思われる。
SFAのマスタを動的に参照して」。原因調査の結果、`/api/delivery_load`が事業種別L1/L2を
`sfa_db.BUSINESS_TYPE_L1`/`BUSINESS_TYPE_L2_BY_L1`という編集不可のコード内定数から返して
おり、`/masters`(事業種別ツリー編集画面)でユーザーがL1/L2を追加・変更・並び替えても、
Hisho dashboard.htmlの`deliveryLoad.business_type_l1`/`business_type_l2_by_l1`（稼働予定
タブの事業種別フィルタの構築元）には一切反映されない状態だった。
`sfa_db.get_business_type_tree(con)`（実際に編集可能なマスタ）を返すよう修正した。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_delivery_load_biztype_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _run_server(db_path):
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_delivery_load_business_type_master_reflects_masters_edit(monkeypatch, tmp_path):
    """事業種別ツリーをマスタ画面相当のAPI(set_business_type_tree)でカスタム編集した状態で
    /api/delivery_loadを叩くと、コード内既定値ではなく編集後のツリーが返ることを確認する。"""
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    sfa_db.set_business_type_tree(con2, {
        "新事業ライン": ["新L2-A", "新L2-B"],
        "コスト削減": ["コスト診断(無償)"],
    })
    con2.close()

    monkeypatch.setattr(webapp, "SFA_API_TOKEN", "secret-token")
    srv, t = _run_server(db_path)
    try:
        port = srv.server_address[1]
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/delivery_load?token=secret-token", timeout=10)
        assert resp.getcode() == 200
        data = json.loads(resp.read())
        assert data["business_type_l1"] == ["新事業ライン", "コスト削減"]
        assert data["business_type_l2_by_l1"] == {
            "新事業ライン": ["新L2-A", "新L2-B"],
            "コスト削減": ["コスト診断(無償)"],
        }
        # 編集前のコード内定数がそのまま漏れ出ていないことも明示的に確認する。
        assert "コンサルティング" not in data["business_type_l1"]
        assert "AI導入" not in data["business_type_l1"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_delivery_load_business_type_master_falls_back_to_defaults_when_unedited(con):
    """マスタ未編集(初期状態)では、get_business_type_treeの既定フォールバック
    (BUSINESS_TYPE_L1×BUSINESS_TYPE_L2_BY_L1)がそのまま返ることの回帰テスト
    （後方互換・既存デプロイでいきなり空になったりしないこと）。"""
    load = sfa_db.compute_delivery_load(con)
    tree = sfa_db.get_business_type_tree(con)
    assert list(tree.keys()) == sfa_db.BUSINESS_TYPE_L1
    for l1, l2s in tree.items():
        assert l2s == sfa_db.BUSINESS_TYPE_L2_BY_L1.get(l1, [])
    assert load  # compute_delivery_load自体は既存仕様のまま動作する
