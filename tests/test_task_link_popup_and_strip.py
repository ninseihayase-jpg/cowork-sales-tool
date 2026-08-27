"""コンサルタスクの関連付けUI強化（ユーザー要望2026-08-27）の回帰テスト。

- カード上の「🔗関連」ポップアップから商談/論点/Delivery/開発案件へ紐づけできる
  （POST /task/{id}/link で1リクエストにまとめて保存）。
- 看板上部に「紐づけられている案件」を商談/論点/Deliveryで分けて表示（未完了タスクが
  1件も無い紐づけ先=完了にしか登場しない案件は表示しない）。
- 紐づけ先フィルタに「紐づけ無しのみ」(__none__)を追加。
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
    d = tempfile.mkdtemp(prefix="sfa_link_popup_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def deal_issue_delivery(con):
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="論点A")
    dv = sfa_db.create_delivery(con, deal_id=did, title="DeliveryA")
    return did, iid, dv


# ── 関連付けポップアップ ─────────────────────────────────────────────────

def test_tasks_page_card_has_link_popup_trigger_and_data_attrs(con, deal_issue_delivery):
    did, _iid, _dv = deal_issue_delivery
    tid = sfa_db.upsert_task(con, title="X", link_type="deal", link_id=did, status="未着手")
    html = webapp.tasks_page(con)
    assert f'data-link-type="deal" data-link-id="{did}"' in html
    assert "function openLinkPop" in html
    assert "function saveLinkPop" in html
    assert 'id="linkPop"' in html
    assert 'id="linkBackdrop"' in html


def test_tasks_page_card_without_link_shows_add_button(con):
    sfa_db.upsert_task(con, title="X", status="未着手")
    html = webapp.tasks_page(con)
    assert "🔗 関連" in html


# ── /task/{id}/link ルート ───────────────────────────────────────────────

@pytest.fixture
def server(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", BASIC_USER)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", BASIC_PASS)
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


def test_task_link_route_persists_delivery_link(server):
    import json
    base, db_path = server
    con = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    dv = sfa_db.create_delivery(con, deal_id=did, title="DeliveryA")
    tid = sfa_db.upsert_task(con, title="X", status="未着手")
    con.close()

    code, body = _post(base + f"/task/{tid}/link", {"link_type": "delivery", "link_id": str(dv)},
                       headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["link_type"] == "delivery"
    assert data["link_id"] == dv
    assert "DeliveryA" in data["label"]
    assert data["href"] == f"/delivery/{dv}"
    assert data["icon"] == "🚚"

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT link_type, link_id FROM tasks WHERE id=?", (tid,)).fetchone()
    con2.close()
    assert row["link_type"] == "delivery" and row["link_id"] == dv


def test_task_link_route_clears_link_when_empty(server):
    import json
    base, db_path = server
    con = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    tid = sfa_db.upsert_task(con, title="X", link_type="deal", link_id=did, status="未着手")
    con.close()

    code, body = _post(base + f"/task/{tid}/link", {"link_type": "", "link_id": ""},
                       headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["link_type"] is None
    assert data["label"] is None

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT link_type, link_id FROM tasks WHERE id=?", (tid,)).fetchone()
    con2.close()
    assert row["link_type"] is None and row["link_id"] is None


def test_task_link_route_rejects_invalid_kind(server):
    import json
    base, db_path = server
    con = sfa_db.connect(db_path)
    tid = sfa_db.upsert_task(con, title="X", status="未着手")
    con.close()

    code, body = _post(base + f"/task/{tid}/link", {"link_type": "でたらめ", "link_id": "1"},
                       headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is False


def test_task_link_route_dev_project(server):
    import json
    base, db_path = server
    con = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    dp = sfa_db.upsert_dev_project(con, deal_id=did, theme="AI基盤")
    tid = sfa_db.upsert_task(con, title="X", status="未着手")
    con.close()

    code, body = _post(base + f"/task/{tid}/link", {"link_type": "dev_project", "link_id": str(dp)},
                       headers=_auth_header())
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["link_type"] == "dev_project"
    assert data["label"] == "AI基盤"
    assert data["icon"] == "🛠"


# ── 紐づけられている案件ストリップ ─────────────────────────────────────────

def test_task_link_summary_excludes_entities_with_only_completed_tasks(con, deal_issue_delivery):
    did, iid, dv = deal_issue_delivery
    sfa_db.upsert_task(con, title="開いてるタスク", link_type="deal", link_id=did, status="未着手")
    sfa_db.upsert_task(con, title="完了のみ論点", link_type="issue", link_id=iid, status="完了")
    sfa_db.upsert_task(con, title="Delivery進行中", link_type="delivery", link_id=dv, status="対応中")

    summary = sfa_db.task_link_summary(con)
    assert len(summary["deal"]) == 1 and summary["deal"][0]["id"] == did
    assert summary["issue"] == []  # 完了しかない=除外
    assert len(summary["delivery"]) == 1 and summary["delivery"][0]["id"] == dv


def test_task_link_summary_excludes_admin_tasks(con, deal_issue_delivery):
    did, _iid, _dv = deal_issue_delivery
    sfa_db.upsert_task(con, title="事務タスク", link_type="deal", link_id=did, status="未着手", is_admin=1)
    summary = sfa_db.task_link_summary(con)
    assert summary["deal"] == []


def test_tasks_page_shows_link_strip_split_by_kind(con, deal_issue_delivery):
    did, iid, dv = deal_issue_delivery
    sfa_db.upsert_task(con, title="開いてるタスク", link_type="deal", link_id=did, status="未着手")
    sfa_db.upsert_task(con, title="完了のみ論点", link_type="issue", link_id=iid, status="完了")
    sfa_db.upsert_task(con, title="Delivery進行中", link_type="delivery", link_id=dv, status="対応中")
    html = webapp.tasks_page(con)

    import re
    strips = re.findall(r'<div class="pj-strip">.*?</div>', html)
    combined = "\n".join(strips)
    assert "商談:" in combined and "Delivery:" in combined
    assert "論点:" not in combined  # 完了しかない論点は表示されない
    assert f"link_type=deal&link_id={did}" in combined
    assert f"link_type=delivery&link_id={dv}" in combined


def test_tasks_page_link_strip_omitted_when_nothing_linked(con):
    sfa_db.upsert_task(con, title="X", status="未着手")
    html = webapp.tasks_page(con)
    import re
    strips = [s for s in re.findall(r'<div class="pj-strip">.*?</div>', html) if "商談:" in s or "論点:" in s or "Delivery:" in s]
    assert strips == []


# ── 紐づけ無しのみフィルタ ────────────────────────────────────────────────

def test_list_tasks_none_filter_returns_only_unlinked(con, deal_issue_delivery):
    did, _iid, _dv = deal_issue_delivery
    linked_id = sfa_db.upsert_task(con, title="紐づけあり", link_type="deal", link_id=did)
    unlinked_id = sfa_db.upsert_task(con, title="紐づけ無し")
    ids = {t["id"] for t in sfa_db.list_tasks(con, link_type="__none__")}
    assert unlinked_id in ids
    assert linked_id not in ids


def test_tasks_page_none_filter_query_param(con, deal_issue_delivery):
    did, _iid, _dv = deal_issue_delivery
    sfa_db.upsert_task(con, title="紐づけありタスク", link_type="deal", link_id=did, status="未着手")
    sfa_db.upsert_task(con, title="紐づけ無しタスク", status="未着手")
    html = webapp.tasks_page(con, link_type="__none__")
    assert "紐づけ無しタスク" in html
    assert "紐づけありタスク" not in html


def test_task_link_picker_offers_none_filter_option_only_when_requested(con):
    filter_html = webapp._task_link_picker_html(con, prefix="x", cur_type=None, cur_id=None,
                                                 allow_none_filter=True)
    assert "紐づけ無しのみ" in filter_html
    form_html = webapp._task_link_picker_html(con, prefix="y", cur_type=None, cur_id=None)
    assert "紐づけ無しのみ" not in form_html
