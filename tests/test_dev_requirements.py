"""開発要件一覧(#165, 2026-09-04)の回帰テスト。

ユーザー確定仕様:
- dev_projects(営業側の開発専用機能)とは別の新規テーブル。
- 商談が「提案」以降ステージに到達、またはDeliveryが作成された時点で1行を自動起票
  （詳細は空欄=dev_involved未判定）。同一商談が両トリガーを踏むと2行できるケースを許容する。
- 「開発有無=無」の行は識別情報(紐づけ先/アカウント/案件名/ステージ/営業主担当)以外を
  "-"表示にする（一覧・xlsx出力の両方）。
- xlsx出力（/dev-requirements/export.xlsx）。
- 初回バックフィルはscripts/backfill_dev_requirements.pyで実施し、
  「プロジェクト概要・ゴール」はHaiku（現状メモ＋直近活動履歴が入力）で自動生成する。
"""
from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, webapp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.backfill_dev_requirements as backfill_mod

BASIC_USER = "test_user"
BASIC_PASS = "test_pass_1234"


@pytest.fixture
def con():
    d = tempfile.mkdtemp(prefix="sfa_dev_req_")
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
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _auth_header():
    token = base64.b64encode(f"{BASIC_USER}:{BASIC_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get_bytes(url):
    req = urllib.request.Request(url, headers=_auth_header(), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── sfa_db層: トリガー・CRUD ──

def test_ensure_dev_requirement_for_deal_only_fires_on_trigger_stages(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="要件詰め", status="open")
    assert sfa_db.ensure_dev_requirement_for_deal(con, did, "要件詰め") is None
    assert sfa_db.get_dev_requirement_by_link(con, "deal", did) is None

    assert sfa_db.ensure_dev_requirement_for_deal(con, did, "提案") is not None
    row = sfa_db.get_dev_requirement_by_link(con, "deal", did)
    assert row is not None
    assert row["dev_involved"] is None  # 未判定


def test_ensure_dev_requirement_for_deal_is_idempotent(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")
    rid1 = sfa_db.ensure_dev_requirement_for_deal(con, did, "提案")
    rid2 = sfa_db.ensure_dev_requirement_for_deal(con, did, "クロージング")
    assert rid1 is not None
    assert rid2 is None
    assert len(sfa_db.list_dev_requirements(con)) == 1


def test_create_delivery_auto_creates_dev_requirement(con):
    """Delivery作成時は必ず開発要件行が自動起票される（create_delivery内に集約、#165）。"""
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="受注", status="open")
    dv_id = sfa_db.create_delivery(con, deal_id=did, title="DeliveryX")
    row = sfa_db.get_dev_requirement_by_link(con, "delivery", dv_id)
    assert row is not None
    assert row["dev_involved"] is None


def test_deal_and_delivery_triggers_both_fire_can_produce_two_rows(con):
    """同一商談が「提案到達」と「Delivery作成」の両方を踏むと2行できる仕様（ユーザー確定）。"""
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")
    sfa_db.ensure_dev_requirement_for_deal(con, did, "提案")
    sfa_db.create_delivery(con, deal_id=did, title="DeliveryX")
    rows = sfa_db.list_dev_requirements(con)
    assert len(rows) == 2
    assert {r["link_type"] for r in rows} == {"deal", "delivery"}


def test_set_dev_requirement_field_does_not_clobber_other_fields(con):
    """set_dev_requirement_fieldは1列だけを更新し、他の既存値を消さない
    （upsert_task/upsert_deal_issueの全列上書きfootgunを避けるための専用ヘルパー）。"""
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")
    rid = sfa_db.ensure_dev_requirement_for_deal(con, did, "提案")
    sfa_db.set_dev_requirement_field(con, rid, "overview", "概要A")
    sfa_db.set_dev_requirement_field(con, rid, "budget", "500万円")
    row = sfa_db.get_dev_requirement(con, rid)
    assert row["overview"] == "概要A"
    assert row["budget"] == "500万円"


def test_list_dev_requirements_enriches_with_deal_and_delivery_context(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案",
                             status="open", owner="早瀬")
    sfa_db.ensure_dev_requirement_for_deal(con, did, "提案")
    rows = sfa_db.list_dev_requirements(con)
    assert len(rows) == 1
    assert rows[0]["account_name"] == "A社"
    assert rows[0]["project_name"] == "案件X"
    assert rows[0]["stage"] == "提案"
    assert rows[0]["owner"] == "早瀬"


def test_list_dev_requirements_excludes_rows_with_deleted_link_target(con):
    """紐づけ先の商談が削除されると、開発要件一覧からも除外される（参照切れガード）。"""
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")
    sfa_db.ensure_dev_requirement_for_deal(con, did, "提案")
    con.execute("DELETE FROM deals WHERE id=?", (did,))
    con.commit()
    assert sfa_db.list_dev_requirements(con) == []


def test_dev_involved_options_and_masters_registered(con):
    assert sfa_db.get_master_list(con, "dev_req_confidentiality") == sfa_db.DEV_REQUIREMENT_CONFIDENTIALITY_LEVELS
    assert sfa_db.get_master_list(con, "dev_req_contract_types") == sfa_db.DEV_REQUIREMENT_CONTRACT_TYPES
    assert sfa_db.get_master_list(con, "dev_req_statuses") == sfa_db.DEV_REQUIREMENT_STATUSES


# ── webapp層: xlsx出力 ──

def test_build_dev_requirements_xlsx_masks_fields_when_no_dev(con):
    import io
    import openpyxl

    acc = sfa_db.upsert_account(con, name="A社")
    did1 = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")
    rid1 = sfa_db.ensure_dev_requirement_for_deal(con, did1, "提案")
    sfa_db.set_dev_requirement_field(con, rid1, "overview", "開発ありの概要")

    did2 = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件Y", stage="提案", status="open")
    rid2 = sfa_db.ensure_dev_requirement_for_deal(con, did2, "提案")
    sfa_db.set_dev_requirement_field(con, rid2, "dev_involved", "無")
    sfa_db.set_dev_requirement_field(con, rid2, "overview", "これは表示されないはず")

    xls = webapp.build_dev_requirements_xlsx(con)
    wb = openpyxl.load_workbook(io.BytesIO(xls))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    overview_idx = header.index("プロジェクト概要・ゴール")
    account_idx = header.index("アカウント")
    project_idx = header.index("案件名")

    by_project = {r[project_idx]: r for r in rows[1:]}
    assert by_project["案件X"][overview_idx] == "開発ありの概要"
    assert by_project["案件Y"][overview_idx] == "-"
    # 識別情報は「開発有無=無」でもマスクされない
    assert by_project["案件Y"][account_idx] == "A社"


def test_dev_requirements_export_route(server):
    code, resp = _get_bytes(server + "/dev-requirements/export.xlsx")
    assert code == 200


# ── scripts/backfill_dev_requirements.py ──

def test_backfill_find_candidates_filters_by_stage(con):
    acc = sfa_db.upsert_account(con, name="A社")
    sfa_db.upsert_deal(con, account_id=acc, deal_name="対象外", stage="要件詰め", status="open")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="対象", stage="提案", status="open")
    candidates = backfill_mod.find_candidates(con)
    assert len(candidates) == 1
    assert candidates[0]["link_id"] == did
    assert candidates[0]["link_type"] == "deal"


def test_backfill_skips_already_created_rows(con):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="対象", stage="提案", status="open")
    sfa_db.ensure_dev_requirement_for_deal(con, did, "提案")
    assert backfill_mod.find_candidates(con) == []


def test_backfill_generate_overview_uses_note_and_activities(con, monkeypatch):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="請求書自動化", stage="提案",
                             status="open", note="請求書処理を自動化したい")
    sfa_db.add_activity(con, deal_id=did, type="面談", occurred_on="2026-09-01",
                        body="受領から支払いまでを自動化したいとの要望")

    captured = {}

    def _fake_haiku(prompt, **kw):
        captured["prompt"] = prompt
        return "生成された概要"

    monkeypatch.setattr(webapp, "_call_claude_haiku", _fake_haiku)
    overview = backfill_mod.generate_overview(con, did)
    assert overview == "生成された概要"
    assert "請求書処理を自動化したい" in captured["prompt"]
    assert "受領から支払いまでを自動化したい" in captured["prompt"]


def test_backfill_generate_overview_empty_when_no_deal(con):
    assert backfill_mod.generate_overview(con, None) == ""


def test_backfill_apply_creates_rows_and_sets_overview(con, monkeypatch):
    acc = sfa_db.upsert_account(con, name="A社")
    did = sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")
    monkeypatch.setattr(webapp, "_call_claude_haiku", lambda prompt, **kw: "自動生成された概要")

    candidates = backfill_mod.find_candidates(con)
    n = backfill_mod.apply_backfill(con, candidates)
    assert n == 1
    row = sfa_db.get_dev_requirement_by_link(con, "deal", did)
    assert row["overview"] == "自動生成された概要"


def test_backfill_apply_survives_haiku_failure(con, monkeypatch):
    """Haiku生成が失敗しても、行の作成自体は失敗しない（overview空欄で継続）。"""
    acc = sfa_db.upsert_account(con, name="A社")
    sfa_db.upsert_deal(con, account_id=acc, deal_name="案件X", stage="提案", status="open")

    def _boom(prompt, **kw):
        raise RuntimeError("API error")
    monkeypatch.setattr(webapp, "_call_claude_haiku", _boom)

    candidates = backfill_mod.find_candidates(con)
    n = backfill_mod.apply_backfill(con, candidates)
    assert n == 1
    rows = sfa_db.list_dev_requirements(con)
    assert rows[0]["overview"] is None
