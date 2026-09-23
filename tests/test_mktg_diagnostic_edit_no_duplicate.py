"""マーケ施策診断ツールの「保存すると重複してしまう」バグ修正の回帰テスト(2026-09-16)。

ユーザー報告:「『この診断を保存』すると重複してしまう」。原因調査の結果、既存の保存済み診断を
↻（読み込む）で読み込んで内容を編集し、再度「この診断を保存」を押しても常に
/mktg-diagnostic/create（新規INSERT）を叩くだけで、元の行を上書きする手段（UPDATE）が
存在しなかった。特にこのセッションで直前に対応した「事業種別L1/L2をマスタ連動化した際、
既存12件の旧区分値をユーザーが個別に選び直す」運用（自動変換はしない、と確定した方針）を
実行しようとすると、読み込み→編集→保存のたびに重複行が増えてしまっていた。

修正: loadSave()でeditingIdをセットし、saveCurrentState()はeditingIdがあれば
/mktg-diagnostic/<id>/update（UPDATE）を、無ければ従来通り/mktg-diagnostic/create
（INSERT）を呼ぶよう分岐した。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_mktg_edit_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def server(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", db_path
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _auth_header():
    return {"Cookie": f"sfa_session={webapp._make_session_token()}"}


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    h = _auth_header()
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── sfa_db層: update_mktg_diagnostic ──

def test_update_mktg_diagnostic_overwrites_existing_row_without_creating_new_one(con):
    created = sfa_db.create_mktg_diagnostic(
        con, tool_name="旧区分ツール", biz_type="調達SCM", priority=False,
        sel1={"cat": "あり", "price": "高", "size": ["ENP"], "period": "長"},
        sel2={"nps": "高い"}, top_methods=[], total_matched=0)
    before_count = con.execute("SELECT COUNT(*) FROM mktg_diagnostics").fetchone()[0]

    updated = sfa_db.update_mktg_diagnostic(
        con, created["id"], tool_name="旧区分ツール", biz_type="コスト削減",
        biz_type_l2="コスト診断(有償)", priority=True,
        sel1={"cat": "あり", "price": "高", "size": ["ENP", "MM"], "period": "長"},
        sel2={"nps": "高い"}, top_methods=["A"], total_matched=5)

    after_count = con.execute("SELECT COUNT(*) FROM mktg_diagnostics").fetchone()[0]
    assert after_count == before_count  # 行数が増えていない（重複していない）
    assert updated["id"] == created["id"]
    assert updated["bizType"] == "コスト削減"
    assert updated["bizTypeL2"] == "コスト診断(有償)"
    assert updated["priority"] is True
    assert updated["sel1"]["size"] == ["ENP", "MM"]


def test_update_mktg_diagnostic_preserves_created_at(con):
    created = sfa_db.create_mktg_diagnostic(
        con, tool_name="ツール", biz_type="他", priority=False,
        sel1={}, sel2={}, top_methods=[], total_matched=0)
    updated = sfa_db.update_mktg_diagnostic(
        con, created["id"], tool_name="ツール改", biz_type="他",
        priority=False, sel1={}, sel2={}, top_methods=[], total_matched=0)
    assert updated["savedAt"] == created["savedAt"]


# ── HTTPルート: /mktg-diagnostic/<id>/update ──

def test_update_route_updates_row_in_place_not_creating_duplicate(server):
    base_url, db_path = server
    conn = sfa_db.connect(db_path)
    created = sfa_db.create_mktg_diagnostic(
        conn, tool_name="ツールA", biz_type="他", priority=False,
        sel1={"cat": "あり", "price": "高", "size": ["ENP"], "period": "長"},
        sel2={}, top_methods=[], total_matched=0)
    conn.close()

    code, body = _post(f"{base_url}/mktg-diagnostic/{created['id']}/update", {
        "tool_name": "ツールA改",
        "biz_type": "コスト削減",
        "biz_type_l2": "コスト診断(有償)",
        "priority": "1",
        "sel1_json": json.dumps({"cat": "あり", "price": "高", "size": ["ENP", "MM"], "period": "長"}),
        "sel2_json": "{}",
        "top_methods_json": "[]",
        "total_matched": "2",
    })
    assert code == 200
    saved = json.loads(body)
    assert saved["id"] == created["id"]
    assert saved["toolName"] == "ツールA改"
    assert saved["bizType"] == "コスト削減"

    conn2 = sfa_db.connect(db_path)
    rows = sfa_db.list_mktg_diagnostics(conn2)
    conn2.close()
    matching = [r for r in rows if r["toolName"] in ("ツールA", "ツールA改")]
    assert len(matching) == 1  # 重複していない


# ── クライアントJS(静的アサーション): editingId分岐・重複防止ロジックの存在確認 ──

def test_page_has_editing_id_state_and_banner(con):
    html = webapp.mktg_sim_page(con)
    assert "let editingId=null;" in html
    assert 'id="edit-banner"' in html
    assert "function setEditBanner(" in html
    assert "window.cancelEditSave=function(){" in html


def test_load_save_sets_editing_id(con):
    html = webapp.mktg_sim_page(con)
    assert "editingId=id;" in html
    assert "setEditBanner(save.toolName);" in html


def test_save_current_state_branches_between_create_and_update(con):
    html = webapp.mktg_sim_page(con)
    assert "const url=editingId?('/mktg-diagnostic/'+editingId+'/update'):'/mktg-diagnostic/create';" in html
    assert "editingId=null;" in html  # 保存成功後にリセットされる


def test_delete_save_clears_editing_id_if_deleting_the_loaded_item(con):
    html = webapp.mktg_sim_page(con)
    assert "if(editingId===id){editingId=null;setEditBanner(null);}" in html
