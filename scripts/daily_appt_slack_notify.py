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

カレンダー突合（#64・2026-08-16追加）:
  GOOGLE_CALENDAR_SA_JSON が設定されている場合のみ、担当者のGoogleカレンダー（ドメイン全体の
  委任で取得）と突合する。未設定の間は従来通りカレンダーチェックをスキップする（fail-open、
  投稿動作は変えない）。詳細設計: docs/calendar-crosscheck/DESIGN.md。
  - 順方向: 対象商談のownerに翌日の外部会議が見当たらない場合、投稿はするが
    「⚠️カレンダー未確認」を付記する（消さない）。
  - 逆方向: 対象メンバー全員の翌日の外部会議のうち、SFA側に対応する商談日付が無いものを
    検知し、当面は早瀬個人へのSlack DMで通知する
    （CALENDAR_CROSSCHECK_NOTIFY_MODE=channel で#salesへ切替可能）。

環境変数（カレンダー突合、任意）:
  GOOGLE_CALENDAR_SA_JSON            - ドメイン全体委任サービスアカウントの鍵JSON（ファイルパス
                                        or JSON文字列）。未設定ならカレンダー突合は無効。
  GOOGLE_WORKSPACE_DOMAIN            - 自社ドメイン（既定 inproc.org）
  CALENDAR_CROSSCHECK_NOTIFY_MODE    - "dm"（既定・早瀬個人へDM）| "channel"（SALES_CHANNEL_IDへ）
  CALENDAR_CROSSCHECK_DM_OWNER       - dmモードの宛先（owner_slack_map.jsonのキー。既定: 早瀬）
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("SALES_CHANNEL_ID", "")
TOOL_URL = os.environ.get("SFA_TOOL_URL", "https://sfa-crm.onrender.com")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")
JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
OWNER_MAP_PATH = ROOT / "config" / "owner_slack_map.json"
GOOGLE_CALENDAR_SA_JSON = os.environ.get("GOOGLE_CALENDAR_SA_JSON", "").strip()
GOOGLE_WORKSPACE_DOMAIN = os.environ.get("GOOGLE_WORKSPACE_DOMAIN", "inproc.org").strip()
CALENDAR_NOTIFY_MODE = os.environ.get("CALENDAR_CROSSCHECK_NOTIFY_MODE", "dm").strip().lower()
CALENDAR_DM_OWNER = os.environ.get("CALENDAR_CROSSCHECK_DM_OWNER", "早瀬").strip()
# #64: 顧客面談に参加するメンバーのみ対象（owner_slack_map.json全体ではない、2026-08-17）。
CALENDAR_CROSSCHECK_OWNERS = [s.strip() for s in
                             os.environ.get("CALENDAR_CROSSCHECK_OWNERS", "吉江,中島,岩崎,早瀬,高橋,土屋").split(",")
                             if s.strip()]
# shadow: #salesの投稿文面・逆方向通知は変えず、早瀬DMへ診断レポートのみ送る（キャリブレーション用）。
# live: 5.2/5.3の通り実際に注記・通知を有効化する。
CALENDAR_CROSSCHECK_MODE = os.environ.get("CALENDAR_CROSSCHECK_MODE", "shadow").strip().lower()


def load_owner_map() -> dict:
    if OWNER_MAP_PATH.exists():
        with open(OWNER_MAP_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def build_calendar_client():
    """GOOGLE_CALENDAR_SA_JSON が設定されていればWorkspaceCalendarClientを返す。未設定はNone。"""
    if not GOOGLE_CALENDAR_SA_JSON:
        return None
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from cowork import workspace_calendar as wc
    sa_info = wc.load_service_account_info(GOOGLE_CALENDAR_SA_JSON)
    return wc.WorkspaceCalendarClient(sa_info, JST)


def check_owner_has_external_meeting(calendar_client, owner_email: str, target_date: _date_cls) -> bool:
    """対象日にownerの外部会議候補が1件でもあればTrue（#64順方向チェック）。"""
    from cowork import workspace_calendar as wc
    events = calendar_client.list_events_for_date(owner_email, target_date)
    return any(wc.is_external_meeting(e, self_email=owner_email, own_domain=GOOGLE_WORKSPACE_DOMAIN)
               for e in events)


def find_unmatched_calendar_meetings(calendar_client, owner_map: dict, deals: list[dict],
                                      target_date: _date_cls, target_date_str: str) -> list[dict]:
    """#64逆方向チェック: 各メンバーの外部会議候補のうち、SFA側に対応する次回MS登録が
    足りていない分を検知する（1メンバーの会議数 > SFA登録数の超過分をまとめて返す）。"""
    from cowork import workspace_calendar as wc
    deals_by_owner: dict[str, int] = {}
    for d in deals:
        if d.get("next_milestone_date") == target_date_str and (d.get("next_milestone_type") or "") != "タスク":
            owner = (d.get("owner") or "").strip()
            deals_by_owner[owner] = deals_by_owner.get(owner, 0) + 1

    unmatched: list[dict] = []
    for owner, email in owner_map.items():
        try:
            events = calendar_client.list_events_for_date(email, target_date)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] カレンダー取得失敗（{owner}）: {e}")
            continue
        ext_events = [e for e in events
                     if wc.is_external_meeting(e, self_email=email, own_domain=GOOGLE_WORKSPACE_DOMAIN)]
        n_deals = deals_by_owner.get(owner, 0)
        for e in ext_events[n_deals:]:
            unmatched.append({"owner": owner, "title": e.summary,
                              "start": e.start.strftime("%H:%M") if not e.all_day else "終日"})
    return unmatched


def build_shadow_diagnostic(calendar_client, owner_map: dict, target_date: _date_cls,
                            target_date_str: str) -> str:
    """#64 §5.4 キャリブレーション用診断レポート。6名それぞれのその日の全予定を、
    is_external_meetingの判定タグ付きでそのまま列挙する（#salesには一切影響しない）。
    人が実際の顧客面談と突き合わせ、癖による誤判定（見逃し/誤検知）を発見するためのもの。"""
    from cowork import workspace_calendar as wc
    lines = [f"🔍 カレンダー突合キャリブレーション診断（{target_date_str}）",
             "※このメッセージは診断用です。#salesへの投稿・通知には反映されていません（shadowモード）。"]
    for owner, email in owner_map.items():
        lines.append(f"\n▼ {owner}")
        try:
            events = calendar_client.list_events_for_date(email, target_date)
        except Exception as e:  # noqa: BLE001
            lines.append(f"   取得失敗: {e}")
            continue
        if not events:
            lines.append("   （予定なし）")
            continue
        for e in events:
            tag = ("🌐外部候補" if wc.is_external_meeting(
                       e, self_email=email, own_domain=GOOGLE_WORKSPACE_DOMAIN) else "・対象外")
            when = "終日" if e.all_day else e.start.strftime("%H:%M")
            lines.append(f"   [{tag}] {when} {e.summary}")
    return "\n".join(lines)


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
        title += ":exclamation:エンプラ"
    if owner:
        title += f"｜{owner}"
    return title


def build_parent_text(deal: dict, date_str: str, *, calendar_unconfirmed: bool = False) -> str:
    title = format_title(deal, date_str)
    if calendar_unconfirmed:
        title += "\n⚠️カレンダー未確認（担当者の予定に外部会議が見当たりませんでした）"
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
        # 次回MS種別が「タスク」のものは投稿しない（fail-safe: アポ／未設定は投稿する）。
        def _is_appt(d: dict) -> bool:
            return (d.get("next_milestone_type") or "") != "タスク"

        targets = [
            d for d in deals
            if d.get("next_milestone_date") == date_str
            and d.get("slack_notified_date") != date_str
            and _is_appt(d)
        ]
        skipped = sum(
            1 for d in deals
            if d.get("next_milestone_date") == date_str and d.get("slack_notified_date") == date_str
        )
        task_skipped = sum(
            1 for d in deals
            if d.get("next_milestone_date") == date_str and not _is_appt(d)
        )
        print(f"[INFO] 対象商談: {len(targets)}件"
              f"（通知済みスキップ: {skipped}件 / タスクのため除外: {task_skipped}件）")

    if not targets:
        print("[INFO] 対象商談がないため終了します。")
        return

    targets.sort(key=time_sort_key)

    # #64: カレンダー突合（GOOGLE_CALENDAR_SA_JSON未設定なら丸ごとスキップ＝現行動作を維持）。
    # shadowモード（既定）では判定はするが#salesの文面は変えず、早瀬DMへ診断レポートのみ送る
    # （§5.4: 6名それぞれの顧客面談登録の癖を確認してからliveへ切替える運用）。
    calendar_client = None
    owner_map: dict = {}
    shadow_mode = CALENDAR_CROSSCHECK_MODE != "live"
    if not deal_id_override:
        try:
            calendar_client = build_calendar_client()
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] カレンダークライアント初期化失敗（突合をスキップ）: {e}")
            calendar_client = None
        if calendar_client is not None:
            full_map = load_owner_map()
            owner_map = {k: v for k, v in full_map.items() if k in CALENDAR_CROSSCHECK_OWNERS}
            print(f"[INFO] カレンダー突合: 有効（mode={CALENDAR_CROSSCHECK_MODE}、"
                  f"対象メンバー{len(owner_map)}名/{len(CALENDAR_CROSSCHECK_OWNERS)}名中）")

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    posted = 0
    for deal in targets:
        calendar_unconfirmed = False
        if calendar_client is not None:
            owner = (deal.get("owner") or "").strip()
            email = owner_map.get(owner)
            if email:
                try:
                    found = check_owner_has_external_meeting(calendar_client, email, target_date)
                    if shadow_mode:
                        print(f"[SHADOW] deal_id={deal['id']} owner={owner} 外部会議あり={found}"
                              "（#salesの文面には反映していません）")
                    else:
                        calendar_unconfirmed = not found
                except Exception as e:  # noqa: BLE001
                    print(f"[WARN] カレンダー確認失敗（deal_id={deal['id']} owner={owner}）: {e}")

        parent_text = build_parent_text(deal, date_str, calendar_unconfirmed=calendar_unconfirmed)
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
        print(f"[OK] deal_id={deal['id']} {deal.get('account_name')} を投稿しました"
              f"{'（⚠️カレンダー未確認）' if calendar_unconfirmed else ''}")
        posted += 1

    print(f"\n完了: {posted}/{len(targets)}件を投稿。")

    if calendar_client is not None and owner_map:
        if shadow_mode:
            report = build_shadow_diagnostic(calendar_client, owner_map, target_date, date_str)
            dm_channel = _resolve_dm_channel(CALENDAR_DM_OWNER)
            if dm_channel:
                result = slack_api("chat.postMessage", channel=dm_channel, text=report)
                print(f"[SHADOW] 診断レポート送信: {'OK' if result.get('ok') else result.get('error')}")
            else:
                print(f"[ERROR] {CALENDAR_DM_OWNER} のDMチャネルを開けず診断レポートを送れませんでした。")
        else:
            unmatched = find_unmatched_calendar_meetings(calendar_client, owner_map, deals, target_date, date_str)
            notify_unmatched_calendar_meetings(unmatched, date_str)


def notify_unmatched_calendar_meetings(unmatched: list, date_str: str) -> None:
    """#64逆方向: SFA未登録の外部会議候補をSlackで通知する（0件なら何もしない）。"""
    if not unmatched:
        print("[INFO] カレンダー逆方向チェック: 未登録の会議候補なし。")
        return

    lines = [f"🔔 {date_str} の担当者カレンダーに、SFA未登録の外部会議候補があります。"]
    for m in unmatched[:10]:
        lines.append(f"   - {m['owner']} {m['start']} {m['title']}")
    if len(unmatched) > 10:
        lines.append(f"   …他{len(unmatched) - 10}件")
    lines.append(f"\n🔗 {TOOL_URL}/deals")
    text = "\n".join(lines)

    if CALENDAR_NOTIFY_MODE == "channel":
        channel_id = CHANNEL_ID
    else:
        channel_id = _resolve_dm_channel(CALENDAR_DM_OWNER)
        if not channel_id:
            print(f"[ERROR] {CALENDAR_DM_OWNER} のDMチャネルを開けなかったため通知を送れませんでした。")
            return

    result = slack_api("chat.postMessage", channel=channel_id, text=text)
    if result.get("ok"):
        print(f"[OK] カレンダー逆方向チェック通知を送信しました（{len(unmatched)}件）。")
    else:
        print(f"[ERROR] カレンダー逆方向チェック通知の送信に失敗: {result.get('error')}")


def _resolve_dm_channel(owner: str) -> str | None:
    email = load_owner_map().get(owner)
    if not email:
        print(f"[ERROR] 「{owner}」のSlackメールがowner_slack_map.jsonに未登録です。")
        return None
    r = slack_get_users_lookup(email)
    if not r.get("ok"):
        print(f"[ERROR] {owner}({email}) のSlackユーザーが見つかりません: {r.get('error')}")
        return None
    user_id = r["user"]["id"]
    r2 = slack_api("conversations.open", users=user_id)
    if not r2.get("ok"):
        print(f"[ERROR] {owner} のDMチャネルを開けませんでした: {r2.get('error')}")
        return None
    return r2["channel"]["id"]


def slack_get_users_lookup(email: str) -> dict:
    qs = urllib.parse.urlencode({"email": email})
    url = f"https://slack.com/api/users.lookupByEmail?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    main()
