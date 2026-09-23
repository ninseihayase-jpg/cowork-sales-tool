"""マーケ施策診断ツール(/mktg-sim) 戦略マップの「実行対象プラン」機能(2026-09-13)の回帰テスト。

ユーザー要望: 戦略マップで「どの事業でどの打ち手を実施するか」を各マスをクリックして選択し、
全体設計を1つの名前で保存できるように（例:「260913_マーケ施策実行対象」）。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import base64
import json
import shutil
import tempfile
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
    d = tempfile.mkdtemp(prefix="sfa_mktg_plan_ui_")
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
    import threading
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


# ── ページ描画: UI要素・JS関数・注入データ ──

def test_page_renders_strategy_plan_bar_widgets(con):
    html = webapp.mktg_sim_page(con)
    assert 'id="plan-name-input"' in html
    assert 'id="btn-save-plan"' in html
    assert 'id="btn-clear-plan"' in html
    assert 'id="plan-list-select"' in html
    assert 'id="btn-delete-plan"' in html
    assert 'id="plan-selected-count"' in html


def test_page_defines_strategy_plan_js_functions(con):
    html = webapp.mktg_sim_page(con)
    for fn in ("saveStrategyPlan", "loadStrategyPlan", "deleteStrategyPlan",
               "renderPlanListSelect", "updatePlanSelectedCount"):
        assert fn in html


def test_page_injects_saved_plans_json(con):
    sfa_db.create_mktg_strategy_plan(
        con, name="260913_マーケ施策実行対象",
        selections=[{"diagnosticId": 1, "method": "SEO / オウンドブログ"}])
    html = webapp.mktg_sim_page(con)
    assert "260913_マーケ施策実行対象" in html
    assert "__INITIAL_STRATEGY_PLANS_JSON__" not in html  # プレースホルダは置換済みであること


def test_heatmap_cells_carry_diag_and_method_data_attrs_for_click_selection(con):
    """マスをクリックして選択できるよう、各<td>にdata-diag/data-methodが載っていること
    （実際のクリック判定はJS側だが、静的マークアップにこの2属性が無ければ機能しない）。"""
    html = webapp.mktg_sim_page(con)
    assert 'data-diag="${save.id}"' in html
    assert 'data-method="${_hmEsc(METHODS[idx].method)}"' in html
    assert "hm-selected" in html


# ── ルート: 保存・削除 ──

def test_strategy_plan_create_route_persists_and_returns_json(server):
    base, db_path = server
    body = {
        "name": "260913_マーケ施策実行対象",
        "selections_json": json.dumps(
            [{"diagnosticId": 1, "method": "ABM"}, {"diagnosticId": 2, "method": "ABM"}],
            ensure_ascii=False),
    }
    code, resp_body = _post(f"{base}/mktg-strategy-plan/create", body)
    assert code == 200
    saved = json.loads(resp_body)
    assert saved["name"] == "260913_マーケ施策実行対象"
    assert saved["selections"] == [{"diagnosticId": 1, "method": "ABM"}, {"diagnosticId": 2, "method": "ABM"}]

    con = sfa_db.connect(db_path)
    rows = sfa_db.list_mktg_strategy_plans(con)
    con.close()
    assert any(r["id"] == saved["id"] for r in rows)


def test_strategy_plan_create_route_defaults_blank_name(server):
    base, _ = server
    code, resp_body = _post(f"{base}/mktg-strategy-plan/create",
                            {"name": "  ", "selections_json": "[]"})
    assert code == 200
    saved = json.loads(resp_body)
    assert saved["name"] == "(無題プラン)"


def test_strategy_plan_create_route_handles_malformed_selections_json(server):
    """selections_jsonが壊れていても500にならず、空リストとして扱うこと。"""
    base, _ = server
    code, resp_body = _post(f"{base}/mktg-strategy-plan/create",
                            {"name": "壊れたJSON", "selections_json": "not json"})
    assert code == 200
    saved = json.loads(resp_body)
    assert saved["selections"] == []


def test_strategy_plan_delete_route(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    saved = sfa_db.create_mktg_strategy_plan(con, name="削除対象", selections=[])
    con.close()

    code, resp_body = _post(f"{base}/mktg-strategy-plan/{saved['id']}/delete", {})
    assert code == 200
    assert json.loads(resp_body) == {"ok": True}

    con2 = sfa_db.connect(db_path)
    rows = sfa_db.list_mktg_strategy_plans(con2)
    con2.close()
    assert all(r["id"] != saved["id"] for r in rows)
