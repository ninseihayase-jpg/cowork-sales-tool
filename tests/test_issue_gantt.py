"""論点プロジェクト管理(#163, 2026-09-06)の回帰テスト。

論点(deal_issues)に対して人間がサブ論点(deal_issue_subitems)を設定し、期間を
ガントチャートで管理する機能。UI・ドラッグ移動/リサイズはコンサルタスクガント(#152)と
同じ操作感（ユーザー確定）。開始日/終了日の入力は自由記述→Haikuで解釈する
（ユーザー確定: 「自由記述。精度をあげるための指示を精緻に設計して」）。
一時DBのみ使用。本番DB(cowork_sfa.db)には一切触れない。
"""
from __future__ import annotations

import base64
import re
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
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
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
    return {"Cookie": f"sfa_session={webapp._make_session_token()}"}


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


def test_gantt_grid_has_explicit_min_width_to_prevent_sticky_label_bug(con):
    """.gantt-gridはwidth:100%のままだと、minmax(var(--ig-daycol-min,28px),...)floor×日数が
    ラップ幅を超えた際に宣言ボックスより描画内容が広くなり、position:stickyな行ラベル
    (.gantt-lbl)がスクロール終盤で追従せず流れてしまう不具合があった
    （2026-09-13、サブタスク行で顕在化・修正）。宣言ボックス自体を実コンテンツ幅
    (260px+var(--ig-daycol-min,28px)×日数)以上に保証するmin-widthが常に明示されていること
    （日次/週次ズーム切替(2026-09-13)でも列幅と連動して縮むよう、固定pxではなくCSS変数を
    参照するcalc()にしている）。"""
    iid = _issue(con, issue="論点MinWidth")
    sfa_db.create_deal_issue_subitem(con, iid, "サブ1", "2026-09-10", "2026-11-20")  # 幅広い期間
    html = webapp.deal_issues_gantt_page(con)
    m = re.search(
        r'class="gantt-grid" style="grid-template-columns:([^"]*?);'
        r'min-width:calc\(260px \+ (\d+) \* var\(--ig-daycol-min, 28px\)\)"', html)
    assert m, "gantt-gridにCSS変数連動のmin-widthが見つからない"
    n_days_match = re.search(r'repeat\((\d+),', m.group(1))
    n_days = int(n_days_match.group(1))
    assert int(m.group(2)) == n_days
    assert "var(--ig-daycol-min, 28px)" in m.group(1)  # 列幅自体もCSS変数を参照していること


def test_gantt_page_lists_missing_items_separately(con):
    """2026-09-26〜: メインタスクは自身の日付を持たない（配下から自動算出）ため、この
    「要確認」枠はサブタスクのみが対象になる。"""
    iid = _issue(con, issue="論点B")
    mid = sfa_db.create_deal_issue_subitem(con, iid, "メイン", start_date="2026-09-01", end_date="2026-09-30")
    sid = sfa_db.create_deal_issue_subitem(con, iid, "日程未定", parent_id=mid)
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
    """タスクが1件も無い社内PJも一覧に表示され、行の「＋」からメインタスク追加の
    インライン入力欄（予約行）を開けること（2026-09-11: フローティングポップアップは廃止し、
    「＋」クリックで直下の行にタスク名/概要/期間の入力欄が現れる方式に変更）。"""
    iid = _issue(con, issue="ステップなしの社内PJ")
    html = webapp.deal_issues_gantt_page(con)
    assert "ステップなしの社内PJ" in html
    assert f"igShowInlineAdd('ig-mainadd-{iid}')" in html
    assert f'id="ig-mainadd-{iid}"' in html
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
    """各社内PJ行にメインタスク追加のインライン入力欄を開く「＋」ボタンがあること
    （2026-09-05要望: 「ステップの追加は、各社内PJに＋ボタンがついていて、それをクリックして行う」。
    2026-09-11: 開き方をポップアップからインライン入力欄に変更）。"""
    iid = _issue(con, issue="論点A")
    html = webapp.deal_issues_gantt_page(con)
    assert f"igShowInlineAdd('ig-mainadd-{iid}')" in html


def test_gantt_page_no_toplevel_terminology_of_old_subitem_name(con):
    """UI文言は「サブ社内PJ」から「ステップ」へ全面改名済みであること。"""
    iid = _issue(con)
    mid = sfa_db.create_deal_issue_subitem(con, iid, "メイン", start_date="2026-09-01", end_date="2026-09-30")
    sfa_db.create_deal_issue_subitem(con, iid, "日程未定", parent_id=mid)  # 要確認枠も描画させる
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
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", user)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = _THS(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = _th.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:

        def post(url, data):
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(url, data=body, headers={"Cookie": f"sfa_session={webapp._make_session_token()}"}, method="POST")
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
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", user)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", pw)
    handler_cls = webapp._make_handler(db_path, None)
    srv = _THS(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = _th.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:

        def post(url, data):
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(url, data=body, headers={"Cookie": f"sfa_session={webapp._make_session_token()}"}, method="POST")
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


# ── ドラッグ並び替え・実施期間ラベル・完了/MS（2026-09-26） ──────────────

def test_reorder_deal_issue_subitems_persists_sort_order(con):
    iid = _issue(con, issue="論点並び替え")
    s1 = sfa_db.create_deal_issue_subitem(con, iid, "A", start_date="2026-10-01", end_date="2026-10-07")
    s2 = sfa_db.create_deal_issue_subitem(con, iid, "B", start_date="2026-10-08", end_date="2026-10-14")
    s3 = sfa_db.create_deal_issue_subitem(con, iid, "C", start_date="2026-10-15", end_date="2026-10-21")

    sfa_db.reorder_deal_issue_subitems(con, [s3, s1, s2])

    rows = {r["id"]: r["sort_order"] for r in sfa_db.list_deal_issue_subitems(con, iid)}
    assert rows[s3] < rows[s1] < rows[s2]


def test_reorder_deal_issue_subitems_ignores_unknown_ids(con):
    """delivery体制のreorder_delivery_rolesと同じ信頼モデル: 実在しないidは無視するだけで
    例外にならない（実在するidの行は書き換えられる）。"""
    iid = _issue(con, issue="論点X2")
    s1 = sfa_db.create_deal_issue_subitem(con, iid, "A", start_date="2026-10-01", end_date="2026-10-07")
    sfa_db.reorder_deal_issue_subitems(con, [999999, s1])  # 例外にならないことを確認
    assert sfa_db.get_deal_issue_subitem(con, s1) is not None


def test_reorder_route_via_http_changes_render_order(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点並び替えHTTP")
    s1 = sfa_db.create_deal_issue_subitem(con2, iid, "先頭タスク", start_date="2026-10-01", end_date="2026-10-07")
    s2 = sfa_db.create_deal_issue_subitem(con2, iid, "後続タスク", start_date="2026-10-08", end_date="2026-10-14")
    con2.close()

    # 初期表示は日付順（s1が先）。
    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert html.index("先頭タスク") < html.index("後続タスク")

    # ドラッグで逆順にしたと仮定してreorderを叩く。
    code, _, _ = _post(f"{server}/deal-issue-subitem/reorder", {"order": f"{s2},{s1}"})
    assert code == 204

    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert html.index("後続タスク") < html.index("先頭タスク"), "並び替え後の順番がレンダリングに反映されていない"


def test_period_label_rendered_next_to_task_title(server, tmp_path):
    """サブタスクは自身の日付をそのままラベル表示。メインタスクは自身の日付を持たず、
    配下サブタスクの最早開始〜最遅終了を自動算出してラベル表示する（2026-09-26〜）。"""
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点期間ラベル")
    mid = sfa_db.create_deal_issue_subitem(con2, iid, "期間表示メイン")
    sfa_db.create_deal_issue_subitem(con2, iid, "期間表示サブ", start_date="2026-10-05", end_date="2026-12-25",
                                     parent_id=mid)
    con2.close()

    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert '<span class="ig-period-lbl">10/5〜12/25</span>' in html


def test_done_field_toggle_persists_as_int_and_grays_out_row(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点完了")
    mid = sfa_db.create_deal_issue_subitem(con2, iid, "メイン")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "完了予定タスク", start_date="2026-10-05", end_date="2026-10-11",
                                           parent_id=mid)
    con2.close()

    code, _, body = _post(f"{server}/deal-issue-subitem/{sid}/field", {"field": "done", "value": "1"})
    assert code == 200
    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["done"] == 1
    con3.close()

    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert 'class="gantt-lbl ig-done"' in html
    assert 'class="gantt-bar ig-done"' in html

    # 0を送ると解除される（文字列ではなくint 0で保存されること）。
    _post(f"{server}/deal-issue-subitem/{sid}/field", {"field": "done", "value": "0"})
    con4 = sfa_db.connect(db_path)
    row2 = sfa_db.get_deal_issue_subitem(con4, sid)
    assert row2["done"] == 0


def test_is_milestone_field_toggle_persists_and_highlights_independent_of_done(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点MS")
    mid = sfa_db.create_deal_issue_subitem(con2, iid, "メイン")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "MSタスク", start_date="2026-10-05", end_date="2026-10-11",
                                           parent_id=mid)
    con2.close()

    _post(f"{server}/deal-issue-subitem/{sid}/field", {"field": "is_milestone", "value": "1"})
    _post(f"{server}/deal-issue-subitem/{sid}/field", {"field": "done", "value": "1"})

    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["is_milestone"] == 1
    assert row["done"] == 1  # 完了とMSは独立フラグで両方立てられる

    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert 'class="gantt-lbl ig-done ig-ms"' in html
    assert 'class="gantt-bar ig-done ig-ms"' in html


def test_deal_issue_subitem_sort_order_migration_preserves_date_order(tmp_path):
    """init_db()の一回限りの移行: 本機能より前からあったデータはsort_order=挿入順のままだったが、
    移行後は現状の(start_date,end_date)順がsort_orderへ書き写され、表示順が変わらない。
    移行フラグは通常DB作成時点(0件)で即座に立つため、ここでは「本機能追加前からデータがある
    DBに新コードをデプロイした」状況を、移行フラグを一度削除して再現する。"""
    db_path = str(tmp_path / "migrate.db")
    sfa_db.init_db(db_path)
    con = sfa_db.connect(db_path)
    iid = _issue(con, issue="論点移行")
    # 日付順とid順をわざと逆にする(後からできたタスクの方が先に始まる)。
    s_late_id_early_date = sfa_db.create_deal_issue_subitem(con, iid, "先に始まる", start_date="2026-09-01", end_date="2026-09-07")
    s_early_id_late_date = sfa_db.create_deal_issue_subitem(con, iid, "後で始まる", start_date="2026-09-08", end_date="2026-09-14")
    con.execute("DELETE FROM masters WHERE key='deal_issue_subitems_sort_migrated'")
    con.commit()
    con.close()

    sfa_db.init_db(db_path)  # 移行フラグが無い状態なので、ここで移行が実行される
    con2 = sfa_db.connect(db_path)
    rows = {r["id"]: r["sort_order"] for r in sfa_db.list_deal_issue_subitems(con2, iid)}
    assert rows[s_late_id_early_date] < rows[s_early_id_late_date]


# ── サブタスクの担当・担当フィルタ（2026-09-26） ──────────────────────────

def test_owner_field_toggle_persists_via_field_route(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点担当")
    sid = sfa_db.create_deal_issue_subitem(con2, iid, "担当割当タスク", start_date="2026-10-05", end_date="2026-10-11")
    con2.close()

    owner = sfa_db.OWNERS[0]
    code, _, body = _post(f"{server}/deal-issue-subitem/{sid}/field", {"field": "owner", "value": owner})
    assert code == 200
    con3 = sfa_db.connect(db_path)
    row = sfa_db.get_deal_issue_subitem(con3, sid)
    assert row["owner"] == owner


def test_owner_label_rendered_next_to_subtask_title(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点担当表示")
    mid = sfa_db.create_deal_issue_subitem(con2, iid, "メイン", start_date="2026-10-05", end_date="2026-12-25")
    owner = sfa_db.OWNERS[0]
    sub_id = sfa_db.create_deal_issue_subitem(con2, iid, "担当付きサブ", start_date="2026-10-05", end_date="2026-10-11",
                                              parent_id=mid)
    sfa_db.update_deal_issue_subitem(con2, sub_id, owner=owner)
    con2.close()

    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert f"👤{owner}" in html


def test_owner_filter_query_param_shows_only_matching_subtasks_but_keeps_main_task(server, tmp_path):
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点担当フィルタ")
    mid = sfa_db.create_deal_issue_subitem(con2, iid, "メインタスク", start_date="2026-10-05", end_date="2026-12-25")
    owner_a, owner_b = sfa_db.OWNERS[0], sfa_db.OWNERS[1]
    sub_a = sfa_db.create_deal_issue_subitem(con2, iid, "Aさんのタスク", start_date="2026-10-05", end_date="2026-10-11",
                                             parent_id=mid)
    sub_b = sfa_db.create_deal_issue_subitem(con2, iid, "Bさんのタスク", start_date="2026-10-12", end_date="2026-10-18",
                                             parent_id=mid)
    sfa_db.update_deal_issue_subitem(con2, sub_a, owner=owner_a)
    sfa_db.update_deal_issue_subitem(con2, sub_b, owner=owner_b)
    con2.close()

    code, body = _get(f"{server}/deal-issues/gantt?owner={urllib.parse.quote(owner_a)}")
    html = body.decode("utf-8")
    assert "メインタスク" in html, "担当フィルタ中でもメインタスクは常に表示されるはず"
    assert "Aさんのタスク" in html
    assert "Bさんのタスク" not in html, "担当が一致しないサブタスクはフィルタで除外されるはず"


# ── メインタスクは期間を持たず配下から自動算出（2026-09-26） ────────────

def test_main_task_period_derived_from_children_ignores_own_stale_dates(con):
    """メインタスクに古い/自身の日付が入っていても（過去の仕様の名残や手違いで残っていても）、
    描画時は一切参照せず、配下サブタスクの最早開始〜最遅終了だけを使う。"""
    iid = _issue(con, issue="論点自動算出")
    # メイン自身の日付は明らかに違う値をわざと入れておく（読まれないことを確認するため）。
    mid = sfa_db.create_deal_issue_subitem(con, iid, "メイン", start_date="2020-01-01", end_date="2020-01-07")
    sfa_db.create_deal_issue_subitem(con, iid, "早いサブ", start_date="2026-10-05", end_date="2026-10-11", parent_id=mid)
    sfa_db.create_deal_issue_subitem(con, iid, "遅いサブ", start_date="2026-11-01", end_date="2026-12-25", parent_id=mid)

    html = webapp.deal_issues_gantt_page(con)
    assert '<span class="ig-period-lbl">10/5〜12/25</span>' in html, \
        "メインタスクの期間ラベルは配下の最早開始(10/5)〜最遅終了(12/25)になるはず"
    assert "2020" not in html, "メイン自身の古い日付が使われてしまっている"
    m = re.search(r'<div class="gantt-bar[^"]*ig-readonly-bar" data-iid="' + str(mid) + r'"', html)
    assert m is not None, "メインタスクの読み取り専用バーが描画されていない"


def test_main_task_with_no_dated_children_renders_label_only_no_bar(con):
    """配下に日付を持つサブタスクが1件も無いメインタスクは、バー・日付背景セルを描画せず
    ラベル行のみになる（「（期間未定）」表示）。missing_items（要確認枠）にも積まれない
    （メインタスクは自身の日付を必須としないため）。"""
    iid = _issue(con, issue="論点期間未定")
    sfa_db.create_deal_issue_subitem(con, iid, "空のメイン")
    html = webapp.deal_issues_gantt_page(con)
    assert "空のメイン" in html
    assert "（期間未定）" in html
    assert "期間を解釈できなかった" not in html
    assert '<div class="gantt-bar' not in html, "配下に日付が無いメインタスクにバーが描画されている"


def test_main_task_edit_popup_has_no_date_inputs(server, tmp_path):
    """メインタスクの編集ポップアップ(igPopHtml)は日付inputを持たない（サブタスクのみ持つ）。
    JS関数自体はテストできないため、サーバ側で埋め込まれるIG_ITEMSのis_mainフラグと、
    igPopHtml内の分岐ロジック（it.is_main ? ... : 日付input）が両方存在することを、
    レンダリングされたJSソースから確認する。"""
    db_path = str(tmp_path / "srv.db")
    con2 = sfa_db.connect(db_path)
    iid = _issue(con2, issue="論点ポップアップ")
    mid = sfa_db.create_deal_issue_subitem(con2, iid, "メイン")
    sfa_db.create_deal_issue_subitem(con2, iid, "サブ", start_date="2026-10-05", end_date="2026-10-11", parent_id=mid)
    con2.close()

    code, body = _get(f"{server}/deal-issues/gantt")
    html = body.decode("utf-8")
    assert re.search(r'"is_main":\s*(true|false)', html), "IG_ITEMSにis_mainフラグが埋め込まれていない"
    assert "it.is_main" in html, "igPopHtmlがis_mainで日付inputの出し分けをしていない"
    assert re.search(r'"' + str(mid) + r'":\s*\{[^}]*"is_main":\s*true', html), \
        "メインタスクのIG_ITEMSエントリでis_main=trueになっていない"
