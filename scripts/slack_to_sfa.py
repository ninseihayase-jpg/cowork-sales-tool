"""
Slackの#Salesチャネルをポーリングし、【SFA】タグ付きの投稿を
SFA DBの活動履歴に取込むスクリプト。

実行: python scripts/slack_to_sfa.py
環境変数:
  SLACK_BOT_TOKEN   - SlackアプリのBot User OAuth Token (xoxb-...)
  ANTHROPIC_API_KEY - Claude API key（メッセージパース用）
  SFA_DB_PATH       - DBパス（省略時はcowork_sfa.db）

フロー:
1. Slackの#Salesチャネルのhistoryを取得（過去24時間）
2. 【SFA】で始まるメッセージを抽出
3. 既に処理済みのメッセージはskip（data/processed_slack_ts.txtでtsを管理）
4. Anthropic APIでメッセージをパースし構造化データ抽出
5. SFA DBで商談名マッチング（部分一致）→ 活動履歴を追加
6. 商談が見つかればSlackにリプライで結果報告
7. 処理済みtsをファイルに記録

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLACK_BOT_TOKEN の取得手順:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. https://api.slack.com/apps → [Create New App] → [From scratch]
   App Name: SFA-CRM Bot / Workspace: InProc

2. [OAuth & Permissions] → [Scopes] → [Bot Token Scopes] に以下を追加:
   - channels:history    （パブリックチャネルの履歴取得）
   - channels:read       （チャネルID検索）
   - chat:write          （リプライ投稿）

3. [Install App to Workspace] → [Allow] → Bot User OAuth Token (xoxb-...) をコピー

4. #Sales チャネルにBotを招待:
   Slackで #Sales を開き [メンバーを追加] → @SFA-CRM Bot

5. 取得したトークンを環境変数またはGitHub Secretsに設定:
   ローカル実行: export SLACK_BOT_TOKEN="xoxb-..."
   GitHub Actions: Settings → Secrets → SLACK_BOT_TOKEN に設定
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【使い方】 #Sales に以下の形式で投稿してください:

  【SFA】
  商談: 株式会社○○
  日付: 2026-06-21
  種別: 面談（面談/電話/メール/メモ）
  相手: 田中部長
  内容: 提案書を提出。予算は5000万円前後との話。次回は来週水曜。
  次回MS日: 2026-06-28
  次回MSラベル: 再提案

  ※ 「次回MS日」「次回MSラベル」は省略可
  ※ 「日付」省略時は本日の日付が使われます
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# プロジェクトルートをsysパスに追加してcowork.sfa_dbをimportできるようにする
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import anthropic
from cowork.sfa_db import add_activity, connect, init_db, upsert_deal

# ── 設定 ───────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SFA_DB_PATH = os.environ.get("SFA_DB_PATH", str(PROJECT_ROOT / "cowork_sfa.db"))

CHANNEL_NAME = "sales"
HISTORY_HOURS = 24           # 何時間前までのメッセージを対象にするか
TRIGGER_PREFIX = "【SFA】"   # このテキストで始まるメッセージを処理する

# 処理済みタイムスタンプの管理ファイル
TS_FILE = PROJECT_ROOT / "data" / "processed_slack_ts.txt"

SLACK_API_BASE = "https://slack.com/api"


# ── Slack API ヘルパー ──────────────────────────────────────────────────────

def _slack_get(method: str, params: dict) -> dict:
    """Slack Web API (GET) を呼び出してJSONを返す。"""
    qs = urllib.parse.urlencode(params)
    url = f"{SLACK_API_BASE}/{method}?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Slack API HTTP {e.code}: {method}") from e

    if not body.get("ok"):
        raise RuntimeError(f"Slack API error [{method}]: {body.get('error', 'unknown')}")
    return body


def _slack_post(method: str, payload: dict) -> dict:
    """Slack Web API (POST JSON) を呼び出してJSONを返す。"""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{SLACK_API_BASE}/{method}",
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Slack API HTTP {e.code}: {method}") from e

    if not body.get("ok"):
        raise RuntimeError(f"Slack API error [{method}]: {body.get('error', 'unknown')}")
    return body


def get_channel_id(channel_name: str) -> str:
    """チャネル名からチャネルIDを取得する（最大1000件検索）。"""
    cursor = None
    while True:
        params: dict = {"exclude_archived": "true", "limit": 200, "types": "public_channel,private_channel"}
        if cursor:
            params["cursor"] = cursor
        body = _slack_get("conversations.list", params)
        for ch in body.get("channels", []):
            if ch["name"] == channel_name:
                return ch["id"]
        next_cursor = body.get("response_metadata", {}).get("next_cursor", "")
        if not next_cursor:
            break
        cursor = next_cursor
    raise RuntimeError(f"チャネル #{channel_name} が見つかりません。Botが招待されているか確認してください。")


def fetch_recent_messages(channel_id: str, hours: int = HISTORY_HOURS) -> list[dict]:
    """指定チャネルの過去N時間のメッセージ一覧を返す。"""
    oldest = str(time.time() - hours * 3600)
    body = _slack_get("conversations.history", {
        "channel": channel_id,
        "oldest": oldest,
        "limit": 200,
    })
    return body.get("messages", [])


def post_reply(channel_id: str, thread_ts: str, text: str) -> None:
    """スレッドにリプライを投稿する。"""
    _slack_post("chat.postMessage", {
        "channel": channel_id,
        "thread_ts": thread_ts,
        "text": text,
    })


# ── 処理済みts 管理 ────────────────────────────────────────────────────────

def load_processed_ts() -> set[str]:
    """処理済みtsセットをファイルから読み込む。"""
    if not TS_FILE.exists():
        return set()
    return set(TS_FILE.read_text(encoding="utf-8").splitlines())


def save_processed_ts(ts: str) -> None:
    """処理済みtsをファイルに追記する。"""
    TS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TS_FILE.open("a", encoding="utf-8") as f:
        f.write(ts + "\n")


# ── Anthropic API: メッセージパース ───────────────────────────────────────

def parse_message_with_claude(message_text: str) -> dict | None:
    """
    Anthropic APIを使ってSlackメッセージから活動履歴情報をJSON抽出する。
    パースに失敗した場合は None を返す。
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    today = time.strftime("%Y-%m-%d")

    prompt = f"""以下のSlackメッセージから活動履歴情報をJSON形式で抽出してください。

メッセージ:
{message_text}

抽出するJSON形式:
{{
  "deal_name_hint": "商談の会社名や案件名（部分一致検索用）",
  "occurred_on": "YYYY-MM-DD形式（不明なら本日 {today} を使用）",
  "activity_type": "面談/電話/メール/メモのいずれか（不明なら「メモ」）",
  "contact_name": "相手の名前（不明ならnull）",
  "body": "活動内容の本文",
  "next_milestone_date": "次回MS日YYYY-MM-DD（不明ならnull）",
  "next_milestone_label": "次回MSラベル（不明ならnull）"
}}

JSONのみ返してください。"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # コードブロックで囲まれている場合を除去
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [WARN] Claude応答のJSONパース失敗: {e}")
        print(f"  [WARN] 応答内容: {raw[:300]}")
        return None

    return parsed


# ── SFA DB 操作 ────────────────────────────────────────────────────────────

def find_deal(con, hint: str) -> dict | None:
    """商談名またはアカウント名の部分一致で商談を1件返す。"""
    row = con.execute(
        """SELECT d.*, a.name AS account_name
           FROM deals d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE (d.deal_name LIKE ? OR a.name LIKE ?)
             AND d.status = 'open'
           ORDER BY d.updated_at DESC
           LIMIT 1""",
        (f"%{hint}%", f"%{hint}%"),
    ).fetchone()
    return dict(row) if row else None


def record_activity(con, deal: dict, parsed: dict) -> int:
    """活動履歴をDBに登録し、必要なら商談のマイルストーンを更新する。"""
    activity_id = add_activity(
        con,
        deal_id=deal["id"],
        type=parsed.get("activity_type") or "メモ",
        occurred_on=parsed.get("occurred_on"),
        contact_name=parsed.get("contact_name"),
        body=parsed.get("body") or "",
    )

    # 次回マイルストーンが指定されていれば商談を更新
    ms_date = parsed.get("next_milestone_date")
    ms_label = parsed.get("next_milestone_label")
    if ms_date or ms_label:
        update_fields = {k: deal[k] for k in [
            "account_id", "theme_id", "deal_name", "stage", "business_type_l1",
            "business_type_l2", "lead_pattern", "owner", "value_lumpsum",
            "value_lumpsum_monthly", "value_recurring", "client_budget",
            "next_milestone_date", "next_milestone_label", "note", "goal",
            "importance", "status",
            "cost_stage", "approach_value", "approach_rate", "reduction_rate",
            "fee_rate", "diagnosis_cost",
        ] if k in deal}
        if ms_date:
            update_fields["next_milestone_date"] = ms_date
        if ms_label:
            update_fields["next_milestone_label"] = ms_label
        upsert_deal(con, id=deal["id"], **update_fields)
        print(f"  [INFO] 商談のマイルストーン更新: {ms_date} / {ms_label}")

    return activity_id


# ── メイン処理 ─────────────────────────────────────────────────────────────

def validate_env() -> bool:
    """必要な環境変数が揃っているか確認する。"""
    ok = True
    if not SLACK_BOT_TOKEN:
        print("[ERROR] 環境変数 SLACK_BOT_TOKEN が未設定です。")
        ok = False
    if not ANTHROPIC_API_KEY:
        print("[ERROR] 環境変数 ANTHROPIC_API_KEY が未設定です。")
        ok = False
    return ok


def main() -> None:
    print(f"=== Slack→SFA 活動履歴取込 開始 ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    if not validate_env():
        sys.exit(1)

    # DB初期化（テーブルが存在しない場合は作成）
    init_db(SFA_DB_PATH)
    con = connect(SFA_DB_PATH)
    print(f"[INFO] DB: {SFA_DB_PATH}")

    # 処理済みts読み込み
    processed = load_processed_ts()
    print(f"[INFO] 既処理件数: {len(processed)}")

    # チャネルID取得
    print(f"[INFO] チャネル #{CHANNEL_NAME} を検索中...")
    try:
        channel_id = get_channel_id(CHANNEL_NAME)
        print(f"[INFO] チャネルID: {channel_id}")
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        con.close()
        sys.exit(1)

    # メッセージ取得
    print(f"[INFO] 過去{HISTORY_HOURS}時間のメッセージを取得中...")
    messages = fetch_recent_messages(channel_id)
    print(f"[INFO] 取得件数: {len(messages)}")

    # 【SFA】プレフィックスのメッセージだけ抽出
    sfa_messages = [
        m for m in messages
        if m.get("text", "").lstrip().startswith(TRIGGER_PREFIX)
        and m.get("ts") not in processed
    ]
    print(f"[INFO] 未処理の【SFA】メッセージ: {len(sfa_messages)} 件")

    success_count = 0
    error_count = 0

    for msg in sfa_messages:
        ts = msg["ts"]
        text = msg.get("text", "")
        thread_ts = msg.get("thread_ts", ts)  # スレッドのルートtsを優先

        print(f"\n--- ts={ts} ---")
        print(f"  メッセージ: {text[:80].replace(chr(10), ' ')}...")

        # Anthropic APIでパース
        print("  [INFO] Claudeでパース中...")
        try:
            parsed = parse_message_with_claude(text)
        except Exception as e:
            print(f"  [ERROR] Anthropic API エラー: {e}")
            error_count += 1
            continue

        if not parsed:
            msg_text = ":x: 【SFA】メッセージのパースに失敗しました。\n形式を確認して再投稿してください。"
            try:
                post_reply(channel_id, thread_ts, msg_text)
            except Exception as e:
                print(f"  [WARN] Slackリプライ失敗: {e}")
            save_processed_ts(ts)
            error_count += 1
            continue

        print(f"  [INFO] パース結果: {json.dumps(parsed, ensure_ascii=False)}")

        # deal_name_hint の確認
        hint = parsed.get("deal_name_hint", "").strip()
        if not hint:
            msg_text = ":x: 「商談:」フィールドが取得できませんでした。\n商談名を明記して再投稿してください。"
            try:
                post_reply(channel_id, thread_ts, msg_text)
            except Exception as e:
                print(f"  [WARN] Slackリプライ失敗: {e}")
            save_processed_ts(ts)
            error_count += 1
            continue

        # 商談マッチング
        deal = find_deal(con, hint)
        if not deal:
            msg_text = (
                f":x: 商談が見つかりませんでした。\n"
                f"キーワード: 「{hint}」\n"
                f"SFA上の商談名またはアカウント名と一致するキーワードを「商談:」に指定してください。"
            )
            try:
                post_reply(channel_id, thread_ts, msg_text)
            except Exception as e:
                print(f"  [WARN] Slackリプライ失敗: {e}")
            save_processed_ts(ts)
            error_count += 1
            continue

        print(f"  [INFO] 商談マッチ: {deal['deal_name']} (id={deal['id']})")

        # 活動履歴登録
        try:
            activity_id = record_activity(con, deal, parsed)
        except Exception as e:
            print(f"  [ERROR] DB書込みエラー: {e}")
            error_count += 1
            continue

        print(f"  [INFO] 活動履歴登録完了 (activity_id={activity_id})")

        # Slackに完了通知
        ms_info = ""
        if parsed.get("next_milestone_date"):
            ms_info = f"\n次回MS: {parsed['next_milestone_date']}"
            if parsed.get("next_milestone_label"):
                ms_info += f"（{parsed['next_milestone_label']}）"

        reply_text = (
            f":white_check_mark: 活動履歴を登録しました。\n"
            f"商談: {deal['deal_name']}\n"
            f"種別: {parsed.get('activity_type', '-')} / "
            f"日付: {parsed.get('occurred_on', '-')}"
            f"{ms_info}"
        )
        try:
            post_reply(channel_id, thread_ts, reply_text)
        except Exception as e:
            print(f"  [WARN] Slackリプライ失敗（DB登録は成功済）: {e}")

        # 処理済みに記録
        save_processed_ts(ts)
        success_count += 1

    con.close()

    print(f"\n=== 完了: 成功 {success_count} 件 / エラー {error_count} 件 ===")
    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
