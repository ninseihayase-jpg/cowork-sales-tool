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
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# JST基準の当日（水17:30 JST=08:30 UTC実行。超過判定は当日以前）
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date().isoformat()


def is_follow_up(d: dict) -> bool:
    """要フォロー＝次回MSが超過(<=当日) または 未入力(空)。"""
    ms = (d.get("next_milestone_date") or "").strip()
    return (not ms) or (ms <= TODAY)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SHEET_ID = os.environ.get("WEEKLY_SHEET_ID", "")
DB_PATH = os.environ.get("SFA_DB_PATH", str(ROOT / "cowork_sfa.db"))
CONFIG_PATH = ROOT / "config" / "owner_slack_map.json"
TOOL_URL = os.environ.get("SFA_TOOL_URL", "http://localhost:8787")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")


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


def _fetch_deals_from_api() -> list[dict]:
    url = f"{TOOL_URL}/api/deals?status=open"
    if SFA_API_TOKEN:
        url += f"&token={urllib.parse.quote(SFA_API_TOKEN)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_deals_by_owner(db_path: str) -> dict[str, list[dict]]:
    # DB ファイルが存在しない場合（Render cron）は API 経由で取得
    if not Path(db_path).exists():
        print(f"[INFO] DB not found at {db_path}, fetching via API: {TOOL_URL}")
        rows = _fetch_deals_from_api()
    else:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows_raw = con.execute(
            """SELECT d.deal_name, d.stage, d.next_milestone_date, d.next_milestone_label, d.owner
               FROM deals d WHERE d.status='open' AND d.owner IS NOT NULL
               ORDER BY d.owner, d.updated_at DESC"""
        ).fetchall()
        con.close()
        rows = [dict(r) for r in rows_raw]
    result: dict[str, list[dict]] = {}
    for r in rows:
        owner = r.get("owner")
        if owner:
            result.setdefault(owner, []).append(r)
    return result


def build_message(owner: str, follow_deals: list[dict]) -> str:
    """要フォロー商談（次回MS超過 or 未入力）だけをまとめ、要フォロータブへのリンクを送る。"""
    tab_url = f"{TOOL_URL}/deals?tab=overdue&owner={urllib.parse.quote(owner)}" if TOOL_URL else ""
    overdue = [d for d in follow_deals if (d.get("next_milestone_date") or "").strip()]
    no_ms = [d for d in follow_deals if not (d.get("next_milestone_date") or "").strip()]

    lines = [
        f"{owner}さん、おつかれさまです。週次の商談フォローのお願いです。",
        "",
        f"担当商談のうち、次回マイルストーン（次に何を・いつやるか）が"
        f"未設定・期限切れのものが *{len(follow_deals)}件* あります。",
        "商談を止めないよう、下記から次回MSの設定・更新をお願いします🙏",
        "",
        f"■ 要対応 {len(follow_deals)}件（期限切れ {len(overdue)}／未設定 {len(no_ms)}）",
    ]
    # 期限切れ（古い順）→ 未設定 の順に、最大10件だけ列挙
    overdue_sorted = sorted(overdue, key=lambda d: d.get("next_milestone_date") or "")
    shown = 0
    for d in overdue_sorted + no_ms:
        if shown >= 10:
            break
        ms = (d.get("next_milestone_date") or "").strip()
        tag = f"期限切れ {ms}" if ms else "次回MS未設定"
        lines.append(f"　• {d.get('deal_name','')}（{d.get('stage','') or '—'}）… {tag}")
        shown += 1
    if len(follow_deals) > 10:
        lines.append(f"　…ほか {len(follow_deals) - 10}件")

    lines.append("")
    if tab_url:
        lines.append(f"▶ ここから更新（あなたの担当分のみ表示）:\n{tab_url}")
    lines.extend([
        "",
        "今週金曜18:00を目安にお願いします。",
    ])
    return "\n".join(lines)


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
        follow_deals = [d for d in deals if is_follow_up(d)]
        if not follow_deals:
            print(f"[SKIP] {owner}: 要フォロー商談なし（超過/未入力ゼロ）")
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
