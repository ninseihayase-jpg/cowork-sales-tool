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
import re
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

# Slackの自動リンク記法 <https://...> / <https://...|表示名> と、素のhttp(s)://... の両方を拾う（#157）。
# 素のURLは、直後に空白無しで日本語の句読点・括弧類が続くケース（例:「...z、以上です」）を
# URLの一部として拾ってしまわないよう、それらの文字で区切る。
_URL_RE = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>|(https?://[^\s<>、。，．！？」』】）\)]+)")


def _extract_urls(text: str, limit: int = 5) -> list[str]:
    """本文からURLを抽出する（#157: TaskBot起票時、本文にリンクがあればタスクの
    「リンク」に自動セットするための下準備）。末尾の句読点・括弧は誤爆しやすいので削る。
    重複除去のうえ最大limit件まで（長文の暴走的な貼り付け対策）。"""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text):
        url = (m.group(1) or m.group(2) or "").rstrip(").,;:、。」』")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


# ── 担当解決（Slackユーザー → SFA担当名） ─────────────────────────────────

def _owner_slack_map() -> dict:
    """担当名→email のマップ。config/owner_slack_map.json をベースに、環境変数
    OWNER_SLACK_MAP_JSON（同形式のJSON）があれば上書きマージする。
    ※公開リポジトリにメールを載せないため、秘匿したい担当は env 側で設定する
    （SFAリポジトリはPUBLIC。env値はRender等の秘匿設定に置く）。_始まりのキーは除外。"""
    out: dict = {}
    p = os.path.join(_PROJECT_ROOT, "config", "owner_slack_map.json")
    try:
        with open(p, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not k.startswith("_"):
                    out[k] = v
    except Exception:
        pass
    env = os.environ.get("OWNER_SLACK_MAP_JSON", "").strip()
    if env:
        try:
            for k, v in json.loads(env).items():
                if not str(k).startswith("_"):
                    out[k] = v   # env が優先（ファイルの値を上書き）
        except Exception:
            print("[slack_tasks] OWNER_SLACK_MAP_JSON のJSON解析に失敗（無視）", flush=True)
    return out


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


_URGENCY_EMOJI = {"高": "🔴", "中": "🟡", "低": "🟢"}


def _estimate_urgency(source_text: str, due_date: str | None = None,
                      category: str | None = None) -> tuple[str, str]:
    """依頼内容から緊急度を推定して (level, reason) を返す。level=高/中/低。
    期日の近さ・内容（請求/支払/締切/法対応は高め）を材料に Haiku で判定。失敗時は中。"""
    prompt = (
        "次の事務依頼の緊急度を推定し、JSONのみで答えてください（前置き・後置き禁止）。\n"
        "level は 高 / 中 / 低 の3段階。reason は12文字以内の日本語（推定根拠）。\n"
        "判断材料: 期日の近さ（明日まで・本日中は高）、内容（請求・支払・締切・法/契約対応は高め、"
        "台帳更新・情報追記など軽作業は低め）。\n"
        f"期日: {due_date or '未設定'}\n種別: {category or '不明'}\n"
        f"本文:\n{(source_text or '')[:1500]}\n"
        '出力例: {"level":"高","reason":"明日締切の請求"}'
    )
    try:
        raw = _call_claude(prompt) or ""
        m = re.search(r'\{.*\}', raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        level = str(d.get("level", "")).strip()
        reason = str(d.get("reason", "")).strip()[:20]
        if level not in ("高", "中", "低"):
            level = "中"
        return level, reason
    except Exception:
        return "中", ""


def notify_task_created(con, task_id: int, source_text: str = "", token: str | None = None) -> bool:
    """事務タスク(is_admin=1)が起票されたとき、担当者へ簡潔なDMを送る（Ope Botから）。
    内容: 推定緊急度／依頼者・タスク名・期日／Slackリンク。緊急度はHaikuで推定。
    担当者がSlack未解決なら送らない。token省略時は事務Bot(SLACK_DESK_TOKEN)。"""
    from .slack_bot import SLACK_DESK_TOKEN
    tok = token or SLACK_DESK_TOKEN
    if not tok:
        return False
    t = sfa_db.get_task(con, task_id)
    if not t or not t.get("is_admin"):
        return False
    assignee = (t.get("assignee") or "").strip()
    uid = _slack_user_id_for(assignee, token=tok)
    if not uid:
        print(f"[slack_tasks] notify_task_created: 担当「{assignee}」Slack未解決でDMスキップ", flush=True)
        return False
    title = (t.get("title") or "(無題)").strip()
    requester = (t.get("requester") or "—").strip() or "—"
    due = (t.get("due_date") or "未設定").strip() or "未設定"
    link = (t.get("slack_permalink") or "").strip() or f"{SFA_TOOL_URL}/desk-tasks#tc-{task_id}"
    level, reason = _estimate_urgency(source_text or title, due_date=t.get("due_date"),
                                      category=t.get("category"))
    emoji = _URGENCY_EMOJI.get(level, "🟡")
    rtxt = f"（{reason}）" if reason else ""
    text = (f"📋 新規事務タスク｜{emoji}緊急度: {level}{rtxt}\n"
            f"依頼者 {requester} ／ タスク「{title}」／ 期日 {due}\n"
            f"🔗 <{link}|Slackで開く>")
    r = _slack_post("chat.postMessage", token=tok, channel=uid, text=text)
    if not r.get("ok"):
        print(f"[slack_tasks] notify_task_created DM failed: {r.get('error')}", flush=True)
    return bool(r.get("ok"))


def _notify_reply_post_failure(user_id: str, tid: int, title: str, error: str | None,
                               token: str | None, path: str) -> None:
    """チャンネルへの確認返信(chat.postMessage)がAPIレベルで失敗した時のフォールバック
    （ユーザー報告2026-08-24: タスク起票は成功するがTaskBotの返信が来ない）。
    起票した本人へ直接DMで知らせる。chat.postMessageはchannelにユーザーIDを渡すとDMを
    自動で開いて送信できる（notify_task_createdと同じ経路）ため、not_in_channel等
    チャンネル固有の理由で本来の返信が失敗していてもDMは高い確率で届く。"""
    if not user_id:
        return
    link = f"{SFA_TOOL_URL}/{path}#tc-{tid}"
    _slack_post("chat.postMessage", token=token, channel=user_id,
                text=f"⚠ タスク化はできましたが、チャンネルへの返信投稿に失敗しました（{error}）。"
                     f"TaskBotがそのチャンネルに参加しているかご確認ください。\n"
                     f"作成したタスク: <{link}|{title}>")


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
    # 請求関連はAI判定の揺れによらず確実に「経費・請求」へ強制する（重要業務の分類漏れ対策）
    if "経費・請求" in categories and sfa_db.is_billing_task(out["title"], text):
        out["category"] = "経費・請求"
    return out


_BULLET_RE = re.compile(r"^[・\-\*•‣◦○●]\s*|^\d+[\.\)、]\s*")


def _fallback_split_tasks(text: str) -> list[dict]:
    """ai_extract_tasksのAI抽出に失敗した時のフォールバック（#148）。箇条書きマーカー
    （・-*•等や「1.」等）で始まる行が複数あればその行ごとに1タスク、無ければ本文全体を
    1タスクとする（従来のai_extract_taskの単一フォールバックを複数行対応に拡張）。"""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    bullets = [_BULLET_RE.sub("", ln) for ln in lines if ln and _BULLET_RE.match(ln)]
    bullets = [b.strip() for b in bullets if b.strip()]
    if len(bullets) >= 2:
        return [{"title": b[:80], "next_action": "", "due_date": "", "category": ""} for b in bullets]
    return [{"title": (text or "")[:80] or "(無題)", "next_action": "", "due_date": "", "category": ""}]


def ai_extract_tasks(text: str, today: str | None = None, categories: list | None = None) -> list[dict]:
    """自由文から1件以上のタスク項目を抽出（title/next_action/due_date/category）（#148）。
    箇条書き（・-*•や1.等）や改行で複数の依頼が並んでいる場合は、それぞれを別タスクとして
    分ける——ユーザー報告2026-09-02: TaskBotへのメンションで箇条書き2行を書いたら、
    1件のタスクに統合されてしまっていた（従来のai_extract_taskは「1件抽出」前提の
    プロンプト・単一dict戻り値だったのが原因）。依頼が1件だけの場合も要素1件のリストで
    返す（呼び出し側はai_extract_task→ai_extract_tasksへの置き換えだけで済む）。
    失敗時は_fallback_split_tasksで本文を機械的に分割する。
    categories を渡すとその分類群から選ばせる（事務タスクは ADMIN_TASK_CATEGORIES を渡す）。"""
    text = (text or "").strip()
    today = today or date.today().isoformat()
    categories = categories or sfa_db.TASK_CATEGORIES
    if not text:
        return [{"title": "(無題)", "next_action": "", "due_date": "", "category": ""}]
    cats = "／".join(categories)
    prompt = (
        "次の文から社内タスクを抽出し、JSON配列だけを出力してください（説明不要）。\n"
        "箇条書き（・-*•や「1.」等）や改行で複数の依頼が並んでいる場合は、それぞれを別々の"
        "タスクとして分けてください（1行1タスクが基本。ただし1つの依頼の説明が複数行にまたがる"
        "場合はまとめて1タスクにしてください）。依頼が1件だけの場合も要素1件の配列にしてください。\n"
        f"今日は {today}。相対表現（例:金曜まで/来週/明日）は今日基準でYYYY-MM-DDに変換。不明な項目は空文字。\n"
        f"カテゴリは次から最も近い1つ: {cats}\n"
        '出力形式: [{"title":"簡潔なタスク名","next_action":"次にやる具体的な一手",'
        '"due_date":"YYYY-MM-DD or 空","category":"カテゴリ名 or 空"}, ...]\n\n'
        f"文:\n{text}")
    fallback_items = _fallback_split_tasks(text)
    try:
        raw = _call_claude(prompt) or ""
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else None
    except Exception:
        return fallback_items
    if not isinstance(data, list) or not data:
        return fallback_items
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row = {"title": "", "next_action": "", "due_date": "", "category": ""}
        for k in row:
            v = (item.get(k) or "").strip()
            if v:
                row[k] = v
        if not row["title"]:
            continue
        if row["category"] not in categories:
            row["category"] = ""
        # is_billing_taskの第2引数には、この項目自身のnext_actionのみを渡す（本文全体を
        # 渡すと、1件だけ請求関連の文言があるだけで他の全項目まで「経費・請求」に
        # 誤判定されてしまうため。#148で複数タスク対応した際に発見・修正）。
        if "経費・請求" in categories and sfa_db.is_billing_task(row["title"], row["next_action"]):
            row["category"] = "経費・請求"
        out.append(row)
    return out or fallback_items


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
        _select("effort_level", "工数感（ガント表示用）", sfa_db.TASK_EFFORT_LEVELS, prefill.get("effort_level")),
    ]
    return {
        "type": "modal", "callback_id": "task_create",
        "title": {"type": "plain_text", "text": "コンサルタスクを起票"},
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
                            is_admin=0, requester=None, return_created=False, effort_level=None,
                            source_text=None):
    """モーダル/AI抽出の値からタスク作成。種類が空かつai_category=Trueならその場でAI判定
    （モーダル送信＝3秒制約のある文脈では ai_category=False にして背景で後追い判定）。
    is_admin=1 で事務タスク（requester=依頼者）。事務タスクは分類体系が異なるためAI後追い判定はしない。
    事務タスクは期限未指定なら既定で3営業日後にし、受信箱に留める（受付=受信箱の運用のため自動整理しない）。
    同一Slackメッセージ(slack_channel+slack_ts+is_admin)から既にタスクがあれば新規作成せず既存idを返す
    （Slackの再送・別event_id二重配信・再操作による重複起票を防ぐ）。同一メッセージの
    重複判定(SELECT)→INSERT は _CREATE_LOCK で直列化する（AI呼び出しはロック外）。
    source_text を渡すと、その本文からURLを抽出しタスクの「リンク」(task_links)に自動セットする
    （#157: TaskBotへの起票時にリンクを同時に送ると、タスクのリンクに自動で入っていてほしいという
    要望。新規作成時のみ実行——重複配信で既存タスクを返す場合は追加しない）。"""
    # AIカテゴリ判定はロック外で先に済ませる（遅い呼び出しでの直列化を避ける）。事務は判定しない。
    if not category and ai_category and not is_admin:
        try:
            from .webapp import _ai_guess_task_category  # 遅延import（循環回避）
            category = _ai_guess_task_category(title or "", next_action or "")
        except Exception:
            category = None
    if is_admin and not (due_date or "").strip():
        due_date = _admin_default_due()   # 事務タスクの既定期限＝3営業日後
    # 請求関連は経路（AI抽出/モーダル手選択）に関わらず確実に「経費・請求」へ強制する（分類漏れ対策）
    if is_admin and sfa_db.is_billing_task(title or "", next_action or ""):
        category = "経費・請求"
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
            created_by=created_by, effort_level=(effort_level if not is_admin else None))
        # 事務タスク×Slack起票は、期限がAI抽出/既定値のどちらであっても「提案」に過ぎず、
        # 依頼者本人がスレッドで確定するまでは未確定扱い（期限確認プロセス、2026-08-27）。
        if is_admin and slack_channel and slack_ts:
            con.execute("UPDATE tasks SET due_date_confirmed=0 WHERE id=?", (tid,))
        con.commit()
        # #157: 起票元の本文にURLがあれば、タスクの「リンク」に自動セットする。
        if source_text:
            _urls = _extract_urls(source_text)
            if not _urls and re.search(r"https?://", source_text):
                # ユーザー報告(2026-09-04, #161): Slackのメッセージパーマリンクを本文に
                # 貼り付けたのにリンクが付かないケースがあった。本番に生ログが残っておらず
                # 再現できなかったため、本文にhttp(s)://らしき文字列があるのに抽出結果が
                # 空だった場合だけ、該当箇所をログに残す（原因特定用の一時計装）。
                _raw_hint = re.findall(r"https?://\S{0,160}", source_text)
                print(f"[slack_tasks] link-extract-miss: task={tid} raw_hint={_raw_hint!r}", flush=True)
            for _url in _urls:
                try:
                    sfa_db.add_task_link(con, tid, _url)
                except Exception as _e:  # noqa: BLE001
                    print(f"[slack_tasks] add_task_link failed: {_e}", flush=True)
        # 担当＋期限が揃えば受信箱→未着手へ自動整理（通常/事務とも）。ユーザー報告2026-08-27
        # 「Slackで期限指定しても(担当がセットできていても)受信箱にたまる」対策として、ローカル
        # 変数(assignee/due_date)を信頼せず、実際に保存された行を読み直して判定するように変更
        # （webapp.pyの/task/{id}/fieldと全く同じ_task_auto_triageに判定ロジックを一本化）。
        try:
            from .webapp import _task_auto_triage  # 遅延import（循環回避）
            _task_auto_triage(con, tid)
        except Exception as _e:  # noqa: BLE001
            print(f"[slack_tasks] _task_auto_triage failed: {_e}", flush=True)
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


def _admin_due_context(con, tid: int, mention_uid: str | None = None) -> dict:
    """事務タスク起票コメントに添える期限確認ブロック（2026-08-27・期限確認プロセス）。
    2026-09-02(#150): 見落とされやすいという報告を受け、目立たせるため通常サイズ・太字の
    sectionブロックに変更（従来はcontextブロックで小さく表示していた）。依頼者への
    メンション（実SlackID）もここに付けられるようにした——「事務タスク化しました」の
    見出し行ではなく、実際に返信してほしいこの確認文の方にメンションを付けてほしい、
    というユーザー要望（2026-09-02）に対応。
    AI抽出/既定値(起票から3営業日後)はあくまで提案であり、依頼者本人の返信で確定するまで
    タスクは due_date_confirmed=0（未確定）のまま。このスレッドへの返信「OK」で提案どおり確定、
    別の日付を書けばその日付で確定（下のクイックボタンでも確定扱いになる）。"""
    t = sfa_db.get_task(con, tid)
    due = ((t or {}).get("due_date") or "").strip()
    due_txt = due or "未設定"
    mention = f"<@{mention_uid}> " if mention_uid else ""
    return {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{mention}*📅 期限は {due_txt} でよろしいですか？*\n"
                    "このスレッドに「OK」と返信、または正しい期限を返信してください"
                    "（例: 9/5, 来週金曜, 明後日 等）。下のボタンでも設定できます。"}}


_AFFIRMATIVE_RE = re.compile(
    r"^(ok+|okay|okey|オッケー+|おっけー+|おけ|オケ|それで(いい|良い|お願いします?)?|"
    r"はい|了解|承知(しました)?|good|👍+|うん|そのままで(いい|お願いします?)?|"
    r"問題(ない|なし)|大丈夫)(です|でした)?[!！。\.\s]*$", re.IGNORECASE)


def _is_affirmative_reply(text: str) -> bool:
    """期限確認スレッドへの返信が「提案どおりでOK」の意味かどうかを判定する。
    様々な表記揺れ（オッケー/了解/大丈夫です等）に対応する簡易パターンマッチ。
    ここに当てはまらないものは全て「別の期限を指定した」とみなしAIで日付抽出する。"""
    return bool(_AFFIRMATIVE_RE.match((text or "").strip()))


def _parse_due_date_reply(text: str, today: str | None = None) -> str:
    """期限確認スレッドへの返信から日付だけを抽出する（#149）。
    ユーザー報告2026-09-02: 「9/2(本日)」「9/2」という単純な日付返信が2回とも
    「期限が読み取れませんでした」と失敗していた。従来はai_extract_task（=タスクを1件
    抽出するための「次の文から社内タスクを1件抽出し」というプロンプト）を流用していたが、
    「9/2」のような日付単体の短い返信は「タスク」として抽出しようとするとAIが混乱しやすく、
    失敗率が高い。日付抽出だけに絞った専用プロンプトに切り出すことで安定させる。"""
    text = (text or "").strip()
    today = today or date.today().isoformat()
    if not text:
        return ""
    prompt = (
        "次の文に含まれる期日を1つだけ抽出し、JSONだけを出力してください（説明不要）。\n"
        f"今日は{today}。「9/2」「9/2(本日)」のような日付そのものの返信も、"
        "「来週金曜」「明後日」「今日中」のような相対表現も、今日基準でYYYY-MM-DD形式に"
        "変換してください。年が省略されている場合は今日以降で最も近い年を補ってください。\n"
        "日付がどうしても読み取れない場合のみdue_dateを空文字にしてください。\n"
        '出力: {"due_date":"YYYY-MM-DD or 空"}\n\n'
        f"文:\n{text}")
    try:
        raw = _call_claude(prompt) or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return ""
    return (data.get("due_date") or "").strip() if isinstance(data, dict) else ""


def handle_admin_due_reply(con, event: dict, token: str | None = None) -> None:
    """事務タスクの期限確認スレッドへの人間の返信を処理（message イベント）。
    channel+thread_ts が一致し、is_admin=1 かつ due_date_confirmed=0 のタスクが対象
    （無ければ何もしない＝無関係なスレッド返信では反応しない）。
    「OK」等の肯定語なら提案中の期限をそのまま確定。それ以外は自由文の期限として
    _parse_due_date_reply（日付抽出専用プロンプト、#149）で抽出する。抽出できなければ
    確定せず再度尋ね返す（無言で諦めない）。"""
    channel = (event.get("channel") or "").strip()
    ts = (event.get("ts") or "").strip()
    thread_ts = (event.get("thread_ts") or "").strip()
    text = (event.get("text") or "").strip()
    # スレッド内の返信のみ対象（thread_tsがルート自身＝新規投稿は対象外）。本文が無ければ無視。
    if not channel or not thread_ts or thread_ts == ts or not text:
        return
    row = con.execute(
        "SELECT id, due_date FROM tasks WHERE slack_channel=? AND slack_ts=? "
        "AND COALESCE(is_admin,0)=1 AND COALESCE(due_date_confirmed,1)=0 "
        "AND (deleted_at IS NULL OR deleted_at='') ORDER BY id DESC LIMIT 1",
        (channel, thread_ts)).fetchone()
    if not row:
        return
    tid, proposed = row["id"], row["due_date"]
    if _is_affirmative_reply(text):
        nd = proposed
    else:
        nd = _parse_due_date_reply(text)
        if not nd:
            _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                        text="🙏 期限が読み取れませんでした。日付でお答えください（例: 9/5, 来週金曜, 明後日 等）。")
            return
    con.execute("UPDATE tasks SET due_date=?, due_date_confirmed=1, updated_at=datetime('now') WHERE id=?",
                (nd, tid))
    con.commit()
    _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
               text=f"✅ 期限を {nd} に設定しました。")


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


def _mention_ts(base_ts: str, i: int) -> str:
    """複数タスク一括起票(#148)時、2件目以降のslack_ts保存値。同一メッセージからの
    重複配信(Slack再送)は各件で個別に判定できるようにしつつ、1件目は元のtsのまま保つ
    （事務タスクの期限確認スレッド返信照合(handle_admin_due_reply)が元tsの完全一致を
    見ているため、1件目だけは必ずそこにヒットできるようにする設計）。"""
    return base_ts if i == 0 else f"{base_ts}#{i}"


def handle_reaction(con, event: dict, token: str | None = None) -> None:
    """リアクションでメッセージをタスク化。🎯(dart)=通常タスク、📋(clipboard)=事務タスク に振り分け。
    token指定で別Bot（事務Bot）のトークンで本文取得・ユーザー解決・返信を行う。
    #151: 本文が複数の依頼に分割できそうな場合は、必ずボタンで「分割する/1件のまま登録する」を
    確認する（#148で自動分割していたが、1行を過剰に分割してしまう事例があったため）。
    1件しか抽出されない場合は確認不要でそのまま起票（従来と同じ返信文言・ブロック構成）。"""
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
        prefills = ai_extract_tasks(text, categories=sfa_db.ADMIN_TASK_CATEGORIES)
        if len(prefills) > 1:
            _post_split_confirm(con, channel=channel, thread_ts=ts, token=token,
                                is_admin=True, prefills=prefills, text=text, user_id=author_id)
            return
        tid, created = create_task_from_fields(
            con, title=prefills[0]["title"], next_action=prefills[0]["next_action"] or None,
            assignee=(DESK_ASSIGNEE or None), due_date=prefills[0]["due_date"] or None,
            category=prefills[0]["category"] or None, slack_channel=channel, slack_ts=ts,
            slack_permalink=permalink,
            created_by=owner_from_slack_user(user_id, token=token) or user_id,
            is_admin=1, requester=requester, return_created=True, source_text=text)
        if created:   # 新規起票時のみ担当者へDM（重複配信では送らない）
            try:
                notify_task_created(con, tid, source_text=text, token=token)
            except Exception as _e:  # noqa: BLE001
                print(f"[slack_tasks] notify_task_created(reaction) error: {_e}", flush=True)
        link = f"{SFA_TOOL_URL}/desk-tasks#tc-{tid}"
        _req = f"（依頼者: {requester}）" if requester else ""
        # 期限確認の返信が来ないというユーザー報告(2026-09-02, #149/#150)への対応。
        # 依頼者本人（＝元メッセージの投稿者。author_idは既にSlack上の実IDなので名前解決を
        # 挟まず直接メンションできる）は、実際に返信してほしい期限確認の一文
        # (_admin_due_context)の方にメンションする。「事務タスク化しました」の見出しは
        # 逆に目立たせなくてよい（#150）ので、通常sectionより小さいcontextブロックにする。
        _r = _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=ts,
                         text=f"📋 事務タスク化しました: {prefills[0]['title']}",
                         blocks=[
                             {"type": "context", "elements": [{"type": "mrkdwn",
                              "text": f"📋 事務タスク化しました{_req}\n*<{link}|{prefills[0]['title']}>*"
                                      + (f"\n▶ {prefills[0]['next_action']}" if prefills[0]['next_action'] else "")}]},
                             _admin_due_context(con, tid, mention_uid=author_id),
                             _task_action_block(tid),
                         ])
        if not _r.get("ok"):
            print(f"[slack_tasks] handle_reaction(admin): reply post failed (task {tid} created): "
                  f"{_r.get('error')}", flush=True)
            _notify_reply_post_failure(user_id, tid, prefills[0]["title"], _r.get("error"), token, "desk-tasks")
        return
    prefills = ai_extract_tasks(text)
    if len(prefills) > 1:
        _post_split_confirm(con, channel=channel, thread_ts=ts, token=token,
                            is_admin=False, prefills=prefills, text=text, user_id=user_id)
        return
    owner = owner_from_slack_user(user_id, token=token)
    tid = create_task_from_fields(
        con, title=prefills[0]["title"], next_action=prefills[0]["next_action"] or None,
        assignee=owner, due_date=prefills[0]["due_date"] or None, category=prefills[0]["category"] or None,
        slack_channel=channel, slack_ts=ts, slack_permalink=permalink, created_by=owner or user_id,
        source_text=text)
    link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
    # 起票したらスレッド投稿（@メンション起票・事務タスクのリアクション起票と同仕様）。
    _r = _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=ts,
                     text=f"🎯 コンサルタスク化しました: {prefills[0]['title']}",
                     blocks=[
                         {"type": "section", "text": {"type": "mrkdwn",
                          "text": f"🎯 コンサルタスク化しました\n*<{link}|{prefills[0]['title']}>*"
                                  + (f"\n▶ {prefills[0]['next_action']}" if prefills[0]['next_action'] else "")}},
                         _task_action_block(tid),
                         _task_effort_block(tid),
                     ])
    if not _r.get("ok"):
        print(f"[slack_tasks] handle_reaction: reply post failed (task {tid} created): "
              f"{_r.get('error')}", flush=True)
        _notify_reply_post_failure(user_id, tid, prefills[0]["title"], _r.get("error"), token, "tasks")


# ── @メンションでタスク化（webapp/slack_botから条件付きで呼ぶ） ──────────────

def handle_mention_task(con, channel: str, thread_ts: str, text: str, user_id: str,
                        token: str | None = None) -> list[int]:
    """@Botメンション(task/タスク:)を通常タスク化。#93: token指定で通常タスクBot（別アプリ）
    として返信・ユーザー解決を行う（desk-tasksのhandle_admin_mention_taskと対）。
    #148で複数タスクへの分割に対応したが、ユーザー報告2026-09-02(#151): 「＋」区切りの
    1行のような、本来1件の依頼を過剰に複数分割してしまうことがあった。#151で、分割できる
    場合は必ずボタンで「分割する/1件のまま登録する」を確認するステップを挟むように変更
    （1件しか抽出されない場合は確認不要でそのまま起票、従来と同じ返信文言・ブロック構成）。
    確認が必要な場合、この時点ではタスクは何も作成していないため戻り値は空リスト。"""
    prefills = ai_extract_tasks(text)
    if len(prefills) > 1:
        _post_split_confirm(con, channel=channel, thread_ts=thread_ts, token=token,
                            is_admin=False, prefills=prefills, text=text, user_id=user_id)
        return []
    owner = owner_from_slack_user(user_id, token=token)
    tid = create_task_from_fields(
        con, title=prefills[0]["title"], next_action=prefills[0]["next_action"] or None,
        assignee=owner, due_date=prefills[0]["due_date"] or None, category=prefills[0]["category"] or None,
        slack_channel=channel, slack_ts=thread_ts, created_by=owner or user_id, source_text=text)
    link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
    _r = _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                     text=f"コンサルタスク化しました: {prefills[0]['title']}",
                     blocks=[{"type": "section", "text": {"type": "mrkdwn",
                             "text": f"✅ コンサルタスク化しました *<{link}|{prefills[0]['title']}>*"}},
                             _task_action_block(tid),
                             _task_effort_block(tid)])
    if not _r.get("ok"):
        # タスク作成自体は成功しているが、返信の投稿に失敗（Botが未参加のprivateチャンネル等で
        # 発生するchat.postMessageのAPIレベル失敗はHTTP200で返るため例外にならず、無言で消えていた
        # ことがユーザー報告で判明。まずはログに残す＝2026-08-24）。
        print(f"[slack_tasks] handle_mention_task: reply post failed (task {tid} created): "
              f"{_r.get('error')}", flush=True)
        _notify_reply_post_failure(user_id, tid, prefills[0]["title"], _r.get("error"), token, "tasks")
    return [tid]


def handle_admin_reaction(con, event: dict, token: str | None = None) -> None:
    """事務Bot専用エンドポイント用: 📋(clipboard)のみを事務タスク化。🎯等は無視（別Botの担当）。"""
    if event.get("reaction") not in ADMIN_TASK_REACTIONS:
        return
    handle_reaction(con, event, token=token)


def handle_admin_mention_task(con, channel: str, thread_ts: str, text: str, user_id: str,
                              token: str | None = None) -> list[int]:
    """@事務Botメンションを事務タスク化。依頼者=メンションした人、担当=事務員、is_admin=1。
    token指定で事務Botとして返信・ユーザー解決を行う。
    #151: 分割できる場合は必ずボタンで確認するステップを挟む（1件のみの場合は従来通り）。
    確認が必要な場合、この時点ではタスクは何も作成していないため戻り値は空リスト。"""
    prefills = ai_extract_tasks(text, categories=sfa_db.ADMIN_TASK_CATEGORIES)
    if len(prefills) > 1:
        _post_split_confirm(con, channel=channel, thread_ts=thread_ts, token=token,
                            is_admin=True, prefills=prefills, text=text, user_id=user_id)
        return []
    requester = owner_from_slack_user(user_id, token=token)  # 依頼＝メンションした本人
    permalink = _message_permalink(channel, thread_ts, token=token)
    tid, created = create_task_from_fields(
        con, title=prefills[0]["title"], next_action=prefills[0]["next_action"] or None,
        assignee=(DESK_ASSIGNEE or None), due_date=prefills[0]["due_date"] or None,
        category=prefills[0]["category"] or None, slack_channel=channel, slack_ts=thread_ts,
        slack_permalink=permalink, created_by=requester or user_id, is_admin=1, requester=requester,
        return_created=True, source_text=text)
    if not created:
        # 同一メッセージからの多重配信（別event_id）＝既に起票済み。返信もしない（3重返信の抑止）。
        return [tid]
    _req = f"（依頼者: {requester}）" if requester else ""
    link = f"{SFA_TOOL_URL}/desk-tasks#tc-{tid}"
    _r = _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                     text=f"事務タスク化しました: {prefills[0]['title']}",
                     blocks=[{"type": "context", "elements": [{"type": "mrkdwn",
                             "text": f"📋 事務タスク化しました{_req} *<{link}|{prefills[0]['title']}>*"}]},
                             _admin_due_context(con, tid, mention_uid=user_id), _task_action_block(tid)])
    if not _r.get("ok"):
        print(f"[slack_tasks] handle_admin_mention_task: reply post failed (task {tid} created): "
              f"{_r.get('error')}", flush=True)
        _notify_reply_post_failure(user_id, tid, prefills[0]["title"], _r.get("error"), token, "desk-tasks")
    # 担当者へ簡潔DM（推定緊急度＋依頼者/タスク/期日＋Slackリンク）。元の依頼本文で緊急度を推定。
    try:
        notify_task_created(con, tid, source_text=text, token=token)
    except Exception as _e:  # noqa: BLE001
        print(f"[slack_tasks] notify_task_created(mention) error: {_e}", flush=True)
    return [tid]


# ── 複数タスクへの分割確認（#151） ───────────────────────────────────────────

def _post_split_confirm(con, *, channel: str, thread_ts: str, token: str | None, is_admin: bool,
                        prefills: list[dict], text: str, user_id: str | None) -> None:
    """本文が複数タスクに分割できそうな時、分割するか/1件のまま登録するかを確認する
    （#151。ユーザー報告2026-09-02: 「＋」区切りの1行が無確認で4件に分割されてしまった
    ため、必ず確認ステップを挟むよう変更。それまでタスクは1件も作成しない）。
    ボタン押下は_handle_block_action→_handle_split_decisionで処理する。"""
    split_id = sfa_db.create_pending_task_split(
        con, channel=channel, thread_ts=thread_ts, text=text, prefills=prefills,
        is_admin=is_admin, user_id=user_id, token=token)
    lines = "\n".join(f"{i + 1}. {p['title']}" for i, p in enumerate(prefills))
    _r = _slack_post(
        "chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
        text=f"🤔 {len(prefills)}件のタスクに分割できそうです。分割しますか？",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"🤔 これは*{len(prefills)}件のタスク*に分割できそうです。分割しますか？\n{lines}"}},
            {"type": "actions", "block_id": f"tsplit_{split_id}", "elements": [
                {"type": "button", "action_id": f"task_split_yes:{split_id}", "style": "primary",
                 "text": {"type": "plain_text", "text": f"✅ {len(prefills)}件に分割して登録"}},
                {"type": "button", "action_id": f"task_split_no:{split_id}",
                 "text": {"type": "plain_text", "text": "📝 1件のまま登録"}},
            ]},
        ])
    if not _r.get("ok"):
        print(f"[slack_tasks] _post_split_confirm: post failed: {_r.get('error')}", flush=True)


def _finalize_normal_tasks(con, *, channel: str, thread_ts: str, prefills: list[dict],
                           assignee: str | None, user_id: str | None, token: str | None,
                           source_text: str = "") -> list[int]:
    """分割確認(#151)の決着後（「分割する」で複数件、または「1件のまま登録」で1件）に、
    コンサルタスクを実際に作成し、まとめて1回のスレッド返信で通知する。
    source_text（起票元の本文全体）を渡すと、含まれるURLを全タスクのリンクに自動セットする
    （#157。分割された各タスクのどれに属するURLかまでは判定せず、元の依頼に含まれていた
    リンクは分割後の全タスクに付ける、という単純な仕様にしている）。"""
    tids = [create_task_from_fields(
        con, title=p["title"], next_action=p["next_action"] or None,
        assignee=assignee, due_date=p["due_date"] or None, category=p["category"] or None,
        slack_channel=channel, slack_ts=_mention_ts(thread_ts, i), created_by=assignee or user_id,
        source_text=source_text)
        for i, p in enumerate(prefills)]
    if len(tids) == 1:
        link = f"{SFA_TOOL_URL}/tasks#tc-{tids[0]}"
        summary_text = f"コンサルタスク化しました: {prefills[0]['title']}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn",
                   "text": f"✅ コンサルタスク化しました *<{link}|{prefills[0]['title']}>*"}},
                  _task_action_block(tids[0]), _task_effort_block(tids[0])]
    else:
        summary_text = f"コンサルタスク化しました（{len(tids)}件）"
        blocks = [{"type": "section", "text": {"type": "mrkdwn",
                   "text": f"✅ コンサルタスク化しました（{len(tids)}件）"}}]
        for tid, p in zip(tids, prefills):
            link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*<{link}|{p['title']}>*"}})
            blocks.append(_task_action_block(tid))
            blocks.append(_task_effort_block(tid))
    _r = _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                     text=summary_text, blocks=blocks)
    if not _r.get("ok"):
        print(f"[slack_tasks] _finalize_normal_tasks: reply post failed (tasks {tids} created): "
              f"{_r.get('error')}", flush=True)
        for tid, p in zip(tids, prefills):
            _notify_reply_post_failure(user_id, tid, p["title"], _r.get("error"), token, "tasks")
    return tids


def _finalize_admin_tasks(con, *, channel: str, thread_ts: str, prefills: list[dict],
                          requester: str | None, user_id: str | None, token: str | None,
                          permalink: str | None = None, source_text: str = "") -> list[int]:
    """分割確認(#151)の決着後（「分割する」で複数件、または「1件のまま登録」で1件）に、
    事務タスクを実際に作成し、まとめて1回のスレッド返信で通知する。
    source_text（起票元の本文全体）に含まれるURLは全タスクのリンクに自動セットする（#157。
    分割後のどのタスクに属するURLかは判定しない単純仕様。_finalize_normal_tasksと同じ扱い）。"""
    created_pairs = []  # (tid, prefill, created)
    for i, p in enumerate(prefills):
        tid, created = create_task_from_fields(
            con, title=p["title"], next_action=p["next_action"] or None,
            assignee=(DESK_ASSIGNEE or None), due_date=p["due_date"] or None,
            category=p["category"] or None, slack_channel=channel, slack_ts=_mention_ts(thread_ts, i),
            slack_permalink=permalink, created_by=requester or user_id, is_admin=1, requester=requester,
            return_created=True, source_text=source_text)
        created_pairs.append((tid, p, created))
    tids = [tid for tid, _, _ in created_pairs]
    new_pairs = [(tid, p) for tid, p, created in created_pairs if created]
    if not new_pairs:
        return tids
    _req = f"（依頼者: {requester}）" if requester else ""
    if len(new_pairs) == 1:
        tid, p = new_pairs[0]
        link = f"{SFA_TOOL_URL}/desk-tasks#tc-{tid}"
        summary_text = f"事務タスク化しました: {p['title']}"
        blocks = [{"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"📋 事務タスク化しました{_req} *<{link}|{p['title']}>*"}]},
                  _admin_due_context(con, tid, mention_uid=user_id), _task_action_block(tid)]
    else:
        summary_text = f"事務タスク化しました{_req}（{len(new_pairs)}件）"
        blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": summary_text}]}]
        for idx, (tid, p) in enumerate(new_pairs):
            link = f"{SFA_TOOL_URL}/desk-tasks#tc-{tid}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": f"*<{link}|{p['title']}>*" + (f"\n▶ {p['next_action']}" if p['next_action'] else "")}})
            # このスレッドへの「OK」返信は元tsの1件目にしかヒットしないため(#148設計)、
            # メンションも1件目にのみ付ける（2件目以降で返信してもらっても確定できない）。
            blocks.append(_admin_due_context(con, tid, mention_uid=(user_id if idx == 0 else None)))
            blocks.append(_task_action_block(tid))
    _r = _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                     text=summary_text, blocks=blocks)
    if not _r.get("ok"):
        print(f"[slack_tasks] _finalize_admin_tasks: reply post failed "
              f"(tasks {[t for t, _ in new_pairs]} created): {_r.get('error')}", flush=True)
        for tid, p in new_pairs:
            _notify_reply_post_failure(user_id, tid, p["title"], _r.get("error"), token, "desk-tasks")
    for tid, _p in new_pairs:
        try:
            notify_task_created(con, tid, source_text=source_text, token=token)
        except Exception as _e:  # noqa: BLE001
            print(f"[slack_tasks] notify_task_created error: {_e}", flush=True)
    return tids


def _handle_split_decision(con, split_id: int, decision: str, response_url: str | None = None) -> None:
    """「分割する/1件のまま登録する」ボタンの押下処理（#151）。
    decision='yes'なら保存済みの複数prefillsそのまま、'no'なら元の本文をai_extract_task
    （単数形）で1件に再抽出してから作成する。ボタンが既に処理済み（二重クリック・保存済み
    データの期限切れ）ならpending行が見つからないので何もしない。"""
    pending = sfa_db.get_pending_task_split(con, split_id)
    if not pending:
        if response_url:
            _respond_url(response_url, "⚠ この確認は既に処理済み、または見つかりませんでした。")
        return
    sfa_db.delete_pending_task_split(con, split_id)
    channel, thread_ts = pending["channel"], pending["thread_ts"]
    token = pending.get("token")
    user_id = pending.get("user_id")
    text = pending.get("text") or ""
    is_admin = bool(pending.get("is_admin"))
    if decision == "yes":
        prefills = pending.get("prefills") or []
        if not prefills:
            return
    else:
        prefills = [ai_extract_task(text, categories=(sfa_db.ADMIN_TASK_CATEGORIES if is_admin else None))]
    if response_url:
        _respond_url(response_url, "✅ 処理しました。")
    if is_admin:
        requester = owner_from_slack_user(user_id, token=token) if user_id else None
        permalink = _message_permalink(channel, thread_ts, token=token)
        _finalize_admin_tasks(con, channel=channel, thread_ts=thread_ts, prefills=prefills,
                              requester=requester, user_id=user_id, token=token, permalink=permalink,
                              source_text=text)
    else:
        owner = owner_from_slack_user(user_id, token=token) if user_id else None
        _finalize_normal_tasks(con, channel=channel, thread_ts=thread_ts, prefills=prefills,
                               assignee=owner, user_id=user_id, token=token, source_text=text)


# ── ボタン付きブロック（消込UI） ───────────────────────────────────────────

def _task_action_block(task_id: int) -> dict:
    """タスクカード返信の基本アクション行。期限クイック設定は当日/+1営業日/+3営業日の3種
    （ユーザー要望2026-08-26。Web側/tasksカードの当日/+1営/+3営/+5営/+8営と揃え、Slackでは
    ボタン数を抑えるため主要な3種のみ）。各ボタンのaction_idは末尾にオフセットを付けて
    一意にする（task_effortボタンで踏んだ重複action_id→invalid_blocksの再発防止）。"""
    return {"type": "actions", "block_id": f"tab_{task_id}", "elements": [
        {"type": "button", "action_id": f"task_done:{task_id}", "value": str(task_id),
         "style": "primary", "text": {"type": "plain_text", "text": "✓完了"}},
        {"type": "button", "action_id": f"task_start:{task_id}", "value": str(task_id),
         "text": {"type": "plain_text", "text": "▶開始"}},
        {"type": "button", "action_id": f"task_progress:{task_id}", "value": str(task_id),
         "text": {"type": "plain_text", "text": "💬進捗"}},
        {"type": "button", "action_id": f"task_snooze:{task_id}:0", "value": "0",
         "text": {"type": "plain_text", "text": "⏰当日"}},
        {"type": "button", "action_id": f"task_snooze:{task_id}:1", "value": "1",
         "text": {"type": "plain_text", "text": "⏰+1営業日"}},
        {"type": "button", "action_id": f"task_snooze:{task_id}:3", "value": "3",
         "text": {"type": "plain_text", "text": "⏰+3営業日"}},
    ]}


def _task_effort_block(task_id: int) -> dict:
    """コンサルタスクのガントチャート用「工数感」をSlackから即時設定するボタン行。
    3秒制約のある起票フロー(dartリアクション/@メンション)では、その場でモーダルを
    挟まず、起票直後の返信にこのボタンを添えて後から1クリックで設定してもらう。

    action_idは各ボタンで一意にする必要がある（レベルごとに末尾を分けて付与）。
    以前は全ボタンが同じ"task_effort:{task_id}"を共有していたため、Slackの
    chat.postMessageがinvalid_blocksで拒否し、タスク自体は作成されるのに確認の
    返信だけが（無言で）消えるという不具合になっていた（2026-08-20の導入時から
    2026-08-24のユーザー報告まで気づかれずに残っていた）。"""
    return {"type": "actions", "block_id": f"tef_{task_id}", "elements": [
        {"type": "button", "action_id": f"task_effort:{task_id}:{lvl}", "value": lvl,
         "text": {"type": "plain_text", "text": f"工数感:{lvl}"}}
        for lvl in sfa_db.TASK_EFFORT_LEVELS
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
            effort_level=_view_val(state, "effort_level"),
            slack_channel=meta.get("channel"), slack_ts=meta.get("ts"),
            created_by=owner or user_id, ai_category=False)  # AIは3秒制約回避のため背景で
        # 起票通知（本人へDM）
        link = f"{SFA_TOOL_URL}/tasks#tc-{tid}"
        _slack_post("chat.postMessage", channel=user_id,
                    text=f"コンサルタスクを起票しました: {_view_val(state,'title')}",
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
    if action_id.startswith("task_split_yes:") or action_id.startswith("task_split_no:"):
        # 複数タスクへの分割確認ボタン（#151）。task_{done,start,...}系とはtid空間が異なる
        # （タスクIDではなくpending_task_splits.idを指す）ため、通常の処理より先に分岐する。
        try:
            _split_id = int(action_id.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        _decision = "yes" if action_id.startswith("task_split_yes:") else "no"
        _handle_split_decision(con, _split_id, _decision, resp_url)
        return
    try:
        # task_effortのみ action_id が "task_effort:{tid}:{level}" と3分割（ボタンごとに
        # action_idを一意にするため。値自体は act["value"] から別途取得するので3つ目は無視）。
        parts = action_id.split(":")
        name, tid = parts[0], int(parts[1])
    except (ValueError, TypeError, IndexError):
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
        # valueにオフセット(営業日数)。0=当日（現在の期限に関係なく今日にリセット）、
        # 1/3等は現在の期限（無ければ今日）からその営業日数だけ後ろへずらす（従来の+3営業日と
        # 同じ「押すたびにさらに延ばせる」挙動）。旧形式(action_id="task_snooze:{tid}"の
        # 1本のみ・valueがtask_id文字列)のメッセージを万一クリックした場合はデフォルト3扱い。
        try:
            _n = int(act.get("value") or "3")
        except (TypeError, ValueError):
            _n = 3
        if _n <= 0:
            nd = date.today().isoformat()
        else:
            due = (tk.get("due_date") or "").strip()
            base = date.fromisoformat(due) if due else date.today()
            nd = sfa_db.add_business_days(base, _n).isoformat()
        # ボタンでの期限設定も人間による明示的な確定として扱う（事務タスクの期限確認プロセス）。
        con.execute("UPDATE tasks SET due_date=?, due_date_confirmed=1, updated_at=datetime('now') "
                   "WHERE id=?", (nd, tid))
        con.commit()
        _label = "当日" if _n <= 0 else f"+{_n}営業日"
        _respond_url(resp_url, f"⏰ 期限を {nd}（{_label}）に変更しました: {tk.get('title')}")
    elif name == "task_progress":
        view = build_progress_modal(tid, "progress", tk.get("title", ""))
        _slack_post("views.open", trigger_id=trigger_id, view=view)
    elif name == "task_effort":
        level = act.get("value") or ""
        if level not in sfa_db.TASK_EFFORT_LEVELS:
            _respond_url(resp_url, "⚠ 工数感の値が不正です")
        else:
            con.execute("UPDATE tasks SET effort_level=?, updated_at=datetime('now') WHERE id=?",
                        (level, tid))
            con.commit()
            _respond_url(resp_url, f"📊 工数感を「{level}」に設定しました: {tk.get('title')}")
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
