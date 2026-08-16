"""
取り込みインボックス見逃し検知 Slackリマインドスクリプト（#98）。
毎朝 JST に、前日以前から残っている「未処理の取り込み」を検知して通知する。

対象は2種類（/api/intake_inbox_stale が返す）:
  1. status='inbox' のまま放置 — 誰も商談/論点へ割当てていない（#97のWeb `/intake-inbox`）。
  2. status='assigned' source='jamie' のまま放置 — Slackの候補ボタンで商談へは割当てたが、
     その面談についてSlackで一度も確定(`@NegoCollection`)しておらず、Jamie全文が
     どの活動履歴にも統合されないまま眠っている（#98 slack_bot.apply_to_dbで拾われるのを待ったまま）。

いずれも「Slackで確定して満足し、誰も気づかない」という運用上の見逃しを拾うための
安全網（docs/hearing-ai/DESIGN.md §10）。両方0件なら何も送らない。

投稿先: 当面は早瀬個人へのSlack DM固定（運用を見てから#salesへの切替を検討）。
       INTAKE_STALE_NOTIFY_MODE=channel にすると SALES_CHANNEL_ID へ投稿するモードに切替可能
       （コード変更なしでRenderの環境変数のみで切替できるようにしてある）。

実行:
  python scripts/intake_inbox_stale_slack_reminder.py
  TARGET_DATE=2026-08-20 python scripts/intake_inbox_stale_slack_reminder.py  # 日付指定（テスト用）

環境変数:
  SLACK_BOT_TOKEN            - SlackアプリのBot User OAuth Token (xoxb-...)
  SFA_TOOL_URL               - SFAツールのベースURL（省略時 https://sfa-crm.onrender.com）
  SFA_API_TOKEN              - /api/intake_inbox_stale 用トークン
  INTAKE_STALE_NOTIFY_MODE   - "dm"（既定）or "channel"
  INTAKE_STALE_DM_OWNER      - dmモードの宛先（owner_slack_map.jsonのキー。既定: 早瀬）
  SALES_CHANNEL_ID           - channelモード時の投稿先チャンネルID
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
TOOL_URL = os.environ.get("SFA_TOOL_URL", "https://sfa-crm.onrender.com")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")
NOTIFY_MODE = os.environ.get("INTAKE_STALE_NOTIFY_MODE", "dm").strip().lower()
DM_OWNER = os.environ.get("INTAKE_STALE_DM_OWNER", "早瀬").strip()
CHANNEL_ID = os.environ.get("SALES_CHANNEL_ID", "")
CONFIG_PATH = ROOT / "config" / "owner_slack_map.json"
JST = timezone(timedelta(hours=9))


def slack_api(method: str, **kwargs) -> dict:
    url = f"https://slack.com/api/{method}"
    data = json.dumps(kwargs).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e)}
    if not result.get("ok"):
        print(f"[Slack] {method} error: {result.get('error')}")
    return result


def slack_get(method: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def load_owner_map() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def resolve_dm_channel(owner: str) -> str | None:
    email = load_owner_map().get(owner)
    if not email:
        print(f"[ERROR] 「{owner}」のSlackメールがowner_slack_map.jsonに未登録です。")
        return None
    r = slack_get("users.lookupByEmail", {"email": email})
    if not r.get("ok"):
        print(f"[ERROR] {owner}({email}) のSlackユーザーが見つかりません: {r.get('error')}")
        return None
    user_id = r["user"]["id"]
    r2 = slack_api("conversations.open", users=user_id)
    if not r2.get("ok"):
        print(f"[ERROR] {owner} のDMチャネルを開けませんでした: {r2.get('error')}")
        return None
    return r2["channel"]["id"]


def fetch_stale() -> dict:
    url = f"{TOOL_URL}/api/intake_inbox_stale"
    if SFA_API_TOKEN:
        url += f"?token={urllib.parse.quote(SFA_API_TOKEN)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def build_message(inbox: list, assigned: list) -> str:
    lines = ["🔔 取り込みインボックスに、前日以前から残っている未処理があります。"]
    if inbox:
        lines.append(f"\n・未割当（商談/論点への割当待ち）: {len(inbox)}件")
        for r in inbox[:5]:
            lines.append(f"   - {r.get('title') or '(無題)'}（{r.get('occurred_on') or '—'}）")
        if len(inbox) > 5:
            lines.append(f"   …他{len(inbox) - 5}件")
    if assigned:
        lines.append(f"\n・商談に割当済みだが、Slackで一度も確定されずJamie全文が未統合: {len(assigned)}件")
        for r in assigned[:5]:
            lines.append(f"   - {r.get('title') or '(無題)'}（{r.get('occurred_on') or '—'}）")
        if len(assigned) > 5:
            lines.append(f"   …他{len(assigned) - 5}件")
    lines.append(f"\n🔗 {TOOL_URL}/intake-inbox")
    return "\n".join(lines)


def main():
    if not SLACK_TOKEN:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)
    if not SFA_API_TOKEN:
        print("[ERROR] SFA_API_TOKEN が設定されていません。")
        sys.exit(1)

    today = os.environ.get("TARGET_DATE", "").strip() or datetime.now(JST).strftime("%Y-%m-%d")
    print(f"[INFO] 対象日: {today} (mode={NOTIFY_MODE})")

    data = fetch_stale()
    inbox = data.get("inbox") or []
    assigned = data.get("assigned_unconsumed") or []
    print(f"[INFO] inbox放置={len(inbox)}件 / assigned放置={len(assigned)}件")

    if not inbox and not assigned:
        print("[INFO] 未処理なし。送信をスキップします。")
        return

    if NOTIFY_MODE == "channel":
        if not CHANNEL_ID:
            print("[ERROR] channelモードですがSALES_CHANNEL_IDが未設定です。")
            sys.exit(1)
        channel_id = CHANNEL_ID
    else:
        channel_id = resolve_dm_channel(DM_OWNER)
        if not channel_id:
            sys.exit(1)

    result = slack_api("chat.postMessage", channel=channel_id, text=build_message(inbox, assigned))
    if result.get("ok"):
        print("[OK] リマインドを送信しました。")
    else:
        print(f"[ERROR] 送信失敗: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
