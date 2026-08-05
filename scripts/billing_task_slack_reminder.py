"""
請求関連タスク Slackリマインドスクリプト。
毎朝 08:00 JST（Render cron: 0 23 * * * UTC）に、種類=「経費・請求」の未完了事務タスクのうち
期限前日以降（期限超過含む）のものを、担当者へSlack DMでリマインドする。
完了したタスクは /api/admin_tasks から外れるため、自然にリマインドが止まる。

実行:
  python scripts/billing_task_slack_reminder.py
  TARGET_DATE=2026-08-10 python scripts/billing_task_slack_reminder.py  # 日付指定（テスト用）

環境変数:
  SLACK_BOT_TOKEN  - SlackアプリのBot User OAuth Token (xoxb-...)
  SFA_TOOL_URL     - SFAツールのベースURL（省略時 https://sfa-crm.onrender.com）
  SFA_API_TOKEN    - /api/admin_tasks 用トークン
  TARGET_DATE      - 省略時は実行日（JST）。テスト時に日付を固定したい場合に指定（YYYY-MM-DD）

冪等性:
  送信済みタスクは tasks.remind_last_at（元々未使用の列）に対象日を記録し、
  同日の再実行では二重送信しない（/api/admin_tasks/mark_reminded で更新）。

担当者→Slackのメール対応が config/owner_slack_map.json に無い場合はスキップしてログに残す
（担当者名が事務員固有の場合、owner_slack_map.json に追記が必要）。
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
CONFIG_PATH = ROOT / "config" / "owner_slack_map.json"
BILLING_CATEGORY = "経費・請求"
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


def get_user_id_by_email(email: str) -> str | None:
    result = slack_get("users.lookupByEmail", {"email": email})
    return result["user"]["id"] if result.get("ok") else None


def open_dm(user_id: str) -> str | None:
    result = slack_api("conversations.open", users=user_id)
    return result["channel"]["id"] if result.get("ok") else None


def load_owner_map() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def fetch_admin_tasks() -> list[dict]:
    url = f"{TOOL_URL}/api/admin_tasks"
    if SFA_API_TOKEN:
        url += f"?token={urllib.parse.quote(SFA_API_TOKEN)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def mark_reminded(task_id: int, date_str: str) -> None:
    url = f"{TOOL_URL}/api/admin_tasks/mark_reminded"
    if SFA_API_TOKEN:
        url += f"?token={urllib.parse.quote(SFA_API_TOKEN)}"
    payload = json.dumps({"task_id": task_id, "date": date_str}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"[WARN] mark_reminded failed（task_id={task_id}）: {result.get('error')}")
    except urllib.error.URLError as e:
        print(f"[WARN] mark_reminded failed（task_id={task_id}）: {e}")


def target_date_str() -> str:
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        return override
    return datetime.now(JST).strftime("%Y-%m-%d")


def in_window(task: dict, today: str) -> bool:
    """期限前日〜期限超過（期限未設定は対象外）。"""
    due = task.get("due_date") or ""
    if not due:
        return False
    try:
        due_dt = datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        return False
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    return today_dt >= due_dt - timedelta(days=1)


def build_message(task: dict, today: str) -> str:
    due = task.get("due_date") or ""
    overdue = due and due < today
    head = "🔴 【期限超過】請求タスクが残っています" if overdue else "🔔 請求タスクの期限が近づいています"
    lines = [
        head,
        f"件名: {task.get('title') or '(無題)'}",
        f"期限: {due or '(未設定)'}",
    ]
    if task.get("next_action"):
        lines.append(f"次アクション: {task['next_action']}")
    if task.get("requester"):
        lines.append(f"依頼者: {task['requester']}")
    lines.append(f"🔗 {TOOL_URL}/desk-tasks")
    return "\n".join(lines)


def main():
    if not SLACK_TOKEN:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)

    today = target_date_str()
    print(f"[INFO] 対象日: {today}")

    owner_map = load_owner_map()
    tasks = fetch_admin_tasks()

    targets = [
        t for t in tasks
        if t.get("category") == BILLING_CATEGORY
        and in_window(t, today)
        and (t.get("remind_last_at") or "") != today
    ]
    print(f"[INFO] リマインド対象: {len(targets)}件")

    sent = 0
    for task in targets:
        assignee = (task.get("assignee") or "").strip()
        email = owner_map.get(assignee)
        if not email:
            print(f"[SKIP] task_id={task['id']} 担当者「{assignee}」のSlackメール未登録（owner_slack_map.jsonに追記してください）")
            continue
        user_id = get_user_id_by_email(email)
        if not user_id:
            print(f"[SKIP] task_id={task['id']} {assignee}({email}) のSlackユーザーが見つかりません")
            continue
        channel_id = open_dm(user_id)
        if not channel_id:
            print(f"[SKIP] task_id={task['id']} {assignee} のDMチャネルを開けませんでした")
            continue
        result = slack_api("chat.postMessage", channel=channel_id, text=build_message(task, today))
        if result.get("ok"):
            mark_reminded(task["id"], today)
            print(f"[OK] task_id={task['id']} {assignee} にリマインド送信")
            sent += 1
        else:
            print(f"[ERROR] task_id={task['id']} 送信失敗: {result.get('error')}")

    print(f"\n完了: {sent}/{len(targets)}件を送信。")


if __name__ == "__main__":
    main()
