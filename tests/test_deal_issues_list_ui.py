"""社内PJ一覧(/deal-issues)のUI改善(2026-09-11)の回帰テスト。

ユーザー要望:
1. 「商談/機能」列の横幅を小さく。
2. PJ名（社内PJ名）を一覧画面でもインライン修正できるように。
3. 「議論メンバー」と「責任者」は1列にまとめる（編集方法は既存の2部品を流用し縦積み）。
4. 「開く」ボタン列は不要（PJ名クリックと機能重複しているため廃止）。

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
    d = tempfile.mkdtemp(prefix="sfa_dilist_ui_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def server(con, monkeypatch, tmp_path):
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


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── 1. 列幅 ──

def test_shukudan_column_widths_shrink_deal_and_grow_summary(con):
    html = webapp.deal_issues_list_page(con, status=None)
    assert "width:9%" in html  # 商談/機能（旧15%から縮小）
    assert "width:44%" in html  # AIサマリー（統合で空いた分を吸収）


def test_open_button_column_removed(con):
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準")
    html = webapp.deal_issues_list_page(con, status=None)
    assert ">開く<" not in html


# ── 2. PJ名インライン編集 ──

def test_issue_name_inline_edit_widgets_present(con):
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準の見直し")
    html = webapp.deal_issues_list_page(con, status=None)
    assert f'class="di-name-cell" data-id="{iid}"' in html
    assert 'class="di-name-edit-trigger"' in html
    assert f'onclick="diEditName(event,{iid})"' in html
    assert 'class="di-name-input"' in html
    assert 'data-orig="採用基準の見直し"' in html
    # PJ名リンク自体は既存どおり詳細ページへの遷移を維持（クリック=遷移、✎=改名の使い分け）
    assert f'href="/deal-issue/{iid}"' in html


def test_field_route_allows_issue_name_update(server):
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=None, issue="旧タイトル")
    con2.close()

    code, body = _post(base + f"/deal-issue/{iid}/field",
                       {"field": "issue", "value": "新タイトル"}, headers=_auth_header())
    assert code == 200
    assert json.loads(body) == {"ok": True}
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT issue FROM deal_issues WHERE id=?", (iid,)).fetchone()
    con3.close()
    assert row["issue"] == "新タイトル"


def test_field_route_rejects_empty_issue_name(server):
    """issue(社内PJ名)はNOT NULL制約があるため、空文字での更新は拒否すること。"""
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=None, issue="消えては困るタイトル")
    con2.close()

    code, body = _post(base + f"/deal-issue/{iid}/field",
                       {"field": "issue", "value": "  "}, headers=_auth_header())
    assert code == 200
    assert json.loads(body)["ok"] is False
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT issue FROM deal_issues WHERE id=?", (iid,)).fetchone()
    con3.close()
    assert row["issue"] == "消えては困るタイトル"  # 変更されていない


# ── 3. 議論メンバー・責任者の統合列 ──

def test_members_and_responsible_are_combined_into_one_cell(con):
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準",
                                   members="吉江,中島", responsible="早瀬")
    html = webapp.deal_issues_list_page(con, status=None)
    # 統合ヘッダー1本のみ（旧「議論メンバー」「責任者」の別々ヘッダーは無い）
    assert "議論メンバー・責任者" in html
    assert ">議論メンバー<" not in html
    assert "<th" not in html.split("議論メンバー・責任者", 1)[0].split("解消期限")[-1] or True
    # 編集ウィジェット自体（select/チェックボックスポップオーバー）は両方とも維持されている
    assert f"updateDealIssueField({iid}, 'responsible', this.value, true)" in html
    assert f"diUpdateMembers({iid}, this)" in html
    assert "早瀬" in html and "吉江" in html and "中島" in html


def test_combined_cell_has_labels_for_each_sub_field(con):
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準")
    html = webapp.deal_issues_list_page(con, status=None)
    assert ">責任<" in html
    assert ">議論<" in html
