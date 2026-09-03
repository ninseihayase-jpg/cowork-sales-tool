"""論点(deal_issues)の会社機能（#147, 2026-09-02）の回帰テスト。

ユーザー確定仕様:
- 論点を商談に紐づけない場合、社内のどの機能（経営企画/総務/法務/人事/財務/経理）に
  紐づくかを選択できる。選択肢はマスタ画面(company_functionsマスタ)で編集可能。
- 論点一覧の一番左列は「商談」→「商談/機能」に変更し、商談紐づけが無ければ会社機能を表示。
- 一覧のフィルタにも会社機能を追加。
- コンサルタスク側でも会社機能フラグを活用できるよう、紐づく論点(issue)にcompany_function
  が設定されているタスクを絞り込めるフィルタを/tasksに新設（task_link_labelの表示にも反映）。
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
    d = tempfile.mkdtemp(prefix="sfa_issue_cf_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


# ── sfa_db層 ──

def test_company_functions_master_has_default_values(con):
    assert sfa_db.get_master_list(con, "company_functions") == sfa_db.COMPANY_FUNCTIONS
    assert "経営企画" in sfa_db.COMPANY_FUNCTIONS and "経理" in sfa_db.COMPANY_FUNCTIONS


def test_upsert_deal_issue_with_company_function(con):
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準の見直し",
                                   company_function="人事")
    it = sfa_db.get_deal_issue(con, iid)
    assert it["deal_id"] is None
    assert it["company_function"] == "人事"


def test_list_deal_issues_filters_by_company_function(con):
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準", company_function="人事")
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="決算対応", company_function="経理")
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="無所属論点")

    hr_only = sfa_db.list_deal_issues(con, company_function="人事")
    assert [i["issue"] for i in hr_only] == ["採用基準"]
    all_issues = sfa_db.list_deal_issues(con)
    assert len(all_issues) == 3


def test_task_link_label_issue_prefers_company_function_over_common(con):
    """#147: 商談共通論点のラベルは、company_functionがあればそれを使う
    （無ければ従来通り「商談共通」）。"""
    iid_cf = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準",
                                      company_function="人事")
    iid_plain = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="無所属論点")
    assert sfa_db.task_link_label(con, "issue", iid_cf) == "採用基準（人事）"
    assert sfa_db.task_link_label(con, "issue", iid_plain) == "無所属論点（商談共通）"


def test_list_tasks_filters_by_issue_company_function(con):
    iid_hr = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準",
                                      company_function="人事")
    iid_fin = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="決算対応",
                                       company_function="経理")
    t_hr = sfa_db.upsert_task(con, title="人事タスク", status="未着手",
                             link_type="issue", link_id=iid_hr)
    sfa_db.upsert_task(con, title="経理タスク", status="未着手", link_type="issue", link_id=iid_fin)
    sfa_db.upsert_task(con, title="紐づけなしタスク", status="未着手")

    hr_tasks = {t["title"] for t in sfa_db.list_tasks(con, issue_company_function="人事")}
    assert hr_tasks == {"人事タスク"}

    # #146の複数関連付け経路(task_entity_links)でも同様に効くこと
    t2 = sfa_db.upsert_task(con, title="複数紐づけ人事タスク", status="未着手")
    sfa_db.set_task_links(con, t2, [("issue", iid_hr)])
    hr_tasks2 = {t["title"] for t in sfa_db.list_tasks(con, issue_company_function="人事")}
    assert hr_tasks2 == {"人事タスク", "複数紐づけ人事タスク"}
    assert t_hr  # 未使用警告よけ


# ── webapp層: 論点一覧 ──

def test_deal_issues_list_page_shows_company_function_instead_of_common(con):
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準", company_function="人事")
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="無所属論点")
    html = webapp.deal_issues_list_page(con, status=None)
    assert "商談/機能" in html
    assert "🏢 人事" in html
    assert "商談共通" in html  # company_function未設定の方はこれまで通り


def test_deal_issues_list_page_has_company_function_filter(con):
    html = webapp.deal_issues_list_page(con, status=None)
    assert 'name="company_function"' in html
    assert "会社機能:全て" in html
    assert "経営企画" in html and "経理" in html


def test_deal_issues_list_page_filters_by_company_function(con):
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準", company_function="人事")
    sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="決算対応", company_function="経理")
    html = webapp.deal_issues_list_page(con, status=None, company_function="人事")
    assert "採用基準" in html
    assert "決算対応" not in html


# ── webapp層: 論点フォーム ──

def test_deal_issue_form_new_shows_company_function_field_hidden_by_default_deal(con):
    """新規フォームは、事前に商談が指定されている(deal_id引数あり)場合は会社機能欄を隠す。"""
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    html = webapp.deal_issue_form(con, deal_id=did)
    assert 'id="diCompanyFuncWrap"' in html
    assert "display:none" in html.split('id="diCompanyFuncWrap"', 1)[1].split(">", 1)[0]
    assert "function diToggleCompanyFunc" in html


def test_deal_issue_form_new_shows_company_function_field_visible_without_deal(con):
    html = webapp.deal_issue_form(con, deal_id=None)
    wrap_attrs = html.split('id="diCompanyFuncWrap"', 1)[1].split(">", 1)[0]
    assert "display:none" not in wrap_attrs
    assert 'name="company_function"' in html


def test_deal_issue_form_edit_with_deal_hides_company_function(con):
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=did, issue="論点A")
    html = webapp.deal_issue_form(con, issue=sfa_db.get_deal_issue(con, iid))
    assert 'name="company_function"' not in html


def test_deal_issue_form_edit_without_deal_shows_company_function(con):
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準",
                                   company_function="人事")
    html = webapp.deal_issue_form(con, issue=sfa_db.get_deal_issue(con, iid))
    assert 'name="company_function"' in html
    assert '<option value="人事" selected>人事</option>' in html


# ── route層: POST保存 ──

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


def test_post_deal_issue_new_persists_company_function_without_deal(server):
    base, db_path = server
    code, _body = _post(base + "/deal-issue/new",
                        {"issue": "採用基準", "company_function": "人事"},
                        headers=_auth_header())
    assert code == 200
    con2 = sfa_db.connect(db_path)
    row = con2.execute("SELECT deal_id, company_function FROM deal_issues WHERE issue=?",
                       ("採用基準",)).fetchone()
    con2.close()
    assert row["deal_id"] is None
    assert row["company_function"] == "人事"


def test_post_deal_issue_new_ignores_company_function_when_deal_set(server):
    """商談を選んだ場合はcompany_functionを送っても無視される（商談紐づけ優先）。"""
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con2, name="テスト商事")
    did = sfa_db.upsert_deal(con2, account_id=acc, deal_name="案件A", status="open")
    con2.close()

    code, _body = _post(base + "/deal-issue/new",
                        {"issue": "案件固有論点", "deal_id": str(did), "company_function": "人事"},
                        headers=_auth_header())
    assert code == 200
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT deal_id, company_function FROM deal_issues WHERE issue=?",
                       ("案件固有論点",)).fetchone()
    con3.close()
    assert row["deal_id"] == did
    assert row["company_function"] is None


def test_post_deal_issue_edit_persists_company_function_change(server):
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=None, issue="採用基準")
    con2.close()

    code, _body = _post(base + f"/deal-issue/{iid}/edit",
                        {"issue": "採用基準", "company_function": "人事"},
                        headers=_auth_header())
    assert code == 200
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT company_function FROM deal_issues WHERE id=?", (iid,)).fetchone()
    con3.close()
    assert row["company_function"] == "人事"


# ── webapp層: /tasks の会社機能フィルタ ──

def test_tasks_page_has_company_function_filter_and_wires_it(con):
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準",
                                   company_function="人事")
    sfa_db.upsert_task(con, title="人事タスク", status="未着手", link_type="issue", link_id=iid)
    sfa_db.upsert_task(con, title="無関係タスク", status="未着手")

    html_all = webapp.tasks_page(con)
    assert 'name="issue_company_function"' in html_all
    assert "人事タスク" in html_all and "無関係タスク" in html_all

    html_filtered = webapp.tasks_page(con, issue_company_function="人事")
    assert "人事タスク" in html_filtered
    assert "無関係タスク" not in html_filtered


# ── #158(2026-09-04): 論点一覧から会社機能をその場で変更できるように ──
# ユーザー報告「変更できない」: 一覧のステータス/責任者/解消期限は他列と同じくその場で
# 変更できるのに、会社機能だけは編集画面に入らないと変更できなかった。

def test_deal_issues_list_page_company_function_is_inline_editable_select(con):
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=None, issue="採用基準",
                                   company_function="人事")
    html = webapp.deal_issues_list_page(con, status=None)
    assert f"updateDealIssueField({iid}, 'company_function', this.value, true)" in html
    assert '<option value="人事" selected>人事</option>' in html


def test_deal_issues_list_page_deal_linked_issue_has_no_company_function_select(con):
    """商談に紐づく論点には会社機能の選択肢を出さない（そもそも選ぶ意味が無い）。"""
    acc = sfa_db.upsert_account(con, name="テスト商事")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件A", status="open")
    iid = sfa_db.upsert_deal_issue(con, id=None, deal_id=did, issue="案件固有論点")
    html = webapp.deal_issues_list_page(con, status=None)
    assert f"updateDealIssueField({iid}, 'company_function'" not in html


def test_post_deal_issue_field_company_function_updates_value(server):
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=None, issue="採用基準")
    con2.close()

    code, body = _post(base + f"/deal-issue/{iid}/field",
                       {"field": "company_function", "value": "経理"}, headers=_auth_header())
    assert code == 200
    assert json.loads(body) == {"ok": True}
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT company_function FROM deal_issues WHERE id=?", (iid,)).fetchone()
    con3.close()
    assert row["company_function"] == "経理"


def test_post_deal_issue_field_company_function_can_clear_value(server):
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=None, issue="採用基準",
                                   company_function="人事")
    con2.close()

    code, body = _post(base + f"/deal-issue/{iid}/field",
                       {"field": "company_function", "value": ""}, headers=_auth_header())
    assert code == 200
    assert json.loads(body) == {"ok": True}
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT company_function FROM deal_issues WHERE id=?", (iid,)).fetchone()
    con3.close()
    assert row["company_function"] is None


def test_post_deal_issue_field_company_function_rejects_invalid_value(server):
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=None, issue="採用基準")
    con2.close()

    code, body = _post(base + f"/deal-issue/{iid}/field",
                       {"field": "company_function", "value": "存在しない機能"}, headers=_auth_header())
    assert code == 200
    assert json.loads(body)["ok"] is False


def test_post_deal_issue_field_company_function_rejected_for_deal_linked_issue(server):
    """商談に紐づく論点のcompany_functionは、APIを直接叩かれても変更を拒否する
    （一覧側では選択肢自体を出さないが、防御的にサーバ側でも弾く）。"""
    base, db_path = server
    con2 = sfa_db.connect(db_path)
    acc = sfa_db.upsert_account(con2, name="テスト商事")
    did = sfa_db.upsert_deal(con2, account_id=acc, deal_name="案件A", status="open")
    iid = sfa_db.upsert_deal_issue(con2, id=None, deal_id=did, issue="案件固有論点")
    con2.close()

    code, body = _post(base + f"/deal-issue/{iid}/field",
                       {"field": "company_function", "value": "人事"}, headers=_auth_header())
    assert code == 200
    assert json.loads(body)["ok"] is False
    con3 = sfa_db.connect(db_path)
    row = con3.execute("SELECT company_function FROM deal_issues WHERE id=?", (iid,)).fetchone()
    con3.close()
    assert row["company_function"] is None
