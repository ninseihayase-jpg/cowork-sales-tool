"""#29 P3: 自動連携（Jamie webhook）受信→インボックス保存の検証。

署名検証・冪等化・パース・未割り当て一覧・割り当てを一時DBで確認する。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tempfile
import time
from pathlib import Path

from cowork import sfa_db, webapp


class _FakeHandler:
    """_handle_jamie_webhook が使う最小のレスポンダ。"""

    def __init__(self, headers):
        self.headers = headers
        self.code = None
        self.body = b""

    def send_response(self, c):
        self.code = c

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    class _W:
        def __init__(self, o):
            self.o = o

        def write(self, b):
            self.o.body = b

    @property
    def wfile(self):
        return _FakeHandler._W(self)


def _sig(secret, body):
    t = str(int(time.time()))
    mac = hmac.new(secret.encode(), (t + ".").encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v0={mac}"


def _payload():
    return json.dumps({
        "event": "meeting.completed", "id": "mtg-1",
        "data": {
            "title": "Acme商談キックオフ", "startTime": "2026-08-14T05:00:00Z",
            "transcript": [{"speakerName": "田中", "text": "直材調達の方針"},
                           {"speakerName": "早瀬", "text": "間材は別立て"}],
            "summary": {"markdown": "要約テキスト"},
            "event": {"attendees": [{"name": "客", "email": "x@acme.co.jp"}]},
        },
    }).encode("utf-8")


def _fresh_db():
    d = tempfile.mkdtemp(prefix="sfa_iw_")
    path = str(Path(d) / "t.db")
    sfa_db.init_db(path)
    return d, sfa_db.connect(path)


def test_webhook_verify_parse_and_idempotency(monkeypatch):
    d, con = _fresh_db()
    try:
        monkeypatch.setattr(webapp, "JAMIE_WEBHOOK_SECRET", "sek")
        monkeypatch.setattr(webapp, "JAMIE_WEBHOOK_API_KEY", "")
        body = _payload()
        h = _FakeHandler({"x-jamie-signature": _sig("sek", body)})
        webapp._handle_jamie_webhook(h, con, body)
        assert h.code == 200
        items = sfa_db.list_inbox_transcripts(con)
        assert len(items) == 1
        assert items[0]["title"] == "Acme商談キックオフ"
        assert items[0]["occurred_on"] == "2026-08-14"
        # 冪等: 同一 meeting id は重複しない
        h2 = _FakeHandler({"x-jamie-signature": _sig("sek", body)})
        webapp._handle_jamie_webhook(h2, con, body)
        assert len(sfa_db.list_inbox_transcripts(con)) == 1
        # 生データ本文が保存されている
        full = sfa_db.get_intake_transcript(con, items[0]["id"])
        assert "直材調達の方針" in (full["transcript"] or "")
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_webhook_rejects_bad_signature_and_unconfigured(monkeypatch):
    d, con = _fresh_db()
    try:
        # 未設定 → 503（fail-closed）
        monkeypatch.setattr(webapp, "JAMIE_WEBHOOK_SECRET", "")
        monkeypatch.setattr(webapp, "JAMIE_WEBHOOK_API_KEY", "")
        h = _FakeHandler({})
        webapp._handle_jamie_webhook(h, con, _payload())
        assert h.code == 503
        assert sfa_db.count_inbox_transcripts(con) == 0
        # 署名不一致 → 401
        monkeypatch.setattr(webapp, "JAMIE_WEBHOOK_SECRET", "sek")
        hb = _FakeHandler({"x-jamie-signature": "t=%d,v0=bad" % int(time.time())})
        webapp._handle_jamie_webhook(hb, con, _payload())
        assert hb.code == 401
        assert sfa_db.count_inbox_transcripts(con) == 0
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)


def test_init_db_migrates_legacy_intake_table():
    """本番再現: 新列の無い旧 intake_transcripts に対し init_db が失敗せず列＋索引を追加する。
    （SCHEMAのCREATE INDEXが未追加列を参照して落ちた回帰の防止）。"""
    import sqlite3
    d = tempfile.mkdtemp(prefix="sfa_iw_")
    try:
        path = str(Path(d) / "t.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE intake_transcripts(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "kind TEXT NOT NULL, entity_id INTEGER NOT NULL, source TEXT DEFAULT 'paste', "
            "filename TEXT, transcript TEXT, file_blob BLOB, "
            "created_at TEXT DEFAULT (datetime('now')))")
        con.execute("INSERT INTO intake_transcripts(kind,entity_id,transcript) VALUES('issue',1,'旧')")
        con.commit()
        con.close()
        sfa_db.init_db(path)   # 例外なく完了すること
        sfa_db.init_db(path)   # 冪等
        con = sfa_db.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(intake_transcripts)")}
        assert {"external_source", "external_id", "title", "occurred_on",
                "attendees_json", "raw_summary", "status"} <= cols
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_intake_transcripts_ext" in idx
        assert con.execute("SELECT transcript FROM intake_transcripts WHERE id=1").fetchone()[0] == "旧"
        con.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_assign_inbox_moves_into_entity_originals():
    d, con = _fresh_db()
    try:
        tid = sfa_db.add_inbox_transcript(
            con, external_source="jamie", external_id="m2", title="論点会議",
            occurred_on="2026-08-14", transcript="本文", attendees_json="[]")
        iid = sfa_db.upsert_deal_issue(con, deal_id=None, issue="論点X", status="議論中")
        assert sfa_db.count_inbox_transcripts(con) == 1
        sfa_db.assign_inbox_transcript(con, tid, kind="issue", entity_id=iid)
        assert sfa_db.count_inbox_transcripts(con) == 0  # インボックスから外れる
        originals = sfa_db.list_intake_transcripts(con, "issue", iid)
        assert len(originals) == 1 and originals[0]["id"] == tid
    finally:
        con.close()
        shutil.rmtree(d, ignore_errors=True)
