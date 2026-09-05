"""社内PJ(deal_issues)の議論メンバー区分ごとに、対象担当者へ毎週金曜にSlackでリマインドする（#47）。

【現在は停止中（スケジュール未登録）】
社内PJの登録がまだ少ないため、本スクリプトは render.yaml のcronに登録していない＝自動実行されない。
有効化するには:
  1. config/issue_member_map.json の各区分に対象担当者名（owner_slack_map.json のキー）を入れる。
  2. render.yaml に cron サービスを追加（例: 金曜17:00 JST = "0 8 * * 5"）:
       startCommand: python scripts/issue_reminder_slack.py
       env: SLACK_BOT_TOKEN / SFA_API_URL(or SFA_TOOL_URL) / SFA_API_TOKEN
手動実行（動作確認）:
  SLACK_BOT_TOKEN=... SFA_TOOL_URL=https://sfa-crm.onrender.com SFA_API_TOKEN=... \
    python scripts/issue_reminder_slack.py
  TEST_OWNER=早瀬 を付けると指定担当のみ送信（テスト用）。

環境変数:
  SLACK_BOT_TOKEN  - SlackアプリのBot User OAuth Token (xoxb-...)
  SFA_TOOL_URL / SFA_API_URL - SFA本体URL（/api/deal_issues を叩く）
  SFA_API_TOKEN    - SFA APIトークン
  COWORK_SFA_DB    - DBパス（ローカルにDBがある場合はAPIより優先せず、無ければAPI経由）
Slackアプリ設定(Bot Scopes): users:read, users:read.email, im:write, chat:write
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
TOOL_URL = os.environ.get("SFA_API_URL", "") or os.environ.get("SFA_TOOL_URL", "http://localhost:8787")
SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")
DB_PATH = os.environ.get("COWORK_SFA_DB", str(ROOT / "cowork_sfa.db"))
OWNER_MAP_PATH = ROOT / "config" / "owner_slack_map.json"
MEMBER_MAP_PATH = ROOT / "config" / "issue_member_map.json"


def slack_api(method: str, **kwargs) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=json.dumps(kwargs).encode("utf-8"),
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        print(f"[Slack] {method} error: {result.get('error')}")
    return result


def slack_get(method: str, params: dict) -> dict:
    url = f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        print(f"[Slack] {method} error: {result.get('error')}")
    return result


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def fetch_open_issues() -> list[dict]:
    """議論中の社内PJを取得。ローカルDBがあればDB、無ければAPI経由。"""
    if Path(DB_PATH).exists():
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            from cowork import sfa_db  # noqa: E402
            return [dict(r) for r in sfa_db.list_deal_issues(con, status="議論中")]
        finally:
            con.close()
    url = f"{TOOL_URL}/api/deal_issues?status=議論中"
    if SFA_API_TOKEN:
        url += f"&token={urllib.parse.quote(SFA_API_TOKEN)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def build_message(owner: str, issues: list[dict]) -> str:
    tool_url = f"{TOOL_URL}/deal-issues" if TOOL_URL else ""
    lines = [
        "【社内PJリマインド（金曜）】",
        f"{owner}さん、あなたが議論メンバーに入っている「議論中」の社内PJです。",
        "",
        f"🧩 議論中の社内PJ: {len(issues)}件",
    ]
    for it in issues[:15]:
        due = (it.get("due_date") or "").strip()
        due_str = f"（解消期限 {due}）" if due else ""
        acc = it.get("account_name") or "共通"
        lines.append(f"  • {it.get('issue', '')} … {acc}{due_str}")
    if len(issues) > 15:
        lines.append(f"  …ほか {len(issues) - 15}件")
    lines.append("")
    if tool_url:
        lines.append(f"🔗 社内PJ一覧: {tool_url}")
    lines.append("")
    lines.append("今週中に、議論・更新・クローズをお願いします。")
    return "\n".join(lines)


def main() -> None:
    if not SLACK_TOKEN:
        print("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)
    owner_map = _load_json(OWNER_MAP_PATH)       # 担当者名 → email
    member_map = _load_json(MEMBER_MAP_PATH)     # 区分 → [担当者名]
    if not member_map or not any(member_map.values()):
        print("[WARN] config/issue_member_map.json が空です。各区分に担当者名を設定してください。中止します。")
        return

    issues = fetch_open_issues()
    if not issues:
        print("[INFO] 議論中の社内PJがありません。通知をスキップします。")
        return

    # 担当者名 → その人に見せる社内PJリスト（区分マッチ・重複排除）
    per_owner: dict[str, list[dict]] = {}
    for it in issues:
        members = [m.strip() for m in (it.get("members") or "").split(",") if m.strip()]
        target_owners: set[str] = set()
        for seg in members:
            for owner in member_map.get(seg, []):
                target_owners.add(owner)
        for owner in target_owners:
            per_owner.setdefault(owner, []).append(it)

    if not per_owner:
        print("[INFO] どの社内PJも区分→担当者マッピングに一致しませんでした。")
        return

    test_owner = os.environ.get("TEST_OWNER", "")
    sent = 0
    for owner, owner_issues in per_owner.items():
        if test_owner and owner != test_owner:
            continue
        email = owner_map.get(owner)
        if not email:
            print(f"[SKIP] {owner}: owner_slack_map にメール未設定")
            continue
        r = slack_get("users.lookupByEmail", {"email": email})
        if not r.get("ok"):
            print(f"[SKIP] {owner} ({email}): Slackユーザーが見つかりません")
            continue
        user_id = r["user"]["id"]
        dm = slack_api("conversations.open", users=user_id)
        if not dm.get("ok"):
            print(f"[SKIP] {owner}: DMチャネルを開けませんでした")
            continue
        res = slack_api("chat.postMessage", channel=dm["channel"]["id"],
                        text=build_message(owner, owner_issues))
        if res.get("ok"):
            print(f"[OK] {owner} に社内PJリマインド送信（{len(owner_issues)}件）")
            sent += 1
        else:
            print(f"[ERROR] {owner} への送信失敗: {res.get('error')}")

    print(f"\n完了: {sent}人に社内PJリマインド送信。")


if __name__ == "__main__":
    main()
