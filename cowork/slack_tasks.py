"""タスク管理 × Slackインタラクティブ（#30）。

Slack上で「起票→整理→進捗→消込」を完結させるハンドラ群。
- 起票: /task スラッシュ・🎯リアクション・@メンション（AI補助で前埋め）
- 消込: 朝ダイジェストのボタン（✓完了/▶開始/💬進捗/⏰後で）＋モーダル
すべて `tasks` に集約（source='slack', slack_channel/slack_ts に起源を記録）。

Web本体(webapp.py)から呼ばれる。エンドポイント側で署名検証済みのペイロードを受け取る前提。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date

from . import sfa_db
from .slack_bot import _slack_post, _slack_get, _call_claude  # 既存基盤を流用

# Slack起票の重複チェック(SELECT)→INSERT を直列化するロック。Slackが同一依頼を複数イベント
# （別event_id）で near-simultaneous に配信しても、重複判定が競合せず1件に収れんさせる。
# 本体は単一プロセスのThreadingHTTPServer（スレッドのみ）なのでプロセス内ロックで十分。
_CREATE_LOCK = threading.Lock()

# 🎯 = "dart"。通常タスク化トリガー（is_admin=0）。誤爆防止のため🎯のみに限定。
TASK_REACTIONS = {"dart"}
# 📋 = "clipboard"。事務タスク化トリガー（is_admin=1）。依頼者=メッセージ投稿者。
ADMIN_TASK_REACTIONS = {"clipboard"}
SFA_TOOL_URL = os.environ.get("SFA_TOOL_URL", "") or "https://sfa-crm.onrender.com"
# 事務員の既定担当（先頭の事務員＝原則この人が受ける。sfa_db側でカンマ区切りを解釈済み）。
DESK_ASSIGNEE = sfa_db.DESK_ASSIGNEE_DEFAULT
# 事務タスク完了通知のフォールバック投稿先＝#オペレータ全体チャネル（起票元スレッドが無い場合）。
SLACK_OPS_CHANNEL_ID = os.environ.get("SLACK_OPS_CHANNEL_ID", "")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 担当解決（Slackユーザー → SFA担当名） ─────────────────────────────────

def _owner_slack_map() -> dict:
    """config/owner_slack_map.json（担当名→email）。_始まりのキーは除外。"""
    p = os.path.join(_PROJECT_ROOT, "config", "owner_slack_map.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if not k.startswith("_")}
    except Exception:
        return {}


def owner_from_slack_user(user_id: str, token: str | None = None) -> str | None:
    """SlackユーザーID → SFA担当名。users.infoのemailを owner_slack_map で逆引き。
    token指定で別Bot（事務Bot）のトークンでusers.infoを叩く。"""
    if not user_id:
        return None
    r = _slack_get("users.info", {"user": user_id}, token=token)
    email = (((r.get("user") or {}).get("profile") or {}).get("email") or "").lower()
    if not email:
        return None
    for name, mail in _owner_slack_map().items():
        if (mail or "").lower() == email:
            return name
    return None


def _slack_user_id_for(name: str, token: str | None = None) -> str | None:
    """SFA担当名 → SlackユーザーID。owner_slack_map の email を users.lookupByEmail で解決。
    マップ未登録・Slack未登録なら None（＝メンション対象外）。"""
    name = (name or "").strip()
    if not name:
        return None
    email = (_owner_slack_map().get(name) or "").strip()
    if not email:
        return None
    r = _slack_get("users.lookupByEmail", {"email": email}, token=token)
    return (r.get("user") or {}).get("id") if r.get("ok") else None


def notify_task_done(con, task_id: int, token: str | None = None) -> bool:
    """事務タスク(is_admin=1)が完了したとき、Ope Botから完了通知を投稿する。
    - 依頼者・担当者のうち Slack登録が見つかる人を @メンション。
    - 宛先: 起票元Slackスレッド(slack_channel+slack_ts)があればそのスレッド、
      無ければ #オペレータ全体チャネル(SLACK_OPS_CHANNEL_ID)。
    - 見つからない相手はメンションせず「直接ご連絡を」と注記。双方不明でもメンションなしで投稿。
    token省略時は事務Bot(SLACK_DESK_TOKEN)として投稿。投稿したら True。"""
    from .slack_bot import SLACK_DESK_TOKEN
    tok = token or SLACK_DESK_TOKEN
    if not tok:
        print("[slack_tasks] notify_task_done: 事務Botトークン未設定でスキップ", flush=True)
        return False
    t = sfa_db.get_task(con, task_id)
    if not t or not t.get("is_admin"):
        return False
    title = (t.get("title") or "(無題)").strip()
    link = f"{SFA_TOOL_URL}/desk-tasks#tc-{task_id}"
    ch = (t.get("slack_channel") or "").strip()
    ts = (t.get("slack_ts") or "").strip()
    if ch and ts:
        channel, thread_ts = ch, ts           # 起票元スレッドに返信
    else:
        channel, thread_ts = SLACK_OPS_CHANNEL_ID, None   # フォールバック=オペレータ全体
    if not channel:
        print("[slack_tasks] notify_task_done: 投稿先が無い（SLACK_OPS_CHANNEL_ID未設定＆スレッド無し）", flush=True)
        return False
    # 依頼者・担当者のメンション解決（Slack登録者のみ）。見つからない相手は注記。
    resolved, missing = [], []
    for role, nm in (("依頼者", t.get("requester")), ("担当者", t.get("assignee"))):
        nm = (nm or "").strip()
        if not nm:
            continue
        uid = _slack_user_id_for(nm, token=tok)
        (resolved if uid else missing).append((role, nm, uid))
    mention = " ".join(f"<@{uid}>" for (_r, _n, uid) in resolved)
    lines = []
    if mention:
        lines.append(mention)
    lines.append(f"✅ 事務タスクが完了しました：*{title}*")
    lines.append("ご確認のうえ、続きの対応や不明点は担当者までお願いします。")
    lines.append(f"<{link}|▶ 対象カードを開く>")
    for (role, nm, _u) in missing:
        lines.append(f"※ {role}「{nm}」がSlackで見つからないため、必要に応じて直接ご連絡ください。")
    payload = {"channel": channel, "text": "\n".join(lines)}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = _slack_post("chat.postMessage", token=tok, **payload)
    if not r.get("ok"):
        print(f"[slack_tasks] notify_task_done post failed: {r.get('error')}", flush=True)
    return bool(r.get("ok"))


# ── AI補助（自由文 → タスク項目を抽出） ───────────────────────────────────

def ai_extract_task(text: str, today: str | None = None, categories: list | None = None) -> dict:
    """自由文からタスク項目を抽出（title/next_action/due_date/category）。失敗時は素の値。
    categories を渡すとその分類群から選ばせる（事務タスクは ADMIN_TASK_CATEGORIES を渡す）。"""
    text = (text or "").strip()
    today = today or date.today().isoformat()
    categories = categories or sfa_db.TASK_CATEGORIES
    fallback = {"title": text[:80] or "(無題)", "next_action": "", "due_date": "", "category": ""}
    if not text:
        return fallback
    cats = "／".join(categories)
    prompt = (
        "次の文から社内タスクを1件抽出し、JSONだけを出力してください（説明不要）。\n"
        f"今日は {today}。相対表現（例:金曜まで/来週/明日）は今日基準でYYYY-MM-DDに変換。不明な項目は空文字。\n"
        f"カテゴリは次から最も近い1つ: {cats}\n"
        '出力: {"title":"簡潔なタスク名","next_action":"次にやる具体的な一手","due_date":"YYYY-MM-DD or 空","category":"カテゴリ名 or 空"}\n\n'
        f"文:\n{text}")
    try:
        raw = _call_claude(prompt) or ""
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return fallback
    out = dict(fallback)
    for k in ("title", "next_action", "due_date", "category"):
        v = (data.get(k) or "").strip()
        if v:
            out[k] = v
    if out["category"] not in categories:
        out["category"] = ""  # 不一致は空に
    if not out["title"]:
        out["title"] = text[:80]
    return out


# ── モーダル（views）ビルダー ─────────────────────────────────────────────

def _select(action_id: str, label: str, values: list, initial=None, optional=True) -> dict:
    opts = [{"text": {"type": "plain_text", "text": str(v)[:75]}, "value": str(v)[:75]}
            for v in values if str(v).strip()]
    element = {"type": "static_select", "action_id": action_id,
               "placeholder": {"type": "plain_text", "text": "選択"}}
    if opts:
        element["options"] = opts
    if initial and any(o["value"] == str(initial) for o in opts):
        element["initial_option"] = {"text": {"type": "plain_text", "text": str(initial)[:75]},
                                     "value": str(initial)[:75]}
    b = {"type": "input", "block_id": action_id, "optional": optional,
         "label": {"type": "plain_text", "text": label}, "element": element}
    return b


def _text_input(action_id: str, label: str, initial: str = "", multiline: bool = False,
                optional: bool = True) -> dict:
    el = {"type": "plain_text_input", "action_id": action_id, "multiline": multiline}
    if initial:
        el["initial_value"] = initial[:2900]
    return {"type": "input", "block_id": action_id, "optional": optional,
            "label": {"type": "plain_text", "text": label}, "element": el}


def build_create_modal(con, prefill: dict, private_meta: dict) -> dict:
    """タスク起票モーダル。prefill=AI抽出結果、private_meta=起源情報(channel/ts等)。"""
    owners = sfa_db.get_master_list(con, "owners")
    projects = [p["name"] for p in sfa_db.list_task_projects(con)]
    cats = sfa_db.get_master_list(con, "task_categories")
    blocks = [
        _text_input("title", "タイトル *", prefill.get("title", ""), optional=False),
        _text_input("next_action", "次アクション", prefill.get("next_action", "")),
        _select("assignee", "担当", owners, prefill.get("assignee")),
        {"type": "input", "block_id": "due_date", "optional": True,
         "label": {"type": "plain_text", "text": "期限"},
         "element": ({"type": "datepicker", "action_id": "due_date",
                      "initial_date": prefill["due_date"]} if prefill.get("due_date")
                     else {"type": "datepicker", "action_id": "due_date"})},
        _select("project", "プロジェクト", projects, prefill.get("project")),
        _select("category", "種類（空ならAI判定）", cats, prefill.get("category")),
    ]
    return {
        "type": "modal", "callback_id": "task_create",
        "title": {"type": "plain_text", "text": "タスクを起票"},
        "submit": {"type": "plain_text", "text": "起票"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "private_metadata": json.dumps(private_meta, ensure_ascii=False),
        "blocks": blocks,
    }


def build_progress_modal(task_id: int, kind: str, title: str = "") -> dict:
    label = "議論メモ" if kind == "discussion" else "進捗メモ"
    return {
        "type": "modal", "callback_id": f"task_note:{task_id}:{kind}",
        "title": {"type": "plain_text", "text": label},
        "submit": {"type": "plain_text", "text": "追記"},
        "close": {"type": "plain_text", "text": "閉じる"},
        "blocks": [
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"*{title[:80]}* への{label}"}]},
            _text_input("body", label, multiline=True, optional=False),
        ],
    }


# ── モーダル送信値の取り出し ───────────────────────────────────────────────

def _view_val(state: dict, block_id: str):
    v = (state.get("values", {}).get(block_id, {}) or {}).get(block_id, {}) or {}
    if "value" in v:
        return (v.get("value") or "").strip() or None
    if "selected_date" in v:
        return v.get("selected_date") or None
    if "selected_option" in v and v["selected_option"]:
        return v["selected_option"].get("value") or None
    return None


# ── 起票の実処理 ───────────────────────────────────────────────────────────

def create_task_from_fields(con, *, title, next_action=None, assignee=None, due_date=None,
                            project=None, category=None, slack_channel=None, slack_ts=None,
                            slack_permalink=None, created_by=None, ai_category=True,
                            is_admin=0, requester=None, return_created=False):
    """モーダル/AI抽出の値からタスク作成。種類が空かつai_category=Trueならその場でAI判定
    （モーダル送信＝3秒制約のある文脈では ai_category=False にして背景で後追い判定）。
    is_admin=1 で事務タスク（requester=依頼者）。事務タスクは分類体系が異なるためAI後追い判定はしない。
    事務タスクは期限未指定なら既定で3営業日後にし、受信箱に留める（受付=受信箱の運用のため自動整理しない）。
    同一Slackメッセージ(slack_channel+slack_ts+is_admin)から既にタスクがあれば新規作成せず既存idを返す
    （Slackの再送・別event_id二重配信・再操作による重複起票を防ぐ）。同一メッセージの
    重複判定(SELECT)→INSERT は _CREATE_LOCK で直列化する（AI呼び出しはロック外）。"""
    # AIカテゴリ判定はロック外で先に済ませる（遅い呼び出しでの直列化を避ける）。事務は判定しない。
    if not category and ai_category and not is_admin:
        try:
            from .webapp import _ai_guess_task_category  # 遅延import（循環回避）
            category = _ai_guess_task_category(title or "", next_action or "")
        except Exception:
            category = None
    if is_admin and not (due_date or "").strip():
        due_date = _admin_default_due()   # 事務タスクの既定期限＝3営業日後
    # 重複チェック→INSERTを直列化。Slackが1依頼を複数イベント(別event_id)で
    # near-simultaneous に配信しても、ここで1件に収れんさせる（実事故: 1依頼が3枚起票）。
    with _CREATE_LOCK:
        if slack_channel and slack_ts:
            _dup = con.execute(
                "SELECT id FROM tasks WHERE slack_channel=? AND slack_ts=? AND COALESCE(is_admin,0)=? LIMIT 1",
                (slack_channel, slack_ts, 1 if is_admin else 0)).fetchone()
            if _dup:
                # 同一メッセージからの重複起票を防止（既存タスクを返す）。created=Falseで多重返信も抑止。
                return (_dup[0], False) if return_created else _dup[0]
        tid = sfa_db.upsert_task(
            con, title=title or "(無題)", next_action=next_action or None,
            assignee=assignee or None, due_date=due_date or None,
            project=project or None, category=category or None,
            status="受信箱", source="slack", is_admin=1 if is_admin else 0,
            requester=requester or None,
            slack_channel=slack_channel, slack_ts=slack_ts, slack_permalink=slack_permalink,
            created_by=created_by)
        # 担当＋期限が揃えば受信箱→未着手へ自動整理（通常/事務とも）。事務タスクはSlack起票時に
        # 既定担当あみ＋期限3営業日後が入るため、実質すぐ未着手に上がる。担当未割当（担当空欄）の
        # 受付だけが受信箱に留まる。
        if (assignee or "").strip() and (due_date or "").strip():
            sfa_db.set_task_status(con, tid, "未着手")
    return (tid, True) if return_created else tid


# ── スラッシュコマンド /task ───────────────────────────────────────────────

def handle_slash(con, form: dict) -> None:
    """/task <自由文>。AI抽出で前埋めした起票モーダルを開く。"""
    trigger_id = form.get("trigger_id", "")
    text = (form.get("text") or "").strip()
    user_id = form.get("user_id", "")
    prefill = ai_extract_task(text) if text else {"title": "", "next_action": "", "due_date": "", "category": ""}
    owner = owner_from_slack_user(user_id)
    if owner:
        prefill["assignee"] = owner
    meta = {"channel": form.get("channel_id", ""), "user": user_id, "response_url": form.get("response_url", "")}
    view = build_create_modal(con, prefill, meta)
    r = _slack_post("views.open", trigger_id=trigger_id, view=view)
    if not r.get("ok"):
        print(f"[slack_tasks] views.open(slash) failed: {r.get('error')}", flush=True)


# ── リアクション🎯 ────────────────────────────────────────────────────────

def _admin_default_due() -> str:
    """事務タスクの既定期限＝今日(JST)から3営業日後（YYYY-MM-DD）。"""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=9))).date()
    return sfa_db.add_business_days(today, 3).isoformat()


def _admin_due_context(con, tid: int) -> dict:
    """事務タスク起票コメントに添える期限の注記ブロック。既定＝起票から3営業日後である旨と、
    締め切りが決まっている場合はカードで直接編集する案内を出す。"""
    t = sfa_db.get_task(con, tid)
    due = ((t or {}).get("due_date") or "").strip()
    due_txt = f"*{due}*" if due else "未設定"
    return {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"📅 期限は {due_txt}（既定＝起票から3営業日後）。"
                    "締め切り日が決まっている場合は、カードで直接編集してください。"}]}


def _message_permalink(channel: str, ts: str, token: str | None = None) -> str | None:
    """起票元Slackメッセージへのpermalink URL（chat.getPermalink）。取得失敗時はNone。"""
    if not channel or not ts:
        return None
    r = _slack_get("chat.getPermalink", {"channel": channel, "message_ts": ts}, token=token)
    return r.get("permalink") if r.get("ok") else None


def _fetch_message(channel: str, ts: str, token: str | None = None) -> dict:
    """channel の ts に厳密一致するメッセージ辞書だけを返す（無ければ{}）。
    トップレベルは conversations.history(oldest=latest=ts)、スレッド返信は conversations.replies で拾う。
    token指定で別Bot（事務Bot）のトークンで取得。"""
    r = _slack_get("conversations.history", {"channel": channel, "latest": ts, "oldest": ts,
                                             "inclusive": "true", "limit": 1}, token=token)
    for m in (r.get("messages") or []):
        if m.get("ts") == ts:
            return m
    r2 = _slack_get("conversations.replies", {"channel": channel, "ts": ts, "limit": 50}, token=token)
    for m in (r2.get("messages") or []):
        if m.get("ts") == ts:
            return m
    return {}


def _fetch_message_text(channel: str, ts: str) -> str:
    """後方互換: 本文だけ返すラッパー。"""
    return (_fetch_message(channel, ts) or {}).get("text") or ""


def handle_reaction(con, event: dict, token: str | None = None) -> None:
    """リアクションでメッセージをタスク化。🎯(dart)=通常タスク、📋(clipboard)=事務タスク に振り分け。
    token指定で別Bot（事務Bot）のトークンで本文取得・ユーザー解決・返信を行う。"""
    reaction = event.get("reaction")
    is_admin = reaction in ADMIN_TASK_REACTIONS
    if reaction not in TASK_REACTIONS and not is_admin:
        return
    item = event.get("item", {}) or {}
    channel = item.get("channel", "")
    ts = item.get("ts", "")
    user_id = event.get("user", "")   # リアクションを付けた人
    if not channel or not ts:
        return
    # 元メッセージを「その ts のメッセージだけ」正確に取得（別メッセージ誤取得を防止）
    msg = _fetch_message(channel, ts, token=token)
    text = (msg or {}).get("text") or ""
    if not text:
        # 本文が取れない場合（権限不足・特殊メッセージ）は誤起票を避けて通知だけ
        _slack_post("chat.postEphemeral", token=token, channel=channel, user=user_id,
                    text="⚠ このメッセージ本文を取得できませんでした（Botの参加/権限をご確認ください）。タスクは作成していません。")
        return
    permalink = _message_permalink(channel, ts, token=token)  # 起票元メッセージのURL
    if is_admin:
        # 事務タスク: 依頼者=メッセージ投稿者、担当=事務員。カテゴリは事務用分類から。
        author_id = (msg or {}).get("user") or ""
        requester = owner_from_slack_user(author_id, token=token) if author_id else None
        prefill = ai_extract_task(text, categories=sfa_db.ADMIN_TASK_CATEGORIES)
        tid = create_task_from_fields(
            con, title=prefill["title"], next_action=prefill["next_action"] or None,
            assignee=(DESK_ASSIGNEE or None), due_date=prefill["due_date"] or None,
            category=prefill["category"] or None, slack_channel=channel, slack_ts=ts,
            slack_permalink=permalink,
            created_by=owner_from_slack_user(user_id, token=token) or user_id,
            is_admin=1, requester=requester)
        link = f"{SFA_TOOL_URL}/desk-tasks#tc-{tid}"
        _req = f"（依頼者: {requester}）" if requester else ""
        _slack_post("chat.postEphemeral", token=token, channel=channel, user=user_id,
                    text=f"📋 事務タスク化しました: {prefill['title']}",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn",
                         "text": f"📋 事務タスク化しました{_req}\n*<{link}|{prefill['title']}>*"
                                 + (f"\n▶ {prefill['next_action']}" if prefill['next_action'] else "")}},
                        _admin_due_context(con, tid),
                        _task_action_block(tid),
                    ])
        return
    prefill = ai_extract_task(text)
    owner = owner_from_slack_user(user_id, token=token)
    tid = create_task_from_fields(
        con, title=prefill["title"], next_action=prefill["next_action"] or None,
        assignee=owner, due_date=prefill["due_date"] or None, category=prefill["category"] or None,
        slack_channel=channel, slack_ts=ts, slack_permalink=permalink, created_by=owner or user_id)
    link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
    _slack_post("chat.postEphemeral", token=token, channel=channel, user=user_id,
                text=f"🎯 タスク化しました: {prefill['title']}",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"🎯 タスク化しました\n*<{link}|{prefill['title']}>*"
                             + (f"\n▶ {prefill['next_action']}" if prefill['next_action'] else "")}},
                    _task_action_block(tid),
                ])


# ── @メンションでタスク化（webapp/slack_botから条件付きで呼ぶ） ──────────────

def handle_mention_task(con, channel: str, thread_ts: str, text: str, user_id: str) -> int:
    prefill = ai_extract_task(text)
    owner = owner_from_slack_user(user_id)
    tid = create_task_from_fields(
        con, title=prefill["title"], next_action=prefill["next_action"] or None,
        assignee=owner, due_date=prefill["due_date"] or None, category=prefill["category"] or None,
        slack_channel=channel, slack_ts=thread_ts, created_by=owner or user_id)
    link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
    _slack_post("chat.postMessage", channel=channel, thread_ts=thread_ts,
                text=f"タスク化しました: {prefill['title']}",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                        "text": f"✅ タスク化しました *<{link}|{prefill['title']}>*"}},
                        _task_action_block(tid)])
    return tid


def handle_admin_reaction(con, event: dict, token: str | None = None) -> None:
    """事務Bot専用エンドポイント用: 📋(clipboard)のみを事務タスク化。🎯等は無視（別Botの担当）。"""
    if event.get("reaction") not in ADMIN_TASK_REACTIONS:
        return
    handle_reaction(con, event, token=token)


def handle_admin_mention_task(con, channel: str, thread_ts: str, text: str, user_id: str,
                              token: str | None = None) -> int:
    """@事務Botメンションを事務タスク化。依頼者=メンションした人、担当=事務員、is_admin=1。
    token指定で事務Botとして返信・ユーザー解決を行う。"""
    prefill = ai_extract_task(text, categories=sfa_db.ADMIN_TASK_CATEGORIES)
    requester = owner_from_slack_user(user_id, token=token)  # 依頼＝メンションした本人
    permalink = _message_permalink(channel, thread_ts, token=token)
    tid, created = create_task_from_fields(
        con, title=prefill["title"], next_action=prefill["next_action"] or None,
        assignee=(DESK_ASSIGNEE or None), due_date=prefill["due_date"] or None,
        category=prefill["category"] or None, slack_channel=channel, slack_ts=thread_ts,
        slack_permalink=permalink, created_by=requester or user_id, is_admin=1, requester=requester,
        return_created=True)
    if not created:
        # 同一メッセージからの多重配信（別event_id）＝既に起票済み。返信もしない（3重返信の抑止）。
        return tid
    link = f"{SFA_TOOL_URL}/desk-tasks#tc-{tid}"
    _req = f"（依頼者: {requester}）" if requester else ""
    _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                text=f"事務タスク化しました: {prefill['title']}",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                        "text": f"📋 事務タスク化しました{_req} *<{link}|{prefill['title']}>*"}},
                        _admin_due_context(con, tid),
                        _task_action_block(tid)])
    return tid


# ── ボタン付きブロック（消込UI） ───────────────────────────────────────────

def _task_action_block(task_id: int) -> dict:
    return {"type": "actions", "block_id": f"tab_{task_id}", "elements": [
        {"type": "button", "action_id": f"task_done:{task_id}", "value": str(task_id),
         "style": "primary", "text": {"type": "plain_text", "text": "✓完了"}},
        {"type": "button", "action_id": f"task_start:{task_id}", "value": str(task_id),
         "text": {"type": "plain_text", "text": "▶開始"}},
        {"type": "button", "action_id": f"task_progress:{task_id}", "value": str(task_id),
         "text": {"type": "plain_text", "text": "💬進捗"}},
        {"type": "button", "action_id": f"task_snooze:{task_id}", "value": str(task_id),
         "text": {"type": "plain_text", "text": "⏰+3営業日"}},
    ]}


# ── インタラクティブ（ボタン・モーダル送信） ───────────────────────────────

def handle_interactive(con, payload: dict) -> dict | None:
    """block_actions（ボタン）と view_submission（モーダル送信）を処理。
    view_submission時はSlackへ返すレスポンス(dict)を返す（例: 空=モーダルを閉じる）。"""
    ptype = payload.get("type")
    if ptype == "view_submission":
        return _handle_view_submission(con, payload)
    if ptype == "block_actions":
        _handle_block_action(con, payload)
        return None
    return None


def _handle_view_submission(con, payload: dict) -> dict | None:
    view = payload.get("view", {}) or {}
    cb = view.get("callback_id", "")
    state = view.get("state", {}) or {}
    user_id = (payload.get("user") or {}).get("id", "")
    if cb == "task_create":
        meta = {}
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
        except Exception:
            meta = {}
        owner = owner_from_slack_user(user_id)
        _cat = _view_val(state, "category")
        tid = create_task_from_fields(
            con,
            title=_view_val(state, "title") or "(無題)",
            next_action=_view_val(state, "next_action"),
            assignee=_view_val(state, "assignee") or owner,
            due_date=_view_val(state, "due_date"),
            project=_view_val(state, "project"),
            category=_cat,
            slack_channel=meta.get("channel"), slack_ts=meta.get("ts"),
            created_by=owner or user_id, ai_category=False)  # AIは3秒制約回避のため背景で
        # 起票通知（本人へDM）
        link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
        _slack_post("chat.postMessage", channel=user_id,
                    text=f"タスクを起票しました: {_view_val(state,'title')}",
                    blocks=[{"type": "section", "text": {"type": "mrkdwn",
                            "text": f"✅ 起票しました *<{link}|{_view_val(state,'title')}>*"}},
                            _task_action_block(tid)])
        resp = {"response_action": "clear"}
        if not _cat:
            resp["_defer_category"] = tid   # 種類が空→背景でAI判定
        return resp
    if cb.startswith("task_note:"):
        _, sid, kind = cb.split(":", 2)
        body = _view_val(state, "body")
        resp = {"response_action": "clear"}
        if body:
            sfa_db.add_task_note(con, int(sid), body, kind=kind)
            if kind == "discussion":
                resp["_defer_summary"] = int(sid)   # 議論メモ→背景でサマリ再生成
            else:
                tk = sfa_db.get_task(con, int(sid))  # 進捗追記＝着手→対応中
                if tk and tk.get("status") in ("受信箱", "未着手", "保留"):
                    sfa_db.set_task_status(con, int(sid), "対応中")
        return resp
    return {"response_action": "clear"}


def regenerate_task_summary(con, task_id: int) -> None:
    """議論メモからタスクサマリを再生成（背景スレッドから呼ぶ想定）。"""
    try:
        from .webapp import _ai_summarize_task
        memos = [n["body"] for n in sfa_db.list_task_notes(con, int(task_id), kind="discussion")]
        tk = sfa_db.get_task(con, int(task_id))
        s = _ai_summarize_task(tk.get("title", "") if tk else "", memos)
        if s:
            sfa_db.set_task_summary(con, int(task_id), s)
    except Exception as e:  # noqa: BLE001
        print(f"[slack_tasks] regen summary failed: {e}", flush=True)


def regenerate_task_category(con, task_id: int) -> None:
    """タイトル/詳細からタスク種類をAI判定（背景スレッドから呼ぶ想定）。"""
    try:
        from .webapp import _ai_guess_task_category
        tk = sfa_db.get_task(con, int(task_id))
        if tk and not (tk.get("category") or "").strip():
            cat = _ai_guess_task_category(tk.get("title", ""), tk.get("next_action") or "")
            if cat:
                con.execute("UPDATE tasks SET category=?, updated_at=datetime('now') WHERE id=?",
                            (cat, int(task_id)))
                con.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[slack_tasks] regen category failed: {e}", flush=True)


def _handle_block_action(con, payload: dict) -> None:
    actions = payload.get("actions", []) or []
    if not actions:
        return
    act = actions[0]
    action_id = act.get("action_id", "")
    trigger_id = payload.get("trigger_id", "")
    resp_url = payload.get("response_url", "")
    try:
        name, sid = action_id.split(":", 1)
        tid = int(sid)
    except (ValueError, TypeError):
        return
    tk = sfa_db.get_task(con, tid)
    if not tk:
        _respond_url(resp_url, "⚠ タスクが見つかりません（削除済み？）")
        return
    if name == "task_done":
        _was_done = (tk.get("status") or "") == "完了"
        sfa_db.set_task_status(con, tid, "完了")
        _respond_url(resp_url, f"✓ 完了にしました: {tk.get('title')}")
        if not _was_done:   # 完了への遷移時のみ通知（既に完了なら二重通知しない）
            try:
                notify_task_done(con, tid)
            except Exception as _e:  # noqa: BLE001
                print(f"[slack_tasks] notify_task_done(slack) error: {_e}", flush=True)
    elif name == "task_start":
        sfa_db.set_task_status(con, tid, "対応中")
        _respond_url(resp_url, f"▶ 対応中にしました: {tk.get('title')}")
    elif name == "task_snooze":
        due = (tk.get("due_date") or "").strip()
        base = date.fromisoformat(due) if due else date.today()
        nd = sfa_db.add_business_days(base, 3).isoformat()
        con.execute("UPDATE tasks SET due_date=?, updated_at=datetime('now') WHERE id=?", (nd, tid))
        con.commit()
        _respond_url(resp_url, f"⏰ 期限を {nd} に延ばしました: {tk.get('title')}")
    elif name == "task_progress":
        view = build_progress_modal(tid, "progress", tk.get("title", ""))
        _slack_post("views.open", trigger_id=trigger_id, view=view)
    elif name == "task_edit":
        # 起票と同じモーダルを既存値で開く（簡易編集）
        prefill = {"title": tk.get("title", ""), "next_action": tk.get("next_action") or "",
                   "assignee": tk.get("assignee") or "", "due_date": tk.get("due_date") or "",
                   "project": tk.get("project") or "", "category": tk.get("category") or ""}
        _slack_post("views.open", trigger_id=trigger_id,
                    view=build_create_modal(con, prefill, {"edit_id": tid}))


def _respond_url(response_url: str, text: str) -> None:
    """response_url にPOSTしてメッセージを更新/追記（ephemeral置換）。"""
    if not response_url:
        return
    import urllib.request
    try:
        req = urllib.request.Request(
            response_url, data=json.dumps({"text": text, "replace_original": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"[slack_tasks] respond_url failed: {e}", flush=True)


# ── 朝ダイジェスト（Block Kit・ボタン付き） ────────────────────────────────

def build_digest_blocks(owner: str, tasks: list, tool_url: str) -> list:
    """朝ダイジェストをBlock Kit化。各タスクに操作ボタンを付ける。"""
    today = date.today().isoformat()

    def mmdd(due):
        try:
            d = date.fromisoformat(due)
            return f"{d.month}/{d.day}"
        except (ValueError, TypeError):
            return "期限なし"

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"☀️ 今日のタスク（{owner}）"}},
              {"type": "context", "elements": [{"type": "mrkdwn",
               "text": f"未完了 {len(tasks)}件・{today}。ボタンでその場で消込できます。"}]}]
    shown = 0
    for t in tasks:
        if shown >= 12:
            break
        due = (t.get("due_date") or "").strip()
        mark = "🔴" if (due and due < today) else ("🟡" if due else "⚪")
        na = (t.get("next_action") or "").strip()
        link = f"{tool_url}/tasks?assignee={owner}#tc-{t['id']}"
        txt = f"{mark} *<{link}|{t.get('title','')}>*  ({mmdd(due) if due else '期限なし'})"
        if na:
            txt += f"\n▶ {na}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
        blocks.append(_task_action_block(t["id"]))
        shown += 1
    if len(tasks) > shown:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                      "text": f"…ほか {len(tasks)-shown}件。<{tool_url}/tasks?assignee={owner}|看板で見る>"}]})
    return blocks
