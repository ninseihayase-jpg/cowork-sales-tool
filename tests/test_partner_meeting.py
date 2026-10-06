"""パートナー定例(週次)機能・「定例」ランディングのルート/RBAC/DB層テスト（2026-10新設）。

設計doc: docs/10_パートナー定例レポート機能_設計構想.md（全論点確定済み）。
Artifactモックアップ（④生産性）: https://claude.ai/artifact/3aZgE76qPDQvw9AVJu68Je

test_monthly_report.py と同じ流儀（ThreadingHTTPServer + 実HTTPリクエスト）で検証する。
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"
KEIEI_EMAIL = "keiei@inproc.org"
MANAGER_EMAIL = "manager@inproc.org"
MEMBER_EMAIL = "member@inproc.org"
PARTNER_EMAIL = "partner@inproc.org"


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="sfa_partner_meeting_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(tmp_dir):
    p = str(tmp_dir / "t.db")
    sfa_db.init_db(p)
    con = sfa_db.connect(p)
    sfa_db.set_user_role(con, KEIEI_EMAIL, "経営")
    sfa_db.set_user_role(con, MANAGER_EMAIL, "マネージャー")
    sfa_db.set_user_role(con, MEMBER_EMAIL, "メンバー")
    sfa_db.set_user_role(con, PARTNER_EMAIL, "外部")
    con.close()
    return p


@pytest.fixture
def basic_auth_env(monkeypatch):
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
    yield


@pytest.fixture
def server(db_path, basic_auth_env):
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


def _header(email):
    return {"Cookie": f"sfa_session={webapp._make_session_token(email)}"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, data, headers=None):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


# 既存テスト群の慣例（webapp._today_jst()を都度呼び、実行時の「今日」を基準にデータを作る。
# 固定日付へクロックをmonkeypatchするテストはこのリポジトリに前例が無い）に合わせ、
# 当該週は実行時点の「今週月曜」を基準にする。
THIS_MONDAY = sfa_db._monday_of(webapp._today_jst())


# ── 1. 「定例」ランディング ──

def test_regular_meeting_landing_shows_both_links(server):
    code, body = _get(server + "/regular-meeting", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert "全社定例" in html and "パートナー定例" in html
    assert 'href="/monthly-report"' in html and 'href="/partner-meeting"' in html


def test_regular_meeting_landing_hidden_for_external(server):
    code, _ = _get(server + "/regular-meeting", headers=_header(PARTNER_EMAIL))
    assert code == 403


# ── 2. RBAC smoke（パートナー定例） ──

def test_partner_meeting_view_access_by_role(server):
    for email in (KEIEI_EMAIL, MANAGER_EMAIL, MEMBER_EMAIL):
        code, _ = _get(server + "/partner-meeting", headers=_header(email))
        assert code == 200, (email, code)
    code, _ = _get(server + "/partner-meeting", headers=_header(PARTNER_EMAIL))
    assert code == 403


def test_partner_meeting_write_routes_blocked_for_external_and_member(server):
    paths_and_bodies = [
        (f"/partner-meeting/{THIS_MONDAY}/create", {}),
        (f"/partner-meeting/{THIS_MONDAY}/field", {"field": "marketing_comment_html", "value": "x"}),
        (f"/partner-meeting/{THIS_MONDAY}/llm-format", {"area": "marketing", "col": "comment", "draft": "x"}),
        (f"/partner-meeting/{THIS_MONDAY}/fix", {}),
        (f"/partner-meeting/{THIS_MONDAY}/reopen", {}),
    ]
    for path, body in paths_and_bodies:
        code, _, _ = _post(server + path, body, headers=_header(PARTNER_EMAIL))
        assert code == 403, path
    code, _, _ = _post(server + f"/partner-meeting/{THIS_MONDAY}/create", {}, headers=_header(MEMBER_EMAIL))
    assert code == 403


# ── 3. 作成+引き継ぎ ──

def test_create_partner_meeting_report_carries_forward_previous_week(server, db_path):
    con = sfa_db.connect(db_path)
    prev_week = sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) - timedelta(days=7))
    sfa_db.create_partner_meeting_report(con, prev_week)
    sfa_db.update_partner_meeting_report_field(con, prev_week, "marketing_comment_html", "<div>前週の気づき</div>")
    sfa_db.update_partner_meeting_report_field(con, prev_week, "marketing_comment_draft", "前週の自由記述")
    con.close()

    code, _, _ = _post(server + f"/partner-meeting/{THIS_MONDAY}/create", {}, headers=_header(KEIEI_EMAIL))
    assert code in (200, 303)

    con2 = sfa_db.connect(db_path)
    r = sfa_db.get_partner_meeting_report(con2, THIS_MONDAY)
    assert r is not None
    assert r["marketing_comment_html"] == "<div>前週の気づき</div>"
    assert r["marketing_comment_draft"] == "前週の自由記述"


# ── 4. Fix後のfield保存は拒否、再オープンで同一行の編集を再開 ──

def test_field_save_blocked_after_fix_then_reopen_resumes_same_row(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_partner_meeting_report(con, THIS_MONDAY)
    con.close()
    keiei = _header(KEIEI_EMAIL)

    code, _, _ = _post(server + f"/partner-meeting/{THIS_MONDAY}/fix", {}, headers=keiei)
    assert code in (200, 303)

    code, body, _ = _post(server + f"/partner-meeting/{THIS_MONDAY}/field",
                           {"field": "marketing_comment_html", "value": "x"}, headers=keiei)
    assert code == 200
    assert b'"ok": false' in body or b'"ok":false' in body

    con2 = sfa_db.connect(db_path)
    fixed_row = sfa_db.get_partner_meeting_report(con2, THIS_MONDAY)
    assert fixed_row["fixed_at"]

    code, _, _ = _post(server + f"/partner-meeting/{THIS_MONDAY}/reopen", {}, headers=keiei)
    assert code in (200, 303)

    con3 = sfa_db.connect(db_path)
    reopened = sfa_db.get_partner_meeting_report(con3, THIS_MONDAY)
    assert reopened["id"] == fixed_row["id"]
    assert reopened["fixed_at"] is None
    ok = sfa_db.update_partner_meeting_report_field(con3, THIS_MONDAY, "marketing_comment_html", "<div>再開後</div>")
    assert ok is True


# ── 5. ①Pipeline(Sales): ステージOR重要度（和集合）、Pipeline金額=value_lumpsumのみ ──

def test_sales_bucket_is_union_of_stage_and_importance(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    # ステージ条件のみで該当（重要度は無し）
    d1 = sfa_db.upsert_deal(con, account_id=aid, deal_name="提案中", stage="提案", owner="吉江", value_lumpsum=500)
    # 重要度条件のみで該当（ステージは初回アポ実施＝対象外ステージ）
    d2 = sfa_db.upsert_deal(con, account_id=aid, deal_name="重要度高", stage="初回アポ実施",
                             owner="中島", importance="高")
    # どちらも満たさない→対象外
    d3 = sfa_db.upsert_deal(con, account_id=aid, deal_name="対象外", stage="要件詰め",
                             owner="早瀬", importance="低", value_lumpsum=999)
    # クローズ済み→対象外（ステージ・重要度条件を満たしていても除外）
    d4 = sfa_db.upsert_deal(con, account_id=aid, deal_name="クローズ済み", stage="提案",
                             owner="吉江", status="closed")

    pl = webapp._partner_meeting_pipeline_lists(con)
    names = {it["name"] for it in pl["Sales"]}
    assert names == {"提案中", "重要度高"}
    assert "対象外" not in names and "クローズ済み" not in names
    # Pipeline金額はvalue_lumpsumのみ、未入力はNone(na表示)
    by_name = {it["name"]: it["pipeline_value"] for it in pl["Sales"]}
    assert by_name["提案中"] == 500
    assert by_name["重要度高"] is None


def test_sales_sort_order_owner_then_stage_then_value_then_id(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    # owner=吉江(マスタ順0)、ステージ=提案(ランク2) と クロージング(ランク1)
    d_a = sfa_db.upsert_deal(con, account_id=aid, deal_name="吉江_提案", stage="提案", owner="吉江", value_lumpsum=100)
    d_b = sfa_db.upsert_deal(con, account_id=aid, deal_name="吉江_クロージング", stage="クロージング",
                              owner="吉江", value_lumpsum=100)
    # owner=中島(マスタ順1、吉江より後)
    d_c = sfa_db.upsert_deal(con, account_id=aid, deal_name="中島_提案", stage="提案", owner="中島", value_lumpsum=900)
    pl = webapp._partner_meeting_pipeline_lists(con)
    order = [it["name"] for it in pl["Sales"]]
    # 吉江(マスタ順が先)の中ではクロージング(ランク1)が提案(ランク2)より先、その後に中島
    assert order == ["吉江_クロージング", "吉江_提案", "中島_提案"]


def test_sales_excludes_won_deals(db_path):
    """2026-10-07実機フィードバック「Salesに受注分は不要」の回帰テスト。
    重要度が高でもstage='受注'なら対象外。"""
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    sfa_db.upsert_deal(con, account_id=aid, deal_name="受注済み", stage="受注", owner="吉江", importance="高")
    pl = webapp._partner_meeting_pipeline_lists(con)
    assert "受注済み" not in {it["name"] for it in pl["Sales"]}


def test_sales_two_tier_grouping(db_path):
    """2026-10-07実機フィードバック: 上段=[提案以上 or 重要度高]、下段=[提案未満 and 重要度中]、
    に分けてまとめ、各段の中では既存の並び順を維持する。"""
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    # 上段: ステージ提案(stage条件のみ)
    sfa_db.upsert_deal(con, account_id=aid, deal_name="上段_提案", stage="提案", owner="高橋")
    # 上段: 重要度高(ステージは初回アポ実施でも該当)
    sfa_db.upsert_deal(con, account_id=aid, deal_name="上段_重要度高", stage="初回アポ実施",
                        owner="吉江", importance="高")
    # 下段: 提案未満 かつ 重要度中
    sfa_db.upsert_deal(con, account_id=aid, deal_name="下段_重要度中", stage="要件詰め",
                        owner="吉江", importance="中")
    pl = webapp._partner_meeting_pipeline_lists(con)
    order = [it["name"] for it in pl["Sales"]]
    assert order.index("上段_提案") < order.index("下段_重要度中")
    assert order.index("上段_重要度高") < order.index("下段_重要度中")


# ── 10. Deliveryの並び順は開始日新しい順のみ（主担当順は入れない、2026-10-07単純化） ──

def test_delivery_sort_is_pure_start_date_descending(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    base = date.fromisoformat(THIS_MONDAY)
    # owner順では逆転するが、開始日降順では正しい順になることを確認する
    d1 = sfa_db.upsert_deal(con, account_id=aid, deal_name="古い開始_高橋", stage="受注", owner="高橋")
    sfa_db.create_delivery(con, deal_id=d1, start_week=sfa_db._monday_of(base - timedelta(weeks=4)),
                            end_week=sfa_db._monday_of(base + timedelta(weeks=10)))
    d2 = sfa_db.upsert_deal(con, account_id=aid, deal_name="新しい開始_吉江", stage="受注", owner="吉江")
    sfa_db.create_delivery(con, deal_id=d2, start_week=sfa_db._monday_of(base - timedelta(weeks=1)),
                            end_week=sfa_db._monday_of(base + timedelta(weeks=10)))
    pl = webapp._partner_meeting_pipeline_lists(con)
    order = [it["name"] for it in pl["Delivery"]]
    assert order.index("新しい開始_吉江") < order.index("古い開始_高橋")


# ── 11. 開始週=当該週の場合はWeek1（開始間近ではない） ──

def test_week1_badge_when_start_week_is_this_week():
    html = webapp._delivery_near_badges_html(THIS_MONDAY, "")
    assert "Week1" in html
    assert "開始間近" not in html


def test_near_start_badge_when_start_is_within_window_but_not_this_week():
    near = (date.fromisoformat(THIS_MONDAY) + timedelta(days=7)).isoformat()
    html = webapp._delivery_near_badges_html(near, "")
    assert "開始間近" in html
    assert "Week1" not in html


# ── 12. ④生産性: 件数がPipeline Deliveryより少なくならない（overflow:hiddenクリップ回帰） ──

def test_productivity_html_contains_all_rows_and_is_scrollable(db_path):
    """2026-10-07実機フィードバック「生産性に載ってるプロジェクトが、PipelineのDeliveryに
    載ってる数より少ない」の回帰テスト。原因はデータ側の抽出漏れではなく、固定高+
    overflow:hiddenのカードに大量行を流し込むと画面に収まらない分がCSSで物理的に
    見えなくなっていたこと（内部スクロール領域が無かった）。十分な件数(20件)のDeliveryを
    用意し、生成されたHTMLに全件の名前が含まれ、かつスクロール領域(.mr-table-scroll)で
    ラップされていることを確認する。"""
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    names = [f"案件{i:02d}" for i in range(20)]
    for name in names:
        did = sfa_db.upsert_deal(con, account_id=aid, deal_name=name, stage="受注", owner="吉江")
        sfa_db.create_delivery(con, deal_id=did, start_week=THIS_MONDAY,
                                end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=4)))
    html = webapp._partner_meeting_productivity_html(con)
    assert all(name in html for name in names)
    assert 'class="mr-table-scroll"' in html


# ── 6. ①Pipeline(Delivery): 全社定例と同じ抽出条件+開始間近/終了間近ワッペン ──

def test_delivery_bucket_excludes_completed_and_shows_near_badges(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")
    near_start = (date.fromisoformat(THIS_MONDAY) + timedelta(days=7)).isoformat()  # 当該週+2週間以内
    far_end = (date.fromisoformat(THIS_MONDAY) + timedelta(days=90)).isoformat()    # 窓の外
    d1 = sfa_db.upsert_deal(con, account_id=aid, deal_name="進行中", stage="受注", owner="岩崎")
    sfa_db.create_delivery(con, deal_id=d1, start_week=near_start, end_week=far_end)
    d2 = sfa_db.upsert_deal(con, account_id=aid, deal_name="完了済み", stage="受注", owner="岩崎")
    dv2 = sfa_db.create_delivery(con, deal_id=d2, start_week=near_start, end_week=far_end)
    sfa_db.update_delivery(con, dv2, status="完了")

    pl = webapp._partner_meeting_pipeline_lists(con)
    names = {it["name"] for it in pl["Delivery"]}
    assert names == {"進行中"}
    row = next(it for it in pl["Delivery"] if it["name"] == "進行中")
    assert "開始間近" in row["near_badges"]
    assert "終了間近" not in row["near_badges"]


# ── 7. ②テーマ別状況のAreaは専用4区分・1列(Comment) ──

def test_partner_meeting_areas_differ_from_monthly_report(server, db_path):
    assert sfa_db.PARTNER_MEETING_AREAS == ["marketing", "development", "product", "finance"]
    assert sfa_db.PARTNER_MEETING_COLS == ["comment"]
    con = sfa_db.connect(db_path)
    sfa_db.create_partner_meeting_report(con, THIS_MONDAY)
    con.close()
    code, body = _get(server + f"/partner-meeting/{THIS_MONDAY}", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert all(lbl in html for lbl in ("Marketing", "Development", "Product", "Finance"))


# ── 8. ④生産性: 当該週時点の生産性降順・責任者バッジ・閾値線 ──

def test_productivity_rows_sorted_descending_by_current(db_path):
    con = sfa_db.connect(db_path)
    aid = sfa_db.upsert_account(con, name="A社")

    def _make(name, owner, fte_pct, fee_total):
        did = sfa_db.upsert_deal(con, account_id=aid, deal_name=name, stage="受注", owner="岩崎")
        dvid = sfa_db.create_delivery(con, deal_id=did, start_week=THIS_MONDAY,
                                       end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=8)))
        sfa_db.update_delivery(con, dvid, fee_mode="total", fee_total=fee_total, responsible_owner=owner)
        sfa_db.add_delivery_assignment(con, delivery_id=dvid, role="PM", owner=owner,
                                        from_week=THIS_MONDAY, to_week=THIS_MONDAY, fte_pct=fte_pct)
        return dvid

    _make("低生産性案件", "高橋", 100, 100)
    _make("高生産性案件", "土屋", 20, 100)

    rows = webapp._partner_meeting_productivity_rows(con)
    names = [r["name"] for r in rows]
    assert names.index("高生産性案件") < names.index("低生産性案件")
    assert {r["owner"] for r in rows} == {"高橋", "土屋"}


def test_productivity_thresholds_are_fixed_constants():
    assert webapp._PARTNER_PRODUCTIVITY_THRESHOLDS == [
        (150, "赤字ライン", "#dc2626"), (350, "最低目標", "#d97706"),
        (400, "目標", "#2563eb"), (500, "優良", "#16a34a"),
    ]


# ── 9. 全社定例側への遡及適用: Deliveryに開始間近/終了間近ワッペンを表示 ──

def test_monthly_report_delivery_also_shows_near_badges(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, THIS_MONDAY[:7])
    aid = sfa_db.upsert_account(con, name="A社")
    near_start = (date.fromisoformat(THIS_MONDAY) + timedelta(days=3)).isoformat()
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="間近案件", stage="受注")
    sfa_db.create_delivery(con, deal_id=did, start_week=near_start,
                            end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=20)))
    con.close()

    code, body = _get(server + f"/monthly-report/{THIS_MONDAY[:7]}", headers=_header(KEIEI_EMAIL))
    assert code == 200
    assert "開始間近" in body.decode("utf-8")


# ── 10. Deliveryは責任者バッジのみ表示（主担当バッジは出さない。2026-10-07実機フィードバック） ──

def test_partner_meeting_delivery_shows_only_responsible_owner_badge(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_partner_meeting_report(con, THIS_MONDAY)
    aid = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="案件Z", stage="受注", owner="中島")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week=THIS_MONDAY,
                                   end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=10)))
    sfa_db.update_delivery(con, dvid, responsible_owner="高橋")
    con.close()

    code, body = _get(server + f"/partner-meeting/{THIS_MONDAY}", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert "高橋" in html  # 責任者は表示される
    assert "中島" not in html  # 主担当は表示されない


# ── 11. 全社定例・パートナー定例双方、案件名クリックでSFA詳細ページへ遷移できる ──

def test_monthly_report_deal_name_links_to_delivery_detail(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_monthly_report(con, THIS_MONDAY[:7])
    aid = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="リンク確認案件", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week=THIS_MONDAY,
                                   end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=5)))
    con.close()

    code, body = _get(server + f"/monthly-report/{THIS_MONDAY[:7]}", headers=_header(KEIEI_EMAIL))
    assert code == 200
    assert f'href="/delivery/{dvid}"' in body.decode("utf-8")


def test_partner_meeting_sales_and_delivery_names_link_to_detail_pages(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_partner_meeting_report(con, THIS_MONDAY)
    aid = sfa_db.upsert_account(con, name="A社")
    deal_id = sfa_db.upsert_deal(con, account_id=aid, deal_name="Sales案件", stage="提案", owner="吉江")
    did2 = sfa_db.upsert_deal(con, account_id=aid, deal_name="Delivery案件", stage="受注", owner="吉江")
    dvid = sfa_db.create_delivery(con, deal_id=did2, start_week=THIS_MONDAY,
                                   end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=5)))
    con.close()

    code, body = _get(server + f"/partner-meeting/{THIS_MONDAY}", headers=_header(KEIEI_EMAIL))
    assert code == 200
    html = body.decode("utf-8")
    assert f'href="/deal/{deal_id}"' in html
    assert f'href="/delivery/{dvid}"' in html


def test_partner_meeting_productivity_name_links_to_delivery_detail(server, db_path):
    con = sfa_db.connect(db_path)
    sfa_db.create_partner_meeting_report(con, THIS_MONDAY)
    aid = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=aid, deal_name="生産性リンク案件", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, start_week=THIS_MONDAY,
                                   end_week=sfa_db._monday_of(date.fromisoformat(THIS_MONDAY) + timedelta(weeks=5)))
    con.close()

    code, body = _get(server + f"/partner-meeting/{THIS_MONDAY}", headers=_header(KEIEI_EMAIL))
    assert code == 200
    assert f'href="/delivery/{dvid}"' in body.decode("utf-8")
