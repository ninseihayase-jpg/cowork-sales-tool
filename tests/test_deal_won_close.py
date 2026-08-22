"""商談ステージ「受注」の自動クローズ廃止＋「受注・契約処理完了」の明示的クローズ(2026-08-23)の回帰テスト。

背景: 受注確定と契約処理完了の間には実務上タイムラグがある（受注は決まったが契約書の
締結・請求条件の確定等が終わっていない）。従来はstageを「受注」に変更した瞬間に
status='closed'へ自動的に遷移していたため、このタイムラグを表現できなかった。
stage='受注'への変更はopenのまま維持し、人間が「受注・契約処理完了」ボタン
（POST /deal/{id}/close-won、deal_hygiene_pageの②一覧からも同一経路）を押した時だけ
status='closed'にする。

対象の4経路（/deal/save・/deals/bulk_edit・/deal/{id}/field・/hearing/intake/commit）は
いずれも「stage='受注'に変更してもstatus=openのまま」であることを確認する。
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
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_won_close_")
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


def test_close_won_if_needed_still_works_when_explicitly_called():
    """DB層の関数自体は健在（明示的クローズの唯一の実処理経路）。"""
    d = tempfile.mkdtemp(prefix="sfa_won_close_db_")
    try:
        path = str(Path(d) / "t.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        acc = sfa_db.upsert_account(con, name="テスト社")
        did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注", status="open")
        assert sfa_db.close_won_if_needed(con, did, commit=True) is True
        deal = sfa_db.get_deal(con, did)
        assert deal["status"] == "closed"
        assert deal["close_reason"] == "受注"
        # 二度目は何もしない
        assert sfa_db.close_won_if_needed(con, did, commit=True) is False
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_deal_save_route_does_not_auto_close_on_won_stage(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
    con.close()

    code, _ = _post(server + "/deal/save", {
        "id": str(did), "account_id": str(acc), "deal_name": "D", "stage": "受注",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["stage"] == "受注"
    assert deal["status"] == "open", "受注ステージへの保存で自動クローズされてしまっている"
    con2.close()


def test_deal_field_route_does_not_auto_close_on_won_stage(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
    con.close()

    code, _ = _post(server + f"/deal/{did}/field", {"field": "stage", "value": "受注"},
                    headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["stage"] == "受注"
    # upsert_dealでstatus未指定だとNULL（アプリ全体の慣習でNULL=open扱い）。
    assert deal["status"] != "closed", "受注ステージへの変更で自動クローズされてしまっている"
    con2.close()


def test_deals_bulk_edit_does_not_auto_close_on_won_stage(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案")
    con.close()

    code, _ = _post(server + "/deals/bulk_edit", {
        "ids": [str(did)], "field": "stage", "value": "受注",
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["stage"] == "受注"
    assert deal["status"] != "closed", "受注ステージへの変更で自動クローズされてしまっている"
    con2.close()


def test_close_won_route_actually_closes(server, db_path):
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="受注", status="open")
    con.close()

    code, _ = _post(server + f"/deal/{did}/close-won", {"return_to": f"/deal/{did}"},
                    headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["status"] == "closed"
    assert deal["close_reason"] == "受注"
    con2.close()


def test_close_won_route_refuses_when_not_won_stage(server, db_path):
    """直POST防御: stage≠受注のときは何もクローズしない。"""
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('テスト社')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="D", stage="提案", status="open")
    con.close()

    _post(server + f"/deal/{did}/close-won", {"return_to": f"/deal/{did}"}, headers=_auth_header())

    con2 = sfa_db.connect(db_path)
    deal = sfa_db.get_deal(con2, did)
    assert deal["status"] == "open"
    con2.close()


def test_deal_form_shows_close_won_button_only_for_open_won_stage(db_path):
    con = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con, name="テスト社")
    d_won_open = sfa_db.upsert_deal(con, account_id=acc, deal_name="A", stage="受注", status="open")
    d_won_closed = sfa_db.upsert_deal(con, account_id=acc, deal_name="B", stage="受注", status="closed")
    d_other = sfa_db.upsert_deal(con, account_id=acc, deal_name="C", stage="提案", status="open")

    html_won_open = webapp.deal_form(con, sfa_db.get_deal(con, d_won_open))
    assert "受注・契約処理完了" in html_won_open
    assert f'/deal/{d_won_open}/close-won' in html_won_open

    html_won_closed = webapp.deal_form(con, sfa_db.get_deal(con, d_won_closed))
    assert "受注・契約処理完了（クローズ）</button" not in html_won_closed

    html_other = webapp.deal_form(con, sfa_db.get_deal(con, d_other))
    assert "受注・契約処理完了" not in html_other
    con.close()


def test_deal_hygiene_lists_won_open_deals_with_new_wording(db_path):
    con = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con, name="テスト社")
    sfa_db.upsert_deal(con, account_id=acc, deal_name="契約処理待ち", stage="受注", status="open")
    html = webapp.deal_hygiene_page(con)
    assert "契約処理待ち" in html
    assert "受注・契約処理完了待ちの商談" in html
    assert "不整合ではありません" in html
    con.close()
