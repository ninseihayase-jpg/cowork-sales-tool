"""コンサルタスクを商談/論点に紐づける機能（ユーザー要望2026-08-24）の回帰テスト。

tasks.link_type/link_id は元々dev_project向けに用意済みの汎用カラム（コメントで
deal/issue/org/personalも許容値と明記済み）。今回はUIとしてdeal/issueを追加した。
- タスク編集フォーム(task_form)で商談/論点を検索して紐づけられる（_task_link_picker_html）
- /tasks/save が link_type/link_id を保存する
- カード上に紐づけ先が表示される
- /tasks の一覧を紐づけ先(link_type/link_id)で絞り込める
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
    d = tempfile.mkdtemp(prefix="sfa_task_link_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def deal_and_issue(con):
    acc = sfa_db.upsert_account(con, name="ソラスト")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="価格交渉の論点")
    return did, iid


def test_task_link_label_deal_and_issue(con, deal_and_issue):
    did, iid = deal_and_issue
    assert sfa_db.task_link_label(con, "deal", did) == "ソラスト：成果報酬コスト削減"
    assert sfa_db.task_link_label(con, "issue", iid) == "価格交渉の論点（ソラスト：成果報酬コスト削減）"
    assert sfa_db.task_link_label(con, "deal", 999999) is None  # 消えていたらNone
    assert sfa_db.task_link_label(con, None, None) is None


def test_task_link_label_issue_without_deal():
    """deal_idがNULLの共通論点（商談に紐づかない論点）でも表示できること。"""
    d = tempfile.mkdtemp(prefix="sfa_task_link2_")
    try:
        path = str(Path(d) / "t.db")
        sfa_db.init_db(path)
        con = sfa_db.connect(path)
        iid = sfa_db.upsert_deal_issue(con, issue="共通論点")
        assert sfa_db.task_link_label(con, "issue", iid) == "共通論点（商談共通）"
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_list_tasks_filters_by_link(con, deal_and_issue):
    did, iid = deal_and_issue
    sfa_db.upsert_task(con, title="A", link_type="deal", link_id=did)
    sfa_db.upsert_task(con, title="B", link_type="issue", link_id=iid)
    sfa_db.upsert_task(con, title="C")
    by_deal = sfa_db.list_tasks(con, link_type="deal", link_id=did)
    assert [t["title"] for t in by_deal] == ["A"]
    by_issue = sfa_db.list_tasks(con, link_type="issue", link_id=iid)
    assert [t["title"] for t in by_issue] == ["B"]


def test_tasks_page_shows_link_chip_on_card(con, deal_and_issue):
    did, iid = deal_and_issue
    sfa_db.upsert_task(con, title="商談紐づけ", link_type="deal", link_id=did)
    sfa_db.upsert_task(con, title="論点紐づけ", link_type="issue", link_id=iid)
    html = webapp.tasks_page(con)
    assert f'href="/deal/{did}"' in html
    assert f'href="/deal-issue/{iid}"' in html
    assert "🤝ソラスト：成果報酬コスト削減" in html
    assert "📌価格交渉の論点" in html


def test_tasks_page_filters_by_link(con, deal_and_issue):
    did, iid = deal_and_issue
    sfa_db.upsert_task(con, title="商談紐づけタスク", link_type="deal", link_id=did)
    sfa_db.upsert_task(con, title="論点紐づけタスク", link_type="issue", link_id=iid)
    sfa_db.upsert_task(con, title="紐づけなしタスク")
    html = webapp.tasks_page(con, link_type="deal", link_id=did)
    assert "商談紐づけタスク" in html
    assert "論点紐づけタスク" not in html
    assert "紐づけなしタスク" not in html


def test_task_form_offers_deal_and_issue_search_and_prefills_current_link(con, deal_and_issue):
    """#146(2026-09-02): 関連付けは複数可になり、既存の紐づけはチップ一覧
    (window.TF_LINKS→tfLinksList)として表示される。ピッカー自体は「追加する1件」を選ぶ
    ためのUIになり、name属性は持たない(emit_name_attrs=False)。"""
    did, iid = deal_and_issue
    tid = sfa_db.upsert_task(con, title="論点タスク", link_type="issue", link_id=iid)
    html = webapp.task_form(con, sfa_db.get_task(con, tid))
    assert 'name="link_type"' not in html and 'name="link_id"' not in html
    assert 'id="tfLinksList"' in html
    assert f'"type": "issue", "id": {iid}' in html
    assert "価格交渉の論点（ソラスト：成果報酬コスト削減）" in html
    assert 'name="links_json"' in html


def test_task_form_no_link_by_default(con):
    tid = sfa_db.upsert_task(con, title="未紐づけ")
    html = webapp.task_form(con, sfa_db.get_task(con, tid))
    assert "window.TF_LINKS = []" in html
    assert 'id="tfLinkType"' in html and 'id="tfLinkId"' in html
    assert 'name="link_type"' not in html and 'name="link_id"' not in html


# ── route ──

@pytest.fixture
def server(con, monkeypatch, tmp_path):
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


def test_tasks_save_route_persists_deal_link(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('ソラスト')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
    con.close()

    code, _ = _post(base + "/tasks/save", {
        "title": "紐づけ保存テスト", "link_type": "deal", "link_id": str(did),
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM tasks WHERE title=?", ("紐づけ保存テスト",)).fetchone()
    assert row["link_type"] == "deal" and row["link_id"] == did
    con2.close()


def test_tasks_save_route_ignores_invalid_link_type(server):
    base, db_path = server
    code, _ = _post(base + "/tasks/save", {
        "title": "不正値テスト", "link_type": "でたらめ", "link_id": "1",
    }, headers=_auth_header())
    assert code in (200, 303)

    con = sfa_db.connect(db_path)
    row = con.execute("SELECT * FROM tasks WHERE title=?", ("不正値テスト",)).fetchone()
    assert row["link_type"] is None and row["link_id"] is None
    con.close()


# ── Delivery紐づけ（ユーザー要望2026-08-27） ─────────────────────────────

@pytest.fixture
def delivery(con, deal_and_issue):
    did, _iid = deal_and_issue
    dv = sfa_db.create_delivery(con, deal_id=did, title="コスト削減稼働案件")
    return dv


def test_task_link_label_delivery(con, delivery):
    assert sfa_db.task_link_label(con, "delivery", delivery) == "ソラスト：コスト削減稼働案件"


def test_task_form_offers_delivery_search(con, delivery):
    task = {"link_type": "delivery", "link_id": delivery}
    html = webapp.task_form(con, task)
    assert "DeliveryWrap" in html
    assert "コスト削減稼働案件" in html


def test_tasks_page_shows_delivery_link_chip_on_card(con, delivery):
    sfa_db.upsert_task(con, title="Delivery紐づけタスク", link_type="delivery", link_id=delivery,
                       assignee="早瀬", status="未着手")
    html = webapp.tasks_page(con)
    assert "🚚" in html
    assert "コスト削減稼働案件" in html
    assert "/delivery/" in html


def test_tasks_save_route_persists_delivery_link(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    acc = con.execute("INSERT INTO accounts(name) VALUES('ソラスト')").lastrowid
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="成果報酬コスト削減", stage="提案")
    dv = sfa_db.create_delivery(con, deal_id=did, title="稼働案件A")
    con.close()

    code, _ = _post(base + "/tasks/save", {
        "title": "Delivery紐づけ保存テスト", "link_type": "delivery", "link_id": str(dv),
    }, headers=_auth_header())
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT * FROM tasks WHERE title=?", ("Delivery紐づけ保存テスト",)).fetchone()
    assert row["link_type"] == "delivery" and row["link_id"] == dv
    con2.close()
