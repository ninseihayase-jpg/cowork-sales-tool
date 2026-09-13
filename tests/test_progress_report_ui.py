"""社内PJ「進捗報告」(2026-09-13)のページ描画・ルートの回帰テスト。

DB層のバージョニング規則自体はtests/test_progress_report.pyで検証済み。ここでは
webapp.progress_report_page()の描画と、/deal-issue/<id>/progress-report/open・
/deal-issue-progress-report/<id>/fieldルートの挙動（特に「過去verは読み取り専用」を
サーバ側でも強制していること）を検証する。

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
    d = tempfile.mkdtemp(prefix="sfa_pr_ui_")
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


def _get(url):
    req = urllib.request.Request(url, headers=_auth_header(), method="GET")
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.getcode(), resp.read()


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    h = _auth_header()
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.geturl(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, url, e.read()


def _issue(con, issue="論点A"):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X", status="open")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue=issue)


# ── ページ描画 ──

def test_page_shows_empty_state_when_no_report_exists(con):
    iid = _issue(con, issue="テストPJ")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.progress_report_page(con, issue)
    assert "進捗報告はまだありません" in html
    assert f'action="/deal-issue/{iid}/progress-report/open"' in html


def test_page_renders_all_ten_sections_in_order(con):
    iid = _issue(con)
    sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.progress_report_page(con, issue)
    order = ["② サマリー", "③ 今回の進捗", "prDecisionLabel", "スケジュール遅延", "予算超過",
             "品質", "対外関係", "法務・コンプライアンス", "その他", "⑥ 次回までの予定"]
    positions = [html.index(s) for s in order]
    assert positions == sorted(positions), "セクションの表示順が②③④⑤×6⑥になっていない"


def test_page_decision_label_reflects_purpose_tag(con):
    """表示中のラベル(<table>内、<script>より前の静的マークアップ)はpurpose_tagに応じて
    切り替わること。<script>内にはクライアント側の即時切替用にPR_DECISION_LABELSの全パターンが
    埋め込まれているため、そこは対象外とする（誤検知を避けるため静的部分だけを見る）。"""
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    sfa_db.update_progress_report(con, rep["id"], purpose_tag="decide")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.progress_report_page(con, issue)
    visible_html, _, _ = html.partition("<script>")
    assert "決めてほしいこと" in visible_html
    assert "論点・自分の意見" not in visible_html


def test_page_shows_version_tabs_when_multiple_versions_exist(con):
    iid = _issue(con)
    sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30")
    sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    issue = sfa_db.get_deal_issue(con, iid)
    html = webapp.progress_report_page(con, issue)
    assert "2026-08-30報告" in html
    assert "2026-09-06報告" in html


def test_latest_version_is_editable_past_version_is_readonly(con):
    iid = _issue(con)
    old = sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30")
    latest = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    issue = sfa_db.get_deal_issue(con, iid)

    latest_html = webapp.progress_report_page(con, issue, view_report_id=latest["id"])
    assert 'contenteditable="true"' in latest_html
    assert "過去の報告" not in latest_html

    old_html = webapp.progress_report_page(con, issue, view_report_id=old["id"])
    assert 'contenteditable="true"' not in old_html
    assert "過去の報告" in old_html
    assert "編集する" in old_html  # 編集へ誘導するボタンはある


# ── ルート: 編集開始（バージョニング）──

def test_open_route_creates_first_version(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    con.close()

    code, url, _ = _post(f"{base}/deal-issue/{iid}/progress-report/open", {})
    assert code == 200
    assert url == f"{base}/deal-issue/{iid}/progress-report"

    con2 = sfa_db.connect(db_path)
    rows = sfa_db.list_progress_reports(con2, iid)
    con2.close()
    assert len(rows) == 1


def test_open_route_does_not_fork_same_day(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    con.close()

    _post(f"{base}/deal-issue/{iid}/progress-report/open", {})
    _post(f"{base}/deal-issue/{iid}/progress-report/open", {})

    con2 = sfa_db.connect(db_path)
    rows = sfa_db.list_progress_reports(con2, iid)
    con2.close()
    assert len(rows) == 1


def test_open_route_missing_issue_does_not_crash(server):
    """存在しないissue_idでもサーバエラー(500)にならないこと。リダイレクト先の
    GET /deal-issue/<id>/progress-report は「社内PJが見つかりません」を404で返す
    （既存の/deal-issue/<id>と同じ規約）ため、ここでは404であることを確認する
    （クラッシュ(500)していないこと自体が主眼）。"""
    base, _ = server
    code, _, _ = _post(f"{base}/deal-issue/999999/progress-report/open", {})
    assert code == 404  # リダイレクト先ページの「社内PJが見つかりません」（500ではない）


# ── ルート: /field保存 ──

def test_field_route_saves_section_content(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    con.close()

    code, body = urllib.request.urlopen(
        urllib.request.Request(
            f"{base}/deal-issue-progress-report/{rep['id']}/field",
            data=urllib.parse.urlencode(
                {"field": "summary", "value": "<div>順調に進んでいます</div>"}).encode(),
            headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            method="POST"), timeout=10
    ).getcode(), None
    assert code == 200

    con2 = sfa_db.connect(db_path)
    fetched = sfa_db.get_progress_report(con2, rep["id"])
    con2.close()
    assert "順調に進んでいます" in fetched["summary_html"]


def test_field_route_sanitizes_section_content(server):
    """自由記述はrich_notesと同じサニタイザ(_sanitize_rich_html)を通すこと
    （scriptタグ等は保存されない）。"""
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    con.close()

    req = urllib.request.Request(
        f"{base}/deal-issue-progress-report/{rep['id']}/field",
        data=urllib.parse.urlencode(
            {"field": "progress", "value": '<script>alert(1)</script><div>本文</div>'}).encode(),
        headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    urllib.request.urlopen(req, timeout=10)

    con2 = sfa_db.connect(db_path)
    fetched = sfa_db.get_progress_report(con2, rep["id"])
    con2.close()
    assert "<script>" not in fetched["progress_html"]
    assert "本文" in fetched["progress_html"]


def test_field_route_saves_status_and_purpose(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    con.close()

    urllib.request.urlopen(urllib.request.Request(
        f"{base}/deal-issue-progress-report/{rep['id']}/field",
        data=urllib.parse.urlencode({"field": "status_signal", "value": "red"}).encode(),
        headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"), timeout=10)
    urllib.request.urlopen(urllib.request.Request(
        f"{base}/deal-issue-progress-report/{rep['id']}/field",
        data=urllib.parse.urlencode({"field": "purpose_tag", "value": "discuss"}).encode(),
        headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"), timeout=10)

    con2 = sfa_db.connect(db_path)
    fetched = sfa_db.get_progress_report(con2, rep["id"])
    con2.close()
    assert fetched["status_signal"] == "red"
    assert fetched["purpose_tag"] == "discuss"


def test_field_route_rejects_invalid_status_signal(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    con.close()

    req = urllib.request.Request(
        f"{base}/deal-issue-progress-report/{rep['id']}/field",
        data=urllib.parse.urlencode({"field": "status_signal", "value": "purple"}).encode(),
        headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=10)
    assert json.loads(resp.read())["ok"] is False


def test_field_route_rejects_edit_of_past_version(server):
    """過去verへの直POSTはサーバ側でも拒否すること（UIを迂回しても書き換えられない）。"""
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    old = sfa_db.open_progress_report_for_edit(con, iid, today="2026-08-30")
    sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")  # 新verにフォーク
    con.close()

    req = urllib.request.Request(
        f"{base}/deal-issue-progress-report/{old['id']}/field",
        data=urllib.parse.urlencode({"field": "summary", "value": "改ざん"}).encode(),
        headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    assert result["ok"] is False

    con2 = sfa_db.connect(db_path)
    fetched = sfa_db.get_progress_report(con2, old["id"])
    con2.close()
    assert "改ざん" not in fetched["summary_html"]


def test_field_route_rejects_unknown_field(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con)
    rep = sfa_db.open_progress_report_for_edit(con, iid, today="2026-09-06")
    con.close()

    req = urllib.request.Request(
        f"{base}/deal-issue-progress-report/{rep['id']}/field",
        data=urllib.parse.urlencode({"field": "not_a_field", "value": "x"}).encode(),
        headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=10)
    assert json.loads(resp.read())["ok"] is False


# ── /deal-issue/<id>/edit との経路衝突が起きていないことの確認 ──
# (path.endswith("/edit")で先にマッチする既存ルートと"/progress-report/open"が衝突しないこと)

def test_progress_report_open_route_does_not_trigger_issue_edit_form_handler(server):
    """/deal-issue/<id>/progress-report/open へのPOSTが、社内PJ編集フォーム
    （/deal-issue/<id>/edit ハンドラ）に誤って捕まらないこと。誤爆した場合、issue名が
    必須のedit処理に巻き込まれてリダイレクト先が変わる、あるいはissueが空文字になり
    エラーになる。"""
    base, db_path = server
    con = sfa_db.connect(db_path)
    iid = _issue(con, issue="衝突確認用PJ")
    con.close()

    code, url, _ = _post(f"{base}/deal-issue/{iid}/progress-report/open", {})
    assert code == 200
    assert url == f"{base}/deal-issue/{iid}/progress-report"

    con2 = sfa_db.connect(db_path)
    still_named = sfa_db.get_deal_issue(con2, iid)
    con2.close()
    assert still_named["issue"] == "衝突確認用PJ"  # issue名が意図せず変更/消去されていないこと
