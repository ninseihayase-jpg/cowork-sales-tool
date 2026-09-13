"""社内報告（見せながら報告）ページ(2026-09-13)の回帰テスト。

ユーザー要望:「ガントと『進捗報告』を見せながら、社内報告できるような機能...
指摘事項のメモを取れるようにする。それは通常の『社内PJメモ』として記録される」。

設計: ガントはJSグローバル/モーダルidの衝突を避けるため<iframe>で埋め込む。フリーメモは
新規の編集面を作らず、既存のrnOpen('issue', id)（社内PJメモの共有ノートブック）をそのまま
起動するボタンのみを置く。

一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import base64
import shutil
import tempfile
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_report_session_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _issue(con, issue="論点A"):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X", status="open")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue=issue)


def test_page_embeds_gantt_via_iframe_not_inline(con):
    """ガント自身のJSグローバル(IG_ITEMS等)・モーダル(#igPop等)との衝突を避けるため、
    <iframe>での埋め込みであり、ガントページのHTMLをインライン展開したものではないこと。"""
    iid = _issue(con)
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.report_session_page(con, issue)
    assert '<iframe src="/deal-issues/gantt"' in html
    assert "IG_ITEMS" not in html  # ガント自身のJSがインライン展開されていないこと
    assert 'id="igPop"' not in html


def test_page_shows_create_report_prompt_when_none_exists(con):
    iid = _issue(con, issue="レポ無しPJ")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.report_session_page(con, issue)
    assert "進捗報告はまだありません" in html
    assert f'href="/deal-issue/{iid}/progress-report"' in html


def test_page_shows_latest_report_readonly(con):
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    sfa_db.update_progress_report(
        con, rep["id"], sections={"summary": "<div>順調に進んでいます</div>"})
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.report_session_page(con, issue)
    assert "順調に進んでいます" in html
    assert 'contenteditable="true"' not in html  # 社内報告の場では読み取り表示のみ


def test_page_has_free_memo_button_wired_to_existing_rich_note_feature(con):
    """フリーメモは新規実装せず、既存のrnOpen('issue', id)を起動するボタンのみであること
    （2026-09-13ユーザー要望:「それは通常の社内PJメモとして記録される」）。"""
    iid = _issue(con)
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.report_session_page(con, issue)
    assert f"rnOpen('issue',{iid})" in html
    assert "社内PJメモを開いて記入" in html


def test_page_links_back_to_issue_and_to_progress_report(con):
    iid = _issue(con)
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.report_session_page(con, issue)
    assert f'href="/deal-issue/{iid}"' in html
    assert f'href="/deal-issue/{iid}/progress-report"' in html


# ── ルート ──

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


def test_report_session_route_renders(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con, issue="ルート検証PJ")
    con.close()

    req = urllib.request.Request(
        f"{base}/deal-issue/{iid}/report-session", headers=_auth_header(), method="GET")
    resp = urllib.request.urlopen(req, timeout=10)
    assert resp.getcode() == 200
    body = resp.read().decode("utf-8")
    assert "ルート検証PJ" in body
    assert "<iframe" in body


def test_report_session_route_missing_issue_returns_404(server):
    base, _ = server
    req = urllib.request.Request(
        f"{base}/deal-issue/999999/report-session", headers=_auth_header(), method="GET")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "404を期待"
    except urllib.error.HTTPError as e:
        assert e.code == 404
