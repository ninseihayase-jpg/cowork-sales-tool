"""採番Bot（見積書・請求書・契約書番号の自動発行）× Slack（2026-09-17）。

「InProc社内採番ルール_v1_2609.xlsx」準拠のフォーマットで、専用Slackチャンネルに投稿された
自然文リクエスト（例:「TOPPANの見積書に採番してもらえますか？」）から番号を発行する。
対象（商談/取引先）・契約種別・改版元番号などが文面から特定できない場合は、スレッド内で
1項目ずつ聞き返し、返信を受けて確定する（slack_tasks.pyの期限確認フロー
_admin_due_context/handle_admin_due_reply と同じ「Bot提案→スレッド返信で確定」設計）。

Web本体(webapp.py)から呼ばれる。エンドポイント側で署名検証済みのペイロードを受け取る前提。
"""
from __future__ import annotations

import json
import os
import re

from . import sfa_db
from .slack_bot import _slack_post, _call_claude, find_deal, find_account

SFA_TOOL_URL = os.environ.get("SFA_TOOL_URL", "") or "https://sfa-crm.onrender.com"
_DEALS_LINK = f"{SFA_TOOL_URL}/deals?tab=active"
_ACCOUNTS_LINK = f"{SFA_TOOL_URL}/accounts"

_SFA_ID_RE = re.compile(r"SFA\s*#?\s*(\d+)", re.IGNORECASE)
_CCC_ID_RE = re.compile(r"CCC\s*#?\s*(\d+)", re.IGNORECASE)
# \bは日本語文字(かな/漢字)もUnicode上の「単語文字」扱いになるため、和文に直接隣接した番号
# （例:「12345Q01を改版」）で境界判定に失敗する。ASCII英数字のみを境界として明示的に除外する。
# 末尾の連続数字は見積書/契約書=2桁、請求書=YYMM+連番の6桁のいずれか。
_NUMBER_RE = re.compile(r"(?<![0-9A-Za-z])\d{5}[A-Z]{1,2}(?:\d{6}|\d{2})(?:R\d+)?(?![0-9A-Za-z])")

_REVISION_KEYWORDS = ["改版", "改定", "訂正", "再発行", "修正版"]

_CONTRACT_TYPE_KEYWORDS = {
    "M": ["基本契約", "MSA", "M契約"],
    "S": ["個別契約", "SOW", "S契約"],
    "N": ["秘密保持契約", "NDA", "N契約"],
    "O": ["その他"],
}

_DOC_TYPE_KEYWORDS = {"quote": ["見積書", "見積", "Q番号"],
                      "invoice": ["請求書", "請求", "I番号"],
                      "contract": ["契約書", "契約", "C番号"]}

# ユーザー報告(2026-09-17)への対応: 対象(商談/取引先)を尋ねる際、書類種別に関係なく
# 「SFA番号/CCC番号」両方を毎回聞いていた（見積書・契約書はSFA番号のみ、請求書はCCC番号のみ
# のはず）。書類種別ごとに対象の識別子・呼び方を分け、一覧画面へのリンクも添える。
_ENTITY_LABELS = {
    "quote": ("商談", "SFA番号", _DEALS_LINK, "商談一覧"),
    "contract": ("商談", "SFA番号", _DEALS_LINK, "商談一覧"),
    "invoice": ("取引先", "CCC番号", _ACCOUNTS_LINK, "取引先一覧"),
}


def _field_question(field: str, req: dict) -> str:
    """未確定項目を尋ねる文言を組み立てる。書類種別が分かっていれば、その種別の呼び方
    （見積書/契約書→商談・SFA番号、請求書→取引先・CCC番号）に絞って聞く
    （ユーザー報告: 種別に関係なく毎回両方を聞いていた不具合の修正）。"""
    doc_type = req.get("doc_type")
    label = sfa_db.DOCUMENT_TYPE_LABELS.get(doc_type, "") if doc_type else ""
    prefix = f"📋 {label}で発行します。" if label else "📋 "
    if field == "doc_type":
        return "📋 見積書・請求書・契約書のどれに採番しますか？"
    if field == "contract_type":
        return f"{prefix}契約種別を教えてください（基本契約/個別契約/秘密保持契約/その他）"
    if field == "entity":
        target, idlabel, link, linktext = _ENTITY_LABELS.get(
            doc_type, ("商談・取引先", "SFA番号/CCC番号", _DEALS_LINK, "商談一覧"))
        return (f"{prefix}対象の{target}が特定できませんでした。{target}名または{idlabel}を"
               f"教えてください（<{link}|{linktext}>）")
    if field == "revision_of":
        return f"{prefix}改版する元の番号を教えてください（例: 12345Q01）"
    return "📋 内容を確認できませんでした。もう一度教えてください"


# ユーザー要望(2026-09-17):「できるだけ手打ちの量を減らし、確認はボタン等の選択肢で」。
# doc_type/契約種別は選択肢が固定・少数のため、ボタン(Block Kit)で選べるようにする
# （対象の商談/取引先は候補が多く選択肢化しにくいため、文言＋一覧リンクのみのまま）。
_DOC_TYPE_BUTTON_LABELS = [("quote", "見積書"), ("invoice", "請求書"), ("contract", "契約書")]
_CONTRACT_TYPE_BUTTON_LABELS = [("M", "基本契約"), ("S", "個別契約"), ("N", "秘密保持契約"), ("O", "その他")]


def _field_blocks(field: str, request_id: int) -> dict | None:
    """ボタン付き質問がある項目ならactionsブロックを返す（無ければNone）。
    action_idは"numbering_<kind>:<request_id>:<value>"の3分割固定形式
    （handle_interactiveで同じ形式を前提にパースする）。"""
    if field == "doc_type":
        return {"type": "actions", "block_id": f"numreq_{request_id}", "elements": [
            {"type": "button", "action_id": f"numbering_doctype:{request_id}:{v}", "value": v,
             "text": {"type": "plain_text", "text": lbl}}
            for v, lbl in _DOC_TYPE_BUTTON_LABELS]}
    if field == "contract_type":
        return {"type": "actions", "block_id": f"numreq_{request_id}", "elements": [
            {"type": "button", "action_id": f"numbering_conttype:{request_id}:{v}", "value": v,
             "text": {"type": "plain_text", "text": lbl}}
            for v, lbl in _CONTRACT_TYPE_BUTTON_LABELS]}
    return None


def _respond_url(response_url: str, text: str, blocks: list | None = None) -> None:
    """response_urlにPOSTしてメッセージを追記する（slack_tasks.pyの同名ヘルパーと同型。
    ボタン押下後の返信はresponse_url経由にすることで、chat.postMessage用のBotトークンを
    /slack/interactive側から個別に渡さずに済む＝他Botのボタン処理と同じ設計に揃えている）。"""
    if not response_url:
        return
    import urllib.request
    payload: dict = {"text": text, "replace_original": False}
    if blocks:
        payload["blocks"] = blocks
    try:
        req = urllib.request.Request(
            response_url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"[slack_numbering] _respond_url error: {e}", flush=True)


def _post_question(*, channel: str | None = None, thread_ts: str | None = None,
                   token: str | None = None, response_url: str | None = None,
                   field: str, req: dict, request_id: int, prefix: str = "") -> None:
    """未確定項目を尋ねるメッセージを送る。ボタン化できる項目(doc_type/契約種別)は
    ボタンを添える。response_url指定時はそちら経由（ボタン押下時）、
    無指定ならchat.postMessage（メンション/スレッド返信時）で送る。"""
    text = prefix + _field_question(field, req)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    btn = _field_blocks(field, request_id)
    if btn:
        blocks.append(btn)
    if response_url:
        _respond_url(response_url, text, blocks)
    else:
        _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                   text=text, blocks=blocks)


# ユーザー報告(2026-09-17):「TOPPANの見積書に採番して」で対象が特定できなかった。
# find_deal/find_accountはメッセージ文中に「取引先名(DBの値)がそのまま部分文字列として
# 含まれるか」しか見ておらず、DB側の値が「TOPPAN株式会社」等、法人格の接尾辞付きだと、
# 接尾辞を書かない自然文（「TOPPANの見積書」）とは一致しない。法人格を取り除いた形でも
# 突合できるようフォールバックを追加する（find_deal/find_account自体はNegoCollection等
# 他の呼び出し元もあるため、共通ロジックは変えずここだけ拡張する）。
_CORP_SUFFIX_RE = re.compile(
    r"(株式会社|有限会社|合同会社|合資会社|一般社団法人|公益財団法人|財団法人|社団法人|"
    r"\(株\)|（株）|\(有\)|（有）|Co\.,?\s*Ltd\.?|Incorporated|Inc\.?|Corporation|Corp\.?|LLC)",
    re.IGNORECASE)


def _norm_company(name: str) -> str:
    return _CORP_SUFFIX_RE.sub("", name or "").strip()


def _find_deal_normalized(con, text: str) -> dict | None:
    """find_deal()のフォールバック: 法人格の接尾辞を除いた名前でも一致を試す。"""
    text_l = text.lower()
    rows = con.execute(
        "SELECT d.*, a.name AS account_name FROM deals d "
        "LEFT JOIN accounts a ON d.account_id = a.id "
        "WHERE d.status='open' ORDER BY d.updated_at DESC").fetchall()
    best, best_score = None, 0
    for r in rows:
        d = dict(r)
        for candidate in (d.get("deal_name"), d.get("account_name")):
            if not candidate or candidate == "未定":
                continue
            norm = _norm_company(candidate).lower()
            if norm and len(norm) > best_score and norm in text_l:
                best_score, best = len(norm), d
    return best


def _find_account_normalized(con, text: str) -> dict | None:
    text_l = text.lower()
    rows = con.execute("SELECT * FROM accounts ORDER BY updated_at DESC").fetchall()
    best, best_score = None, 0
    for r in rows:
        a = dict(r)
        norm = _norm_company(a.get("name")).lower()
        if norm and len(norm) > best_score and norm in text_l:
            best_score, best = len(norm), a
    return best


def _get_deal_by_id(con, deal_id: int) -> dict | None:
    row = con.execute(
        "SELECT d.*, a.name AS account_name FROM deals d "
        "LEFT JOIN accounts a ON d.account_id = a.id WHERE d.id=?", (int(deal_id),)).fetchone()
    return dict(row) if row else None


def _get_account_by_id(con, account_id: int) -> dict | None:
    row = con.execute("SELECT * FROM accounts WHERE id=?", (int(account_id),)).fetchone()
    return dict(row) if row else None


def _resolve_entity(con, doc_type: str, text: str) -> tuple[str, int, str] | None:
    """テキストから対象(商談/取引先)を特定する。見つかれば(entity_kind, entity_id, 表示名)を返す。
    quote/contract=商談(SFA番号優先→商談名/取引先名の部分一致)、invoice=取引先(CCC番号優先→
    取引先名の部分一致)。"""
    if doc_type in ("quote", "contract"):
        m = _SFA_ID_RE.search(text)
        if m:
            deal = _get_deal_by_id(con, int(m.group(1)))
            if deal:
                return ("deal", deal["id"], deal.get("deal_name") or "")
        deal = find_deal(con, text) or _find_deal_normalized(con, text)
        if deal:
            return ("deal", deal["id"], deal.get("deal_name") or "")
        return None
    if doc_type == "invoice":
        m = _CCC_ID_RE.search(text)
        if m:
            acc = _get_account_by_id(con, int(m.group(1)))
            if acc:
                return ("account", acc["id"], acc.get("name") or "")
        acc = find_account(con, text) or _find_account_normalized(con, text)
        if acc:
            return ("account", acc["id"], acc.get("name") or "")
        return None
    return None


def _detect_doc_type(text: str) -> str | None:
    for dt, kws in _DOC_TYPE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return dt
    return None


def _detect_contract_type(text: str) -> str | None:
    for ct, kws in _CONTRACT_TYPE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return ct
    if re.search(r"\bM\b", text):
        return "M"
    if re.search(r"\bS\b", text):
        return "S"
    if re.search(r"\bN\b", text):
        return "N"
    return None


def parse_numbering_request(text: str) -> dict:
    """自由文の採番リクエストを解析する。Claude抽出失敗時はキーワード検出にフォールバック
    （_call_claudeはAPIキー未設定/失敗時に"{}"を返す既定挙動のため、フォールバックが必須）。"""
    prompt = (
        "次のSlackメッセージは、社内SFAツールの『見積書/請求書/契約書番号』の発行依頼です。"
        "以下の項目をJSONだけで出力してください（説明不要）。読み取れない項目は\"不明\"にしてください。\n"
        '{"doc_type":"quote(見積書)|invoice(請求書)|contract(契約書)|不明",'
        '"contract_type":"M(基本契約)|S(個別契約)|N(秘密保持契約)|O(その他)|不明",'
        '"entity_hint":"文中の会社名・案件名など(無ければ空文字)",'
        '"is_revision":true/false,'
        '"revision_number_hint":"文中に既存の採番済み番号らしき文字列があればそのまま(無ければ空文字)"}\n\n'
        f"文:\n{text}")
    data: dict = {}
    try:
        raw = _call_claude(prompt) or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    doc_type = data.get("doc_type") or ""
    doc_type = doc_type if doc_type in sfa_db.DOCUMENT_TYPES else (_detect_doc_type(text) or "")
    contract_type = data.get("contract_type") or ""
    contract_type = contract_type if contract_type in sfa_db.CONTRACT_TYPES else (_detect_contract_type(text) or "")
    entity_hint = (data.get("entity_hint") or "").strip() or text
    is_revision = (bool(data.get("is_revision")) or bool(_NUMBER_RE.search(text))
                  or any(kw in text for kw in _REVISION_KEYWORDS))
    revision_hint = (data.get("revision_number_hint") or "").strip()
    if not revision_hint:
        m2 = _NUMBER_RE.search(text)
        if m2:
            revision_hint = m2.group(0)
    return {"doc_type": doc_type or None, "contract_type": contract_type or None,
            "entity_hint": entity_hint, "is_revision": is_revision,
            "revision_number_hint": revision_hint or None}


def _apply_revision_base(req: dict, base: dict) -> None:
    """改版対象の元番号が確定した際、doc_type/entity/contract_typeを元番号から継承する
    （issue_document_number()の改版発行時と同じ考え方＝改版はユーザーに再入力させない）。"""
    req["revision_of"] = base["id"]
    req["doc_type"] = base["docType"]
    req["contract_type"] = base["contractType"]
    req["entity_kind"] = base["entityKind"]
    req["entity_id"] = base["entityId"]


def _missing_field(req: dict) -> str | None:
    if req.get("is_revision"):
        # 改版は元番号さえ特定できれば発行に必要な情報が全て揃う（doc_type/entity/契約種別は
        # 元番号から継承するため、これらを個別に尋ねる必要は無い）。
        return "revision_of" if req.get("revision_of") is None else None
    if not req.get("doc_type"):
        return "doc_type"
    if req.get("doc_type") == "contract" and not req.get("contract_type"):
        return "contract_type"
    if req.get("entity_id") is None:
        return "entity"
    return None


def _issue_and_reply(con, *, channel: str | None = None, thread_ts: str | None = None,
                     req: dict, requested_by: str, token: str | None = None,
                     response_url: str | None = None) -> None:
    saved = sfa_db.issue_document_number(
        con, doc_type=req["doc_type"], entity_id=req["entity_id"],
        contract_type=req.get("contract_type"), revision_of=req.get("revision_of"),
        issued_by=requested_by, issued_via="slack")
    label = sfa_db.DOCUMENT_TYPE_LABELS[req["doc_type"]]
    text = f"✅ {label}番号を発行しました: {saved['fullNumber']}"
    if response_url:
        _respond_url(response_url, text)
    else:
        _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts, text=text)


def handle_mention_numbering(con, channel: str, thread_ts: str, text: str, user_id: str,
                             token: str | None = None) -> None:
    """採番Botへのメンションを処理する。文面から必要項目を抽出し、全て揃えば即発行、
    足りなければ1項目ずつスレッドで聞き返す（numbering_requestsに保留状態を記録）。"""
    parsed = parse_numbering_request(text)
    req: dict = {"doc_type": parsed["doc_type"], "contract_type": parsed["contract_type"],
                "entity_kind": None, "entity_id": None,
                "is_revision": parsed["is_revision"], "revision_of": None}

    if parsed["is_revision"] and parsed["revision_number_hint"]:
        base = sfa_db.find_document_number_by_full_number(con, parsed["revision_number_hint"])
        if base:
            _apply_revision_base(req, base)
    elif parsed["doc_type"]:
        resolved = _resolve_entity(con, parsed["doc_type"], parsed["entity_hint"])
        if resolved:
            req["entity_kind"], req["entity_id"], _label = resolved

    missing = _missing_field(req)
    if missing is None:
        try:
            _issue_and_reply(con, channel=channel, thread_ts=thread_ts, req=req,
                             requested_by=user_id, token=token)
        except sfa_db.DocumentNumberOverflowError as e:
            _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                       text=f"⚠️ {e}")
        return

    created = sfa_db.create_numbering_request(
        con, slack_channel=channel, slack_ts=thread_ts, doc_type=req["doc_type"],
        contract_type=req["contract_type"], entity_kind=req["entity_kind"],
        entity_id=req["entity_id"], entity_hint=parsed["entity_hint"],
        is_revision=req["is_revision"],
        revision_of=req["revision_of"], requested_by=user_id)
    _post_question(channel=channel, thread_ts=thread_ts, token=token, field=missing,
                   req=req, request_id=created["id"])


def handle_message_numbering(con, event: dict, token: str | None = None) -> None:
    """採番リクエストの保留スレッドへの返信を処理する（message イベント）。
    handle_admin_due_reply（slack_tasks.py）と同型: channel+thread_tsで保留リクエストを
    検索し、返信内容で不足項目を1つ埋める→まだ足りなければ次の項目を尋ね返す、を繰り返す。"""
    channel = (event.get("channel") or "").strip()
    ts = (event.get("ts") or "").strip()
    thread_ts = (event.get("thread_ts") or "").strip()
    text = (event.get("text") or "").strip()
    user_id = (event.get("user") or "").strip()
    if not channel or not thread_ts or thread_ts == ts or not text:
        return
    pending = sfa_db.get_pending_numbering_request(con, channel, thread_ts)
    if not pending:
        return

    req = {"doc_type": pending["docType"], "contract_type": pending["contractType"],
           "entity_kind": pending["entityKind"], "entity_id": pending["entityId"],
           "is_revision": pending["isRevision"], "revision_of": pending["revisionOf"]}
    missing = _missing_field(req)
    if missing == "revision_of":
        m = _NUMBER_RE.search(text)
        base = sfa_db.find_document_number_by_full_number(con, m.group(0)) if m else None
        if base:
            _apply_revision_base(req, base)
    elif missing == "doc_type":
        req["doc_type"] = _detect_doc_type(text)
    elif missing == "contract_type":
        req["contract_type"] = _detect_contract_type(text)
    elif missing == "entity" and req["doc_type"]:
        resolved = _resolve_entity(con, req["doc_type"], text)
        if resolved:
            req["entity_kind"], req["entity_id"], _label = resolved

    _continue_pending(con, pending, req, channel=channel, thread_ts=thread_ts, token=token,
                      requested_by=user_id)


def _continue_pending(con, pending: dict, req: dict, *, channel: str | None = None,
                      thread_ts: str | None = None, token: str | None = None,
                      requested_by: str, response_url: str | None = None) -> None:
    """保留リクエストの項目を1つ埋めた後の共通処理（テキスト返信・ボタン押下の両方から
    呼ばれる）。まだ足りなければ次の項目をボタン付きで尋ね返し、揃えば発行する。
    response_url指定時（ボタン押下）はそちら経由で返信する。"""
    if req.get("doc_type") and req.get("entity_id") is None and pending.get("entityHint"):
        # doc_type確定前は対象(商談/取引先)の突合を試みていないため、doc_typeが今回新たに
        # 判明した時点で、保留時に保存しておいた元メッセージ文面(entity_hint)を使って
        # 再突合を試みる。これにより「対象は既に書いてあったのに聞き直す」typing量増加を防ぐ。
        resolved = _resolve_entity(con, req["doc_type"], pending["entityHint"])
        if resolved:
            req["entity_kind"], req["entity_id"], _label = resolved
    still_missing = _missing_field(req)
    sfa_db.resolve_numbering_request(
        con, pending["id"], doc_type=req["doc_type"], contract_type=req["contract_type"],
        entity_kind=req["entity_kind"], entity_id=req["entity_id"], revision_of=req["revision_of"])
    if still_missing is not None:
        _post_question(channel=channel, thread_ts=thread_ts, token=token, response_url=response_url,
                       field=still_missing, req=req, request_id=pending["id"],
                       prefix="🙏 まだ特定できませんでした。")
        return

    sfa_db.resolve_numbering_request(con, pending["id"], status="issued")
    try:
        _issue_and_reply(con, channel=channel, thread_ts=thread_ts, req=req,
                         requested_by=requested_by, token=token, response_url=response_url)
    except sfa_db.DocumentNumberOverflowError as e:
        if response_url:
            _respond_url(response_url, f"⚠️ {e}")
        else:
            _slack_post("chat.postMessage", token=token, channel=channel, thread_ts=thread_ts,
                       text=f"⚠️ {e}")


def handle_interactive(con, payload: dict) -> None:
    """採番Botのボタン(block_actions)押下を処理する。action_idは
    "numbering_doctype:<request_id>:<value>" / "numbering_conttype:<request_id>:<value>"
    の3分割形式（_field_blocksで生成）。/slack/interactiveから他Bot共通で呼ばれる
    （task/desk系のボタンと同じくresponse_url経由で返信するため、Botトークンは不要）。"""
    actions = payload.get("actions") or []
    if not actions:
        return
    act = actions[0]
    action_id = act.get("action_id", "")
    response_url = payload.get("response_url", "")
    parts = action_id.split(":")
    if len(parts) != 3 or parts[0] not in ("numbering_doctype", "numbering_conttype"):
        return
    kind, request_id_s, value = parts
    try:
        request_id = int(request_id_s)
    except ValueError:
        return
    pending = sfa_db.get_numbering_request(con, request_id)
    if not pending or pending["status"] != "pending":
        _respond_url(response_url, "⚠ この確認は既に処理済み、または見つかりませんでした。")
        return

    req = {"doc_type": pending["docType"], "contract_type": pending["contractType"],
           "entity_kind": pending["entityKind"], "entity_id": pending["entityId"],
           "is_revision": pending["isRevision"], "revision_of": pending["revisionOf"]}
    if kind == "numbering_doctype" and value in sfa_db.DOCUMENT_TYPES:
        req["doc_type"] = value
    elif kind == "numbering_conttype" and value in sfa_db.CONTRACT_TYPES:
        req["contract_type"] = value
    else:
        return

    _continue_pending(con, pending, req, response_url=response_url,
                      requested_by=pending.get("requestedBy") or (payload.get("user") or {}).get("id", ""))
