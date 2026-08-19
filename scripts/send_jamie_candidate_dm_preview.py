"""#98 動作確認用: Jamie商談候補提示メッセージを、#salesではなく個人DMへ試験送信するスクリプト。

本番の#salesを汚さずに「Jamie文字起こし到着時、実際にどんなメッセージ・ボタンがSlackに
出るか」を確認するためのもの。既存の未割当インボックス（status='inbox'）から1件を選んで
実際の候補判定（_inbox_candidates、商談名のタイトル文字列一致）をかけ、同じメッセージを
DMに送る。ボタンのaction_id/valueは本番と同一実装を使うため、

    ⚠️ 「対象外」ボタンはDB変更なしで安全に押せる。
    ⚠️ 商談名の候補ボタンを押すと、そのinbox_idが本当にその商談へ割り当てられる
       （プレビューではなく本番動作そのものが走る）。押さずに見るだけならOK。

使い方:
    python scripts/send_jamie_candidate_dm_preview.py                # 最古のinbox放置1件を使う
    python scripts/send_jamie_candidate_dm_preview.py --inbox-id 42  # 指定のinbox_idを使う

環境変数:
    SLACK_BOT_TOKEN  - 必須
    COWORK_SFA_DB    - DBファイルパス（省略時 cowork_sfa.db）
    DM_OWNER         - 送り先（config/owner_slack_map.jsonのキー。既定: 早瀬）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cowork import sfa_db  # noqa: E402
from cowork import slack_bot  # noqa: E402
from cowork.webapp import _inbox_candidates  # noqa: E402

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
DB_PATH = os.environ.get("COWORK_SFA_DB", sfa_db.DEFAULT_DB_PATH)
DM_OWNER = os.environ.get("DM_OWNER", "早瀬").strip()
CONFIG_PATH = ROOT / "config" / "owner_slack_map.json"


def slack_api(method: str, **kwargs) -> dict:
    url = f"https://slack.com/api/{method}"
    data = json.dumps(kwargs).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def slack_get(method: str, params: dict) -> dict:
    import urllib.parse
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def resolve_dm_channel(owner: str) -> str | None:
    if not CONFIG_PATH.exists():
        print(f"[ERROR] {CONFIG_PATH} が見つかりません。")
        return None
    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    email = data.get(owner)
    if not email:
        print(f"[ERROR] 「{owner}」のSlackメールがowner_slack_map.jsonに未登録です。")
        return None
    r = slack_get("users.lookupByEmail", {"email": email})
    if not r.get("ok"):
        print(f"[ERROR] {owner}({email}) のSlackユーザーが見つかりません: {r.get('error')}")
        return None
    r2 = slack_api("conversations.open", users=r["user"]["id"])
    if not r2.get("ok"):
        print(f"[ERROR] {owner} のDMチャネルを開けませんでした: {r2.get('error')}")
        return None
    return r2["channel"]["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox-id", type=int, default=None, help="使用するintake_transcripts.id（省略時は最古のinbox放置1件）")
    args = parser.parse_args()

    if not SLACK_TOKEN:
        sys.exit("[ERROR] SLACK_BOT_TOKEN が設定されていません。")
    if not Path(DB_PATH).exists():
        sys.exit(f"[ERROR] DBファイルが見つかりません: {DB_PATH}")

    con = sfa_db.connect(DB_PATH)
    try:
        if args.inbox_id:
            row = con.execute(
                "SELECT * FROM intake_transcripts WHERE id=?", (args.inbox_id,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM intake_transcripts WHERE status='inbox' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if not row:
            sys.exit("[ERROR] 対象のinbox文字起こしが見つかりません。")
        t = dict(row)
        title = t.get("title") or "（無題）"
        occurred_on = t.get("occurred_on") or (t.get("created_at") or "")[:10]
        print(f"[INFO] 対象: inbox_id={t['id']} title={title!r} occurred_on={occurred_on}")

        deals = sfa_db.list_deals(con, status="open")
        candidates = _inbox_candidates(title, [], deals, [])
        print(f"[INFO] 候補: {candidates}")
        if not candidates:
            print("[WARN] 候補が0件のため、本番の#salesでも実際には投稿されないケースです"
                  "（会議タイトルが商談名/アカウント名を一致しないケース）。")
            print("       それでもメッセージの見た目自体は確認できるよう、テスト表示用の"
                  "ダミー候補1件を付けて送信します。")
            candidates = [("deal:0", "（例）商談: サンプル株式会社 / サンプル案件")]

        dm_channel = resolve_dm_channel(DM_OWNER)
        if not dm_channel:
            sys.exit(1)

        ts = slack_bot.post_jamie_candidate_prompt(
            con, inbox_id=t["id"], title=title, occurred_on=occurred_on,
            candidates=candidates, channel=dm_channel)
        if ts:
            print(f"[OK] {DM_OWNER}のDMへ送信しました（ts={ts}）。")
            print("     ⚠️ 「対象外」ボタンは安全に押せます。商談名のボタンは押すと本当に割り当てが実行されます。")
        else:
            print("[ERROR] 送信に失敗しました（ログ参照）。")
    finally:
        con.close()


if __name__ == "__main__":
    main()
