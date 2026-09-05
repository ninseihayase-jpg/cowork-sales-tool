"""論点プロジェクト管理(#163, 2026-09-06)の回帰テスト。

論点(deal_issues)に対して人間がサブ論点(deal_issue_subitems)を設定し、期間を
ガントチャートで管理する機能。UI・ドラッグ移動/リサイズはコンサルタスクガント(#152)と
同じ操作感（ユーザー確定）。開始日/終了日の入力は自由記述→Haikuで解釈する
（ユーザー確定: 「自由記述。精度をあげるための指示を精緻に設計して」）。
一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import base64
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
    d = tempfile.mkdtemp(prefix="sfa_issue_gantt_")
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
    monkeypatch.setattr(webapp, "_parse_issue_period_text", lambda text: ("2026-09-10", "2026-09-20"))
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
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


def _get(url):
    req = urllib.request.Request(url, headers=_auth_header(), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, data: dict):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=_auth_header(), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.geturl(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, url, e.read()


def _issue(con, acc_name="A社", deal_name="X", issue="論点A"):
    acc = sfa_db.upsert_account(con, name=acc_name)
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name=deal_name, status="open")
    return sfa_db.upsert_deal_issue(con, deal_id=did, issue=issue)


# ── sfa_db層: CRUD ──

def test_create_and_list_subitem(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20")
    rows = sfa_db.list_deal_issue_subitems(con, iid)
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["title"] == "サブ1"
    assert rows[0]["start_date"] == "2026-09-10"
    assert rows[0]["end_date"] == "2026-09-20"


def test_create_subitem_without_dates_allowed(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "日程未定")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["start_date"] is None and row["end_date"] is None


def test_list_all_subitems_groups_by_issue(con):
    i1 = _issue(con, issue="論点A")
    i2 = _issue(con, issue="論点B")
    sfa_db.create_deal_issue_subitem(con, i1, "A-1", "2026-09-01", "2026-09-05")
    sfa_db.create_deal_issue_subitem(con, i2, "B-1", "2026-09-02", "2026-09-06")
    all_rows = sfa_db.list_deal_issue_subitems(con)
    assert {r["issue_id"] for r in all_rows} == {i1, i2}


def test_update_subitem_partial_does_not_clobber_other_fields(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20")
    sfa_db.update_deal_issue_subitem(con, sid, title="サブ1改")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["title"] == "サブ1改"
    assert row["start_date"] == "2026-09-10"  # 変更していないフィールドは残る
    assert row["end_date"] == "2026-09-20"


def test_update_subitem_start_date_only(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20")
    sfa_db.update_deal_issue_subitem(con, sid, start_date="2026-09-12")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["start_date"] == "2026-09-12"
    assert row["end_date"] == "2026-09-20"


def test_delete_subitem(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1")
    sfa_db.delete_deal_issue_subitem(con, sid)
    assert sfa_db.get_deal_issue_subitem(con, sid) is None


def test_deleting_issue_cascades_subitems(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1")
    con.execute("DELETE FROM deal_issues WHERE id=?", (iid,))
    con.commit()
    assert sfa_db.get_deal_issue_subitem(con, sid) is None


# ── 概要(overview)フィールド（2026-09-05要望: ステップ名・期間に加えて概要を追加） ──

def test_create_subitem_with_overview(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20",
                                           overview="概要テキスト")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["overview"] == "概要テキスト"


def test_create_subitem_without_overview_defaults_to_none(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["overview"] is None


def test_update_subitem_overview_only_does_not_clobber_title(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20")
    sfa_db.update_deal_issue_subitem(con, sid, overview="後から追記")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["overview"] == "後から追記"
    assert row["title"] == "サブ1"


def test_update_subitem_overview_empty_string_clears_it(con):
    """overviewは空文字での明示的クリアを許す（他フィールドと違い、Noneのみ「変更しない」の意味）。"""
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", overview="消される予定")
    sfa_db.update_deal_issue_subitem(con, sid, overview="")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["overview"] == ""


def test_update_subitem_without_overview_kwarg_leaves_overview_unchanged(con):
    iid = _issue(con)
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", overview="変わらないはず")
    sfa_db.update_deal_issue_subitem(con, sid, title="サブ1改")
    row = sfa_db.get_deal_issue_subitem(con, sid)
    assert row["overview"] == "変わらないはず"


# ── Haiku自由記述パーサ ──

def test_parse_issue_period_text_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(webapp, "ANTHROPIC_API_KEY", "")
    s, e = webapp._parse_issue_period_text("来週から3週間")
    assert s is None and e is None


def test_parse_issue_period_text_empty_string_returns_none(monkeypatch):
    monkeypatch.setattr(webapp, "ANTHROPIC_API_KEY", "dummy")
    s, e = webapp._parse_issue_period_text("")
    assert s is None and e is None


def test_parse_issue_period_text_swaps_reversed_dates(monkeypatch):
    monkeypatch.setattr(webapp, "ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(webapp, "_call_claude_haiku",
                        lambda *a, **k: '{"start_date": "2026-09-15", "end_date": "2026-09-05"}')
    s, e = webapp._parse_issue_period_text("テスト")
    assert (s, e) == ("2026-09-05", "2026-09-15")


def test_parse_issue_period_text_handles_null_response(monkeypatch):
    monkeypatch.setattr(webapp, "ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(webapp, "_call_claude_haiku",
                        lambda *a, **k: '{"start_date": null, "end_date": null}')
    s, e = webapp._parse_issue_period_text("わけのわからない文字列")
    assert s is None and e is None


def test_parse_issue_period_text_handles_garbage_response(monkeypatch):
    monkeypatch.setattr(webapp, "ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(webapp, "_call_claude_haiku", lambda *a, **k: "not json at all")
    s, e = webapp._parse_issue_period_text("x")
    assert s is None and e is None


def test_parse_issue_period_text_prompt_includes_today_and_rules(monkeypatch):
    """精度確保のため、今日の日付・曜日・複数の解釈ルールがプロンプトに含まれること
    （ユーザー確定要件「精度をあげるための指示を精緻に設計して」の検証）。"""
    monkeypatch.setattr(webapp, "ANTHROPIC_API_KEY", "dummy")
    captured = {}

    def _fake(prompt, **kw):
        captured["prompt"] = prompt
        return '{"start_date": "2026-09-10", "end_date": "2026-09-20"}'

    monkeypatch.setattr(webapp, "_call_claude_haiku", _fake)
    webapp._parse_issue_period_text("来週から3週間")
    prompt = captured["prompt"]
    assert "今日の日付は" in prompt
    assert "来週から3週間" in prompt
    assert "曜日" in prompt
    assert "西暦" in prompt or "年" in prompt


# ── webapp.py: ページレンダリング ──

def test_gantt_page_empty_state(con):
    html = webapp.deal_issues_gantt_page(con)
    assert "社内PJがまだありません" in html


def test_gantt_page_renders_ready_group_and_bar(con):
    iid = _issue(con, issue="論点A")
    sid = sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20")
    html = webapp.deal_issues_gantt_page(con)
    assert "論点A" in html
    assert "サブ1" in html
    assert f'data-iid="{sid}"' in html
    assert "igOpenItem(" in html
    assert "gantt-bar" in html


def test_gantt_page_lists_missing_items_separately(con):
    iid = _issue(con, issue="論点B")
    sid = sfa_db.create_deal_issue_subitem(con, iid, "日程未定")
    html = webapp.deal_issues_gantt_page(con)
    assert "期間を解釈できなかった" in html
    assert "日程未定" in html
    assert f"/deal-issue-subitem/{sid}/fix-date" in html


def test_gantt_page_excludes_cancelled_issues(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X", status="open")
    iid = sfa_db.upsert_deal_issue(con, deal_id=did, issue="取消済み論点", status="取り消し")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20")
    html = webapp.deal_issues_gantt_page(con)
    assert "取消済み論点" not in html


def test_gantt_page_categories_sorted_by_account_name(con):
    """分類（商談=アカウント名/商談名）別に一覧表示し、分類はアカウント名順に並ぶこと
    （2026-09-06要望: 「先に論点一覧が分類別に並んでいて」）。"""
    i1 = _issue(con, acc_name="B社", deal_name="乙", issue="論点1")
    i2 = _issue(con, acc_name="A社", deal_name="甲", issue="論点2")
    html = webapp.deal_issues_gantt_page(con)
    assert html.find("🗂 A社 / 甲") < html.find("🗂 B社 / 乙")


def test_gantt_page_issue_without_subitems_still_listed_with_add_button(con):
    """ステップが1件も無い社内PJも一覧に表示され、行の「＋」ボタンからステップ追加
    ポップアップを開けること（2026-09-05要望: 常時展開フォームはポップアップ化して行の
    折り返し重なりを解消。ポップアップはJS側でissue_idを渡してfetch送信するため、
    静的HTML上にはonclickハンドラとしてissue_idが載る）。"""
    iid = _issue(con, issue="ステップなしの社内PJ")
    html = webapp.deal_issues_gantt_page(con)
    assert "ステップなしの社内PJ" in html
    assert f"igOpenAddStep({iid})" in html
    assert "/deal-issue-subitem/new" in html  # JS側fetch先として埋め込まれている


def test_gantt_page_bar_shows_only_title_and_period_not_overview(con):
    """ガントバーの可視部分（<script>より前の静的HTML）にはステップ名+期間のみが出て、
    概要は現れないこと（2026-09-05ユーザー確定: 「ガントチャートのバーに表示されるのは、
    ステップ名と期間」）。ただしクリック時の編集ポップアップ用データ(IG_ITEMS)には
    概要が必要なため、<script>内のJSONブロブには含まれてよい——そこまで検証すると
    機能を壊す誤検知になるため、静的マークアップ部分だけを対象にする。"""
    iid = _issue(con)
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-09-20",
                                     overview="これはバーに出てはいけない秘密の概要文")
    html = webapp.deal_issues_gantt_page(con)
    visible_html, _, _ = html.partition("<script>")
    assert "gt-bar-label" in visible_html and "サブ1" in visible_html
    assert "これはバーに出てはいけない秘密の概要文" not in visible_html
    assert "これはバーに出てはいけない秘密の概要文" in html  # IG_ITEMS内(編集ポップアップ用)には残る


def test_gantt_page_add_step_button_present_per_issue(con):
    """各社内PJ行にステップ追加ポップアップを開く「＋」ボタンがあること
    （2026-09-05要望: 「ステップの追加は、各社内PJに＋ボタンがついていて、それをクリックして行う」）。"""
    iid = _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert f"igOpenAddStep({iid})" in html


def test_gantt_page_no_toplevel_terminology_of_old_subitem_name(con):
    """UI文言は「サブ社内PJ」から「ステップ」へ全面改名済みであること。"""
    iid = _issue(con)
    sfa_db.create_deal_issue_subitem(con, iid, "日程未定")  # 要確認枠も描画させる
    html = webapp.deal_issues_gantt_page(con)
    assert "サブ社内PJ" not in html
    assert "期間を解釈できなかったステップ" in html


def test_gantt_page_no_top_level_flat_form(con):
    """画面最上部の独立した論点セレクタ式フォーム（複数論点を1つの<select>にまとめたもの）は
    廃止されていること。追加は各論点ブロック内の固有フォームのみ。"""
    _issue(con, issue="論点A")
    _issue(con, issue="論点B")
    html = webapp.deal_issues_gantt_page(con)
    assert "論点を選んでサブ論点を追加" not in html


def test_gantt_page_company_common_issue_category_label(con):
    """商談に紐づかない論点(company_function)は、その分類名で表示されること。"""
    iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="共通論点", company_function="経理")
    html = webapp.deal_issues_gantt_page(con)
    assert "🗂 🏢 経理" in html
    assert "共通論点" in html
    assert f'href="/deal-issue/{iid}"' in html


# ── HTTPルート ──

def test_gantt_route_via_http(server):
    code, body = _get(f"{server}/deal-issues/gantt")
    assert code == 200
    assert "社内PJ管理".encode() in body


def test_create_subitem_route_via_http(con, server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点X")
    con2.close()

    code, url, _ = _post(f"{server}/deal-issue-subitem/new",
                         {"issue_id": iid, "title": "新サブ論点", "period_text": "来週から3週間"})
    assert code == 200
    assert url == f"{server}/deal-issues/gantt"

    con3 = sfa_db.connect(db_path)
    rows = sfa_db.list_deal_issue_subitems(con3, iid)
    assert len(rows) == 1
    assert rows[0]["title"] == "新サブ論点"
    assert rows[0]["start_date"] == "2026-09-10"  # serverフィクスチャでモック済み
    assert rows[0]["end_date"] == "2026-09-20"


def test_create_subitem_route_with_overview(con, server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点X2")
    con2.close()

    _post(f"{server}/deal-issue-subitem/new",
          {"issue_id": iid, "title": "新ステップ", "overview": "概要メモ", "period_text": "来週から3週間"})

    con3 = sfa_db.connect(db_path)
    rows = sfa_db.list_deal_issue_subitems(con3, iid)
    assert len(rows) == 1
    assert rows[0]["overview"] == "概要メモ"


def test_create_subitem_route_rejects_missing_issue(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    code, url, _ = _post(f"{server}/deal-issue-subitem/new",
                         {"issue_id": 999999, "title": "存在しない論点", "period_text": "来週"})
    assert code == 200
    con3 = sfa_db.connect(db_path)
    assert sfa_db.list_deal_issue_subitems(con3) == []


def test_field_route_updates_and_validates(tmp_path, monkeypatch):
    import threading as _th
    from http.server import ThreadingHTTPServer as _THS

    db_path = str(tmp_path / "srv2.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点Y")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "サブ1", "2026-09-10", "2026-09-20")
    con2.close()

    user, pw = "u", "p"
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", user)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = _THS(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = _th.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()

        def post(url, data):
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(url, data=body, headers={"Authorization": f"Basic {tok}"}, method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.getcode(), resp.read()

        code, body = post(f"{base}/deal-issue-subitem/{sid}/field",
                          {"field": "end_date", "value": "2026-09-25"})
        assert code == 200
        import json
        assert json.loads(body)["ok"] is True

        code2, body2 = post(f"{base}/deal-issue-subitem/{sid}/field",
                            {"field": "issue_id", "value": "999"})
        assert json.loads(body2)["ok"] is False
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["end_date"] == "2026-09-25"


def test_field_route_overview_empty_string_clears_not_ignored(tmp_path, monkeypatch):
    """/fieldルートのoverviewは、他フィールドと違い空文字を「クリア」として処理すること
    （他フィールドは空文字→None変換で「変更しない」扱いになる、既存の仕様との違い）。"""
    import threading as _th
    from http.server import ThreadingHTTPServer as _THS

    db_path = str(tmp_path / "srv3.db")
    sfa_db.init_db(db_path)
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点Y2")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "サブ1", overview="消される予定")
    con2.close()

    user, pw = "u", "p"
    monkeypatch.setattr(webapp, "SFA_BASIC_USER", user)
    monkeypatch.setattr(webapp, "SFA_BASIC_PASS", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = _THS(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = _th.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()

        def post(url, data):
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(url, data=body, headers={"Authorization": f"Basic {tok}"}, method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.getcode(), resp.read()

        code, body = post(f"{base}/deal-issue-subitem/{sid}/field", {"field": "overview", "value": ""})
        assert code == 200
        import json
        assert json.loads(body)["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["overview"] == ""


def test_delete_route_via_http(con, server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点Z")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "削除対象")
    con2.close()

    _post(f"{server}/deal-issue-subitem/{sid}/delete", {})

    con3 = sfa_db.connect(db_path)
    assert sfa_db.get_deal_issue_subitem(con3, sid) is None


def test_fix_date_route_autofills_end_date_two_weeks_later(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点W")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "要確認サブ論点")  # 日付未設定
    con2.close()

    _post(f"{server}/deal-issue-subitem/{sid}/fix-date", {"start_date": "2026-10-01"})

    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["start_date"] == "2026-10-01"
    assert row["end_date"] == "2026-10-15"  # 2週間後を仮設定


def test_fix_date_route_preserves_existing_end_date(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点V")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "サブ", end_date="2026-11-01")
    con2.close()

    _post(f"{server}/deal-issue-subitem/{sid}/fix-date", {"start_date": "2026-10-01"})

    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["start_date"] == "2026-10-01"
    assert row["end_date"] == "2026-11-01"  # 既存の終了日は上書きしない
