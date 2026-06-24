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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SFA_TOOL_URL = os.environ.get("SFA_TOOL_URL", "http://localhost:8787")

_bot_user_id: str | None = None


# ── Slack API ──────────────────────────────────────────────────────────────

def _slack_post(method: str, **kwargs) -> dict:
    url = f"https://slack.com/api/{method}"
    data = json.dumps(kwargs).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[SlackBot] {method} error: {e}")
        return {"ok": False, "error": str(e)}


def _slack_get(method: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
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


def verify_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        return True
    if abs(time.time() - float(timestamp)) > 300:
        return False
    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
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
                        state: str = "pending"):
    con.execute("""
        INSERT OR REPLACE INTO slack_threads
            (thread_ts, channel_id, deal_id, bot_message_ts, state)
        VALUES (?, ?, ?, ?, ?)
    """, (thread_ts, channel_id, deal_id, bot_message_ts, state))
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


# ── Claude helpers ─────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "{}"
    import urllib.request
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    return body["content"][0]["text"].strip()


def draft_template(thread_text: str, deal: dict | None) -> str:
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

    prompt = f"""以下はSlackスレッドの会話内容と、現在のSFA商談情報です。
スレッドの内容を分析し、SFA更新ドラフトをJSONで作成してください。

【現在の商談情報】
{deal_info}

【スレッド内容】
{thread_text}

以下のJSONのみ出力（説明不要）:
{{
  "activity_date": "YYYY-MM-DD（読み取れなければ【記載なし】）",
  "activity_type": "面談/電話/メール/メモ（読み取れなければ【記載なし】）",
  "contact_name": "相手の名前（読み取れなければ【記載なし】）",
  "activity_content": "活動内容の要約（スレッドから作成）",
  "stage_update": "変更後ステージ名（変更不要なら null）",
  "next_milestone_date": "YYYY-MM-DD（変更不要なら null、不明なら【記載なし】）",
  "next_milestone_label": "次回MSラベル（変更不要なら null、不明なら【記載なし】）",
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
    cur_memo = deal.get("note", "") or "（なし）" if deal else "（なし）"

    stage_upd = parsed.get("stage_update") or "-"
    ms_date = parsed.get("next_milestone_date") or "-"
    ms_label = parsed.get("next_milestone_label") or "-"
    memo_add = parsed.get("memo_addition") or "-"

    lines = [
        "【SFA更新テンプレート】",
        f"商談: {deal_name}",
        "─── 現在の商談情報 ───",
        f"ステージ: {cur_stage}",
        f"次回MS: {cur_ms_date} / {cur_ms_label}",
        f"現状メモ: {cur_memo}",
        "",
        "─── 今回の活動 ───",
        f"活動日: {v(parsed.get('activity_date'))}",
        f"種別: {v(parsed.get('activity_type'))}",
        f"相手: {v(parsed.get('contact_name'))}",
        f"内容: {v(parsed.get('activity_content'))}",
        "",
        "─── 商談更新（変更なしは「-」のまま） ───",
        f"ステージ: {stage_upd}",
        f"次回MS日: {ms_date}",
        f"次回MSラベル: {ms_label}",
        f"追記メモ: {memo_add}",
        "",
        "✅ 確認・編集後「確定」または「ok」と返信してください",
        "（修正は「活動日: 2026-06-23」のように返信で上書きできます）",
    ]
    if not deal:
        lines.insert(2, "⚠️ 商談を自動特定できませんでした。商談名を明示して再メンションしてください。")

    return "\n".join(lines)


# ── Template parser ────────────────────────────────────────────────────────

def _extract_field(text: str, label: str) -> str | None:
    """テンプレートまたは返信テキストからフィールド値を抽出。"""
    m = re.search(rf"^{re.escape(label)}: *(.+)$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if val in ("-", "【記載なし】", "変更なし", "（なし）"):
        return None
    return val


def collect_fields(messages: list[dict], bot_ts: str, confirm_ts: str) -> dict:
    """
    bot_ts のテンプレートを基準に、その後の人間の返信で上書きした最終値を返す。
    confirm_ts より前のメッセージのみ対象。
    """
    bot_uid = get_bot_user_id()
    base: dict = {}
    overrides: dict = {}

    for m in messages:
        ts = m.get("ts", "")
        is_bot = m.get("bot_id") or m.get("user") == bot_uid
        text = m.get("text", "")

        if ts == bot_ts and is_bot:
            # ベーステンプレート
            for label in ("活動日", "種別", "相手", "内容", "ステージ", "次回MS日", "次回MSラベル", "追記メモ"):
                val = _extract_field(text, label)
                if val:
                    base[label] = val

        elif not is_bot and ts != confirm_ts and ts > bot_ts:
            # 人間による上書き返信
            for label in ("活動日", "種別", "相手", "内容", "ステージ", "次回MS日", "次回MSラベル", "追記メモ"):
                val = _extract_field(text, label)
                if val:
                    overrides[label] = val

    return {**base, **overrides}


# ── DB update ──────────────────────────────────────────────────────────────

def apply_to_db(con: sqlite3.Connection, fields: dict, deal_id: int | None):
    import datetime

    # 活動履歴
    content = fields.get("内容")
    if deal_id and content:
        date_str = fields.get("活動日") or datetime.date.today().isoformat()
        con.execute("""
            INSERT INTO activities (deal_id, type, occurred_on, contact_name, body)
            VALUES (?, ?, ?, ?, ?)
        """, (
            deal_id,
            fields.get("種別") or "メモ",
            date_str,
            fields.get("相手") or "",
            content,
        ))

    # 商談更新
    if deal_id:
        updates: dict = {}
        if fields.get("ステージ"):
            updates["stage"] = fields["ステージ"]
        if fields.get("次回MS日"):
            updates["next_milestone_date"] = fields["次回MS日"]
        if fields.get("次回MSラベル"):
            updates["next_milestone_label"] = fields["次回MSラベル"]
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


# ── Event handlers ─────────────────────────────────────────────────────────

def handle_mention(event: dict, con: sqlite3.Connection):
    channel = event.get("channel", "")
    event_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or event_ts

    import socket as _socket
    _socket.setdefaulttimeout(15)  # urllib のハング対策
    print(f"[SlackBot] mention: channel={channel} thread={thread_ts}", flush=True)

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
            post_message(channel, thread_ts,
                "✅ このスレッドはDB反映済みです。新しい活動は別スレッドでメンションしてください。")
            return
        elif state == "cancelled":
            # キャンセル済みは再処理を許可
            con.execute("DELETE FROM slack_threads WHERE thread_ts=?", (thread_ts,))
            con.commit()
        else:
            return

    # スレッド全文取得（botメッセージと@メンション除去）
    print("[SlackBot] getting bot_uid...", flush=True)
    bot_uid = get_bot_user_id()
    print(f"[SlackBot] bot_uid={bot_uid}", flush=True)
    messages = get_thread_messages(channel, thread_ts)
    print(f"[SlackBot] thread messages={len(messages)}", flush=True)
    parts = []
    for m in messages:
        if m.get("bot_id") or m.get("user") == bot_uid:
            continue
        text = re.sub(r"<@[A-Z0-9]+>", "", m.get("text", "")).strip()
        if text:
            parts.append(text)
    thread_text = "\n".join(parts)

    # 商談マッチ
    deal = find_deal(con, thread_text)

    if not deal:
        post_message(channel, thread_ts,
            "⚠️ 商談を特定できませんでした。\n"
            "スレッド内に会社名か商談名を含めて、再度 @NegoCollection をメンションしてください。")
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
        f"「はい」で続行 / 「いいえ」の場合は正しいSFA番号（数字のみ）を返信してください\n"
        f"商談一覧: {SFA_TOOL_URL}/deals"
    )
    bot_ts = post_message(channel, thread_ts, confirm_text)

    # state='identifying' で保存（商談確認待ち）
    save_pending_thread(con, thread_ts, channel, deal["id"], bot_ts,
                        state="identifying")


def handle_message(event: dict, con: sqlite3.Connection):
    # botメッセージ・編集イベントはスキップ
    if event.get("bot_id") or event.get("subtype"):
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event.get("channel", "")
    text = event.get("text", "").strip()
    text_l = text.lower()
    confirm_ts = event.get("ts", "")

    pending = get_pending_thread(con, thread_ts)
    if not pending:
        return

    state = pending.get("state", "")
    deal_id = pending.get("deal_id")
    bot_ts = pending.get("bot_message_ts", "")

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

            template = draft_template(thread_text, deal)
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
            # キャンセルせずにSFA番号指定を促す
            post_message(channel, thread_ts,
                f"🔄 商談一覧からSFA番号を確認して、数字のみ（例: `8`）で返信してください。\n"
                f"商談一覧: {SFA_TOOL_URL}/deals")

        elif text.strip().isdigit():
            # SFA番号で商談を直接指定
            specified_id = int(text.strip())
            row = con.execute("""
                SELECT d.*, a.name as account_name FROM deals d
                LEFT JOIN accounts a ON d.account_id = a.id
                WHERE d.id = ? AND d.status = 'open'
            """, (specified_id,)).fetchone()

            if not row:
                post_message(channel, thread_ts,
                    f"❌ SFA#{specified_id} が見つかりません（open商談のみ指定可）。\n"
                    f"商談一覧: {SFA_TOOL_URL}/deals")
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
                    f"「はい」で続行 / 「いいえ」の場合は正しいSFA番号（数字のみ）を返信してください\n"
                    f"商談一覧: {SFA_TOOL_URL}/deals"
                )
                new_bot_ts = post_message(channel, thread_ts, confirm_text)
                con.execute(
                    "UPDATE slack_threads SET deal_id=?, bot_message_ts=? WHERE thread_ts=?",
                    (specified_id, new_bot_ts, thread_ts)
                )
                con.commit()
                print(f"[SlackBot] deal switched to #{specified_id}: thread={thread_ts}")

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

    if text_l not in ("確定", "ok"):
        return

    # テンプレート + 上書き値を収集
    messages = get_thread_messages(channel, thread_ts)
    fields = collect_fields(messages, bot_ts, confirm_ts)

    if not fields.get("内容"):
        post_message(channel, thread_ts,
            "❌ 活動内容（内容:）が読み取れませんでした。テンプレートの「内容:」を記入して「確定」と再送してください。")
        return

    try:
        apply_to_db(con, fields, deal_id)
        mark_completed(con, thread_ts)

        updated_parts = []
        if fields.get("ステージ"): updated_parts.append(f"ステージ→{fields['ステージ']}")
        if fields.get("次回MS日"): updated_parts.append(f"次回MS→{fields['次回MS日']}")
        summary = "、".join(updated_parts) if updated_parts else "商談フィールド変更なし"
        post_message(channel, thread_ts,
            f"✅ SFA DB を更新しました。\n活動履歴: 追加完了 / {summary}")
        print(f"[SlackBot] DB updated: thread={thread_ts} deal_id={deal_id}")
    except Exception as e:
        post_message(channel, thread_ts, f"❌ DB更新エラー: {e}")
        print(f"[SlackBot] DB update error: {e}")


def handle_event(data: dict, con: sqlite3.Connection):
    """Slack Events API ディスパッチャ。webapp.py の /slack/events から呼ばれる。"""
    event = data.get("event", {})
    etype = event.get("type", "")
    try:
        if etype == "app_mention":
            handle_mention(event, con)
        elif etype in ("message", "message.groups", "message.channels"):
            handle_message(event, con)
    except Exception as e:
        print(f"[SlackBot] unhandled error ({etype}): {e}")
        import traceback; traceback.print_exc()
