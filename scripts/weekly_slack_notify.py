"""
週次商談確認Slack通知スクリプト。
水曜17:30 JST（GitHub Actions cron: 30 8 * * 3）に各担当へDMを送信。

実行: python scripts/weekly_slack_notify.py

環境変数:
  SLACK_BOT_TOKEN  - SlackアプリのBot User OAuth Token (xoxb-...)
  WEEKLY_SHEET_ID  - 確認用スプシID
  SFA_DB_PATH      - DBパス（省略時はcowork_sfa.db）

Slackアプリ設定:
  OAuth Scopes (Bot): users:read, users:read.email, im:write, chat:write
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SHEET_ID = os.environ.get("WEEKLY_SHEET_ID", "")
DB_PATH = os.environ.get("SFA_DB_PATH", str(ROOT / "cowork_sfa.db"))
CONFIG_PATH = ROOT / "config" / "owner_slack_map.json"
TOOL_URL = os.environ.get("SFA_TOOL_URL", "http://localhost:8787")


def slack_api(method: str, **kwargs) -> dict:
    url = f"https://slack.com/api/{method}"
    data = json.dumps(kwargs).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        print(f"[Slack] {method} error: {result.get('error')}")
    return result


def slack_get(method: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        print(f"[Slack] {method} error: {result.get('error')}")
    return result


def get_user_id_by_email(email: str) -> str | None:
    result = slack_get("users.lookupByEmail", {"email": email})
    if result.get("ok"):
        return result["user"]["id"]
    return None


def open_dm(user_id: str) -> str | None:
    result = slack_api("conversations.open", users=user_id)
    if result.get("ok"):
        return result["channel"]["id"]
    return None


def load_owner_map() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def get_deals_by_owner(db_path: str) -> dict[str, list[dict]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT d.deal_name, d.stage, d.next_milestone_date, d.next_milestone_label, d.owner
           FROM deals d WHERE d.status='open' AND d.owner IS NOT NULL
           ORDER BY d.owner, d.updated_at DESC"""
    ).fetchall()
    con.close()
    result: dict[str, list[dict]] = {}
    for r in rows:
        owner = r["owner"]
        result.setdefault(owner, []).append(dict(r))
    return result


def build_message(owner: str, deals: list[dict]) -> str:
    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit" if SHEET_ID else ""
    tool_url = f"{TOOL_URL}/deals?owner={urllib.parse.quote(owner)}" if TOOL_URL else ""

    lines = [
        f"【週次商談確認依頼】",
        f"{owner}さん、担当商談の状況確認をお願いします。",
        "",
        f"📋 担当中の商談: {len(deals)}件",
    ]
    for d in deals[:10]:  # 最大10件表示
        ms = d.get("next_milestone_date") or ""
        label = d.get("next_milestone_label") or ""
        ms_str = f" → {ms}" if ms else ""
        lines.append(f"  • {d['deal_name']} ({d.get('stage','')}){ms_str}")
    if len(deals) > 10:
        lines.append(f"  ...他 {len(deals)-10}件")

    lines.extend([""])
    if sheet_url:
        lines.append(f"🔗 スプシ: {sheet_url}")
    if tool_url:
        lines.append(f"✏️ 営業支援ツール: {tool_url}")
    lines.extend([
        "",
        "金曜18:00までに営業支援ツールで最新状況を更新してください。",
    ])
    return "\n".join(lines)


import urllib.parse


def main():
    if not SLACK_TOKEN:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)

    owner_map = load_owner_map()
    if not owner_map:
        print("[WARN] config/owner_slack_map.json が見つからないか空です。")

    deals_by_owner = get_deals_by_owner(DB_PATH)
    if not deals_by_owner:
        print("[INFO] 進行中の商談がありません。通知をスキップします。")
        return

    test_owner = os.environ.get("TEST_OWNER", "")  # テスト用: 指定した担当者のみ送信

    sent = 0
    for owner, deals in deals_by_owner.items():
        if test_owner and owner != test_owner:
            print(f"[SKIP] {owner}: TEST_OWNER={test_owner} のためスキップ")
            continue
        email = owner_map.get(owner)
        if not email:
            print(f"[SKIP] {owner}: メールアドレスが config に未設定")
            continue

        user_id = get_user_id_by_email(email)
        if not user_id:
            print(f"[SKIP] {owner} ({email}): Slackユーザーが見つかりません")
            continue

        channel_id = open_dm(user_id)
        if not channel_id:
            print(f"[SKIP] {owner}: DMチャネルを開けませんでした")
            continue

        message = build_message(owner, deals)
        result = slack_api("chat.postMessage", channel=channel_id, text=message)
        if result.get("ok"):
            print(f"[OK] {owner} に通知送信（{len(deals)}件）")
            sent += 1
        else:
            print(f"[ERROR] {owner} への送信失敗: {result.get('error')}")

    print(f"\n完了: {sent}人に通知送信。")


if __name__ == "__main__":
    main()
