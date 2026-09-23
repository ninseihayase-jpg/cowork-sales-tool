"""取り込みインボックスでDeliveryも割り当て先に選べるようにした機能の回帰テスト(2026-09-09)。

ユーザー要望: 「取り込み機能で、Deliveryも選べるように」。商談/社内PJに加えて、
Delivery（納品案件）へ会議の文字起こしを割り当て、AI整形→Delivery議論メモ
(rich_notes kind='delivery')として保存できるようにした。
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
    d = tempfile.mkdtemp(prefix="sfa_dv_intake_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    conn = sfa_db.connect(path)
    yield conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def server(tmp_path, monkeypatch):
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", BASIC_USER)
    monkeypatch.setattr(webapp, "GOOGLE_CLIENT_SECRET", BASIC_PASS)
    handler_cls = webapp._make_handler(db_path, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
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


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=_auth_header(), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.geturl(), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode()


def _get(url):
    req = urllib.request.Request(url, headers=_auth_header(), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _s(x):
    return x.decode() if isinstance(x, (bytes, bytearray)) else x


def _make_delivery(con, title="Deliveryテスト案件"):
    acc = sfa_db.upsert_account(con, name="デリバリー株式会社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="X案件", stage="受注")
    dvid = sfa_db.create_delivery(con, deal_id=did, title=title, status="進行中")
    return acc, did, dvid


# ── 共有コンポーネント側の設定 ──

def test_rich_note_kinds_includes_delivery():
    assert "delivery" in sfa_db.RICH_NOTE_KINDS


# ── インボックス一覧: 種別ドロップダウン・target候補 ──

def test_inbox_page_has_delivery_type_option(con):
    _make_delivery(con)
    sfa_db.add_inbox_transcript(con, external_source="jamie", external_id="m1",
                               title="面談", occurred_on="2026-09-08",
                               transcript="本文", attendees_json="[]")
    html = _s(webapp.intake_inbox_page(con))
    assert '<option value="delivery">Delivery</option>' in html
    assert 'data-type="delivery"' in html


def test_inbox_target_options_includes_delivery():
    deliveries = [{"id": 1, "account_name": "A社", "title": "案件A", "deal_name": "X"}]
    html = webapp._inbox_target_options([], [], deliveries)
    assert 'value="delivery:1"' in html
    assert 'data-type="delivery"' in html
    assert "案件A" in html


def test_inbox_candidates_matches_delivery_by_account_name():
    deliveries = [{"id": 5, "account_name": "加藤製作所株式会社", "title": "生産管理システム",
                  "deal_name": "X", "account_aliases": None}]
    cands = webapp._inbox_candidates("加藤製作所株式会社との定例会議", [], [], [], deliveries)
    assert len(cands) == 1
    assert cands[0][0] == "delivery:5"
    assert cands[0][1].startswith("Delivery: 加藤製作所株式会社")


def test_inbox_candidates_excludes_unrelated_delivery():
    deliveries = [{"id": 9, "account_name": "全く無関係な株式会社", "title": "無関係案件",
                  "deal_name": "", "account_aliases": None}]
    assert webapp._inbox_candidates("サンプル商事との打ち合わせ", [], [], [], deliveries) == []


# ── Delivery割り当て・議論整形の入口ページ ──

def test_delivery_intake_page_renders_form(con):
    _, _, dvid = _make_delivery(con)
    dv = sfa_db.get_delivery(con, dvid)
    html = _s(webapp.delivery_intake_page(con, dv))
    assert "議論を取り込む" in html
    assert f"/delivery/{dvid}/intake/structure" in html


def test_delivery_review_page_posts_to_delivery_commit(con):
    _, _, dvid = _make_delivery(con)
    dv = sfa_db.get_delivery(con, dvid)
    html = _s(webapp.delivery_review_page(con, dv, {}, intake_transcript_id=None))
    assert f'action="/delivery/{dvid}/intake/commit"' in html
    assert "Delivery議論メモに保存" in html


# ── HTTPルート: 割り当て→AI整形→議論メモ確定の一連 ──

def test_assign_inbox_to_delivery_route(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    try:
        _, _, dvid = _make_delivery(con, title="割り当て先Delivery")
        tid = sfa_db.add_inbox_transcript(
            con, external_source="jamie", external_id="dv1",
            title="Delivery定例", occurred_on="2026-09-08",
            transcript="議論の文字起こし本文です。", attendees_json="[]")
        con.commit()
    finally:
        con.close()

    code, _, body = _post(f"{base}/intake-inbox/{tid}/assign", {"target": f"delivery:{dvid}"})
    assert code == 200
    assert f'/delivery/{dvid}/intake/commit' in body  # レビュー画面が直接返る（社内PJと同じ導線）

    con = sfa_db.connect(db_path)
    try:
        it = con.execute("SELECT kind, entity_id, status FROM intake_transcripts WHERE id=?", (tid,)).fetchone()
        assert it["kind"] == "delivery" and it["entity_id"] == dvid and it["status"] == "assigned"
    finally:
        con.close()


def test_delivery_intake_structure_and_commit_flow(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    try:
        _, _, dvid = _make_delivery(con)
        con.commit()
    finally:
        con.close()

    code, _, body = _post(f"{base}/delivery/{dvid}/intake/structure",
                          {"transcript": "Deliveryの議論の文字起こし本文です。"})
    assert code == 200
    m = re.search(r'name="intake_transcript_id" value="(\d+)"', body)
    assert m, "intake_transcript_idの隠しフィールドが無い"
    itid = int(m.group(1))

    con = sfa_db.connect(db_path)
    try:
        it = con.execute(
            "SELECT id FROM intake_transcripts WHERE kind='delivery' AND entity_id=?", (dvid,)).fetchone()
        assert it["id"] == itid
    finally:
        con.close()

    code, url, _ = _post(f"{base}/delivery/{dvid}/intake/commit",
                         {"intake_transcript_id": str(itid), "overview": "全体像テスト",
                          "note_title": "Delivery議論メモ"})
    assert code == 200
    assert url == f"{base}/delivery/{dvid}"

    con = sfa_db.connect(db_path)
    try:
        note = con.execute(
            "SELECT * FROM rich_notes WHERE kind='delivery' AND entity_id=?", (dvid,)).fetchone()
        assert note is not None and note["intake_transcript_id"] == itid
        usages = sfa_db.find_intake_transcript_usages(con, itid)
        assert len(usages) == 1 and usages[0]["type"] == "rich_note"
    finally:
        con.close()

    # delivery_formページにメモが表示されること
    code, html = _get(f"{base}/delivery/{dvid}")
    assert code == 200
    assert "Delivery議論メモ" in html or "議論メモ" in html
    assert "全体像テスト" in html or "議論整形メモ" in html or note["title"] in html


def test_delivery_intake_get_route_404_for_missing_delivery(server):
    base, _ = server
    code, _ = _get(f"{base}/delivery/999999/intake")
    assert code == 404


def test_rich_note_save_route_accepts_delivery_kind(server):
    base, db_path = server
    con = sfa_db.connect(db_path)
    try:
        _, _, dvid = _make_delivery(con)
        con.commit()
    finally:
        con.close()

    code, _, body = _post(f"{base}/rich-note/save",
                          {"kind": "delivery", "id": str(dvid), "title": "手動メモ", "body": "<p>本文</p>"})
    assert code == 200
    import json
    assert json.loads(body)["ok"] is True

    con = sfa_db.connect(db_path)
    try:
        note = con.execute("SELECT * FROM rich_notes WHERE kind='delivery' AND entity_id=?", (dvid,)).fetchone()
        assert note is not None and note["title"] == "手動メモ"
    finally:
        con.close()
