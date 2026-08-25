"""/slack/interactive の署名検証がNegoCollection・事務Bot・TaskBotの3アプリすべてを
受け付けることの回帰テスト（ユーザー報告2026-08-25: TaskBotのボタンでSlackが
「インタラクティビティURLが設定されていません」を表示。原因の一つは、署名検証が
デフォルト(NegoCollection)のSigning Secretしか試しておらず、事務Bot/TaskBot自身の
Signing Secretで署名されたリクエストは（Slack App側でURLを設定しても）401で
拒否されてしまうことだった）。
"""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cowork import sfa_db, slack_bot, webapp

NEGO_SECRET = "nego-secret"
DESK_SECRET = "desk-secret"
TASK_SECRET = "task-secret"


@pytest.fixture
def server(monkeypatch, tmp_path):
    db_path = str(tmp_path / "srv.db")
    sfa_db.init_db(db_path)
    monkeypatch.setattr(slack_bot, "SLACK_SIGNING_SECRET", NEGO_SECRET)
    monkeypatch.setattr(slack_bot, "SLACK_DESK_SIGNING_SECRET", DESK_SECRET)
    monkeypatch.setattr(slack_bot, "SLACK_TASK_SIGNING_SECRET", TASK_SECRET)
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


def _sign(secret, body):
    ts = str(int(time.time()))
    base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return ts, sig


def _post_interactive(server, secret, action_id="task_effort:1:軽"):
    body = "payload=" + urllib.request.quote(
        '{"type":"block_actions","actions":[{"action_id":"%s","value":"軽"}],'
        '"response_url":"","trigger_id":""}' % action_id)
    ts, sig = _sign(secret, body)
    req = urllib.request.Request(
        server + "/slack/interactive", data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode()
    except urllib.error.HTTPError as e:
        return e.code


def test_interactive_accepts_nego_secret(server):
    assert _post_interactive(server, NEGO_SECRET) == 200


def test_interactive_accepts_desk_secret(server):
    assert _post_interactive(server, DESK_SECRET) == 200


def test_interactive_accepts_task_secret(server):
    assert _post_interactive(server, TASK_SECRET) == 200


def test_interactive_rejects_unknown_secret(server):
    assert _post_interactive(server, "wrong-secret") == 401
