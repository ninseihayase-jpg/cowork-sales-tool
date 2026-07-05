"""
翌日アポ Slack自動スレ立てスクリプト。
毎朝 06:00 JST（Render cron: 0 21 * * * UTC）に、次回MS日=翌日の商談を
#sales に1商談1スレッドで投稿する。

対象: SFA上の開設中(status=open)の商談のうち next_milestone_date が
      「実行時点のJST翌日」と一致するもの。
      （初回アポに限らず、以降の商談で次回MS日が更新されていっても
       同じ仕組みでその日の商談を拾える設計）

投稿内容:
  親メッセージ: "M/D HH:MM 会社名｜担当者" + 商談リンク
  スレッド返信: 商談のnoteフィールド（仕分け_▼SFA転記項目相当）
              + 基礎レポートリンク（SharePoint格納のため自動取得はできない。
                手動で追記する前提のプレースホルダ行を残す）

投稿順序: next_milestone_labelにHH:MM表記があるものは時間の早い順に投稿する。
         時間表記がないものは末尾にまとめる。

実行:
  python scripts/daily_appt_slack_notify.py          # 通常実行（翌日分）
  TARGET_DATE=2026-07-07 python scripts/daily_appt_slack_notify.py  # 日付指定（手動実行・テスト用）
  DEAL_ID=123 python scripts/daily_appt_slack_notify.py             # 1件だけテスト投稿（next_milestone_date無視）

環境変数:
  SLACK_BOT_TOKEN    - SlackアプリのBot User OAuth Token (xoxb-...)
  SALES_CHANNEL_ID   - 投稿先チャンネルID（#sales = C0AT55W40ET）
  SFA_TOOL_URL       - SFAツールのベースURL（省略時 https://sfa-crm.onrender.com）
  SFA_API_TOKEN      - /api/deals 用トークン
  TARGET_DATE        - 省略時はJST翌日。手動実行・テスト時に日付を固定したい場合に指定（YYYY-MM-DD）
  DEAL_ID            - 指定すると、その商談IDだけを対象に投稿する（日付条件・通知済みガードを無視した単発テスト用）。
                        本番の日次実行では絶対に指定しないこと。

冪等性:
  投稿済みの商談は deals.slack_notified_date（= 投稿対象日）に記録し、
  同じ対象日で再実行しても二重投稿しない（/api/deals/mark_notified で更新）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("SALES_CHANNEL_ID", "")
TOOL_URL = os.environ.get("SFA_TOOL_URL", "https://sfa-crm.onrender.com")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")
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


def fetch_open_deals() -> list[dict]:
    url = f"{TOOL_URL}/api/deals?status=open"
    if SFA_API_TOKEN:
        url += f"&token={urllib.parse.quote(SFA_API_TOKEN)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def mark_notified(deal_id: int, date_str: str) -> None:
    url = f"{TOOL_URL}/api/deals/mark_notified"
    if SFA_API_TOKEN:
        url += f"?token={urllib.parse.quote(SFA_API_TOKEN)}"
    payload = json.dumps({"deal_id": deal_id, "date": date_str}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"[WARN] mark_notified failed（deal_id={deal_id}）: {result.get('error')}")
    except urllib.error.URLError as e:
        print(f"[WARN] mark_notified failed（deal_id={deal_id}）: {e}")


def target_date_str() -> str:
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        return override
    now_jst = datetime.now(JST)
    return (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")


def format_title(deal: dict, date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    md = f"{dt.month}/{dt.day}"
    m = re.search(r"\d{2}:\d{2}", deal.get("next_milestone_label") or "")
    time_part = m.group(0) if m else ""
    company = deal.get("account_name") or "(会社名不明)"
    owner = deal.get("owner") or ""
    parts = [p for p in [md, time_part] if p]
    title = " ".join(parts) + f" {company}"
    if deal.get("company_size") == "5000億以上":
        title += " ！エンプラ"
    if owner:
        title += f"｜{owner}"
    return title


def build_parent_text(deal: dict, date_str: str) -> str:
    title = format_title(deal, date_str)
    link = f"{TOOL_URL}/deal/{deal['id']}"
    return f"{title}\n{link}"


def time_sort_key(deal: dict) -> tuple[int, str]:
    m = re.search(r"(\d{2}):(\d{2})", deal.get("next_milestone_label") or "")
    if not m:
        return (1, "")  # 時間表記なしは後ろへ
    return (0, f"{m.group(1)}:{m.group(2)}")


def build_thread_text(deal: dict) -> str:
    note = (deal.get("note") or "").strip() or "（メモなし）"
    return (
        f"{note}\n\n"
        f"📄 基礎レポート: （SharePoint格納のため手動で追記してください）"
    )


def main():
    if not SLACK_TOKEN:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)
    if not CHANNEL_ID:
        print("[ERROR] SALES_CHANNEL_ID が設定されていません。")
        sys.exit(1)

    deal_id_override = os.environ.get("DEAL_ID", "").strip()
    deals = fetch_open_deals()

    if deal_id_override:
        deal = next((d for d in deals if str(d.get("id")) == deal_id_override), None)
        if not deal:
            print(f"[ERROR] deal_id={deal_id_override} が見つかりません（status=openの商談のみ対象）。")
            sys.exit(1)
        date_str = deal.get("next_milestone_date") or target_date_str()
        print(f"[TEST] DEAL_ID={deal_id_override} 単発テスト投稿（date_str={date_str}）")
        targets = [deal]
    else:
        date_str = target_date_str()
        print(f"[INFO] 対象日: {date_str}")
        targets = [
            d for d in deals
            if d.get("next_milestone_date") == date_str and d.get("slack_notified_date") != date_str
        ]
        skipped = sum(
            1 for d in deals
            if d.get("next_milestone_date") == date_str and d.get("slack_notified_date") == date_str
        )
        print(f"[INFO] 対象商談: {len(targets)}件（既に通知済みでスキップ: {skipped}件）")

    if not targets:
        print("[INFO] 対象商談がないため終了します。")
        return

    targets.sort(key=time_sort_key)

    posted = 0
    for deal in targets:
        parent_text = build_parent_text(deal, date_str)
        result = slack_api("chat.postMessage", channel=CHANNEL_ID, text=parent_text)
        if not result.get("ok"):
            print(f"[ERROR] 親メッセージ投稿失敗（deal_id={deal['id']}）: {result.get('error')}")
            continue
        parent_ts = result["ts"]

        thread_text = build_thread_text(deal)
        reply_result = slack_api(
            "chat.postMessage", channel=CHANNEL_ID, thread_ts=parent_ts, text=thread_text,
        )
        if not reply_result.get("ok"):
            print(f"[ERROR] スレッド返信投稿失敗（deal_id={deal['id']}）: {reply_result.get('error')}")
            continue

        mark_notified(deal["id"], date_str)
        print(f"[OK] deal_id={deal['id']} {deal.get('account_name')} を投稿しました")
        posted += 1

    print(f"\n完了: {posted}/{len(targets)}件を投稿。")


if __name__ == "__main__":
    main()
