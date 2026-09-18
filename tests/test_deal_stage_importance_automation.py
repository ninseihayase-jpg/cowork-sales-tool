"""ステージ/重要度の自動化(#181, 2026-09-18)の回帰テスト。

ユーザー要望:
1. Closeした案件は、Close理由に基づいて自動的にステージ変更してほしい
   （失注→失注／保留・時期尚早→保留中／ニーズなし・キャンセル・自社都合で撤退→他Closed）。
2. Closeされた場合、自動的に重要度を「Closed」にしてほしい。
3. ステージが「提案」「クロージング」に進んだ場合、重要度は自動的に「高」に変更される
   仕様にしたい。手動変更できることは変わらない（＝ステージが変わらない再保存では上書きしない）。

#67の教訓（直接変更だと本処理がスキップされるバグ）を踏まえ、ステージを変更しうる
複数の経路（/deal/save・/deal/{id}/field・/deals/bulk_edit・/hearing/intake/commit）
すべてで同じ挙動になることを確認する。
"""
from __future__ import annotations

import base64
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
    d = tempfile.mkdtemp(prefix="sfa_stage_imp_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def acc_id(con):
    return con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid


def _deal(con, acc_id, stage, **kw):
    return sfa_db.upsert_deal(con, account_id=acc_id, deal_name=kw.pop("name", "D"),
                              stage=stage, status=kw.pop("status", "open"), **kw)


# ── close_reason → stage の自動対応 ──

@pytest.mark.parametrize("reason,expected_stage", [
    ("失注", "失注"),
    ("保留・時期尚早", "保留中"),
    ("ニーズなし", "他Closed"),
    ("キャンセル", "他Closed"),
    ("自社都合で撤退", "他Closed"),
])
def test_close_deal_to_lead_maps_close_reason_to_stage(con, acc_id, reason, expected_stage):
    did = _deal(con, acc_id, "クロージング")
    sfa_db.close_deal_to_lead(con, did, reason)
    deal = sfa_db.get_deal(con, did)
    assert deal["status"] == "closed"
    assert deal["close_reason"] == reason
    assert deal["stage"] == expected_stage


def test_close_deal_to_lead_sets_importance_closed(con, acc_id):
    did = _deal(con, acc_id, "提案", importance="高")
    sfa_db.close_deal_to_lead(con, did, "ニーズなし")
    deal = sfa_db.get_deal(con, did)
    assert deal["importance"] == "Closed"


def test_close_won_if_needed_sets_importance_closed(con, acc_id):
    did = _deal(con, acc_id, "受注", importance="高")
    assert sfa_db.close_won_if_needed(con, did, commit=True) is True
    deal = sfa_db.get_deal(con, did)
    assert deal["status"] == "closed"
    assert deal["stage"] == "受注"   # 受注は保持（クロージング処理と別軸）
    assert deal["importance"] == "Closed"


# ── ステージ→重要度「高」の自動ブースト ──

def test_bump_importance_fires_on_transition_into_proposal(con, acc_id):
    did = _deal(con, acc_id, "要件詰め", importance="低")
    fired = sfa_db.bump_importance_on_stage_change(con, did, "要件詰め", "提案", commit=True)
    assert fired is True
    assert sfa_db.get_deal(con, did)["importance"] == "高"


def test_bump_importance_fires_on_transition_into_closing(con, acc_id):
    did = _deal(con, acc_id, "提案", importance="中")
    fired = sfa_db.bump_importance_on_stage_change(con, did, "提案", "クロージング", commit=True)
    assert fired is True
    assert sfa_db.get_deal(con, did)["importance"] == "高"


def test_bump_importance_does_not_fire_without_stage_transition(con, acc_id):
    """同じステージのまま再保存した場合は発火しない＝その間の手動変更を踏みつけない
    （ユーザー要望「手動変更できることは変わらない」）。"""
    did = _deal(con, acc_id, "提案", importance="低")
    fired = sfa_db.bump_importance_on_stage_change(con, did, "提案", "提案", commit=True)
    assert fired is False
    assert sfa_db.get_deal(con, did)["importance"] == "低"


def test_bump_importance_does_not_fire_for_other_stages(con, acc_id):
    did = _deal(con, acc_id, "初回アポ実施", importance="低")
    fired = sfa_db.bump_importance_on_stage_change(con, did, "初回アポ実施", "要件詰め", commit=True)
    assert fired is False
    assert sfa_db.get_deal(con, did)["importance"] == "低"


# ── reopen_deal の対称動作 ──

def test_reopen_deal_resets_close_only_stage_and_bumps_importance(con, acc_id):
    did = _deal(con, acc_id, "提案")
    sfa_db.close_deal_to_lead(con, did, "失注")
    deal = sfa_db.get_deal(con, did)
    assert deal["stage"] == "失注" and deal["importance"] == "Closed"

    sfa_db.reopen_deal(con, did)
    deal2 = sfa_db.get_deal(con, did)
    assert deal2["status"] == "open"
    assert deal2["stage"] == "提案"       # クローズ専用ステージから復帰
    assert deal2["importance"] == "高"    # 提案への遷移で自動ブースト


def test_reopen_deal_keeps_won_stage_but_clears_closed_importance(con, acc_id):
    did = _deal(con, acc_id, "受注")
    sfa_db.close_won_if_needed(con, did, commit=True)
    deal = sfa_db.get_deal(con, did)
    assert deal["stage"] == "受注" and deal["importance"] == "Closed"

    sfa_db.reopen_deal(con, did)
    deal2 = sfa_db.get_deal(con, did)
    assert deal2["status"] == "open"
    assert deal2["stage"] == "受注"       # 受注ステージは保持
    assert deal2["importance"] is None    # Closedは解除（受注は自動高ブースト対象外）


def test_reopen_deal_does_not_touch_manually_set_importance(con, acc_id):
    """importanceが'Closed'（自動付与）以外の場合は、reopenでも手動値を尊重する。"""
    did = _deal(con, acc_id, "受注")
    sfa_db.close_won_if_needed(con, did, commit=True)
    con.execute("UPDATE deals SET importance='低' WHERE id=?", (did,))
    con.commit()

    sfa_db.reopen_deal(con, did)
    assert sfa_db.get_deal(con, did)["importance"] == "低"


# ── HTTPルート経由（複数の経路で同一挙動になることの確認, #67の教訓） ──

@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_stage_imp_http_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    return p


@pytest.fixture
def server(db_path, monkeypatch):
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", BASIC_USER)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", BASIC_PASS)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _auth_header():
    token = base64.b64encode(f"{BASIC_USER}:{BASIC_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_deal_field_route_bumps_importance_on_stage_transition(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="要件詰め", importance="低")
    con.close()

    code, _ = _post(server + f"/deal/{did}/field", {"field": "stage", "value": "提案"},
                    headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["stage"] == "提案"
    assert deal["importance"] == "高"
    con2.close()


def test_deal_field_route_does_not_rebump_importance_on_same_stage_resave(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案", importance="低")
    con.close()

    code, _ = _post(server + f"/deal/{did}/field", {"field": "stage", "value": "提案"},
                    headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["stage"] == "提案"
    assert deal["importance"] == "低", "ステージが変わらない再保存で手動値が上書きされている"
    con2.close()


def test_deals_bulk_edit_bumps_importance_on_stage_transition(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    d1 = sfa_db.upsert_deal(con, account_id=acc, deal_name="D1", stage="要件詰め", importance="低")
    d2 = sfa_db.upsert_deal(con, account_id=acc, deal_name="D2", stage="初回アポ実施", importance="中")
    con.close()

    code, _ = _post(server + "/deals/bulk_edit", {
        "ids": [str(d1), str(d2)], "field": "stage", "value": "クロージング",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    assert sfa_db.get_deal(con2, d1)["importance"] == "高"
    assert sfa_db.get_deal(con2, d2)["importance"] == "高"
    con2.close()


def test_deal_save_route_bumps_importance_on_stage_transition(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="要件詰め", importance="低")
    con.close()

    code, _ = _post(server + "/deal/save", {
        "id": str(did), "account_id": str(acc), "deal_name": "D", "stage": "提案",
        "importance": "低",  # フォームは保存前の表示値をそのまま送る想定
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["stage"] == "提案"
    assert deal["importance"] == "高", "フォーム保存経由でもステージ遷移時の自動ブーストが働くこと"
    con2.close()


def test_deal_field_route_close_reason_maps_to_stage(server, db_path):
    """#26のバックフィルUI等から終了理由を直接付与するインライン編集でも、
    ステージが自動対応すること。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="クロージング", status="closed")
    con.close()

    code, _ = _post(server + f"/deal/{did}/field", {"field": "close_reason", "value": "キャンセル"},
                    headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["close_reason"] == "キャンセル"
    assert deal["stage"] == "他Closed"
    con2.close()
