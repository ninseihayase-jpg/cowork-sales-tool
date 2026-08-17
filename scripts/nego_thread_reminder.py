"""
Slack NegoCollectionスレッドの放置検知リマインドスクリプト。

背景: #salesスレッドで@NegoCollectionが下書きを投稿した後、人が「フィールド: 値」で
修正だけして「確定」/「ok」と返信しないと、ボットは（サイレント無言仕様のため）何もアナウンスせず、
スレッドは`slack_threads.state != 'completed'`のまま静かに残り続ける。人間は「返信した＝完了した」
と誤解しやすく、実際には保存されていない。

このスクリプトは、`/api/nego_threads_stale`（first_seen_atからN時間以上経過・未リマインドの
スレッドを返す）を叩き、各スレッドに直接（そのスレッド自体への返信として）
「まだ保存されていません。再開方法」をアナウンスする。投稿できたら`/api/nego_threads_stale/ack`で
reminded_atを記録し、次回以降の再送を抑止する（状態が変わればreminded_atはNULLに戻り再送可能）。

実行:
  python scripts/nego_thread_reminder.py
  HOURS=2 python scripts/nego_thread_reminder.py   # 閾値を変える（既定3時間）

環境変数:
  SLACK_BOT_TOKEN  - SlackアプリのBot User OAuth Token (xoxb-...)
  SFA_TOOL_URL     - SFAツールのベースURL（省略時 https://sfa-crm.onrender.com）
  SFA_API_TOKEN    - /api/nego_threads_stale 用トークン
  HOURS            - 放置とみなす経過時間（省略時 3）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
TOOL_URL = os.environ.get("SFA_TOOL_URL", "https://sfa-crm.onrender.com")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")
HOURS = os.environ.get("HOURS", "3")

_STATE_LABEL = {
    "identifying": "商談の特定待ち",
    "pending": "内容の確認・確定待ち",
    "new_deal_ask": "新規商談の作成確認待ち",
    "new_deal_select": "新規商談のアカウント選択待ち",
    "new_deal_pending": "新規商談テンプレートの確定待ち",
    "new_deal_acc_confirm": "新規アカウント作成の確認待ち",
}


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


def fetch_stale(hours: str) -> list:
    url = f"{TOOL_URL}/api/nego_threads_stale?hours={urllib.parse.quote(hours)}"
    if SFA_API_TOKEN:
        url += f"&token={urllib.parse.quote(SFA_API_TOKEN)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read()).get("threads") or []


def ack(thread_ts: str) -> None:
    url = f"{TOOL_URL}/api/nego_threads_stale/ack"
    if SFA_API_TOKEN:
        url += f"?token={urllib.parse.quote(SFA_API_TOKEN)}"
    body = json.dumps({"thread_ts": thread_ts}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.URLError as e:
        print(f"[ERROR] ack失敗 thread_ts={thread_ts}: {e}")


def build_message(t: dict) -> str:
    state_label = _STATE_LABEL.get(t.get("state") or "", t.get("state") or "不明")
    lines = [
        "⏰ このスレッドはまだSFAに保存されていません（現在の状態: " + state_label + "）。",
        "内容を確認し、間違いがあれば「フィールド: 値」（半角コロン）で修正してから、",
        "「確定」または「ok」と返信すると保存されます。不要な場合は「キャンセル」と返信してください。",
    ]
    if t.get("deal_id"):
        lines.append(f"🔗 対象の商談: {TOOL_URL}/deal/{t['deal_id']}")
    return "\n".join(lines)


def main():
    if not SLACK_TOKEN:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)
    if not SFA_API_TOKEN:
        print("[ERROR] SFA_API_TOKEN が設定されていません。")
        sys.exit(1)

    threads = fetch_stale(HOURS)
    print(f"[INFO] 放置検知: {len(threads)}件（閾値{HOURS}時間）")
    if not threads:
        print("[INFO] 放置なし。送信をスキップします。")
        return

    ok_count = 0
    for t in threads:
        result = slack_api("chat.postMessage", channel=t["channel_id"], thread_ts=t["thread_ts"],
                           text=build_message(t))
        if result.get("ok"):
            ack(t["thread_ts"])
            ok_count += 1
        else:
            print(f"[ERROR] 送信失敗 thread_ts={t['thread_ts']}: {result.get('error')}")
    print(f"[OK] {ok_count}/{len(threads)}件のリマインドを送信しました。")


if __name__ == "__main__":
    main()
