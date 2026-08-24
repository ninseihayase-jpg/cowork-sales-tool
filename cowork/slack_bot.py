"""
@NegoCollection Slack bot — SFA連携。

フロー:
  ① #sales スレッドで @NegoCollection をメンション
  ② Bot がスレッド内容 + 既存商談情報を読み取り、Claude でドラフト作成
     → テンプレートをスレッドに投稿（不明項目は【記載なし】）
  ③ 人間が内容を確認・編集（返信で「フィールド名: 値」形式でも上書き可）
  ④ 「確定」or「ok」と返信 → Bot が SFA DB を更新
     （活動履歴追加 + 商談のステージ/次回MS/メモ更新）

環境変数:
  SLACK_BOT_TOKEN      xoxb-...
  SLACK_SIGNING_SECRET Slack App の Signing Secret（省略時は署名検証スキップ）
  ANTHROPIC_API_KEY    Claude API キー
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
# 事務Bot（別Slackアプリ）用。/slack/desk-events で使用（未設定なら事務Botエンドポイントは無効）。
SLACK_DESK_TOKEN = os.environ.get("SLACK_DESK_BOT_TOKEN", "")
SLACK_DESK_SIGNING_SECRET = os.environ.get("SLACK_DESK_SIGNING_SECRET", "")
# #93: 通常タスクBot（別Slackアプリ）用。/slack/task-events で使用（未設定ならタスクBot
# エンドポイントは無効。/task スラッシュコマンド・ダイジェストのボタン等は引き続き
# NegoCollection（既存共有アプリ）側のまま＝desk-eventsと同じ分離範囲）。
SLACK_TASK_TOKEN = os.environ.get("SLACK_TASK_BOT_TOKEN", "")
SLACK_TASK_SIGNING_SECRET = os.environ.get("SLACK_TASK_SIGNING_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SFA_TOOL_URL = os.environ.get("SFA_TOOL_URL", "http://localhost:8787")
# #98: Jamie文字起こし到着時の商談候補提示を投稿する先（#sales）。2026-08-19以降は個人DM
# （JAMIE_CANDIDATE_DM_OWNER）へ変更したため未使用だが、明示的にchannelを渡す呼び出し用に残す。
SALES_CHANNEL_ID = os.environ.get("SALES_CHANNEL_ID", "")
# Jamie商談候補提示の送り先（個人DM）。#sales全体ではなくこの担当者個人へ届ける。
JAMIE_CANDIDATE_DM_OWNER = os.environ.get("JAMIE_CANDIDATE_DM_OWNER", "早瀬").strip()

_bot_user_id: str | None = None


# ── Slack API ──────────────────────────────────────────────────────────────

def _slack_post(method: str, token: str | None = None, **kwargs) -> dict:
    url = f"https://slack.com/api/{method}"
    data = json.dumps(kwargs).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token or SLACK_TOKEN}",  # token指定で別Bot(事務Bot等)として送信
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[SlackBot] {method} error: {e}")
        return {"ok": False, "error": str(e)}


def _slack_get(method: str, params: dict, token: str | None = None) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token or SLACK_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[SlackBot] {method} error: {e}")
        return {"ok": False, "error": str(e)}


def get_bot_user_id() -> str | None:
    global _bot_user_id
    if _bot_user_id:
        return _bot_user_id
    r = _slack_get("auth.test", {})
    if r.get("ok"):
        _bot_user_id = r.get("user_id")
    return _bot_user_id


def get_thread_messages(channel: str, thread_ts: str) -> list[dict]:
    r = _slack_get("conversations.replies", {
        "channel": channel, "ts": thread_ts, "limit": 100,
    })
    return r.get("messages", [])


def post_message(channel: str, thread_ts: str, text: str) -> str | None:
    r = _slack_post("chat.postMessage", channel=channel, thread_ts=thread_ts, text=text)
    if r.get("ok"):
        return r.get("ts")
    print(f"[SlackBot] post_message failed: {r.get('error')}")
    return None


def verify_signature(body: bytes, timestamp: str, signature: str, secret: str | None = None) -> bool:
    """Slack署名検証。秘密未設定・ヘッダー不正時はFalse（fail-closed）。
    secret指定で別アプリ（事務Bot等）のSigning Secretで検証する。"""
    sec = secret if secret is not None else SLACK_SIGNING_SECRET
    if not sec:
        print("[SlackBot] Signing Secret未設定のためリクエストを拒否（fail-closed）", flush=True)
        return False
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:{body.decode()}"
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
    expected = "v0=" + hmac.new(
        sec.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── DB helpers ─────────────────────────────────────────────────────────────

def get_pending_thread(con: sqlite3.Connection, thread_ts: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM slack_threads WHERE thread_ts = ?", (thread_ts,)
    ).fetchone()
    return dict(row) if row else None


def save_pending_thread(con: sqlite3.Connection, thread_ts: str, channel_id: str,
                        deal_id: int | None, bot_message_ts: str | None,
                        state: str = "pending", meta: str | None = None):
    """#新規: 放置検知用にfirst_seen_atを保持する。INSERT OR REPLACEは行を作り直すため、
    既存行のfirst_seen_atを引いてそのまま引き継ぐ（無ければ今回が初回として今の時刻を採用）。
    reminded_atは指定しない＝REPLACEで毎回NULLに戻り、状態が変わるたびリマインドが再送可能になる。"""
    existing = con.execute(
        "SELECT first_seen_at FROM slack_threads WHERE thread_ts=?", (thread_ts,)
    ).fetchone()
    first_seen_at = existing["first_seen_at"] if existing and existing["first_seen_at"] else None
    con.execute("""
        INSERT OR REPLACE INTO slack_threads
            (thread_ts, channel_id, deal_id, bot_message_ts, state, meta, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
    """, (thread_ts, channel_id, deal_id, bot_message_ts, state, meta, first_seen_at))
    con.commit()


def mark_completed(con: sqlite3.Connection, thread_ts: str):
    con.execute(
        "UPDATE slack_threads SET state='completed' WHERE thread_ts=?", (thread_ts,)
    )
    con.commit()


def find_deal(con: sqlite3.Connection, text: str) -> dict | None:
    rows = con.execute("""
        SELECT d.*, a.name as account_name FROM deals d
        LEFT JOIN accounts a ON d.account_id = a.id
        WHERE d.status='open' ORDER BY d.updated_at DESC
    """).fetchall()
    text_l = text.lower()
    # deal_name 優先マッチ（長い名前ほど優先）
    best = None
    best_score = 0
    for r in rows:
        d = dict(r)
        name = (d.get("deal_name") or "").lower()
        if name and name != "未定" and name in text_l:
            score = len(name)
            if score > best_score:
                best_score = score
                best = d
    if best:
        return best
    # account_name フォールバック
    for r in rows:
        d = dict(r)
        acct = (d.get("account_name") or "").lower()
        if acct and acct in text_l:
            return d
    return None


# ── #98: Jamie文字起こし×Slack識別 ─────────────────────────────────────────
# Jamie webhook到着時点では商談との紐付けが無い（識別ステップが#97のWeb inboxでしか
# 発生せず、Slack先攻の運用実態と噛み合わず放置されるバグを設計で特定）。この識別を
# Slackに引き取らせ、候補ボタンの選択で即座に割り当てる。
# 2026-08-19改訂: #salesチャンネル全体ではなくJAMIE_CANDIDATE_DM_OWNER個人のDMへ送る。
# 候補が0件でも投稿する（以前は候補0件なら投稿自体をスキップしWeb inboxに委ねていたが、
# それこそが見逃しの原因だったため、常に通知した上で「対象（候補以外）」の逃げ道を用意する）。

def _resolve_jamie_dm_channel() -> str | None:
    """JAMIE_CANDIDATE_DM_OWNER宛のDMチャネルIDを解決する（owner_slack_map.json→
    users.lookupByEmail→conversations.open）。"""
    from cowork.slack_tasks import _slack_user_id_for
    uid = _slack_user_id_for(JAMIE_CANDIDATE_DM_OWNER)
    if not uid:
        print(f"[SlackBot] Jamie候補DM: 「{JAMIE_CANDIDATE_DM_OWNER}」のSlackユーザーが見つかりません", flush=True)
        return None
    r = _slack_post("conversations.open", users=uid)
    if not r.get("ok"):
        print(f"[SlackBot] Jamie候補DM: DMチャネルを開けませんでした: {r.get('error')}", flush=True)
        return None
    return r["channel"]["id"]


def post_jamie_candidate_prompt(con: sqlite3.Connection, *, inbox_id: int, title: str,
                                occurred_on: str, candidates: list, channel: str | None = None) -> str | None:
    """Jamie webhook到着時、商談候補（[(value,label),...] 例: [("deal:12","A社/案件X")]）を
    個人DM（JAMIE_CANDIDATE_DM_OWNER）に投稿する。ボタンは「候補（最大5件）」＋
    「対象（候補以外）」＋「対象外（割当不要）」の3種類。
    channel省略時はJAMIE_CANDIDATE_DM_OWNERのDMを自動解決。テスト用に別チャネル
    （scripts/send_jamie_candidate_dm_preview.py 等）へ差し替え可能。
    ボタンのaction/valueは本番と同一なので、候補ボタンを押すと実際にそのinbox_idが
    割り当てられる点に注意。「対象外」ボタンは割当不要として完了扱いにするのみでDeal未指定。"""
    channel = channel or _resolve_jamie_dm_channel()
    if not channel:
        return None
    # action_idは各候補ボタンで一意にする必要がある（同じactions block内で複数要素が同じ
    # action_idを共有すると、Slackのchat.postMessageがinvalid_blocksで拒否する。以前は
    # 候補全ボタンが同じ"jamie_pick_deal"を共有しており、候補が2件以上あると本メッセージ自体が
    # 投稿されずに消えていた不具合と同種＝TaskBotのtask_effortボタンでも発生していたもの）。
    buttons = [{
        "type": "button", "action_id": f"jamie_pick_deal:{v.split(':', 1)[1]}",
        "value": f"{inbox_id}:{v.split(':', 1)[1]}",
        "text": {"type": "plain_text", "text": lbl[:75]},
    } for v, lbl in candidates[:5]]
    buttons.append({
        "type": "button", "action_id": "jamie_not_candidate", "value": str(inbox_id),
        "text": {"type": "plain_text", "text": "対象（候補以外）"},
    })
    buttons.append({
        "type": "button", "action_id": "jamie_skip", "value": str(inbox_id),
        "text": {"type": "plain_text", "text": "対象外（割当不要）"},
    })
    text = f"🎙️ Jamie文字起こし到着：*{title}*（{occurred_on}）\nどの商談ですか？"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": buttons},
    ]
    r = _slack_post("chat.postMessage", channel=channel, text=text, blocks=blocks)
    if r.get("ok"):
        return r.get("ts")
    print(f"[SlackBot] post_jamie_candidate_prompt failed: {r.get('error')}")
    return None


def _jamie_update_message(channel: str, ts: str, text: str) -> None:
    r = _slack_post("chat.update", channel=channel, ts=ts, text=text, blocks=[])
    if not r.get("ok"):
        print(f"[SlackBot] jamie chat.update failed: {r.get('error')}")


def handle_interactive(con: sqlite3.Connection, payload: dict) -> None:
    """#98: Jamie候補ボタン（jamie_pick_deal/jamie_skip/jamie_append_yes/jamie_append_no）を処理する。"""
    from cowork import sfa_db as _sfa_db
    actions = payload.get("actions") or []
    if not actions:
        return
    action_id = actions[0].get("action_id") or ""
    value = actions[0].get("value") or ""
    channel = (payload.get("channel") or {}).get("id") or ""
    msg_ts = (payload.get("message") or {}).get("ts") or ""

    if action_id == "jamie_pick_deal" or action_id.startswith("jamie_pick_deal:"):
        try:
            inbox_id_s, deal_id_s = value.split(":", 1)
            inbox_id, deal_id = int(inbox_id_s), int(deal_id_s)
        except ValueError:
            return
        t = _sfa_db.get_intake_transcript(con, inbox_id)
        deal = con.execute(
            "SELECT d.*, a.name as account_name FROM deals d "
            "LEFT JOIN accounts a ON d.account_id=a.id WHERE d.id=?", (deal_id,)).fetchone()
        if not t or not deal:
            _jamie_update_message(channel, msg_ts, "割り当てに失敗しました（対象が見つかりません）。")
            return
        deal = dict(deal)
        occurred_on = t.get("occurred_on") or ""
        _sfa_db.assign_inbox_transcript(con, inbox_id, kind="deal", entity_id=deal_id)
        existing = _sfa_db.find_activity_by_deal_and_date(con, deal_id, occurred_on)
        deal_label = f'{deal.get("account_name") or "—"} / {deal.get("deal_name") or "—"}'
        if existing:
            text = (f"「{deal_label}」に割り当てました。{occurred_on}の活動記録が既にあります。"
                    "Jamie全文をその記録に追記しますか？")
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "actions", "elements": [
                    {"type": "button", "action_id": "jamie_append_yes",
                     "value": f"{inbox_id}:{existing['id']}",
                     "text": {"type": "plain_text", "text": "追記する"}},
                    {"type": "button", "action_id": "jamie_append_no", "value": str(inbox_id),
                     "text": {"type": "plain_text", "text": "追記しない"}},
                ]},
            ]
            r = _slack_post("chat.update", channel=channel, ts=msg_ts, text=text, blocks=blocks)
            if not r.get("ok"):
                print(f"[SlackBot] jamie append-prompt failed: {r.get('error')}")
        else:
            link = f"{SFA_TOOL_URL}/hearing/intake?deal={deal_id}&inbox_id={inbox_id}"
            _jamie_update_message(
                channel, msg_ts,
                f"✅「{deal_label}」に割り当てました。\n<{link}|▶ この文字起こしをWebで取り込む（AI整形）>")

    elif action_id == "jamie_not_candidate":
        try:
            inbox_id = int(value)
        except ValueError:
            return
        link = f"{SFA_TOOL_URL}/intake-inbox#inbox-{inbox_id}"
        _jamie_update_message(channel, msg_ts, f"📋 Webの取り込みインボックスで商談/論点を選んでください → <{link}|開く>")

    elif action_id == "jamie_skip":
        try:
            inbox_id = int(value)
        except ValueError:
            return
        _sfa_db.mark_intake_transcript_status(con, inbox_id, "not_needed")
        _jamie_update_message(channel, msg_ts, "🙅 対象外（割当不要）として完了扱いにしました。")

    elif action_id == "jamie_append_yes":
        try:
            inbox_id_s, activity_id_s = value.split(":", 1)
            inbox_id, activity_id = int(inbox_id_s), int(activity_id_s)
        except ValueError:
            return
        t = _sfa_db.get_intake_transcript(con, inbox_id)
        tx = (t.get("transcript") or "").strip() if t else ""
        if tx:
            cur = con.execute("SELECT body FROM activities WHERE id=?", (activity_id,)).fetchone()
            body = (dict(cur).get("body") or "") if cur else ""
            new_body = (body + "\n\n---\n【Jamie全文】\n" + tx).strip() if body else ("【Jamie全文】\n" + tx)
            con.execute("UPDATE activities SET body=? WHERE id=?", (new_body, activity_id))
            con.commit()
        _sfa_db.mark_intake_transcript_status(con, inbox_id, "saved")
        _jamie_update_message(channel, msg_ts, "✅ 活動履歴にJamie全文を追記しました。")

    elif action_id == "jamie_append_no":
        try:
            inbox_id = int(value)
        except ValueError:
            return
        _sfa_db.mark_intake_transcript_status(con, inbox_id, "saved")
        _jamie_update_message(channel, msg_ts, "追記せず保留しました。")


# ── Claude helpers ─────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "{}"
    import threading as _threading
    result = [None]
    error = [None]

    def _do():
        try:
            url = "https://api.anthropic.com/v1/messages"
            payload = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(url, data=payload, headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
            result[0] = body["content"][0]["text"].strip()
        except Exception as e:
            error[0] = e

    t = _threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=25)  # 最大25秒で強制打ち切り
    if t.is_alive():
        print("[SlackBot] Claude API timeout (25s)", flush=True)
        return "{}"
    if error[0]:
        raise error[0]
    return result[0] or "{}"


def draft_template(thread_text: str, deal: dict | None, con=None) -> str:
    """Claude でスレッド内容からSFA更新ドラフトを作成する。"""
    if deal:
        deal_info = (
            f"商談名: {deal.get('deal_name','')}\n"
            f"ステージ: {deal.get('stage','')}\n"
            f"次回MS日: {deal.get('next_milestone_date','')}\n"
            f"次回MSラベル: {deal.get('next_milestone_label','')}\n"
            f"現状メモ: {deal.get('note','') or '（なし）'}"
        )
    else:
        deal_info = "（商談を特定できませんでした）"

    # DB からマスタを動的取得（未接続時はデフォルト値にフォールバック）
    from cowork import sfa_db as _sfa_db
    _stages = (con and _sfa_db.get_master_list(con, "deal_stages")) or _sfa_db.DEAL_STAGES
    _atypes = (con and _sfa_db.get_master_list(con, "activity_types")) or _sfa_db.ACTIVITY_TYPES
    stages_str = "・".join(_stages)
    atypes_str = "・".join(_atypes)

    prompt = f"""以下はSlackスレッドの会話内容と、現在のSFA商談情報です。
スレッドの内容を分析し、SFA更新ドラフトをJSONで作成してください。

【現在の商談情報】
{deal_info}

【スレッド内容】
{thread_text}

以下のJSONのみ出力（説明不要）:
{{
  "activity_date": "YYYY-MM-DD（読み取れなければ【記載なし】）",
  "activity_type": "{atypes_str} のいずれか（読み取れなければ【記載なし】）",
  "contact_name": "相手の名前（読み取れなければ【記載なし】）",
  "activity_content": "活動内容の要約（スレッドから作成）",
  "stage_update": "{stages_str} のいずれか（変更不要なら null）",
  "next_milestone_date": "YYYY-MM-DD（変更不要なら null、不明なら【記載なし】）",
  "next_milestone_label": "次回MSラベル（変更不要なら null、不明なら【記載なし】）",
  "next_milestone_type": "アポ または タスク（顧客接点=アポ / 社内作業=タスク。変更不要なら null、不明なら【記載なし】）",
  "memo_addition": "追記すべきメモ（追記不要なら null）"
}}"""

    try:
        raw = _call_claude(prompt)
        # JSONブロックを抽出
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"[SlackBot] Claude parse error: {e}")
        parsed = {}

    def v(val):
        return val if val else "【記載なし】"

    deal_name = deal.get("deal_name", "❓ 特定できません") if deal else "❓ 特定できません"
    cur_stage = deal.get("stage", "") if deal else ""
    cur_ms_date = deal.get("next_milestone_date", "") if deal else ""
    cur_ms_label = deal.get("next_milestone_label", "") if deal else ""
    cur_ms_type = deal.get("next_milestone_type", "") if deal else ""
    cur_memo = deal.get("note", "") or "（なし）" if deal else "（なし）"

    stage_upd = parsed.get("stage_update") or "-"
    ms_date = parsed.get("next_milestone_date") or "-"
    ms_label = parsed.get("next_milestone_label") or "-"
    ms_type = parsed.get("next_milestone_type") or "-"
    memo_add = parsed.get("memo_addition") or "-"

    lines = [
        "【SFA更新テンプレート】",
        f"商談: {deal_name}",
        "─── 現在の商談情報 ───",
        f"ステージ: {cur_stage}",
        f"次回MS: {cur_ms_date} / {cur_ms_label}" + (f"（{cur_ms_type}）" if cur_ms_type else ""),
        f"現状メモ: {cur_memo}",
        "",
        "─── 今回の活動 ───",
        f"*活動日: {v(parsed.get('activity_date'))}*",
        f"*種別: {v(parsed.get('activity_type'))}*　　＊{' / '.join(_atypes)}",
        f"相手: {v(parsed.get('contact_name'))}",
        f"内容: {v(parsed.get('activity_content'))}",
        "",
        "─── 商談更新（変更なしは「-」のまま） ───",
        f"ステージ: {stage_upd}　　＊{' / '.join(_stages)}",
        f"*次回MS日: {ms_date}*",
        f"*次回MSラベル: {ms_label}*",
        f"*次回MS種別: {ms_type}*　　＊{' / '.join(_sfa_db.NEXT_MS_TYPES)}",
        f"追記メモ: {memo_add}",
        "",
        "✅ 確認後「確定」または「ok」（yes可）と返信すると保存します。",
        "✏️ 修正する場合は、修正部分だけ記載して返信すると上書きできます。",
        "　※ *次回MS（日付・ラベル・種別）は必ず入力してください*。",
        "　例) 次回MS日: 2026-07-31",
        "　例) 次回MSラベル: (調整中)2次面談/デモあり",
        "　例) 次回MS種別: アポ",
        "　例) ステージ: 要件詰め",
    ]
    if not deal:
        lines.insert(2, "⚠️ 商談を自動特定できませんでした。商談名を明示して再メンションしてください。")

    return "\n".join(lines)


def draft_new_deal_template(thread_text: str, create_mode: str, con=None) -> str:
    """新規商談追加用ドラフトテンプレートをClaudeで作成する。"""
    from cowork import sfa_db as _sfa_db
    _stages = (con and _sfa_db.get_master_list(con, "deal_stages")) or _sfa_db.DEAL_STAGES
    _atypes = (con and _sfa_db.get_master_list(con, "activity_types")) or _sfa_db.ACTIVITY_TYPES
    stages_str = "・".join(_stages)
    atypes_str = "・".join(_atypes)

    activity_json = ""
    if create_mode == "deal_and_activity":
        activity_json = (
            '\n  "activity_date": "YYYY-MM-DD（読み取れなければ【記載なし】）",'
            '\n  "activity_type": "' + atypes_str + ' のいずれか（読み取れなければ【記載なし】）",'
            '\n  "contact_name": "相手の名前（読み取れなければ【記載なし】）",'
            '\n  "activity_content": "活動内容の要約",'
        )

    prompt = (
        "以下はSlackスレッドの会話内容です。新規商談を追加するための情報を抽出してJSONで回答してください。\n\n"
        "【スレッド内容】\n" + thread_text + "\n\n"
        "以下のJSONのみ出力（説明不要）:\n"
        "{\n"
        '  "account_name": "会社名（読み取れなければ【記載なし】）",\n'
        '  "deal_name": "案件名（会社名でよければ会社名）",\n'
        '  "stage": "' + stages_str + ' のいずれか",\n'
        '  "owner": "担当者名（読み取れなければ【記載なし】）",\n'
        '  "note": "メモ（あれば、なければ null）"' + activity_json + "\n"
        "}"
    )

    try:
        raw = _call_claude(prompt)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"[SlackBot] Claude parse error (new deal): {e}")
        parsed = {}

    def v(val):
        return val if val and val != "null" else "【記載なし】"

    title = "【新規商談＋活動履歴追加テンプレート】" if create_mode == "deal_and_activity" else "【新規商談追加テンプレート】"
    lines = [
        title,
        "─── 商談情報 ───",
        f"アカウント名: {v(parsed.get('account_name'))}",
        f"案件名: {v(parsed.get('deal_name'))}",
        f"ステージ: {parsed.get('stage') or (_stages[0] if _stages else '【記載なし】')}　　＊{' / '.join(_stages)}",
        f"担当: {v(parsed.get('owner'))}",
        f"メモ: {parsed.get('note') or '-'}",
    ]

    if create_mode == "deal_and_activity":
        lines += [
            "",
            "─── 活動履歴 ───",
            f"*活動日: {v(parsed.get('activity_date'))}*",
            f"*種別: {v(parsed.get('activity_type'))}*　　＊{' / '.join(_atypes)}",
            f"相手: {v(parsed.get('contact_name'))}",
            f"内容: {v(parsed.get('activity_content'))}",
        ]

    lines += [
        "",
        "✅ 確認後「確定」または「ok」（yes可）と返信すると保存します。",
        "✏️ 修正する場合は、修正部分だけ記載して返信すると上書きできます。",
        "　例) アカウント名: 〇〇社",
        "　例) ステージ: 要件詰め",
    ]
    return "\n".join(lines)


# ── Template parser ────────────────────────────────────────────────────────

# テンプレートで使う全フィールドラベル（複数行フリーテキストの終端判定に使う）。
_FIELD_LABELS_ALL = (
    "活動日", "種別", "相手", "内容", "ステージ", "次回MS日", "次回MSラベル", "次回MS種別",
    "追記メモ", "アカウント名", "案件名", "担当", "メモ", "現状メモ", "目標", "予算",
)
# 複数行になりうるフリーテキスト欄（値が次の行以降に続く）。次のラベル/区切り/フッタまで取り込む。
_FREE_TEXT_FIELDS = ("内容", "追記メモ", "メモ", "現状メモ")


def _extract_field(text: str, label: str) -> str | None:
    """テンプレートまたは返信テキストからフィールド値を抽出。
    Slackの太字 *...*（行頭の*・ラベル直後の*・値末尾の*）に対応。
    内容/追記メモ 等のフリーテキストは、次のラベル行や区切り/フッタまで複数行を取り込む。"""
    lines = text.split("\n")
    # コロンは全角「：」で入力する人が多いため半角「:」と両対応（#新規: 全角コロンで認識されず
    # サイレントに無視される不具合の修正）。
    label_pat = re.compile(rf"^\*?{re.escape(label)}[:：]\*? *(.*)$")
    _others = [l for l in _FIELD_LABELS_ALL if l != label]
    other_label_pat = re.compile(r"^\*?(?:" + "|".join(re.escape(l) for l in _others) + r")[:：]")
    for i, line in enumerate(lines):
        m = label_pat.match(line)
        if not m:
            continue
        if label in _FREE_TEXT_FIELDS:
            # 先頭行＋後続行を、次のラベル/区切り/フッタが来るまで取り込む。
            collected = [m.group(1)]
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if other_label_pat.match(nxt):
                    break
                if s[:1] in ("─", "—", "―", "✅", "✏️", "🏢", "🔄") or s.startswith(("※", "例)")):
                    break
                collected.append(nxt)
            val = "\n".join(collected).strip()
            val = val.rstrip("*").strip()
            if val in ("-", "【記載なし】", "変更なし", "（なし）", ""):
                return None
            return val
        # 単一行フィールド（従来どおり）
        val = m.group(1).strip()
        val = re.sub(r'[\s　]+＊.*$', '', val).strip()   # ヒント（　　＊選択肢...）除去
        val = val.rstrip('*').strip()                     # 行全体太字の末尾*除去
        if val in ("-", "【記載なし】", "変更なし", "（なし）", ""):
            return None
        return val
    return None


_DATE_FIELDS = ("次回MS日", "活動日")


def _normalize_date_str(s: str) -> str | None:
    """人間の自由記述の日付表記（2026/10/31・2026.10.31・2026年10月31日等）をISO(YYYY-MM-DD)に
    正規化する。パース不能ならNone（=未指定扱い）。

    背景（実事故）: SFAのdeals.next_milestone_date/deal_milestones.ms_dateは<input type="date">で
    表示するため、スラッシュ区切り等の非ISO文字列を保存するとブラウザが値を表示できず「MSが空」に
    見える不具合が起きた。Bot側は確定時にfieldsの生テキストをそのまま「次回MS→...」と成功報告して
    しまうため、保存に失敗している（=表示できないゴミが入っている）ことに気付けなかった。"""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r'^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$', s)
    if not m:
        return None
    from datetime import date as _date
    try:
        iso = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        _date.fromisoformat(iso)
        return iso
    except ValueError:
        return None


def collect_fields(messages: list[dict], bot_ts: str, confirm_ts: str) -> dict:
    """
    bot_ts のテンプレートを基準に、その後の人間の返信で上書きした最終値を返す。
    confirm_ts より前のメッセージのみ対象。
    日付欄（次回MS日・活動日）は表記揺れ（区切り文字）を吸収してISOへ正規化する。
    """
    bot_uid = get_bot_user_id()
    base: dict = {}
    overrides: dict = {}
    # bot_ts が None（post_message失敗等でDBにNULLが残った場合）でも
    # 文字列比較(ts > bot_ts)でTypeErrorにならないようフォールバック
    bot_ts = bot_ts or ""

    all_labels = (
        "活動日", "種別", "相手", "内容", "ステージ", "次回MS日", "次回MSラベル", "次回MS種別",
        "追記メモ", "アカウント名", "案件名", "担当", "メモ",
    )
    for m in messages:
        ts = m.get("ts", "")
        is_bot = m.get("bot_id") or m.get("user") == bot_uid
        text = m.get("text", "")

        if ts == bot_ts and is_bot:
            # ベーステンプレート
            for label in all_labels:
                val = _extract_field(text, label)
                if val:
                    base[label] = val

        elif not is_bot and ts != confirm_ts and ts > bot_ts:
            # 人間による上書き返信
            for label in all_labels:
                val = _extract_field(text, label)
                if val:
                    overrides[label] = val

    merged = {**base, **overrides}
    for _lbl in _DATE_FIELDS:
        if _lbl in merged:
            _norm = _normalize_date_str(merged[_lbl])
            if _norm:
                merged[_lbl] = _norm
            else:
                del merged[_lbl]
    return merged


# ── DB update ──────────────────────────────────────────────────────────────

def apply_to_db(con: sqlite3.Connection, fields: dict, deal_id: int | None,
                theme_client=None, meta: str | None = None) -> int | None:
    import datetime
    from cowork import sfa_db as _sfa_db
    valid_stages = set(_sfa_db.get_master_list(con, "deal_stages") or _sfa_db.DEAL_STAGES)
    valid_atypes = set(_sfa_db.get_master_list(con, "activity_types") or _sfa_db.ACTIVITY_TYPES)

    meta_dict = {}
    if meta:
        try:
            meta_dict = json.loads(meta)
        except Exception:
            pass
    create_mode = meta_dict.get("create_mode")

    # 新規商談作成
    if deal_id is None and create_mode:
        account_name = (fields.get("アカウント名") or "").strip() or "(未設定)"
        existing_acc = con.execute(
            "SELECT id FROM accounts WHERE name=?", (account_name,)
        ).fetchone()
        if existing_acc:
            account_id = dict(existing_acc)["id"]
        else:
            account_id = _sfa_db.upsert_account(con, name=account_name)

        stage = fields.get("ステージ")
        if not stage or stage not in valid_stages:
            stage = next(iter(valid_stages), None)

        deal_id = _sfa_db.upsert_deal(
            con,
            account_id=account_id,
            deal_name=(fields.get("案件名") or "").strip() or account_name,
            stage=stage,
            owner=(fields.get("担当") or "").strip() or None,
            status="open",
            note=(fields.get("メモ") or "").strip() or None,
        )
        print(f"[SlackBot] new deal created: deal_id={deal_id} account={account_name}", flush=True)

    # 活動履歴（既存商談の更新 or 新規商談+活動）
    content = fields.get("内容")
    if deal_id and content and create_mode != "deal_only":
        date_str = fields.get("活動日") or datetime.date.today().isoformat()
        activity_type = fields.get("種別") or "メモ"
        if activity_type not in valid_atypes:
            activity_type = "メモ"
        # #98: 同一(deal_id,面談日)で割当済み・未消化のJamie全文があれば統合
        # （Jamie全文を本文、Slackで人が入力した内容を強調点として追記）。
        _jamie = _sfa_db.find_assigned_jamie_transcript(con, deal_id, date_str)
        if _jamie and (_jamie.get("transcript") or "").strip():
            content = _jamie["transcript"].strip() + "\n\n---\n【Slack強調】\n" + content.strip()
        # 次回MSライフサイクル是正（新規）: add_activityが内部でdate_str以前の未完了MSを
        # 自動完了にする（生SQL直INSERTだとこの安全網が効かず、旧MSがMS超過に残り続けていた）。
        _sfa_db.add_activity(con, deal_id=deal_id, type=activity_type, occurred_on=date_str,
                             contact_name=(fields.get("相手") or None), body=content)
        if _jamie:
            _sfa_db.mark_intake_transcript_status(con, _jamie["id"], "saved")

    # 商談フィールド更新（既存商談のみ）
    if deal_id and not create_mode:
        updates: dict = {}
        if fields.get("ステージ") and fields["ステージ"] in valid_stages:
            updates["stage"] = fields["ステージ"]
        # 次回MSライフサイクル是正（新規）: deals.next_milestone_*への直接書き込みは
        # deal_milestones(正本)とキャッシュを乖離させるため廃止。add_deal_milestoneで
        # 正本テーブルに追加し、recompute経由でキャッシュへ反映させる。
        if fields.get("次回MS日") or fields.get("次回MSラベル"):
            _ms_type = fields.get("次回MS種別") if fields.get("次回MS種別") in _sfa_db.NEXT_MS_TYPES else None
            _sfa_db.add_deal_milestone(con, deal_id, date=fields.get("次回MS日"),
                                       label=fields.get("次回MSラベル"), ms_type=_ms_type)
        if fields.get("追記メモ"):
            cur = con.execute("SELECT note FROM deals WHERE id=?", (deal_id,)).fetchone()
            existing = (dict(cur).get("note") or "") if cur else ""
            new_note = (existing + "\n" + fields["追記メモ"]).strip() if existing else fields["追記メモ"]
            updates["note"] = new_note

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            con.execute(
                f"UPDATE deals SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                [*updates.values(), deal_id],
            )

    con.commit()

    # Hisho テーマDB sync
    if theme_client and deal_id:
        try:
            from cowork import theme_link as _tl
            result = _tl.sync_deal(theme_client, con, deal_id)
            print(f"[SlackBot] Hisho sync: deal_id={deal_id} action={result.get('action')} theme_id={result.get('theme_id')}", flush=True)
        except Exception as _e:
            # sync_deal内でcommit前に失敗した場合に備え、未コミットの書き込みを破棄
            con.rollback()
            print(f"[SlackBot] Hisho sync error: {_e}", flush=True)

    return deal_id


# ── Event handlers ─────────────────────────────────────────────────────────

def handle_mention(event: dict, con: sqlite3.Connection):
    channel = event.get("channel", "")
    event_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or event_ts

    import socket as _socket
    _socket.setdefaulttimeout(15)  # urllib のハング対策
    print(f"[SlackBot] mention: channel={channel} thread={thread_ts}", flush=True)

    # 前回完了以降のメッセージのみ使う場合にセット（同一スレッド追記対応）
    since_ts = ""

    # 二重処理防止
    existing = get_pending_thread(con, thread_ts)
    print(f"[SlackBot] existing={existing and existing.get('state')}", flush=True)
    if existing:
        state = existing.get("state", "")
        bot_ts = existing.get("bot_message_ts")
        if state == "identifying":
            post_message(channel, thread_ts,
                "⏳ 商談確認待ちです。「はい」または「いいえ」で返信してください。\n"
                "やり直す場合は「キャンセル」と返信してください。")
            return
        elif state == "pending" and not bot_ts:
            # ドラフト未投稿のまま pending になっている（デプロイ中断等）→ 自動リセット
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            print(f"[SlackBot] stuck pending reset: thread={thread_ts}")
            # 以降は通常フローで再処理
        elif state == "pending":
            post_message(channel, thread_ts,
                "⏳ テンプレートは投稿済みです。内容を確認し「確定」または「ok」と返信してください。\n"
                "やり直す場合は「キャンセル」と返信してください。")
            return
        elif state == "completed":
            # 完了済みスレッドへの再メンション → 前回完了以降の新規メッセージで新サイクル開始
            since_ts = existing.get("bot_message_ts") or ""
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            print(f"[SlackBot] completed→re-trigger: thread={thread_ts} since_ts={since_ts}", flush=True)
            # 以降は通常フローで再処理（since_ts 以降のメッセージのみ使用）
        elif state == "cancelled":
            # キャンセル済みは再処理を許可
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
        else:
            return

    # スレッド全文取得（botメッセージ・@メンション除去、since_ts 以前の既処理メッセージも除外）
    print("[SlackBot] getting bot_uid...", flush=True)
    bot_uid = get_bot_user_id()
    print(f"[SlackBot] bot_uid={bot_uid}", flush=True)
    messages = get_thread_messages(channel, thread_ts)
    print(f"[SlackBot] thread messages={len(messages)}", flush=True)
    parts = []
    for m in messages:
        if m.get("bot_id") or m.get("user") == bot_uid:
            continue
        if since_ts and m.get("ts", "") <= since_ts:
            continue
        text = re.sub(r"<@[A-Z0-9]+>", "", m.get("text", "")).strip()
        if text:
            parts.append(text)
    thread_text = "\n".join(parts)

    # 商談マッチ
    deal = find_deal(con, thread_text)

    if not deal:
        msg = (
            "⚠️ 既存の商談が見つかりませんでした。\n\n"
            f"• 商談番号が分かる場合 → 数字のみで返信（例: `8`）\n"
            f"• 新規商談として追加する場合 → `new` と返信\n"
            f"商談一覧: {SFA_TOOL_URL}/deals?tab=active\n\n"
            "「キャンセル」でやり直し"
        )
        bot_ts = post_message(channel, thread_ts, msg)
        if bot_ts:
            save_pending_thread(con, thread_ts, channel, None, bot_ts, state="new_deal_ask")
        else:
            print(f"[SlackBot] post_message failed — pending not saved: thread={thread_ts}", flush=True)
        return

    # 商談特定結果を人間に確認
    acct = deal.get("account_name") or deal.get("deal_name") or "不明"
    deal_name = deal.get("deal_name") or "未定"
    stage = deal.get("stage") or "未設定"
    ms_date = deal.get("next_milestone_date") or "—"
    ms_label = deal.get("next_milestone_label") or "—"

    deal_id_str = deal.get("id", "?")
    confirm_text = (
        f"🔍 以下の商談でよいですか？\n\n"
        f"*SFA#{deal_id_str}* | *{acct}* / {deal_name}\n"
        f"ステージ: {stage}　　次回MS: {ms_date} / {ms_label}\n\n"
        f"「はい」(yes / ok)で続行 / 「いいえ」(no)の場合は正しいSFA番号（数字のみ）を返信してください\n"
        f"商談一覧: {SFA_TOOL_URL}/deals?tab=active"
    )
    bot_ts = post_message(channel, thread_ts, confirm_text)

    # state='identifying' で保存（商談確認待ち）
    if bot_ts:
        save_pending_thread(con, thread_ts, channel, deal["id"], bot_ts,
                            state="identifying")
    else:
        print(f"[SlackBot] post_message failed — pending not saved: thread={thread_ts}", flush=True)


def handle_message(event: dict, con: sqlite3.Connection, theme_client=None):
    # botメッセージ・編集イベントはスキップ
    if event.get("bot_id") or event.get("subtype"):
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    # @NegoCollection で開始していないスレッドは無音でスキップ
    pending = get_pending_thread(con, thread_ts)
    if not pending:
        return

    channel = event.get("channel", "")
    text = event.get("text", "").strip()
    text_l = text.lower()
    confirm_ts = event.get("ts", "")

    print(f"[SlackBot] handle_message: thread={thread_ts!r} state={pending.get('state')!r}", flush=True)

    state = pending.get("state", "")
    deal_id = pending.get("deal_id")
    bot_ts = pending.get("bot_message_ts", "")

    # ── State: new_deal_ask — 商談番号 or new 入力待ち ─────────────────────
    if state == "new_deal_ask":
        if text_l in ("キャンセル", "cancel"):
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            post_message(channel, thread_ts,
                "🔄 リセットしました。再度 @NegoCollection をメンションしてください。")
            return

        if text_l in ("new", "新規", "新規商談"):
            new_bot_ts = post_message(channel, thread_ts,
                "新規商談として追加します。パターンを選択してください。\n\n"
                "*1️⃣ 商談のみ追加*（活動履歴なし）\n"
                "*2️⃣ 商談＋活動履歴を追加*\n\n"
                "「1」または「2」で返信 / 「キャンセル」でやり直し")
            if new_bot_ts:
                con.execute(
                    "UPDATE slack_threads SET state='new_deal_select', bot_message_ts=? WHERE thread_ts=?",
                    (new_bot_ts, thread_ts),
                )
                con.commit()
            else:
                print(f"[SlackBot] post_message failed — state stays new_deal_ask: thread={thread_ts}", flush=True)
            return

        if text.strip().isdigit():
            specified_id = int(text.strip())
            row = con.execute("""
                SELECT d.*, a.name as account_name FROM deals d
                LEFT JOIN accounts a ON d.account_id = a.id
                WHERE d.id = ? AND (d.status = 'open' OR d.status IS NULL)
            """, (specified_id,)).fetchone()
            if not row:
                post_message(channel, thread_ts,
                    f"❌ SFA#{specified_id} が見つかりません（open商談のみ指定可）。\n"
                    f"商談一覧: {SFA_TOOL_URL}/deals?tab=active")
            else:
                d = dict(row)
                acct = d.get("account_name") or d.get("deal_name") or "不明"
                dn   = d.get("deal_name") or "未定"
                st   = d.get("stage") or "未設定"
                ms   = d.get("next_milestone_date") or "—"
                msl  = d.get("next_milestone_label") or "—"
                confirm_text = (
                    f"🔍 以下の商談でよいですか？\n\n"
                    f"*SFA#{specified_id}* | *{acct}* / {dn}\n"
                    f"ステージ: {st}　　次回MS: {ms} / {msl}\n\n"
                    f"「はい」(yes / ok)で続行 / 「いいえ」(no)で別の番号を指定 / 「new」で新規商談追加\n"
                    f"商談一覧: {SFA_TOOL_URL}/deals?tab=active"
                )
                new_bot_ts = post_message(channel, thread_ts, confirm_text)
                if new_bot_ts:
                    con.execute(
                        "UPDATE slack_threads SET deal_id=?, bot_message_ts=?, state='identifying' WHERE thread_ts=?",
                        (specified_id, new_bot_ts, thread_ts),
                    )
                    con.commit()
                else:
                    print(f"[SlackBot] post_message failed — state stays new_deal_ask: thread={thread_ts}", flush=True)
            return

        post_message(channel, thread_ts,
            f"商談番号（数字）または「new」で返信してください。\n商談一覧: {SFA_TOOL_URL}/deals?tab=active")
        return

    # ── State: new_deal_select — 新規商談パターン選択待ち ──────────────────
    if state == "new_deal_select":
        if text_l in ("キャンセル", "cancel"):
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            post_message(channel, thread_ts,
                "🔄 リセットしました。再度 @NegoCollection をメンションしてください。")
            return

        if text_l in ("1", "１", "商談のみ", "商談のみ追加"):
            create_mode = "deal_only"
        elif text_l in ("2", "２", "商談+活動", "商談＋活動", "商談+活動履歴", "商談＋活動履歴"):
            create_mode = "deal_and_activity"
        else:
            post_message(channel, thread_ts,
                "「1」または「2」で返信してください。\n"
                "1: 商談のみ追加　2: 商談＋活動履歴を追加")
            return

        bot_uid = get_bot_user_id()
        messages = get_thread_messages(channel, thread_ts)
        parts = []
        for m in messages:
            if m.get("bot_id") or m.get("user") == bot_uid:
                continue
            t = re.sub(r"<@[A-Z0-9]+>", "", m.get("text", "")).strip()
            if t and t.lower() not in ("1", "２", "2", "１", "キャンセル", "cancel"):
                parts.append(t)
        thread_text = "\n".join(parts)

        template = draft_new_deal_template(thread_text, create_mode, con)
        new_bot_ts = post_message(channel, thread_ts, template)

        if new_bot_ts:
            meta_json = json.dumps({"create_mode": create_mode}, ensure_ascii=False)
            con.execute(
                "UPDATE slack_threads SET state='new_deal_pending', bot_message_ts=?, meta=? WHERE thread_ts=?",
                (new_bot_ts, meta_json, thread_ts),
            )
            con.commit()
            print(f"[SlackBot] new_deal_select→new_deal_pending: thread={thread_ts} mode={create_mode}", flush=True)
        return

    # ── State: new_deal_pending — 新規商談テンプレート確定待ち ─────────────
    if state == "new_deal_pending":
        if text_l in ("キャンセル", "cancel"):
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            post_message(channel, thread_ts,
                "🔄 リセットしました。再度 @NegoCollection をメンションしてください。")
            return

        if text_l not in ("確定", "ok", "yes", "はい"):
            # 上書き返信を検知したら短く応答して"無言"を防ぐ
            if any(_extract_field(text, lb) for lb in
                   ("アカウント名", "案件名", "担当", "ステージ", "メモ", "種別", "相手", "内容", "活動日")):
                post_message(channel, thread_ts,
                    "✍️ 上書きを受け付けました。反映するには「確定」または「ok」と返信してください。")
            return

        messages = get_thread_messages(channel, thread_ts)
        fields = collect_fields(messages, bot_ts, confirm_ts)
        meta_str = pending.get("meta") or "{}"
        meta_dict = {}
        try:
            meta_dict = json.loads(meta_str)
        except Exception:
            pass
        create_mode = meta_dict.get("create_mode", "deal_only")

        account_name = (fields.get("アカウント名") or "").strip()
        if not account_name or account_name == "【記載なし】":
            post_message(channel, thread_ts,
                "❌ アカウント名が読み取れませんでした。\n"
                "「アカウント名: 〇〇社」の形式で返信して「確定」と再送してください。")
            return

        if create_mode == "deal_and_activity" and not fields.get("内容"):
            post_message(channel, thread_ts,
                "❌ 活動内容（内容:）が読み取れませんでした。\n"
                "「内容: ...」の形式で返信して「確定」と再送してください。")
            return

        # アカウントが既存か確認
        existing_acc = con.execute(
            "SELECT id FROM accounts WHERE name=?", (account_name,)
        ).fetchone()

        if not existing_acc:
            # 新規アカウント → 明示的に確認を取る
            meta_with_fields = json.dumps(
                {"create_mode": create_mode, "new_account_name": account_name,
                 "pending_fields": fields, "template_bot_ts": bot_ts},
                ensure_ascii=False,
            )
            deal_name = (fields.get("案件名") or "").strip() or account_name
            new_bot_ts = post_message(channel, thread_ts,
                f"🏢 *新規アカウント「{account_name}」* はまだ登録されていません。\n\n"
                f"以下の内容でアカウントと商談を追加してよいですか？\n"
                f"• アカウント: {account_name}\n"
                f"• 案件名: {deal_name}\n"
                f"• create_mode: {'商談のみ' if create_mode == 'deal_only' else '商談＋活動履歴'}\n\n"
                f"「はい」で追加確定 / アカウント名を直すなら「アカウント名: 正しい名前」と返信→「確定」\n"
                f"（テンプレートごと直すなら「いいえ」／「キャンセル」でやり直し）")
            if new_bot_ts:
                con.execute(
                    "UPDATE slack_threads SET state='new_deal_acc_confirm', bot_message_ts=?, meta=? WHERE thread_ts=?",
                    (new_bot_ts, meta_with_fields, thread_ts),
                )
                con.commit()
            else:
                print(f"[SlackBot] post_message failed — state stays new_deal_pending: thread={thread_ts}", flush=True)
            return

        # 既存アカウント → そのまま商談作成
        try:
            new_deal_id = apply_to_db(con, fields, None, theme_client, meta=meta_str)
            mark_completed(con, thread_ts)
            deal_name = (fields.get("案件名") or "").strip() or account_name
            act_msg = "活動履歴: 追加完了 / " if create_mode == "deal_and_activity" else ""
            post_message(channel, thread_ts,
                f"✅ 商談を追加しました。\n"
                f"SFA#{new_deal_id} | {account_name} / {deal_name}\n"
                f"{act_msg}{SFA_TOOL_URL}/deal/{new_deal_id}")
            print(f"[SlackBot] new deal added: thread={thread_ts} deal_id={new_deal_id}", flush=True)
        except Exception as e:
            con.rollback()
            post_message(channel, thread_ts, f"❌ DB追加エラー: {e}")
            print(f"[SlackBot] new deal error: {e}", flush=True)
        return

    # ── State: new_deal_acc_confirm — 新規アカウント追加確認待ち ────────────
    if state == "new_deal_acc_confirm":
        if text_l in ("キャンセル", "cancel"):
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            post_message(channel, thread_ts,
                "🔄 リセットしました。再度 @NegoCollection をメンションしてください。")
            return

        meta_str = pending.get("meta") or "{}"
        meta_dict = {}
        try:
            meta_dict = json.loads(meta_str)
        except Exception:
            pass
        create_mode = meta_dict.get("create_mode", "deal_only")

        # アカウント名の直接上書きを受け付ける（活動履歴修正と同じ仕様）。
        # 「アカウント名: 正しい名前」または「アカウント: 正しい名前」で修正 →「確定/ok」で反映。
        if text_l not in ("はい", "yes", "y", "ok", "確定", "いいえ", "no", "n"):
            _acc_ov = _extract_field(text, "アカウント名") or _extract_field(text, "アカウント")
            if _acc_ov:
                _new_name = _acc_ov.strip()
                meta_dict["new_account_name"] = _new_name
                _pf = meta_dict.get("pending_fields", {}) or {}
                _pf["アカウント名"] = _new_name
                meta_dict["pending_fields"] = _pf
                con.execute("UPDATE slack_threads SET meta=? WHERE thread_ts=?",
                            (json.dumps(meta_dict, ensure_ascii=False), thread_ts))
                con.commit()
                post_message(channel, thread_ts,
                    f"✍️ アカウント名を「{_new_name}」で受け付けました。"
                    "反映するには「確定」または「ok」と返信してください。")
                return

        if text_l in ("いいえ", "no", "n"):
            # テンプレートを再投稿して pending に戻す
            from cowork import sfa_db as _sfa_db
            _stages = _sfa_db.get_master_list(con, "deal_stages") or _sfa_db.DEAL_STAGES
            _atypes = _sfa_db.get_master_list(con, "activity_types") or _sfa_db.ACTIVITY_TYPES
            old_fields = meta_dict.get("pending_fields", {})
            title = "【新規商談" + ("＋活動履歴" if create_mode == "deal_and_activity" else "") + "追加テンプレート（再編集）】"
            lines = [
                title,
                "─── 商談情報 ─── ⚠️ アカウント名を修正してください",
                f"アカウント名: {old_fields.get('アカウント名', '【記載なし】')}",
                f"案件名: {old_fields.get('案件名', '【記載なし】')}",
                f"ステージ: {old_fields.get('ステージ', _stages[0] if _stages else '【記載なし】')}　　＊{' / '.join(_stages)}",
                f"担当: {old_fields.get('担当', '【記載なし】')}",
                f"メモ: {old_fields.get('メモ', '-')}",
            ]
            if create_mode == "deal_and_activity":
                lines += [
                    "",
                    "─── 活動履歴 ───",
                    f"*活動日: {old_fields.get('活動日', '【記載なし】')}*",
                    f"*種別: {old_fields.get('種別', '【記載なし】')}*　　＊{' / '.join(_atypes)}",
                    f"相手: {old_fields.get('相手', '【記載なし】')}",
                    f"内容: {old_fields.get('内容', '【記載なし】')}",
                ]
            lines += ["", "✅ 修正後「確定」と返信してください / 「キャンセル」でやり直し"]
            new_bot_ts = post_message(channel, thread_ts, "\n".join(lines))
            new_meta = json.dumps({"create_mode": create_mode}, ensure_ascii=False)
            if new_bot_ts:
                con.execute(
                    "UPDATE slack_threads SET state='new_deal_pending', bot_message_ts=?, meta=? WHERE thread_ts=?",
                    (new_bot_ts, new_meta, thread_ts),
                )
                con.commit()
            else:
                print(f"[SlackBot] post_message failed — state stays new_deal_acc_confirm: thread={thread_ts}", flush=True)
            return

        if text_l not in ("はい", "yes", "y", "ok", "確定"):
            post_message(channel, thread_ts,
                "「はい」/「確定」/「ok」でアカウント・商談を追加、"
                "「アカウント名: 正しい名前」で名称を修正、「いいえ」でテンプレート再編集、"
                "「キャンセル」でやり直し。")
            return

        # 「はい」/「確定」/「ok」→ アカウント + 商談を作成
        fields = meta_dict.get("pending_fields", {})
        account_name = meta_dict.get("new_account_name", (fields.get("アカウント名") or "").strip())
        try:
            new_deal_id = apply_to_db(con, fields, None, theme_client, meta=meta_str)
            mark_completed(con, thread_ts)
            deal_name = (fields.get("案件名") or "").strip() or account_name
            act_msg = "活動履歴: 追加完了 / " if create_mode == "deal_and_activity" else ""
            post_message(channel, thread_ts,
                f"✅ 新規アカウント「{account_name}」と商談を追加しました。\n"
                f"SFA#{new_deal_id} | {account_name} / {deal_name}\n"
                f"{act_msg}{SFA_TOOL_URL}/deal/{new_deal_id}")
            print(f"[SlackBot] new deal+account: thread={thread_ts} deal_id={new_deal_id}", flush=True)
        except Exception as e:
            con.rollback()
            post_message(channel, thread_ts, f"❌ DB追加エラー: {e}")
            print(f"[SlackBot] new deal+account error: {e}", flush=True)
        return

    # ── State: identifying — 商談確認待ち ──────────────────────────────────
    if state == "identifying":
        if text_l in ("キャンセル", "cancel"):
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
            post_message(channel, thread_ts,
                "🔄 リセットしました。再度 @NegoCollection をメンションしてください。")
            return
        if text_l in ("はい", "yes", "y", "ok"):
            # 確認OK → スレッド全文を再取得してドラフト生成
            bot_uid = get_bot_user_id()
            messages = get_thread_messages(channel, thread_ts)
            parts = []
            for m in messages:
                if m.get("bot_id") or m.get("user") == bot_uid:
                    continue
                t = re.sub(r"<@[A-Z0-9]+>", "", m.get("text", "")).strip()
                # 確認返信（はい/いいえ）はドラフト生成用テキストから除外
                if t and t.lower() not in ("はい", "yes", "y", "ok", "いいえ", "no", "n"):
                    parts.append(t)
            thread_text = "\n".join(parts)

            # DBから商談情報取得（account_name含む）
            deal = None
            if deal_id:
                row = con.execute("""
                    SELECT d.*, a.name as account_name FROM deals d
                    LEFT JOIN accounts a ON d.account_id = a.id
                    WHERE d.id = ?
                """, (deal_id,)).fetchone()
                if row:
                    deal = dict(row)

            template = draft_template(thread_text, deal, con)
            new_bot_ts = post_message(channel, thread_ts, template)

            if new_bot_ts:
                con.execute(
                    "UPDATE slack_threads SET state='pending', bot_message_ts=? WHERE thread_ts=?",
                    (new_bot_ts, thread_ts)
                )
                con.commit()
                print(f"[SlackBot] identifying→pending: thread={thread_ts} deal_id={deal_id}")
            else:
                print(f"[SlackBot] post_message failed — state stays identifying: thread={thread_ts}")

        elif text_l in ("いいえ", "no", "n"):
            # キャンセルせずにSFA番号指定 or new を促す
            post_message(channel, thread_ts,
                f"🔄 商談番号（数字のみ、例: `8`）で返信するか、新規商談の場合は `new` と返信してください。\n"
                f"商談一覧: {SFA_TOOL_URL}/deals?tab=active")

        elif text_l in ("new", "新規", "新規商談"):
            new_bot_ts = post_message(channel, thread_ts,
                "新規商談として追加します。パターンを選択してください。\n\n"
                "*1️⃣ 商談のみ追加*（活動履歴なし）\n"
                "*2️⃣ 商談＋活動履歴を追加*\n\n"
                "「1」または「2」で返信 / 「キャンセル」でやり直し")
            if new_bot_ts:
                con.execute(
                    "UPDATE slack_threads SET deal_id=NULL, state='new_deal_select', bot_message_ts=? WHERE thread_ts=?",
                    (new_bot_ts, thread_ts),
                )
                con.commit()
            else:
                print(f"[SlackBot] post_message failed — state stays identifying: thread={thread_ts}", flush=True)

        elif text.strip().isdigit():
            # SFA番号で商談を直接指定
            specified_id = int(text.strip())
            row = con.execute("""
                SELECT d.*, a.name as account_name FROM deals d
                LEFT JOIN accounts a ON d.account_id = a.id
                WHERE d.id = ? AND (d.status = 'open' OR d.status IS NULL)
            """, (specified_id,)).fetchone()

            if not row:
                post_message(channel, thread_ts,
                    f"❌ SFA#{specified_id} が見つかりません（open商談のみ指定可）。\n"
                    f"商談一覧: {SFA_TOOL_URL}/deals?tab=active")
            else:
                deal = dict(row)
                acct = deal.get("account_name") or deal.get("deal_name") or "不明"
                deal_name = deal.get("deal_name") or "未定"
                stage = deal.get("stage") or "未設定"
                ms_date = deal.get("next_milestone_date") or "—"
                ms_label = deal.get("next_milestone_label") or "—"

                confirm_text = (
                    f"🔍 以下の商談でよいですか？\n\n"
                    f"*SFA#{specified_id}* | *{acct}* / {deal_name}\n"
                    f"ステージ: {stage}　　次回MS: {ms_date} / {ms_label}\n\n"
                    f"「はい」(yes / ok)で続行 / 「いいえ」(no)で別の番号を指定 / 「new」で新規商談追加\n"
                    f"商談一覧: {SFA_TOOL_URL}/deals?tab=active"
                )
                new_bot_ts = post_message(channel, thread_ts, confirm_text)
                if new_bot_ts:
                    con.execute(
                        "UPDATE slack_threads SET deal_id=?, bot_message_ts=? WHERE thread_ts=?",
                        (specified_id, new_bot_ts, thread_ts)
                    )
                    con.commit()
                    print(f"[SlackBot] deal switched to #{specified_id}: thread={thread_ts}")
                else:
                    print(f"[SlackBot] post_message failed — deal switch to #{specified_id} not saved: thread={thread_ts}", flush=True)

        # それ以外（会話の続き等）は無視
        return

    # ── State: pending — テンプレート確定待ち ─────────────────────────────
    if state != "pending":
        return

    if text_l in ("キャンセル", "cancel"):
        con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
        con.commit()
        post_message(channel, thread_ts,
            "🔄 リセットしました。再度 @NegoCollection をメンションしてください。")
        return

    if text_l not in ("確定", "ok", "yes", "はい"):
        # 上書き返信（「フィールド: 値」）を検知したら短く応答して"無言"を防ぐ
        if any(_extract_field(text, lb) for lb in
               ("種別", "相手", "内容", "活動日", "ステージ", "次回MS日", "次回MSラベル", "次回MS種別", "追記メモ")):
            post_message(channel, thread_ts,
                "✍️ 上書きを受け付けました。反映するには「確定」または「ok」と返信してください。")
        return

    # テンプレート + 上書き値を収集
    messages = get_thread_messages(channel, thread_ts)
    fields = collect_fields(messages, bot_ts, confirm_ts)

    if not fields.get("内容"):
        post_message(channel, thread_ts,
            "❌ 活動内容（内容:）が読み取れませんでした。テンプレートの「内容:」を記入して「確定」と再送してください。")
        return

    try:
        apply_to_db(con, fields, deal_id, theme_client)
        mark_completed(con, thread_ts)

        updated_parts = []
        if fields.get("ステージ"): updated_parts.append(f"ステージ→{fields['ステージ']}")
        if fields.get("次回MS日"): updated_parts.append(f"次回MS→{fields['次回MS日']}")
        summary = "、".join(updated_parts) if updated_parts else "商談フィールド変更なし"
        post_message(channel, thread_ts,
            f"✅ SFA DB を更新しました。\n活動履歴: 追加完了 / {summary}")
        print(f"[SlackBot] DB updated: thread={thread_ts} deal_id={deal_id}")
    except Exception as e:
        con.rollback()
        post_message(channel, thread_ts, f"❌ DB更新エラー: {e}")
        print(f"[SlackBot] DB update error: {e}")


def _ensure_event_dedup_table(con: sqlite3.Connection) -> None:
    """Slackイベント冪等化用テーブル（sfa_db.py側のスキーマは変更しない）。"""
    con.execute("""
        CREATE TABLE IF NOT EXISTS slack_processed_events (
            event_id TEXT PRIMARY KEY,
            processed_at TEXT
        )
    """)


def _mark_event_processed(con: sqlite3.Connection, event_id: str) -> bool:
    """
    event_id を処理済みとして記録する。
    既に記録済み（Slackの再送等による重複イベント）なら False を返す。
    """
    _ensure_event_dedup_table(con)
    try:
        con.execute(
            "INSERT INTO slack_processed_events (event_id, processed_at) VALUES (?, datetime('now'))",
            (event_id,),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        # 既に同じ event_id が記録済み = 重複配信
        con.rollback()
        return False


def handle_event(data: dict, con: sqlite3.Connection, theme_client=None):
    """Slack Events API ディスパッチャ。webapp.py の /slack/events から呼ばれる。"""
    event = data.get("event", {})
    etype = event.get("type", "")
    subtype = event.get("subtype", "")
    bot_id = event.get("bot_id", "")
    thread_ts = event.get("thread_ts", "")
    event_id = data.get("event_id")
    print(f"[SlackBot] event: type={etype!r} subtype={subtype!r} bot_id={bool(bot_id)} thread={thread_ts!r} event_id={event_id!r}", flush=True)

    # Slackの再送（同一event_idの二重配信）による重複DB書き込み・重複返信を防ぐ
    if event_id:
        try:
            is_new = _mark_event_processed(con, event_id)
        except Exception as _e:
            # 冪等化機構自体の異常時はフェイルオープン（処理は継続、記録のみ諦める）
            con.rollback()
            is_new = True
            print(f"[SlackBot] event dedup check failed (continuing): {_e}", flush=True)
        if not is_new:
            print(f"[SlackBot] duplicate event skipped: event_id={event_id}", flush=True)
            return

    try:
        if etype == "app_mention":
            # 「@Bot 事務 ...」=事務タスク(is_admin=1)、「@Bot task/タスク ...」=通常タスク。
            # それ以外は従来の商談フロー。
            _clean = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
            _ma = re.match(r"^(事務|desk|jimu)[:：\s]+(.+)", _clean, flags=re.IGNORECASE | re.DOTALL)
            _m = re.match(r"^(task|タスク)[:：\s]+(.+)", _clean, flags=re.IGNORECASE | re.DOTALL)
            if _ma:
                from cowork import slack_tasks as _st
                _st.handle_admin_mention_task(
                    con, event.get("channel", ""),
                    event.get("thread_ts") or event.get("ts"),
                    _ma.group(2).strip(), event.get("user", ""))
            elif _m:
                from cowork import slack_tasks as _st
                _st.handle_mention_task(
                    con, event.get("channel", ""),
                    event.get("thread_ts") or event.get("ts"),
                    _m.group(2).strip(), event.get("user", ""))
            else:
                handle_mention(event, con)
        elif etype in ("message", "message.groups", "message.channels"):
            handle_message(event, con, theme_client)
    except Exception as e:
        con.rollback()
        print(f"[SlackBot] unhandled error ({etype}): {e}")
        import traceback; traceback.print_exc()
