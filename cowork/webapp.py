"""フェーズ2-1：営業情報DBのブラウザ入力画面（標準ライブラリのみ）。

アカウント・商談・活動、リード・ピッチテーマを入力／一覧し、商談をテーマDBへ同期できる。
入力負荷を抑えるためステージ等はプルダウン。挙動安定を優先し外部依存なし。

起動: python scripts/run_webapp.py  → http://localhost:8787
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
from datetime import date, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import sfa_db
from . import leads_csv
from . import deals_csv
from . import csv_utils
from .theme_db import ThemeDBClient
from . import theme_link
from . import dev_project_link

SFA_API_TOKEN = os.environ.get("SFA_API_TOKEN", "")

INPROC_MEMBERS = [
    ("吉江", "takuya.yoshie@inproc.org"),
    ("中島", "yasutaka.nakajima@inproc.org"),
    ("早瀬", "ninsei.hayase@inproc.org"),
    ("岩崎", "eijiro.iwasaki@inproc.org"),
    ("高橋", "masanori.takahashi@inproc.org"),
    ("土屋", "tetsuhiro.tsuchiya@inproc.org"),
    ("戸田", "toda@inproc.org"),
    ("片山", "akito.katayama@inproc.org"),
    ("杉山", "hiroki.sugiyama@inproc.org"),
    ("山端", "rei.yamaberi@inproc.org"),
    ("堀籠", "wataru.horigome@inproc.org"),
]


def _opt(values: list[str], selected: str | None) -> str:
    out = ['<option value=""></option>']
    for v in values:
        sel = " selected" if v == selected else ""
        out.append(f'<option value="{html.escape(v)}"{sel}>{html.escape(v)}</option>')
    return "".join(out)


def _opt_kv(pairs: list[tuple[str, str]], selected: str | None) -> str:
    """(value, label) ペアリストから select options を生成。"""
    out = ['<option value=""></option>']
    for v, label in pairs:
        sel = " selected" if v == selected else ""
        out.append(f'<option value="{html.escape(v)}"{sel}>{html.escape(label)}</option>')
    return "".join(out)


def _esc(v) -> str:
    return "" if v is None else html.escape(str(v))


def _opt_l2(l1: str | None, selected: str | None) -> str:
    """L1に対応するL2選択肢を生成。"""
    opts = ['<option value=""></option>']
    for v in sfa_db.BUSINESS_TYPE_L2_BY_L1.get(l1 or "", []):
        sel = " selected" if v == selected else ""
        opts.append(f'<option value="{html.escape(v)}"{sel}>{html.escape(v)}</option>')
    return "".join(opts)


def _decode_uploaded_csv(file_item) -> str | None:
    """CSV一括取込のファイルアップロード欄（multipart）から取得したバイト列をCSVテキストにする。

    - .xlsx/.xlsは csv_utils.xlsx_to_csv_text() でセル座標ベースに変換する
      （名刺xlsxアップロードは別の固定列レイアウト専用なので、テンプレート形式の
      xlsxをそちらに誤ってアップロードすると列がズレて壊れる。こちらの欄で
      xlsxも直接受け付けることで、その事故を防ぐ）。
    - .csv等のテキストファイルはUTF-8(BOM可)を試し、失敗したらcp932にフォールバック
      （Excel日本語Windowsの「CSV(カンマ区切り)」保存はShift-JISになることが多いため）。
    """
    if not file_item or not isinstance(file_item, tuple):
        return None
    filename, data = file_item
    if not data:
        return None
    if (filename or "").lower().endswith((".xlsx", ".xls")):
        try:
            return csv_utils.xlsx_to_csv_text(data)
        except Exception:
            return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def _chips(values) -> str:
    """CSV一括取込ページ向け: 選択肢一覧をチップ表示するHTMLを生成。"""
    return "".join(
        f'<span style="display:inline-block;background:#eef1f6;border-radius:4px;'
        f'padding:2px 8px;margin:2px 4px 2px 0;font-size:12px">{html.escape(v)}</span>'
        for v in values
    ) or '<span class="muted">(未設定)</span>'


def _ai_prompt_block(prompt_text: str, download_url: str) -> str:
    """CSV一括取込ページ向け: AI代行入力の指示文セクションを生成。"""
    return f"""
    <details style="margin:12px 0;background:#f3f0ff;border-radius:6px;padding:10px 14px">
      <summary style="cursor:pointer;font-weight:600;color:#5b21b6">
        🤖 AIにCSVを代行入力してもらう場合の指示文（コピーしてAIに貼り付け）
      </summary>
      <p class="muted" style="margin:10px 0 6px">
        調査済みの情報などを渡してAIにCSVを作らせる場合は、下記の指示文をそのままAIへの依頼文の先頭に貼り付けてください
        （選択肢は現在のマスタ設定を反映して自動生成されています）。
      </p>
      <p style="margin:0 0 8px">
        <a class="btn sec" href="{download_url}">📥 指示文をMarkdown(.md)でダウンロード</a>
      </p>
      <textarea readonly rows="14" onclick="this.select()"
        style="font-family:monospace;font-size:11px;background:#fff">{html.escape(prompt_text)}</textarea>
    </details>"""


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inproc Salesforce</title>
<style>
 body{{font-family:system-ui,'Segoe UI','Hiragino Kaku Gothic ProN',sans-serif;margin:0;background:#f4f6f9;color:#1d2430}}
 header{{background:#1f2a44;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
 header h1{{font-size:18px;margin:0}} header a{{color:#cdd7ff;text-decoration:none;font-size:14px}}
 main{{max-width:1080px;margin:20px auto;padding:0 16px}}
 .card{{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 h2{{font-size:15px;margin:0 0 12px;color:#3a4760}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid #eef1f5}}
 th{{color:#8893a8;font-weight:600;font-size:12px}}
 tr:hover td{{background:#fafbfd}}
 .stage{{display:inline-block;padding:2px 9px;border-radius:12px;font-size:12px;background:#e8edf7;color:#33406b}}
 .btn{{display:inline-block;background:#2f6fed;color:#fff;border:0;border-radius:7px;padding:8px 14px;font-size:13px;cursor:pointer;text-decoration:none}}
 .btn.sec{{background:#e8edf7;color:#33406b}} .btn.sync{{background:#0c9b6a}}
 label{{display:block;font-size:12px;color:#6b7689;margin:10px 0 3px}}
 input,select,textarea{{width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #d4dae4;border-radius:6px;font-size:13px;font-family:inherit}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}} .full{{grid-column:1/3}}
 .muted{{color:#8893a8;font-size:12px}} .right{{text-align:right}}
 .flash{{background:#e6f7ef;color:#0c6b4a;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px}}
 .s-new{{background:#f1f5f9;color:#475569}} .s-following{{background:#dbeafe;color:#1e40af}}
 .s-meeting{{background:#fef9c3;color:#92400e}} .s-proposal{{background:#ede9fe;color:#5b21b6}}
 .s-won{{background:#dcfce7;color:#166534}} .s-lost{{background:#fee2e2;color:#991b1b}}
 .theme-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:middle;margin-right:4px}}
 .filter-row{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;align-items:center}}
 .filter-row select,.filter-row input{{width:auto}}
 pre{{overflow-x:auto;white-space:pre-wrap;font-size:11px;line-height:1.6}}
 .dash-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:20px}}
 .dash-card{{background:#fff;border-radius:10px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 .dash-card .icon{{font-size:26px;margin-bottom:6px}}
 .dash-card h3{{font-size:15px;margin:0 0 4px;color:#1d2430}}
 .dash-card .desc{{font-size:12px;color:#8893a8;margin:0 0 10px;line-height:1.5}}
 .dash-card .count{{font-size:28px;font-weight:700;color:#2f6fed;margin-bottom:10px}}
 .dash-card .actions{{display:flex;gap:8px;flex-wrap:wrap}}
 .btn.ext{{background:#f3f0ff;color:#5b21b6}}
 @media(max-width:640px){{.grid{{grid-template-columns:1fr}}.full{{grid-column:1}}.hide-sm{{display:none}}table{{display:block;overflow-x:auto}}}}
</style></head><body>
<header>
  <h1>Inproc Salesforce</h1>
  <a href="/">ホーム</a>
  <a href="/deals">商談一覧</a>
  <a href="/leads">リード</a>
  <a href="/hearings" style="opacity:.8;font-size:13px">ヒアリング</a>
  <a href="/dev-projects" style="opacity:.8;font-size:13px">開発案件</a>
  <a href="/email-draft" style="opacity:.8;font-size:13px">メール</a>
  <a href="/masters" style="opacity:.65;font-size:12px">⚙ マスタ編集</a>
  <a href="https://hisho-ohxe.onrender.com/dashboard" target="_blank" style="margin-left:auto;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;color:#e0e8ff;text-decoration:none">Inproc Dashboard ↗</a>
</header>
<main>{flash}{body}</main></body></html>"""


def render(body: str, flash: str = "") -> bytes:
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return PAGE.format(body=body, flash=flash_html).encode("utf-8")


# ── メールパターン管理 ───────────────────────────────────────────────────────────

def email_patterns_page(con) -> str:
    patterns = sfa_db.list_email_patterns(con)
    rows = ""
    for p in patterns:
        cc = _esc(p.get("cc_addresses") or "")
        rows += (
            f'<tr>'
            f'<td><a href="/email-patterns/{p["id"]}/edit"><strong>{_esc(p["name"])}</strong></a></td>'
            f'<td>{_esc(p.get("from_address") or "—")}</td>'
            f'<td class="muted">{cc or "—"}</td>'
            f'<td>{_esc(p.get("subject") or "")}</td>'
            f'<td><form method="post" action="/email-patterns/{p["id"]}/delete" style="display:inline">'
            f'<button class="btn sec" style="font-size:11px;padding:4px 8px" '
            f'onclick="return confirm(\'削除しますか？\')">削除</button></form></td>'
            f'</tr>'
        )
    count = sfa_db.list_leads(con, status=None)
    assigned = sum(1 for l in count if l.get("email_pattern_id"))
    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>メールパターン管理</span>
        <span style="display:flex;gap:8px">
          <a class="btn sec" href="/email-draft">ドラフト生成 ({assigned}件選択中)</a>
          <a class="btn" href="/email-patterns/new">＋パターン追加</a>
        </span>
      </h2>
      <p class="muted" style="margin-bottom:14px">テンプレート変数: <code>{{company}}</code> 社名　<code>{{name}}</code> 氏名　<code>{{title}}</code> 役職</p>
      <table>
        <tr><th>パターン名</th><th>From</th><th>CC</th><th>件名テンプレート</th><th></th></tr>
        {rows or '<tr><td colspan=5 class="muted">パターンがありません。</td></tr>'}
      </table>
    </div>"""


def email_pattern_form(con, pattern=None) -> str:
    pid = pattern["id"] if pattern else None
    action = f"/email-patterns/{pid}/save" if pid else "/email-patterns/save"
    title = "パターン編集" if pid else "パターン追加"
    from_opts = '<option value=""></option>' + "".join(
        f'<option value="{email}"{" selected" if pattern and pattern.get("from_address") == email else ""}>'
        f'{name} &lt;{email}&gt;</option>'
        for name, email in INPROC_MEMBERS
    )
    cc_existing = set((pattern.get("cc_addresses") or "").split(",")) if pattern else set()
    cc_checks = "".join(
        f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0">'
        f'<input type="checkbox" name="cc" value="{email}" id="cc_{email}"'
        f'{" checked" if email in cc_existing else ""} style="flex-shrink:0;width:14px;height:14px">'
        f'<label for="cc_{email}" style="display:inline;font-size:13px;color:#2a3245;margin:0;cursor:pointer">'
        f'{name} &lt;{email}&gt;</label></div>'
        for name, email in INPROC_MEMBERS
    )
    return f"""
    <div class="card" style="max-width:700px">
      <h2>{title}</h2>
      <form method="post" action="{action}">
        <label>パターン名</label>
        <input name="name" required value="{_esc(pattern.get('name') if pattern else '')}">
        <label>From（送信元）</label>
        <select name="from_address">{from_opts}</select>
        <label>CC</label>
        <div style="background:#f4f6f9;border-radius:6px;padding:10px 14px;border:1px solid #d4dae4">{cc_checks}</div>
        <label>件名テンプレート <span class="muted">（{{company}} 等使用可）</span></label>
        <input name="subject" required value="{_esc(pattern.get('subject') if pattern else '')}">
        <label>本文テンプレート <span class="muted">（{{company}} / {{name}} / {{title}} 使用可）</span></label>
        <textarea name="body" rows="12" style="min-height:220px">{_esc(pattern.get('body') if pattern else '')}</textarea>
        <div style="margin-top:14px;display:flex;gap:8px">
          <button class="btn" type="submit">保存</button>
          <a class="btn sec" href="/email-patterns">キャンセル</a>
        </div>
      </form>
    </div>"""


def _render_tmpl(tmpl, lead) -> str:
    return (tmpl or "").replace("{company}", lead.get("company") or "").replace(
        "{name}", lead.get("name") or "").replace("{title}", lead.get("title") or "")


def build_eml_bytes(p, lead) -> bytes:
    """メールパターン + リードからEMLファイルのバイト列を生成する。"""
    subj = _render_tmpl(p.get("subject", ""), lead)
    body_raw = _render_tmpl(p.get("body", ""), lead)
    escaped = html.escape(body_raw)
    escaped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
    escaped = re.sub(
        r'\[([^\]]+)\]',
        r'<span style="background-color:yellow">[\1]</span>',
        escaped,
    )
    body_content = escaped.replace('\n', '<br>')
    full_html = (
        '<html><head><meta charset="UTF-8"></head>'
        '<body style="font-family:Meiryo UI,Meiryo,sans-serif;font-size:13px;line-height:1.7;color:#222">'
        f'{body_content}'
        '</body></html>'
    )
    msg = EmailMessage()
    to_addr = lead.get("email") or ""
    msg['To'] = to_addr
    if p.get("cc_addresses"):
        msg['CC'] = p["cc_addresses"]
    if p.get("from_address"):
        msg['From'] = p["from_address"]
    msg['Subject'] = subj
    msg.set_content(full_html, subtype='html')
    return msg.as_bytes()


def email_draft_page(con, *, status_filter=None, q=None) -> str:
    """メール送信ワークスペース。
    上段: リードごとにパターンを選択（前回値プリセット、変更はAJAX保存）
    下段: 選択済みリードのドラフトを常時表示
    """
    patterns_list = sfa_db.list_email_patterns(con)
    patterns = {p["id"]: p for p in patterns_list}

    # リード取得（デフォルト: converted/lost 除外）
    all_leads = sfa_db.list_leads(con, q=q)
    if status_filter:
        leads = [l for l in all_leads if l.get("lead_status") == status_filter]
    else:
        leads = [l for l in all_leads if l.get("lead_status") not in ("converted", "lost")]

    def _render(tmpl, lead):
        return _render_tmpl(tmpl, lead)

    def _cc_param(p):
        """CC アドレスをOutlook互換の '; ' 区切りでURLエンコードして返す。"""
        cc_raw = p.get("cc_addresses") or ""
        if not cc_raw:
            return ""
        cc_str = "; ".join(a.strip() for a in cc_raw.split(",") if a.strip())
        return "&cc=" + urllib.parse.quote(cc_str, safe="@;, ")

    def _mailto(p, lead):
        to_addr = lead.get("email") or ""
        if not to_addr:
            return ""
        subj = _render(p.get("subject", ""), lead)
        body_plain = _render(p.get("body", ""), lead).replace("**", "")
        qs = "subject=" + urllib.parse.quote(subj) + "&body=" + urllib.parse.quote(body_plain)
        qs += _cc_param(p)
        return "mailto:" + urllib.parse.quote(to_addr, safe="@") + "?" + qs

    def _mailto_noBody(p, lead):
        """To/CC/Subject のみ（bodyなし）のmailtoリンク。クリップボード貼り付け用。"""
        to_addr = lead.get("email") or ""
        if not to_addr:
            return ""
        subj = _render(p.get("subject", ""), lead)
        qs = "subject=" + urllib.parse.quote(subj)
        qs += _cc_param(p)
        return "mailto:" + urllib.parse.quote(to_addr, safe="@") + "?" + qs

    def _clipboard_html(text):
        """クリップボード用HTMLボディ: ** → <strong>、[括弧] → 黄色ハイライト（Outlook互換）。"""
        escaped = html.escape(text)
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
        escaped = re.sub(
            r'\[([^\]]+)\]',
            r'<span style="background:yellow;mso-highlight:yellow">[\1]</span>',
            escaped,
        )
        body_content = escaped.replace('\n', '<br>')
        return (
            '<html><head><meta charset="UTF-8"></head>'
            '<body style="font-family:Meiryo UI,Meiryo,sans-serif;font-size:13px;line-height:1.7;color:#222">'
            f'{body_content}</body></html>'
        )

    def _body_html(text):
        escaped = html.escape(text)
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
        escaped = re.sub(r'\[([^\]]+)\]',
                         r'<mark style="background:#fef08a;border-radius:2px;padding:1px 3px">\1</mark>',
                         escaped)
        return escaped.replace('\n', '<br>')

    # ── 上段: リスト + パターン選択 ──
    pattern_opts_base = '<option value="">— なし —</option>' + "".join(
        f'<option value="{p["id"]}">{_esc(p["name"])}</option>'
        for p in patterns_list
    )

    status_filter_opts = (
        '<option value="">アクティブ（default）</option>'
        + "".join(
            f'<option value="{s}"{" selected" if s == status_filter else ""}>'
            f'{sfa_db.LEAD_STATUS_LABELS[s]}</option>'
            for s in sfa_db.LEAD_STATUSES
        )
    )

    sel_rows = ""
    for ld in leads:
        cur_pid = ld.get("email_pattern_id")
        opts = '<option value="">— なし —</option>' + "".join(
            f'<option value="{p["id"]}"{" selected" if p["id"] == cur_pid else ""}>'
            f'{_esc(p["name"])}</option>'
            for p in patterns_list
        )
        has_email = bool(ld.get("email"))
        email_badge = (f'<span style="color:#16a34a;font-size:11px">✓</span>'
                       if has_email else '<span class="muted" style="font-size:11px">—</span>')
        sel_rows += (
            f'<tr>'
            f'<td><strong>{_esc(ld.get("company"))}</strong>'
            f'<span class="muted" style="font-size:11px;margin-left:6px">{_esc(ld.get("name"))}</span></td>'
            f'<td style="text-align:center">{email_badge}</td>'
            f'<td><select onchange="setLeadPattern({ld["id"]}, this.value)"'
            f' style="font-size:12px;padding:2px 4px;width:100%;">'
            f'{opts}</select></td>'
            f'</tr>'
        )

    if not leads:
        sel_rows = '<tr><td colspan=3 class="muted">リードがありません。</td></tr>'

    # ── 下段: ドラフト ──
    assigned = [l for l in sfa_db.list_leads(con) if l.get("email_pattern_id")]
    by_pattern: dict = {}
    for lead in assigned:
        pid = lead["email_pattern_id"]
        by_pattern.setdefault(pid, []).append(lead)

    preview_data = []
    draft_sections = []
    all_mailto_all = []
    for pid, p_leads in by_pattern.items():
        p = patterns.get(pid)
        if not p:
            continue
        cc_str = _esc(p.get("cc_addresses") or "")
        fr_str = _esc(p.get("from_address") or "")
        mailto_list = [_mailto(p, l) for l in p_leads if l.get("email")]
        all_mailto_all.extend(mailto_list)
        js_links = json.dumps(mailto_list, ensure_ascii=False)
        open_js = f"var lnks={js_links};lnks.forEach(function(u){{window.open(u)}});"
        rows = ""
        for lead in p_leads:
            mailto = _mailto(p, lead)
            subj = _render(p.get("subject", ""), lead)
            body_raw = _render(p.get("body", ""), lead)
            pidx = len(preview_data)
            preview_data.append({
                "label": f'{lead.get("company") or ""} / {lead.get("name") or ""}',
                "subject": subj,
                "body_html": _body_html(body_raw),
                "mailto": mailto,
                "mailto_noBody": _mailto_noBody(p, lead),
                "clipboard_html": _clipboard_html(body_raw),
                "eml_url": f"/email-draft/eml?lead_id={lead['id']}&pattern_id={pid}",
            })
            btn = (
                f'<button class="btn" onclick="showEmailPreview({pidx})" style="font-size:12px;padding:4px 10px">プレビュー</button>'
                if mailto else '<span class="muted" style="font-size:11px">アドレス未登録</span>'
            )
            rows += (
                f'<tr>'
                f'<td><strong>{_esc(lead.get("company"))}</strong>'
                f'<span class="muted" style="font-size:11px;margin-left:6px">{_esc(lead.get("name"))}</span></td>'
                f'<td class="muted">{_esc(lead.get("email") or "—")}</td>'
                f'<td>{_esc(subj)}</td>'
                f'<td>{btn}</td>'
                f'</tr>'
            )
        draft_sections.append(f"""
        <div style="margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px">
            <strong>{_esc(p["name"])}</strong>
            <span style="display:flex;gap:8px;align-items:center">
              <span class="muted" style="font-size:11px">From: {fr_str}{"　CC: "+cc_str if cc_str else ""}</span>
              <button class="btn sec" style="font-size:11px;padding:4px 10px"
                onclick="{html.escape(open_js)}">このパターン全件開く</button>
            </span>
          </div>
          <table><tr><th>会社 / 氏名</th><th>メールアドレス</th><th>件名プレビュー</th><th></th></tr>
          {rows}</table>
        </div>""")

    draft_count = len(assigned)
    draft_body = "".join(draft_sections) if draft_sections else '<p class="muted">パターンが選択されているリードがありません。</p>'
    all_js = json.dumps(all_mailto_all, ensure_ascii=False)
    all_open_js = f"var all={all_js};all.forEach(function(u){{window.open(u)}});"

    no_patterns_note = (
        f'<p class="muted" style="margin-bottom:10px">'
        f'パターンがまだありません。<a href="/email-patterns">パターン管理</a> から作成してください。</p>'
        if not patterns_list else ""
    )

    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>メール送信ワークスペース</span>
        <a class="btn sec" href="/email-patterns" style="font-size:12px">パターン管理</a>
      </h2>
      {no_patterns_note}
      <p class="muted" style="margin-bottom:12px;font-size:12px">
        各リードにパターンを選択してください。選択内容は自動保存され、次回も引き継がれます。
      </p>
      <form class="filter-row" style="margin-bottom:10px">
        <select name="status" onchange="this.form.submit()" style="width:auto">{status_filter_opts}</select>
        <input name="q" placeholder="会社・氏名検索" value="{_esc(q or '')}" style="min-width:140px">
        <button class="btn sec" type="submit">絞り込み</button>
        <a class="btn sec" href="/email-draft">リセット</a>
      </form>
      <div style="overflow-x:auto">
      <table>
        <tr><th>会社 / 氏名</th><th style="text-align:center;width:40px">メール</th><th>パターン選択</th></tr>
        {sel_rows}
      </table>
      </div>
    </div>

    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>ドラフト <span class="muted" style="font-weight:normal">({draft_count}件選択中)</span></span>
        {'<button class="btn" style="font-size:12px" onclick="' + html.escape(all_open_js) + '">全件まとめて開く</button>' if all_mailto_all else ''}
      </h2>
      {draft_body}
    </div>

    <!-- メールプレビューモーダル -->
    <div id="emailPreviewModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;align-items:flex-start;justify-content:center;padding-top:60px">
      <div style="background:#fff;border-radius:10px;max-width:620px;width:92%;max-height:80vh;overflow-y:auto;padding:24px 28px;box-shadow:0 12px 40px rgba(0,0,0,.3)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px">
          <strong id="epLabel" style="font-size:14px;color:#2a3245"></strong>
          <button onclick="closeEmailPreview()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#999;line-height:1">×</button>
        </div>
        <div style="font-size:11px;color:#8a98b4;margin-bottom:3px;letter-spacing:.04em">件名</div>
        <div id="epSubject" style="font-size:13px;font-weight:600;padding:8px 12px;background:#f5f7fa;border-radius:5px;margin-bottom:16px;color:#2a3245"></div>
        <div style="font-size:11px;color:#8a98b4;margin-bottom:3px;letter-spacing:.04em">本文</div>
        <div id="epBody" style="font-size:13px;line-height:1.75;padding:14px 16px;background:#f5f7fa;border-radius:5px;color:#2a3245"></div>
        <div style="margin-top:10px;font-size:11px;color:#aab">
          <mark style="background:#fef08a;padding:1px 4px;border-radius:2px">黄色</mark>＝送信前に書き換えが必要な箇所
        </div>
        <div style="margin-top:20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <a id="epEml" href="#" style="font-size:11px;color:#8a98b4">EMLダウンロード（書式あり）</a>
          <div style="display:flex;gap:8px">
            <button onclick="closeEmailPreview()" class="btn sec" style="font-size:13px">閉じる</button>
            <button id="epOpen" onclick="openWithOutlook()" class="btn" style="font-size:13px">Outlookで開く</button>
          </div>
        </div>
      </div>
    </div>

    <script>
    var _epData = {json.dumps(preview_data)};
    var _epCurrent = null;
    function showEmailPreview(idx) {{
      _epCurrent = _epData[idx];
      document.getElementById('epLabel').textContent = _epCurrent.label;
      document.getElementById('epSubject').textContent = _epCurrent.subject;
      document.getElementById('epBody').innerHTML = _epCurrent.body_html;
      document.getElementById('epEml').href = _epCurrent.eml_url || '#';
      var m = document.getElementById('emailPreviewModal');
      m.style.display = 'flex';
    }}
    function closeEmailPreview() {{
      document.getElementById('emailPreviewModal').style.display = 'none';
    }}
    document.getElementById('emailPreviewModal').addEventListener('click', function(e) {{
      if (e.target === this) closeEmailPreview();
    }});
    function openWithOutlook() {{
      if (!_epCurrent) return;
      var mailto = _epCurrent.mailto_noBody || _epCurrent.mailto;
      // execCommand('copy') でレンダリング済みDOMをコピー → background-color が Outlook に伝わる
      try {{
        var parser = new DOMParser();
        var doc = parser.parseFromString(_epCurrent.clipboard_html, 'text/html');
        var temp = document.createElement('div');
        var bodyStyle = doc.body.getAttribute('style') || '';
        temp.style.cssText = bodyStyle + ';position:fixed;left:-9999px;top:0;width:600px;pointer-events:none';
        temp.innerHTML = doc.body.innerHTML;
        document.body.appendChild(temp);
        var range = document.createRange();
        range.selectNodeContents(temp);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        document.execCommand('copy');
        sel.removeAllRanges();
        document.body.removeChild(temp);
        window.location.href = mailto;
        setTimeout(function() {{ showToast('本文をコピーしました。Outlookの本文欄に Ctrl+V で貼り付けてください'); }}, 600);
      }} catch(e) {{
        window.location.href = _epCurrent.mailto;
        setTimeout(function() {{ showToast('Outlookを開きます（書式なし）'); }}, 600);
      }}
    }}
    function showToast(msg) {{
      var t = document.createElement('div');
      t.textContent = msg;
      t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#2a3245;color:#fff;padding:10px 20px;border-radius:6px;font-size:13px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,.3);white-space:nowrap';
      document.body.appendChild(t);
      setTimeout(function(){{ t.style.opacity='0'; t.style.transition='opacity .4s'; setTimeout(function(){{t.remove()}},400); }}, 3500);
    }}
    function setLeadPattern(id, patternId) {{
      fetch('/leads/' + id + '/set_pattern', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
        body: 'pattern_id=' + encodeURIComponent(patternId)
      }}).then(r => r.json()).then(d => {{
        if (d.ok) {{ location.reload(); }} else {{ alert('エラー: ' + (d.error||'')); }}
      }}).catch(() => alert('通信エラー'));
    }}
    </script>"""


# ── ダッシュボード ──────────────────────────────────────────────────────────────

def dashboard_page(con) -> str:
    deals = sfa_db.list_deals(con, status="open")
    accounts = sfa_db.list_accounts(con)
    leads = sfa_db.list_leads(con)
    sheet_id = os.environ.get("SALES_SHEET_ID", "")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else "#"
    hisho_url = os.environ.get("THEME_API_URL", "https://hisho-ohxe.onrender.com").rstrip("/") + "/dashboard"

    # 当日〜1週間以内に次回MSがある商談
    today_str = date.today().isoformat()
    week_later_str = (date.today() + timedelta(days=7)).isoformat()
    recent_deals = sorted(
        [d for d in deals
         if d.get("next_milestone_date")
         and today_str <= d["next_milestone_date"] <= week_later_str],
        key=lambda d: d["next_milestone_date"],
    )
    recent_rows = ""
    for d in recent_deals:
        ms_raw = d.get("next_milestone_date", "")
        if ms_raw == today_str:
            ms = f'<span style="color:#dc2626;font-weight:700">今日 {_esc(ms_raw)}</span>'
        else:
            ms = _esc(ms_raw)
        if d.get("next_milestone_label"):
            ms += f'<br><span class="muted" style="font-size:.85em">{_esc(d["next_milestone_label"])}</span>'
        recent_rows += (
            f'<tr><td><a href="/deal/{d["id"]}">{_esc(d.get("account_name"))}</a></td>'
            f'<td>{_esc(d.get("deal_name"))}</td>'
            f'<td><span class="stage">{_esc(d.get("stage"))}</span></td>'
            f'<td>{ms}</td></tr>'
        )

    return f"""
    <div class="dash-grid">
      <div class="dash-card">
        <div class="icon">🎯</div>
        <h3>リード</h3>
        <p class="desc">展示会・SNS・初回接触など、<br>まだ関係が薄い相手の接触記録。<br><span style="color:#2f6fed;font-size:11px">紹介・既存顧客は商談から直接追加</span></p>
        <div class="count">{len(leads)}</div>
        <div class="actions">
          <a class="btn sec" href="/leads">一覧</a>
          <a class="btn" href="/leads/new">＋追加</a>
        </div>
      </div>
      <div class="dash-card">
        <div class="icon">💼</div>
        <h3>商談</h3>
        <p class="desc">Sales案件の進捗管理。<br><span style="color:#2f6fed;font-size:11px">紹介・既存顧客はここから直接追加。<br>リードからの商談化はリード画面から。</span></p>
        <div class="count">{len(deals)}</div>
        <div class="actions">
          <a class="btn sec" href="/deals">一覧</a>
          <a class="btn" href="/deal/new">＋追加</a>
        </div>
      </div>
      <div class="dash-card">
        <div class="icon">📝</div>
        <h3>活動履歴</h3>
        <p class="desc">面談・電話・メール等の記録。<br>商談の現状メモ・次回MSも同時更新できます。</p>
        <div class="count" style="font-size:18px;padding-top:6px">商談を<br>選んで記録</div>
        <div class="actions" style="margin-top:10px">
          <a class="btn" href="/activity/new">＋活動を追加</a>
        </div>
      </div>
      <div class="dash-card">
        <div class="icon">🏢</div>
        <h3>アカウント</h3>
        <p class="desc">取引先企業。基本はリード追加時に自動作成されます。<br>手動追加は既存企業との取引開始時のみ。</p>
        <div class="count">{len(accounts)}</div>
        <div class="actions">
          <a class="btn sec" href="/accounts">一覧</a>
          <a class="btn sec" href="/account/new">＋手動追加</a>
        </div>
      </div>
      <div class="dash-card">
        <div class="icon">🚚</div>
        <h3>Delivery案件</h3>
        <p class="desc">稼働中・完了済のDelivery案件はスプシで管理。<br>編集後はsync_cli.pyでテーマDBへ反映。</p>
        <div class="count" style="font-size:14px;color:#5b21b6;padding-top:4px">スプシで管理</div>
        <div class="actions" style="margin-top:10px">
          <a class="btn ext" href="{sheet_url}" target="_blank">スプシを開く ↗</a>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>進行中の商談（直近1週間）</h2>
      <table>
        <tr><th>アカウント</th><th>案件名</th><th>ステージ</th><th>次回MS</th></tr>
        {recent_rows or '<tr><td colspan=4 class=muted>今週1週間以内に次回MSがある商談はありません</td></tr>'}
      </table>
        <p style="margin-top:10px">
        <a class="btn sec" href="/deals">すべての商談を見る</a>
        <a class="btn ext" href="{hisho_url}" target="_blank" style="margin-left:8px">Inproc Dashboard ↗</a>
      </p>
    </div>
    <div style="text-align:right;margin-top:-10px;margin-bottom:6px">
      <a class="btn sec" href="/masters" style="font-size:12px;padding:5px 10px;opacity:0.7">⚙ 入力マスタの編集</a>
    </div>"""


def masters_page(con) -> str:
    """入力マスタ編集ページ。各リストの選択肢を追加・削除・並び替えできる。"""
    cards = []
    for key, label in sfa_db.MASTER_LABELS.items():
        values = sfa_db.get_master_list(con, key)
        items_html = "".join(
            f'<div class="master-item" draggable="true" data-key="{html.escape(key)}" data-idx="{i}">'
            f'<span class="drag-handle" title="ドラッグで並び替え">⠿</span>'
            f'<span class="item-label">{html.escape(v)}</span>'
            f'<button type="button" onclick="delItem(\'{html.escape(key)}\',{i})" '
            f'style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px;padding:0 4px">✕</button>'
            f'</div>'
            for i, v in enumerate(values)
        )
        hidden_inputs = "".join(
            f'<input type="hidden" name="{html.escape(key)}[]" value="{html.escape(v)}">'
            for v in values
        )
        cards.append(f"""
        <div class="card" id="master_{key}">
          <h2>{label}</h2>
          <div id="items_{key}" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
            {items_html}
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <input id="new_{key}" placeholder="新しい選択肢を追加" style="max-width:200px">
            <button type="button" class="btn sec" onclick="addItem('{html.escape(key)}')">追加</button>
          </div>
          <div id="hidden_{key}">{hidden_inputs}</div>
        </div>""")

    return f"""
    <div class="card" style="background:#f0f4f8;border:1.5px solid #d4dae4">
      <h2>⚙ 入力マスタの編集</h2>
      <p class="muted">各項目の選択肢を編集できます。変更は「すべて保存」ボタンで反映されます。</p>
    </div>
    <form method="post" action="/masters/save" id="master_form">
      {''.join(cards)}
      <p><button class="btn">すべて保存</button>
         <a class="btn sec" href="/">キャンセル</a></p>
    </form>
    <style>
      .master-item{{display:inline-flex;align-items:center;background:#e8edf7;border-radius:20px;padding:3px 10px;font-size:13px;gap:4px;cursor:default;user-select:none}}
      .master-item.drag-over{{outline:2px dashed #2f6fed;background:#dbeafe}}
      .drag-handle{{cursor:grab;color:#aab;font-size:15px;line-height:1}}
    </style>
    <script>
    function rebuildHidden(key) {{
      const container = document.getElementById('items_' + key);
      const hidden = document.getElementById('hidden_' + key);
      const items = Array.from(container.querySelectorAll('.master-item'));
      hidden.innerHTML = items.map(el =>
        `<input type="hidden" name="${{key}}[]" value="${{el.querySelector('.item-label').textContent}}">`
      ).join('');
      items.forEach((el, i) => {{
        el.dataset.idx = i;
        el.querySelector('button').setAttribute('onclick', `delItem('${{key}}',${{i}})`);
      }});
    }}
    function delItem(key, idx) {{
      const container = document.getElementById('items_' + key);
      Array.from(container.querySelectorAll('.master-item'))[idx].remove();
      rebuildHidden(key);
    }}
    function addItem(key) {{
      const input = document.getElementById('new_' + key);
      const val = input.value.trim();
      if (!val) return;
      const container = document.getElementById('items_' + key);
      const idx = container.querySelectorAll('.master-item').length;
      container.insertAdjacentHTML('beforeend',
        `<div class="master-item" draggable="true" data-key="${{key}}" data-idx="${{idx}}">` +
        `<span class="drag-handle" title="ドラッグで並び替え">⠿</span>` +
        `<span class="item-label">${{val}}</span>` +
        `<button type="button" onclick="delItem('${{key}}',${{idx}})" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px;padding:0 4px">✕</button>` +
        `</div>`
      );
      rebuildHidden(key);
      input.value = '';
      initDrag(key);
    }}
    function initDrag(key) {{
      const container = document.getElementById('items_' + key);
      container.querySelectorAll('.master-item[draggable]').forEach(item => {{
        item.ondragstart = e => {{
          e.dataTransfer.effectAllowed = 'move';
          container._dragging = item;
        }};
        item.ondragover = e => {{
          e.preventDefault();
          const dragging = container._dragging;
          if (!dragging || dragging === item) return;
          const rect = item.getBoundingClientRect();
          if (e.clientY < rect.top + rect.height / 2) container.insertBefore(dragging, item);
          else container.insertBefore(dragging, item.nextSibling);
        }};
        item.ondragend = () => {{ rebuildHidden(key); container._dragging = null; }};
      }});
    }}
    document.addEventListener('DOMContentLoaded', () => {{
      {'; '.join(f"initDrag('{html.escape(key)}')" for key in sfa_db.MASTER_LABELS)}
    }});
    </script>"""


def activity_deal_picker(con) -> str:
    deals = sfa_db.list_deals(con, status="open")
    rows = "".join(
        f'<tr style="cursor:pointer" onclick="location.href=\'/deal/{d["id"]}#activity\'">'
        f'<td><a href="/deal/{d["id"]}">{_esc(d.get("account_name"))}</a></td>'
        f'<td>{_esc(d.get("deal_name"))}</td>'
        f'<td><span class="stage">{_esc(d.get("stage"))}</span></td>'
        f'<td>{_esc(d.get("next_milestone_date") or "—")}</td></tr>'
        for d in deals
    ) or '<tr><td colspan=4 class=muted>進行中の商談がありません</td></tr>'
    return f"""
    <div class="card">
      <h2>活動を追加する商談を選択</h2>
      <p class="muted">行をクリックすると商談ページへ移動し、活動履歴を追加できます。</p>
      <table>
        <tr><th>アカウント</th><th>案件名</th><th>ステージ</th><th>次回MS</th></tr>
        {rows}
      </table>
    </div>"""


# ── 既存ページ（商談・アカウント）─────────────────────────────────────────────

def home_page(con, owner: str | None = None, status_filter: str | None = None,
              stage_filter: str | None = None) -> str:
    # デフォルトでclosedを除外（NULLもopenとして扱う）。"all"は全件表示
    effective_status = None if status_filter == "all" else (status_filter or "open")
    deals = sfa_db.list_deals(con, status=effective_status, owner=owner, stage=stage_filter)
    pending_sync = con.execute("SELECT COUNT(*) c FROM deals WHERE theme_id IS NULL").fetchone()["c"]
    owners = sfa_db.get_master_list(con, "owners")
    stages = sfa_db.get_master_list(con, "deal_stages")
    biz_l1_list = sfa_db.get_master_list(con, "business_type_l1")
    owner_opts = '<option value="">全担当</option>' + "".join(
        f'<option value="{html.escape(o)}"{" selected" if o == owner else ""}>{html.escape(o)}</option>'
        for o in owners
    )
    status_opts = (
        f'<option value="all"{"  selected" if status_filter=="all" else ""}>全て（クローズ含む）</option>'
        + f'<option value="open"{"  selected" if status_filter is None or status_filter=="open" else ""}>進行中のみ</option>'
        + f'<option value="closed"{" selected" if status_filter=="closed" else ""}>クローズ済のみ</option>'
    )
    stage_opts = '<option value="">全ステージ</option>' + "".join(
        f'<option value="{html.escape(s)}"{" selected" if s == stage_filter else ""}>{html.escape(s)}</option>'
        for s in stages
    )
    filter_row = f"""<form method="get" action="/deals" class="filter-row">
      <select name="owner">{owner_opts}</select>
      <select name="status">{status_opts}</select>
      <select name="stage">{stage_opts}</select>
      <button class="btn sec" type="submit">絞り込み</button>
      <a class="btn sec" href="/deals">リセット</a>
    </form>"""
    def _deal_inline_select(deal_id, field, values, current, sel_id=None):
        opts = "".join(
            f'<option value="{html.escape(v)}"{" selected" if v == current else ""}>{html.escape(v)}</option>'
            for v in values
        )
        id_attr = f' id="{sel_id}"' if sel_id else ""
        onchange = (f"updateDealL1({deal_id}, this.value)" if field == "business_type_l1"
                    else f"updateDealField({deal_id}, '{field}', this.value)")
        return (f'<select{id_attr} onchange="{onchange}"'
                f' style="font-size:11px;padding:1px 2px;max-width:90px">'
                f'<option value=""></option>{opts}</select>')

    # バルク編集用JSオブジェクト構築
    deal_bulk_options = {
        "stage": [["", "（変更なし）"]] + [[s, s] for s in stages],
        "owner": [["", "（変更なし）"]] + [[o, o] for o in owners],
        "sub_owner": [["", "（変更なし）"]] + [[o, o] for o in owners],
        "business_type_l1": [["", "（変更なし）"]] + [[v, v] for v in biz_l1_list],
    }
    deal_bulk_options_json = json.dumps(deal_bulk_options, ensure_ascii=False)

    rows = []
    for d in deals:
        val = d.get("value_lumpsum") or d.get("value_recurring") or ""
        linked = "🔗" if d.get("theme_id") else "—"
        ms = ""
        if d.get("next_milestone_date"):
            ms = _esc(d["next_milestone_date"])
            if d.get("next_milestone_label"):
                ms += f'<br><span class="muted" style="font-size:.85em">{_esc(d["next_milestone_label"])}</span>'
        elif d.get("next_milestone_label"):
            ms = f'<span class="muted">{_esc(d["next_milestone_label"])}</span>'
        sel_stage = _deal_inline_select(d["id"], "stage", stages, d.get("stage") or "")
        sel_owner = _deal_inline_select(d["id"], "owner", owners, d.get("owner") or "")
        sel_sub_owner = _deal_inline_select(d["id"], "sub_owner", owners, d.get("sub_owner") or "")
        sel_biz_l1 = _deal_inline_select(d["id"], "business_type_l1", biz_l1_list, d.get("business_type_l1") or "")
        biz_l2_values = sfa_db.BUSINESS_TYPE_L2_BY_L1.get(d.get("business_type_l1") or "", [])
        sel_biz_l2 = _deal_inline_select(d["id"], "business_type_l2", biz_l2_values, d.get("business_type_l2") or "", sel_id=f"l2_{d['id']}")
        did = d["id"]
        cb_val = d.get("client_budget") or ""
        vl_val = d.get("value_lumpsum") or ""
        inp_client_budget = (
            f'<input type="text" value="{_esc(cb_val)}"'
            f' onchange="updateDealField({did}, \'client_budget\', this.value)"'
            f' style="font-size:11px;padding:1px 2px;width:75px">'
        )
        inp_value_lumpsum = (
            f'<input type="number" step="0.1" value="{_esc(vl_val)}"'
            f' onchange="updateDealField({did}, \'value_lumpsum\', this.value)"'
            f' style="font-size:11px;padding:1px 2px;width:75px">'
        )
        rows.append(
            f'<tr>'
            f'<td style="width:32px"><input type="checkbox" name="ids" value="{d["id"]}"></td>'
            f'<td class="muted" style="font-size:.8em;color:#888;white-space:nowrap">#{d["id"]}</td>'
            f'<td><a href="/deal/{d["id"]}">{_esc(d.get("account_name"))}</a></td>'
            f'<td>{_esc(d.get("deal_name"))}</td>'
            f'<td>{sel_stage}</td>'
            f'<td>{sel_owner}</td>'
            f'<td>{sel_sub_owner}</td>'
            f'<td>{sel_biz_l1}</td>'
            f'<td>{sel_biz_l2}</td>'
            f'<td>{inp_client_budget}</td>'
            f'<td>{inp_value_lumpsum}</td>'
            f'<td>{ms}</td>'
            f'<td class="right" title="テーマDB連携">{linked}</td></tr>'
        )
    accounts = sfa_db.list_accounts(con)
    acc_rows = "".join(
        f'<tr><td><a href="/account/{a["id"]}">{_esc(a["name"])}</a></td>'
        f'<td>{_esc(a.get("industry"))}</td><td>{_esc(a.get("company_size"))}</td></tr>'
        for a in accounts
    )
    sync_btn = (
        f'<form method="post" action="/deals/sync_pending" style="display:inline">'
        f'<button class="btn sec" style="font-size:12px" '
        f'onclick="return confirm(\'テーマDB未同期の商談{pending_sync}件をバックグラウンドで同期します。よろしいですか？\')">'
        f'⏳ テーマDB未同期 {pending_sync}件を同期</button></form>'
        if pending_sync else ""
    )
    return f"""
    <div class="card"><h2 style="display:flex;justify-content:space-between;align-items:center">
      <span>商談 ({len(deals)})</span>
      <span style="display:flex;gap:8px">
        {sync_btn}
        <a class="btn sec" href="/deals/import">CSV取込</a>
        <a class="btn" href="/deal/new">＋商談追加</a>
      </span>
    </h2>
    {filter_row}
    <form id="deal_bulk_form" method="post" action="/deals/bulk_edit">
    <div style="overflow-x:auto">
    <table style="min-width:900px"><tr>
      <th style="width:28px"><input type="checkbox" id="deal_chk_all" title="全選択"
            onchange="document.querySelectorAll('#deal_bulk_form [name=ids]').forEach(c=>c.checked=this.checked)"></th>
      <th>#</th><th>アカウント</th><th>案件名</th><th>ステージ</th><th>主担当</th><th>サブ担当</th>
      <th>種別L1</th><th>種別L2</th>
      <th>予算<br><span style="font-size:10px;font-weight:normal;color:#8893a8">(万円)</span></th>
      <th>提案総額<br><span style="font-size:10px;font-weight:normal;color:#8893a8">(万円)</span></th>
      <th>次回MS</th><th class="right">連携</th></tr>
    {''.join(rows) or '<tr><td colspan=13 class=muted>商談がありません。</td></tr>'}
    </table></div>
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap">
      <select id="deal_bulk_field" name="field" style="width:auto">
        <option value="stage">ステージ</option>
        <option value="owner">主担当</option>
        <option value="sub_owner">サブ担当</option>
        <option value="business_type_l1">事業種別L1</option>
      </select>
      <select id="deal_bulk_value" name="value" style="width:auto"></select>
      <button class="btn sec" type="submit">選択した件を一括変更</button>
    </div>
    </form>
    </div>
    <div class="card"><h2>アカウント ({len(accounts)})</h2>
    <table><tr><th>企業名</th><th>業界</th><th>企業規模</th></tr>
    {acc_rows or '<tr><td colspan=3 class=muted>まだアカウントがありません。</td></tr>'}
    </table></div>
    <script>
    const DEAL_BULK_OPTIONS = {deal_bulk_options_json};
    const DEAL_L2_MAP = {json.dumps(sfa_db.BUSINESS_TYPE_L2_BY_L1, ensure_ascii=False)};
    function updateDealL1(id, l1_value) {{
      updateDealField(id, 'business_type_l1', l1_value);
      var l2sel = document.getElementById('l2_' + id);
      if (l2sel) {{
        var opts = DEAL_L2_MAP[l1_value] || [];
        l2sel.innerHTML = '<option value=""></option>' +
          opts.map(function(v) {{ return '<option value="' + v + '">' + v + '</option>'; }}).join('');
        l2sel.value = '';
        updateDealField(id, 'business_type_l2', '');
      }}
    }}
    function updateDealField(id, field, value) {{
      fetch('/deal/' + id + '/field', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
        body: 'field=' + encodeURIComponent(field) + '&value=' + encodeURIComponent(value)
      }}).then(r => r.json()).then(d => {{
        if (!d.ok) alert('更新エラー');
      }}).catch(() => alert('通信エラー'));
    }}
    function repopulateDealBulkValue() {{
      var field = document.getElementById('deal_bulk_field').value;
      var opts = DEAL_BULK_OPTIONS[field] || [];
      var sel = document.getElementById('deal_bulk_value');
      sel.innerHTML = opts.map(function(pair) {{
        return '<option value="' + pair[0] + '">' + pair[1] + '</option>';
      }}).join('');
    }}
    document.getElementById('deal_bulk_field').addEventListener('change', repopulateDealBulkValue);
    repopulateDealBulkValue();
    </script>
    """


def account_form(con, acc=None) -> str:
    acc = acc or {}
    cancel_url = f"/account/{acc['id']}" if acc.get("id") else "/accounts"
    return f"""
    <div class="card"><h2>{'アカウント編集' if acc.get('id') else '新規アカウント'}</h2>
    <form method="post" action="/account/save">
      <input type="hidden" name="id" value="{_esc(acc.get('id'))}">
      <label>企業名 *</label><input name="name" required value="{_esc(acc.get('name'))}">
      <div class="grid">
        <div><label>業界</label><input name="industry" value="{_esc(acc.get('industry'))}"></div>
        <div><label>企業規模</label>
          <select name="company_size">{_opt(sfa_db.COMPANY_SIZES, acc.get('company_size'))}</select>
        </div>
      </div>
      <label>メモ</label><textarea name="note" rows="2">{_esc(acc.get('note'))}</textarea>
      <p><button class="btn">保存</button> <a class="btn sec" href="{cancel_url}">キャンセル</a></p>
    </form></div>"""


def accounts_page(con) -> str:
    """アカウント一覧ページ。"""
    accounts = sfa_db.list_accounts(con)
    deal_counts = {
        r["account_id"]: r["cnt"]
        for r in con.execute(
            "SELECT account_id, COUNT(*) as cnt FROM deals WHERE account_id IS NOT NULL GROUP BY account_id"
        )
    }
    rows_html = "".join(
        f'<tr>'
        f'<td style="width:32px"><input type="checkbox" name="ids" value="{a["id"]}"></td>'
        f'<td><a href="/account/{a["id"]}">{_esc(a["name"])}</a></td>'
        f'<td>{_esc(a.get("industry")) or "<span class=muted>―</span>"}</td>'
        f'<td>{_esc(a.get("company_size")) or "<span class=muted>―</span>"}</td>'
        f'<td class="right muted">{deal_counts.get(a["id"], 0)}</td>'
        f'</tr>'
        for a in accounts
    ) or '<tr><td colspan=5 class=muted>アカウントがありません。</td></tr>'
    deal_counts_json = json.dumps(deal_counts, ensure_ascii=False)
    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center">
        <span>アカウント一覧 ({len(accounts)})</span>
        <a class="btn" href="/account/new">＋手動追加</a>
      </h2>
      <form id="acc_bulk_form" method="post" action="/accounts/bulk_delete">
      <div style="overflow-x:auto">
      <table>
        <tr><th style="width:32px"><input type="checkbox" id="acc_chk_all" title="全選択"
              onchange="document.querySelectorAll('#acc_bulk_form [name=ids]').forEach(c=>c.checked=this.checked)"></th>
            <th>企業名</th><th>業界</th><th>企業規模</th><th class="right">商談数</th></tr>
        {rows_html}
      </table>
      <div style="margin-top:10px">
        <button class="btn" type="button" onclick="accBulkDelete()"
          style="background:#c53030;border-color:#c53030;color:#fff">選択した件を削除</button>
      </div>
      </div>
      </form>
    </div>
    <script>
    const ACC_DEAL_COUNTS = {deal_counts_json};
    function accBulkDelete() {{
      var ids = Array.from(document.querySelectorAll('#acc_bulk_form [name=ids]:checked')).map(function(c){{return c.value;}});
      if (!ids.length) {{ alert('削除するアカウントを選択してください。'); return; }}
      var dealTotal = ids.reduce(function(sum, id){{ return sum + (ACC_DEAL_COUNTS[id] || 0); }}, 0);
      var msg = ids.length + '件のアカウントを削除します。この操作は取り消せません。';
      if (dealTotal > 0) msg += '\\n※紐づく商談も合計' + dealTotal + '件まとめて削除されます。';
      if (!confirm(msg)) return;
      document.getElementById('acc_bulk_form').submit();
    }}
    </script>"""


def account_detail(con, acc: dict) -> str:
    """アカウント詳細ページ（関連商談含む）。"""
    deals = [dict(r) for r in con.execute(
        "SELECT id, deal_name, stage, owner, sub_owner, status FROM deals WHERE account_id=? ORDER BY id DESC",
        (acc["id"],)
    )]
    deal_rows = "".join(
        f'<tr>'
        f'<td><a href="/deal/{d["id"]}">{_esc(d["deal_name"])}</a></td>'
        f'<td>{_esc(d.get("stage")) or "<span class=muted>―</span>"}</td>'
        f'<td>{_esc(d.get("owner")) or "<span class=muted>―</span>"}</td>'
        f'<td>{_esc(d.get("sub_owner")) or "<span class=muted>―</span>"}</td>'
        f'<td><span class="muted">{_esc(d.get("status") or "open")}</span></td>'
        f'</tr>'
        for d in deals
    ) or '<tr><td colspan=5 class=muted>関連商談がありません</td></tr>'
    note_html = (
        f'<p style="margin-top:8px;white-space:pre-wrap;font-size:13px">{_esc(acc.get("note"))}</p>'
        if acc.get("note") else ""
    )
    _del_msg = f"このアカウントを削除しますか？"
    if deals:
        _del_msg += f" 紐づく商談{len(deals)}件も一緒に削除されます。"
    _del_msg += " この操作は取り消せません。"
    return f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h2 style="margin:0">{_esc(acc["name"])}</h2>
        <span style="display:flex;gap:8px">
          <a class="btn sec" href="/account/{acc['id']}/edit" style="font-size:12px;padding:5px 10px">編集</a>
          <form method="post" action="/account/{acc['id']}/delete" style="display:inline;margin:0">
            <button class="btn" style="background:#c53030;border-color:#c53030;color:#fff;font-size:12px;padding:5px 10px"
              onclick="return confirm('{_del_msg}')">削除</button>
          </form>
        </span>
      </div>
      <div class="grid" style="margin-top:12px">
        <div><label>業界</label><p style="margin:2px 0">{_esc(acc.get("industry")) or "―"}</p></div>
        <div><label>企業規模</label><p style="margin:2px 0">{_esc(acc.get("company_size")) or "―"}</p></div>
      </div>
      {note_html}
    </div>
    <div class="card">
      <h2>関連商談 ({len(deals)})</h2>
      <table><tr><th>案件名</th><th>ステージ</th><th>主担当</th><th>サブ担当</th><th>状態</th></tr>
      {deal_rows}
      </table>
      <p style="margin-top:12px">
        <a class="btn" href="/deal/new">＋商談追加</a>
        <a class="btn sec" href="/accounts" style="margin-left:8px">一覧へ</a>
      </p>
    </div>"""


def deal_form(con, deal=None) -> str:
    deal = deal or {}
    accounts = sfa_db.list_accounts(con)
    acc_opts = ['<option value=""></option>']
    for a in accounts:
        sel = " selected" if a["id"] == deal.get("account_id") else ""
        acc_opts.append(f'<option value="{a["id"]}"{sel}>{html.escape(a["name"])}</option>')

    # 新規作成時のみリード引用セクション
    lead_picker_html = ""
    if not deal.get("id"):
        open_leads = [l for l in sfa_db.list_leads(con)
                      if l.get("lead_status") not in ("converted", "lost")]
        acc_by_name = {a["name"]: a["id"] for a in accounts}
        leads_data = json.dumps([{
            "id": l["id"],
            "account_id": acc_by_name.get(l.get("company", ""), ""),
            "owner": l.get("assigned_to") or "",
            "lead_pattern": _SOURCE_TO_LP.get(l.get("source", "other"), "na"),
            "notes": l.get("notes") or "",
        } for l in open_leads], ensure_ascii=False)
        lead_opts = '<option value="">（リードを引用しない）</option>' + "".join(
            f'<option value="{l["id"]}">{html.escape(l.get("company","?"))} / {html.escape(l.get("name","?"))}</option>'
            for l in open_leads
        )
        lead_picker_html = f"""
        <div style="background:#f0f6ff;border-radius:8px;padding:12px 14px;margin-bottom:14px">
          <label style="color:#2f6fed;font-weight:600;font-size:13px">リードから引用</label>
          <select id="lead_ref" onchange="applyLead()" style="margin-top:6px">{lead_opts}</select>
          <p class="muted" style="margin-top:4px">選ぶとアカウント・担当・経路・メモが自動入力されます</p>
        </div>
        <script>
        const _LEADS = {leads_data};
        function applyLead() {{
          const lid = parseInt(document.getElementById('lead_ref').value);
          if (!lid) return;
          const l = _LEADS.find(x => x.id === lid);
          if (!l) return;
          if (l.account_id) document.querySelector('[name=account_id]').value = l.account_id;
          document.querySelector('[name=owner]').value = l.owner;
          document.querySelector('[name=lead_pattern]').value = l.lead_pattern;
          document.querySelector('[name=note]').value = l.notes;
        }}
        </script>"""

    hearing_html = ""
    if deal.get("id"):
        n_hearings = sfa_db.count_hearing_results(con, deal["id"])
        if n_hearings:
            latest = sfa_db.list_hearing_results(con, deal["id"])[0]
            hearing_html = f"""
        <div class="card">
          <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <span>初回ヒアリング</span>
            <span style="display:flex;gap:8px">
              <a class="btn" href="/hearing/result/{latest['id']}">📋 初回ヒアリング結果（{n_hearings}件）</a>
              <a class="btn sec" href="/hearing/new?target=deal:{deal['id']}">＋追加ヒアリング</a>
            </span>
          </h2>
          <p class="muted" style="margin:0">最新ヒアリング日: {_esc(latest.get('conducted_on') or '—')}（{_esc(latest.get('template_name') or '')}）</p>
        </div>"""
        else:
            hearing_html = f"""
        <div class="card">
          <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <span>初回ヒアリング</span>
            <a class="btn" href="/hearing/new?target=deal:{deal['id']}">ヒアリングを実施</a>
          </h2>
          <p class="muted" style="margin:0">ヒアリング未実施</p>
        </div>"""

    dev_projects_html = ""
    if deal.get("id"):
        dps = sfa_db.list_dev_projects(con, deal_id=deal["id"])
        add_btn = f'<a class="btn sec" href="/dev-projects/new?deal_id={deal["id"]}">＋開発案件を追加</a>'
        if dps:
            dp_rows = "".join(
                f'<tr><td><a href="/dev-project/{p["id"]}/edit">{_esc(p.get("theme"))}</a></td>'
                f'<td><span class="stage">{_esc(p.get("stage"))}</span></td>'
                f'<td>{_esc(p.get("status"))}</td>'
                f'<td>{_esc(p.get("order_potential"))}</td>'
                f'<td>{_esc(p.get("dev_owner"))}</td>'
                f'<td>{_esc(p.get("deadline") or "—")}</td></tr>'
                for p in dps
            )
            dev_projects_html = f"""
        <div class="card">
          <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <span>開発案件（{len(dps)}件）</span>{add_btn}
          </h2>
          <table><tr><th>テーマ</th><th>ステージ</th><th>状況</th><th>受注余地</th><th>開発担当</th><th>期限</th></tr>{dp_rows}</table>
        </div>"""
        else:
            dev_projects_html = f"""
        <div class="card">
          <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
            <span>開発案件</span>{add_btn}
          </h2>
          <p class="muted" style="margin:0">開発案件なし</p>
        </div>"""

    activities_html = ""
    sync_btn = ""
    if deal.get("id"):
        acts = sfa_db.list_activities(con, deal["id"])
        act_rows = "".join(
            f'<tr><td>{_esc(a.get("occurred_on"))}</td>'
            f'<td>{_esc(a.get("type"))}</td>'
            f'<td>{_esc(a.get("contact_name"))}</td>'
            f'<td style="white-space:pre-wrap">{_esc(a.get("body"))}</td></tr>'
            for a in acts
        ) or '<tr><td colspan=4 class=muted>活動なし</td></tr>'
        activities_html = f"""
        <div class="card" id="activity"><h2>活動履歴</h2>
        <table><tr><th>日付</th><th>種別</th><th>相手</th><th>内容</th></tr>{act_rows}</table>
        <form method="post" action="/activity/add" style="margin-top:16px">
          <input type="hidden" name="deal_id" value="{deal['id']}">
          <div class="grid">
            <div><label>日付</label><input type="date" name="occurred_on"></div>
            <div><label>種別</label><select name="type">{_opt(sfa_db.ACTIVITY_TYPES, '面談')}</select></div>
            <div><label>相手</label><input name="contact_name" placeholder="例：田中部長"></div>
          </div>
          <label>内容・決定事項</label><textarea name="body" rows="3"></textarea>
          <div style="margin-top:10px;padding:12px;background:#f8f9fa;border-radius:6px">
            <p style="margin:0 0 8px;font-size:.9em;font-weight:600;color:#555">商談の現状を更新</p>
            <div class="grid">
              <div><label>次回MS日</label><input type="date" name="next_milestone_date" value="{_esc(deal.get('next_milestone_date'))}"></div>
              <div><label>次回MSラベル</label><input name="next_milestone_label" value="{_esc(deal.get('next_milestone_label'))}"></div>
            </div>
            <label>現状メモ</label><textarea name="update_note" rows="2">{_esc(deal.get('note'))}</textarea>
          </div>
          <p><button class="btn sec">活動を追加して更新</button></p>
        </form></div>"""
        sync_btn = (
            f'<span class="muted" style="font-size:.85em">'
            f'{"🔗 テーマDB連携済 (id="+str(deal.get("theme_id"))+")" if deal.get("theme_id") else "テーマDB未連携（保存時に自動連携）"}'
            f'</span>'
        )
    acc_req = "required" if deal.get("id") else ""
    new_acc_html = ""
    new_acc_js = ""
    if not deal.get("id"):
        new_acc_html = (
            '<div style="margin-top:5px;text-align:left">'
            '<label style="font-size:11px;color:#6b7689;cursor:pointer">'
            '<input type="checkbox" id="new_acc_chk" onchange="toggleNewAcc()" style="width:auto;margin-right:4px">'
            '新規アカウントを追加（業界・規模を自動推定）</label>'
            '<div id="new_acc_row" style="display:none;margin-top:4px">'
            '<input name="new_account_name" placeholder="新しい会社名"></div>'
            '</div>'
        )
        new_acc_js = (
            'function toggleNewAcc() {'
            ' var chk=document.getElementById("new_acc_chk");'
            ' document.getElementById("new_acc_row").style.display=chk.checked?"":"none";'
            ' document.getElementById("acc_id_sel").required=!chk.checked;'
            '}'
        )
    revert_btn = ""
    if deal.get("id") and deal.get("status") != "closed":
        revert_btn = (
            f'<form method="post" action="/deal/{deal["id"]}/revert_to_lead" style="margin-top:8px"'
            ' onsubmit="return confirm(\'アポ獲得前の状態（リード）に戻します。\\n商談はクローズされます。\')">'
            '<button type="submit" class="btn" style="background:#f59e0b;font-size:12px;padding:6px 12px">'
            '↩ リードに戻す（アポ獲得前に戻る）'
            '</button></form>'
        )
    return f"""
    <div class="card"><h2>{'商談編集' if deal.get('id') else '新規商談'}</h2>
    {lead_picker_html}
    <form method="post" action="/deal/save">
      <input type="hidden" name="id" value="{_esc(deal.get('id'))}">
      <div class="grid">
        <div><label>アカウント{"" if not deal.get("id") else " *"}</label>
          <select name="account_id" id="acc_id_sel" {acc_req}>{''.join(acc_opts)}</select>
          {new_acc_html}</div>
        <div><label>案件名 *</label>
          <input name="deal_name" required value="{_esc(deal.get('deal_name'))}"></div>
        <div><label>ステージ</label>
          <select name="stage">{_opt(sfa_db.get_master_list(con,'deal_stages'), deal.get('stage'))}</select></div>
        <div><label>主担当</label>
          <select name="owner">{_opt(sfa_db.get_master_list(con,'owners'), deal.get('owner'))}</select></div>
        <div><label>サブ担当</label>
          <select name="sub_owner">{_opt(sfa_db.get_master_list(con,'owners'), deal.get('sub_owner'))}</select></div>
        <div><label>事業種別L1</label>
          <select name="business_type_l1" id="biz_l1" onchange="updateL2()">{_opt(sfa_db.get_master_list(con,'business_type_l1'), deal.get('business_type_l1'))}</select></div>
        <div><label>事業種別L2</label>
          <select name="business_type_l2" id="biz_l2">{_opt_l2(deal.get('business_type_l1'), deal.get('business_type_l2'))}</select></div>
        <div><label>リード経路</label>
          <select name="lead_pattern">{_opt(sfa_db.get_master_list(con,'lead_patterns'), deal.get('lead_pattern'))}</select></div>
        <div><label>ワンタイム総額（万円）</label>
          <input name="value_lumpsum" value="{_esc(deal.get('value_lumpsum'))}"></div>
        <div><label>ワンタイム月額換算（万円）</label>
          <input name="value_lumpsum_monthly" value="{_esc(deal.get('value_lumpsum_monthly'))}"></div>
        <div><label>継続月額（万円）</label>
          <input name="value_recurring" value="{_esc(deal.get('value_recurring'))}"></div>
        <div><label>クライアント予算</label>
          <input name="client_budget" value="{_esc(deal.get('client_budget'))}"></div>
        <div><label>重要度</label>
          <select name="importance">{_opt(sfa_db.IMPORTANCE_OPTIONS, deal.get('importance'))}</select></div>
        <div><label>ステータス</label>
          <select name="status">{_opt(['open', 'closed'], deal.get('status') or 'open')}</select></div>
        <div><label>次回MS日</label>
          <input type="date" name="next_milestone_date" value="{_esc(deal.get('next_milestone_date'))}"></div>
        <div><label>次回MSラベル</label>
          <input name="next_milestone_label" value="{_esc(deal.get('next_milestone_label'))}"></div>
      </div>
      <label>現状メモ</label><textarea name="note" rows="2">{_esc(deal.get('note'))}</textarea>
      <label>ゴール</label><textarea name="goal" rows="2">{_esc(deal.get('goal'))}</textarea>
      <div id="cost_section" style="{'display:none' if deal.get('business_type_l1') != 'コスト削減' else ''}">
        <hr style="margin:16px 0">
        <p style="font-weight:600;margin-bottom:8px;color:#555">コスト削減モデル詳細</p>
        <div class="grid">
          <div><label>コスト削減ステージ</label>
            <select name="cost_stage">{_opt(sfa_db.COST_STAGES, deal.get('cost_stage'))}</select></div>
          <div><label>アプローチ額（億円）</label>
            <input name="approach_value" type="number" step="0.01" value="{_esc(deal.get('approach_value'))}"></div>
          <div><label>アプローチ率（%）</label>
            <input name="approach_rate" type="number" step="0.1" value="{_esc(deal.get('approach_rate'))}"></div>
          <div><label>コスト削減率（%）</label>
            <input name="reduction_rate" type="number" step="0.1" value="{_esc(deal.get('reduction_rate'))}"></div>
          <div><label>成果報酬率（%）</label>
            <input name="fee_rate" type="number" step="0.1" value="{_esc(deal.get('fee_rate'))}"></div>
          <div><label>診断原価（万円）</label>
            <input name="diagnosis_cost" type="number" step="1" value="{_esc(deal.get('diagnosis_cost'))}"></div>
        </div>
      </div>
      <p><button class="btn">保存</button> <a class="btn sec" href="/">一覧へ</a> {sync_btn}</p>
    </form>
    {revert_btn}
    <script>
    {new_acc_js}
    const L2_MAP = {json.dumps(sfa_db.BUSINESS_TYPE_L2_BY_L1, ensure_ascii=False)};
    function updateL2() {{
      const l1 = document.getElementById('biz_l1').value;
      const sel = document.getElementById('biz_l2');
      const cur = sel.value;
      sel.innerHTML = '<option value=""></option>' +
        (L2_MAP[l1] || []).map(v => `<option value="${{v}}"${{v===cur?' selected':''}}>${{v}}</option>`).join('');
      document.getElementById('cost_section').style.display = l1 === 'コスト削減' ? '' : 'none';
    }}
    </script></div>
    {hearing_html}
    {dev_projects_html}
    {activities_html}"""


# ── 開発案件（商談に紐づく開発テーマ管理）───────────────────────────────────────

def dev_projects_list_page(con) -> str:
    projects = sfa_db.list_dev_projects(con)
    rows = "".join(
        f'<tr><td><a href="/dev-project/{p["id"]}/edit">{_esc(p.get("theme"))}</a>'
        f'<div class="muted">{_esc(p.get("theme_detail") or "")}</div></td>'
        f'<td>{_esc(p.get("account_name"))}<div class="muted">{_esc(p.get("deal_name"))}</div></td>'
        f'<td><span class="stage">{_esc(p.get("stage"))}</span></td>'
        f'<td>{_esc(p.get("status"))}</td>'
        f'<td>{_esc(p.get("order_potential"))}</td>'
        f'<td>{_esc(p.get("dev_owner"))}</td>'
        f'<td>{_esc(p.get("sales_owner"))}{(" / " + _esc(p["sales_sub_owner"])) if p.get("sales_sub_owner") else ""}</td>'
        f'<td>{_esc(p.get("deadline") or "—")}</td>'
        f'<td><form method="post" action="/dev-project/{p["id"]}/delete" style="display:inline" '
        f'onsubmit="return confirm(\'削除しますか？\')">'
        f'<button class="btn sec" style="font-size:11px;padding:4px 8px">削除</button></form></td></tr>'
        for p in projects
    ) or '<tr><td colspan=9 class=muted>開発案件がまだありません</td></tr>'
    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>開発案件一覧（{len(projects)}件）</span>
        <a class="btn" href="/dev-projects/new">＋新規入力</a>
      </h2>
      <table>
        <tr><th>開発テーマ</th><th>商談</th><th>ステージ</th><th>状況</th><th>受注余地</th>
            <th>開発担当</th><th>営業担当</th><th>期限</th><th></th></tr>
        {rows}
      </table>
    </div>"""


def dev_project_form(con, project: dict | None = None, deal_id: int | None = None) -> str:
    """開発案件の新規/編集フォーム。project未指定時は新規入力（商談選択欄あり）。"""
    is_edit = project is not None
    p = project or {}
    owners = sfa_db.get_master_list(con, "owners")

    if is_edit:
        deal_label = f'{_esc(p.get("account_name"))} / {_esc(p.get("deal_name"))}'
        deal_field_html = (
            f'<input type="hidden" name="deal_id" value="{p["deal_id"]}">'
            f'<div class="muted" style="margin:4px 0 10px">{deal_label}</div>'
        )
        sales_owner_text = f'{_esc(p.get("sales_owner") or "—")} / {_esc(p.get("sales_sub_owner") or "—")}'
        action = f'/dev-project/{p["id"]}/edit'
        back_href = f'/deal/{p["deal_id"]}'
        preselect_deal_id = None
    else:
        deals = sfa_db.list_deals(con, status="open")
        opts = "".join(
            f'<option value="{d["id"]}" data-owner="{_esc(d.get("owner"))}" '
            f'data-sub-owner="{_esc(d.get("sub_owner"))}">'
            f'{_esc(d.get("account_name"))} / {_esc(d.get("deal_name"))}</option>'
            for d in deals
        )
        deal_field_html = f"""
          <input type="text" id="dpDealFilter" placeholder="会社名・商談名で絞り込み" oninput="dpFilterDeals()">
          <select name="deal_id" id="dpDealSelect" required size="8" style="height:170px" onchange="dpShowSalesOwner()">
            <option value=""></option>
            {opts}
          </select>
          <p class="muted" id="dpSalesOwnerLine" style="margin-top:6px">営業担当: —</p>"""
        sales_owner_text = None
        action = "/dev-project/new"
        back_href = f'/deal/{deal_id}' if deal_id else "/dev-projects"
        preselect_deal_id = deal_id

    sales_owner_block = "" if sales_owner_text is None else (
        f'<label>営業担当（商談の主担当・サブ担当を自動反映）</label>'
        f'<div class="muted" style="margin-bottom:6px">{sales_owner_text}</div>'
    )

    return f"""
    <div class="card" style="max-width:680px">
      <p style="margin:0 0 10px"><a class="btn sec" href="{back_href}">← 戻る</a></p>
      <h2>{'開発案件を編集' if is_edit else '開発案件 新規入力'}</h2>
      <form method="post" action="{action}">
        <label>商談</label>
        {deal_field_html}
        <label>開発テーマ *</label>
        <input name="theme" required value="{_esc(p.get('theme'))}">
        <label>開発テーマ詳細</label>
        <textarea name="theme_detail" rows="3">{_esc(p.get('theme_detail'))}</textarea>
        <div class="grid">
          <div><label>状況</label><select name="status">{_opt(sfa_db.DEV_PROJECT_STATUSES, p.get('status'))}</select></div>
          <div><label>ステージ</label><select name="stage">{_opt(sfa_db.DEV_PROJECT_STAGES, p.get('stage'))}</select></div>
          <div><label>解像度</label><select name="resolution" id="dpResolution" onchange="dpRecalcPotential()">{_opt(sfa_db.DEV_RESOLUTIONS, p.get('resolution'))}</select></div>
          <div><label>予算確認</label><select name="budget_confirmed" id="dpBudget" onchange="dpRecalcPotential()">{_opt(sfa_db.DEV_BUDGET_CONFIRMED, p.get('budget_confirmed'))}</select></div>
          <div><label>実現難易度</label><select name="difficulty" id="dpDifficulty" onchange="dpRecalcPotential()">{_opt(sfa_db.DEV_DIFFICULTIES, p.get('difficulty'))}</select></div>
          <div><label>バックエンド有無</label><select name="has_backend">{_opt(sfa_db.DEV_HAS_BACKEND, p.get('has_backend'))}</select></div>
          <div><label>開発担当</label><select name="dev_owner">{_opt(owners, p.get('dev_owner'))}</select></div>
          <div><label>期限</label><input type="date" name="deadline" value="{_esc(p.get('deadline'))}"></div>
        </div>
        <label>受注余地（自動判定）</label>
        <div><span class="stage" id="dpOrderPotential">{_esc(p.get('order_potential') or '（保存時に判定）')}</span></div>
        <label>技術サポート</label>
        <input name="tech_support" value="{_esc(p.get('tech_support'))}">
        {sales_owner_block}
        <label>開発MS</label>
        <input name="dev_milestone" value="{_esc(p.get('dev_milestone'))}">
        <label>開発方針</label>
        <textarea name="dev_policy" rows="3">{_esc(p.get('dev_policy'))}</textarea>
        <div style="margin-top:16px">
          <button class="btn" type="submit">保存</button>
          <a class="btn sec" href="{back_href}">キャンセル</a>
        </div>
      </form>
    </div>
    <script>
    function dpFilterDeals() {{
      const q = document.getElementById('dpDealFilter').value.trim();
      const sel = document.getElementById('dpDealSelect');
      for (const o of sel.options) {{
        if (!o.value) continue;
        o.style.display = (!q || o.text.includes(q)) ? '' : 'none';
      }}
    }}
    function dpShowSalesOwner() {{
      const sel = document.getElementById('dpDealSelect');
      const o = sel.options[sel.selectedIndex];
      const owner = o ? (o.getAttribute('data-owner') || '—') : '—';
      const sub = o ? (o.getAttribute('data-sub-owner') || '—') : '—';
      document.getElementById('dpSalesOwnerLine').textContent = '営業担当: ' + owner + ' / ' + sub;
    }}
    function dpRecalcPotential() {{
      const budget = document.getElementById('dpBudget').value;
      const resolution = document.getElementById('dpResolution').value;
      const difficulty = document.getElementById('dpDifficulty').value;
      let potential = '中';
      if (budget === '×') potential = '低';
      else if (budget === '〇' && resolution === '〇' && (difficulty === '易' || difficulty === '中')) potential = '高';
      document.getElementById('dpOrderPotential').textContent = potential;
    }}
    (function() {{
      const sel = document.getElementById('dpDealSelect');
      if (sel) {{
        const pre = {json.dumps(preselect_deal_id)};
        if (pre) sel.value = String(pre);
        dpShowSalesOwner();
      }}
    }})();
    </script>"""


# ── リード / ピッチテーマ ページ（CRM吸収）─────────────────────────────────────

_SOURCE_TO_LP = {"exhibition": "Exh.", "referral": "Connection", "inbound": "HP", "other": "na"}


def convert_lead_to_deal(con, lead: dict) -> int:
    """リードを商談化してdeal_idを返す（アカウント・コンタクト作成、リードをconvertedに）。
    既存のオープン商談がある場合はそのidを返す。クローズ済なら再変換する。"""
    if lead.get("deal_id"):
        _ed = sfa_db.get_deal(con, lead["deal_id"])
        if _ed and _ed.get("status") != "closed":
            return int(lead["deal_id"])
        con.execute("UPDATE leads SET deal_id=NULL WHERE id=?", (lead["id"],))
        con.commit()
    # 1. アカウントを検索または作成
    company_name = (lead.get("company") or "").strip() or "(未設定)"
    existing_acc = con.execute(
        "SELECT id FROM accounts WHERE name=?", (company_name,)
    ).fetchone()
    account_id = (dict(existing_acc)["id"] if existing_acc
                  else sfa_db.upsert_account(
                      con, name=company_name,
                      industry=lead.get("industry"),
                      company_size=lead.get("company_size"),
                  ))
    # 2. コンタクト作成（重複チェック）
    if not con.execute(
        "SELECT id FROM contacts WHERE account_id=? AND name=?",
        (account_id, lead["name"]),
    ).fetchone():
        con.execute(
            "INSERT INTO contacts (account_id,name,title,email,phone) VALUES (?,?,?,?,?)",
            (account_id, lead["name"], lead.get("title"),
             lead.get("email"), lead.get("phone")),
        )
        con.commit()
    # 3. 商談作成
    deal_id = sfa_db.upsert_deal(
        con, account_id=account_id,
        deal_name=company_name, stage="初回アポ実施", status="open",
        lead_pattern=_SOURCE_TO_LP.get(lead.get("source", "other"), "na"),
        owner=lead.get("assigned_to"), note=lead.get("notes"),
    )
    # 4. リードをクローズ（商談化済）してdeal_idをセット
    con.execute(
        "UPDATE leads SET deal_id=?, lead_status='converted', updated_at=datetime('now') WHERE id=?",
        (deal_id, lead["id"]),
    )
    con.commit()
    return int(deal_id)


# ── 初回ヒアリング ───────────────────────────────────────────────────────────────

def hearing_templates_page(con) -> str:
    tmpls = sfa_db.list_hearing_templates(con)
    counts = sfa_db.count_hearing_results_by_template(con)
    rows = ""
    for t in tmpls:
        n_items = len(t.get("items") or [])
        n_results = counts.get(t["id"], 0)
        count_cell = (
            f'<a href="/hearings?template_id={t["id"]}" title="このテンプレートの実施済みヒアリング一覧へ">{n_results}件</a>'
            if n_results else '<span class="muted">0件</span>'
        )
        rows += (
            f'<tr>'
            f'<td><a href="/hearing-templates/{t["id"]}/edit"><strong>{_esc(t["name"])}</strong></a></td>'
            f'<td class="muted">{_esc(t.get("description") or "—")}</td>'
            f'<td style="text-align:center">{n_items}</td>'
            f'<td style="text-align:center">{count_cell}</td>'
            f'<td><form method="post" action="/hearing-templates/{t["id"]}/delete" style="display:inline">'
            f'<button class="btn sec" style="font-size:11px;padding:4px 8px" '
            f'onclick="return confirm(\'削除しますか？（既存のヒアリング結果は残ります）\')">削除</button></form></td>'
            f'</tr>'
        )
    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>ヒアリングテンプレート管理</span>
        <span style="display:flex;gap:8px">
          <a class="btn sec" href="/hearings">ヒアリング一覧</a>
          <a class="btn" href="/hearing-templates/new">＋テンプレート追加</a>
        </span>
      </h2>
      <p class="muted" style="margin-bottom:14px">初回商談で使う定型ヒアリング項目を定義します。自由記述／選択肢（単一・複数）を項目ごとに指定できます。</p>
      <table>
        <tr><th>テンプレート名</th><th>説明</th><th>項目数</th><th>実施数</th><th></th></tr>
        {rows or '<tr><td colspan=5 class="muted">テンプレートがありません。</td></tr>'}
      </table>
    </div>"""


def hearing_template_form(con, tmpl=None) -> str:
    tid = tmpl["id"] if tmpl else None
    action = f"/hearing-templates/{tid}/save" if tid else "/hearing-templates/save"
    title = "テンプレート編集" if tmpl else "テンプレート追加"
    items = (tmpl.get("items") if tmpl else None) or []
    items_data = json.dumps(items, ensure_ascii=False)
    return f"""
    <div class="card" style="max-width:820px">
      <h2>{title}</h2>
      <form method="post" action="{action}" onsubmit="return serializeItems()">
        <label>テンプレート名</label>
        <input name="name" required value="{_esc(tmpl.get('name') if tmpl else '')}">
        <label>説明（任意）</label>
        <input name="description" value="{_esc(tmpl.get('description') if tmpl else '')}">
        <label style="margin-top:14px">ヒアリング項目</label>
        <p class="muted" style="font-size:12px;margin:4px 0 8px">Q&amp;A項目と矢羽セクションを自由に組み合わせられます。</p>
        <div id="items_box"></div>
        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
          <button type="button" class="btn sec" onclick="addItem()">＋Q&amp;A項目を追加</button>
          <button type="button" class="btn sec" style="border-color:#3b82f660;color:#3b82f6"
            onclick="addYabaneItem(null)">＋矢羽セクションを追加</button>
          <button type="button" class="btn sec" style="border-color:#7c3aed60;color:#7c3aed"
            onclick="addRadarItem(null)">＋レーダーチャートを追加</button>
          <button type="button" class="btn sec" style="border-color:#05966960;color:#059669"
            onclick="addTimelineItem(null)">＋タイムラインを追加</button>
          <button type="button" class="btn sec" style="border-color:#d9770660;color:#d97706"
            onclick="addScorecardItem(null)">＋スコアカードを追加</button>
        </div>
        <input type="hidden" name="items_json" id="items_json">
        <div style="margin-top:16px;display:flex;gap:8px">
          <button class="btn" type="submit">保存</button>
          <a class="btn sec" href="/hearing-templates">キャンセル</a>
        </div>
      </form>
    </div>
    <script>
    var _ITEMS = {items_data};

    // ── 矢羽ブロック ──
    function _addYbPairRow(pairsBox, step, dept) {{
      var div = document.createElement('div');
      div.style.cssText = 'display:flex;gap:6px;align-items:center;margin:4px 0';
      var stepInp = document.createElement('input');
      stepInp.type='text'; stepInp.className='yb-pair-step';
      stepInp.value=step||''; stepInp.placeholder='ステップ（例：受注）'; stepInp.style.cssText='flex:1';
      var deptInp = document.createElement('input');
      deptInp.type='text'; deptInp.className='yb-pair-dept';
      deptInp.value=dept||''; deptInp.placeholder='部署（例：営業）'; deptInp.style.cssText='flex:1';
      var btn=document.createElement('button');
      btn.type='button'; btn.className='btn sec';
      btn.style.cssText='font-size:11px;padding:4px 8px;background:#fde8e8;color:#c0392b';
      btn.textContent='削除'; btn.onclick=function(){{div.remove();}};
      div.appendChild(stepInp); div.appendChild(deptInp); div.appendChild(btn); pairsBox.appendChild(div);
    }}

    function addYabaneItem(cfg) {{
      cfg=cfg||{{label:'業務プロセス',rows:[{{step:'ステップ1',dept:'部署1'}},{{step:'ステップ2',dept:'部署2'}}]}};
      // 旧形式（departments/steps）で保存された既存テンプレートを開いた場合の変換
      var rows = cfg.rows;
      if (!rows) {{
        var depts = cfg.departments || [];
        var steps = cfg.steps || [];
        rows = steps.map(function(s, i) {{ return {{step: s.label || '', dept: depts[i] || ''}}; }});
        if (!rows.length) rows = [{{step:'ステップ1',dept:'部署1'}}];
      }}
      var box=document.getElementById('items_box');
      var el=document.createElement('div'); el.className='yb-block';
      el.setAttribute('draggable','true');
      el.style.cssText='border:2px solid #3b82f660;border-radius:8px;padding:12px;margin:8px 0;background:#0a1828';
      // header
      var hdr=document.createElement('div'); hdr.style.cssText='display:flex;align-items:center;gap:8px;margin-bottom:10px';
      var dh=document.createElement('span'); dh.className='drag-handle';
      dh.title='ドラッグで並び替え';
      dh.style.cssText='cursor:grab;color:#555;font-size:18px;user-select:none;line-height:1;flex-shrink:0';
      dh.textContent='⠿';
      var badge=document.createElement('span');
      badge.style.cssText='font-size:10px;font-weight:700;color:#3b82f6;background:#3b82f615;border:1px solid #3b82f640;border-radius:4px;padding:2px 8px;white-space:nowrap;flex-shrink:0';
      badge.textContent='矢羽';
      var lw=document.createElement('div'); lw.style.cssText='flex:1;min-width:0';
      var ll=document.createElement('label'); ll.style.cssText='font-size:12px'; ll.textContent='セクション名';
      var li=document.createElement('input'); li.className='yb-block-label'; li.value=cfg.label||'業務プロセス'; li.placeholder='例：業務プロセス';
      lw.appendChild(ll); lw.appendChild(li);
      var db=document.createElement('button'); db.type='button'; db.className='btn sec';
      db.style.cssText='font-size:11px;padding:4px 8px;background:#fde8e8;color:#c0392b;flex-shrink:0';
      db.textContent='削除'; db.onclick=function(){{this.closest('.yb-block').remove();}};
      hdr.appendChild(dh); hdr.appendChild(badge); hdr.appendChild(lw); hdr.appendChild(db); el.appendChild(hdr);
      // body: ステップ・部署をペアで並べて入力
      var pl=document.createElement('label'); pl.style.cssText='font-size:12px'; pl.textContent='初期のステップ・部署（1行=1ステップ1部署。ヒアリング入力画面はこの並びをそのまま行として表示します）';
      el.appendChild(pl);
      var pairsBox=document.createElement('div'); pairsBox.className='yb-block-pairs'; pairsBox.style.cssText='margin:4px 0';
      el.appendChild(pairsBox);
      var addBtn=document.createElement('button'); addBtn.type='button'; addBtn.className='btn sec';
      addBtn.style.cssText='font-size:11px;padding:4px 8px;margin-top:4px'; addBtn.textContent='＋行追加';
      addBtn.onclick=function(){{_addYbPairRow(pairsBox,'','');}};
      el.appendChild(addBtn);
      box.appendChild(el);
      rows.forEach(function(r){{_addYbPairRow(pairsBox, r.step||'', r.dept||'');}});
    }}

    function _makeBlockHeader(el, blockClass, badgeText, badgeColor, labelClass, defaultLabel) {{
      var hdr=document.createElement('div'); hdr.style.cssText='display:flex;align-items:center;gap:8px;margin-bottom:10px';
      var dh=document.createElement('span'); dh.className='drag-handle'; dh.title='ドラッグで並び替え';
      dh.style.cssText='cursor:grab;color:#555;font-size:18px;user-select:none;line-height:1;flex-shrink:0';
      dh.textContent='⠿';
      var badge=document.createElement('span');
      badge.style.cssText='font-size:10px;font-weight:700;color:'+badgeColor+';background:'+badgeColor+'15;border:1px solid '+badgeColor+'40;border-radius:4px;padding:2px 8px;white-space:nowrap;flex-shrink:0';
      badge.textContent=badgeText;
      var lw=document.createElement('div'); lw.style.cssText='flex:1;min-width:0';
      var ll=document.createElement('label'); ll.style.cssText='font-size:12px'; ll.textContent='セクション名';
      var li=document.createElement('input'); li.className=labelClass; li.value=defaultLabel; li.placeholder='例：'+defaultLabel;
      lw.appendChild(ll); lw.appendChild(li);
      var db=document.createElement('button'); db.type='button'; db.className='btn sec';
      db.style.cssText='font-size:11px;padding:4px 8px;background:#fde8e8;color:#c0392b;flex-shrink:0';
      db.textContent='削除'; db.onclick=function(){{this.closest('.'+blockClass).remove();}};
      hdr.appendChild(dh); hdr.appendChild(badge); hdr.appendChild(lw); hdr.appendChild(db); el.appendChild(hdr);
    }}

    function addRadarItem(cfg) {{
      cfg=cfg||{{label:'DX成熟度評価',axes:['戦略','組織','プロセス','テクノロジー','データ']}};
      var box=document.getElementById('items_box');
      var el=document.createElement('div'); el.className='ra-block'; el.setAttribute('draggable','true');
      el.style.cssText='border:2px solid #7c3aed60;border-radius:8px;padding:12px;margin:8px 0;background:#0a1828';
      _makeBlockHeader(el,'ra-block','レーダー','#7c3aed','ra-block-label',cfg.label||'DX成熟度評価');
      var axDiv=document.createElement('div');
      var axL=document.createElement('label'); axL.style.cssText='font-size:12px'; axL.textContent='評価軸（1行に1つ、3〜8軸推奨）';
      var axTa=document.createElement('textarea'); axTa.className='ra-block-axes'; axTa.rows=5;
      axTa.style.cssText='font-family:inherit'; axTa.placeholder='例：戦略\\n組織\\nプロセス\\nテクノロジー\\nデータ';
      axTa.value=(cfg.axes||[]).join('\\n');
      axDiv.appendChild(axL); axDiv.appendChild(axTa); el.appendChild(axDiv);
      box.appendChild(el);
    }}

    function addTimelineItem(cfg) {{
      cfg=cfg||{{label:'導入スケジュール',milestones:['要件定義','設計・開発','テスト','本稼働']}};
      var box=document.getElementById('items_box');
      var el=document.createElement('div'); el.className='tl-block'; el.setAttribute('draggable','true');
      el.style.cssText='border:2px solid #05966940;border-radius:8px;padding:12px;margin:8px 0;background:#0a1828';
      _makeBlockHeader(el,'tl-block','タイムライン','#059669','tl-block-label',cfg.label||'導入スケジュール');
      var msDiv=document.createElement('div');
      var msL=document.createElement('label'); msL.style.cssText='font-size:12px'; msL.textContent='初期マイルストーン（1行に1つ）';
      var msTa=document.createElement('textarea'); msTa.className='tl-block-milestones'; msTa.rows=4;
      msTa.style.cssText='font-family:inherit'; msTa.placeholder='例：要件定義\\n設計・開発\\nテスト\\n本稼働';
      msTa.value=(cfg.milestones||[]).join('\\n');
      msDiv.appendChild(msL); msDiv.appendChild(msTa); el.appendChild(msDiv);
      box.appendChild(el);
    }}

    function addScorecardItem(cfg) {{
      cfg=cfg||{{label:'競合比較',criteria:['コスト','機能性','サポート','実績'],items:['自社','競合A']}};
      var box=document.getElementById('items_box');
      var el=document.createElement('div'); el.className='sc-block'; el.setAttribute('draggable','true');
      el.style.cssText='border:2px solid #d9770640;border-radius:8px;padding:12px;margin:8px 0;background:#0a1828';
      _makeBlockHeader(el,'sc-block','スコアカード','#d97706','sc-block-label',cfg.label||'競合比較');
      var body=document.createElement('div'); body.style.cssText='display:flex;gap:12px;flex-wrap:wrap';
      var crDiv=document.createElement('div'); crDiv.style.cssText='flex:1;min-width:160px';
      var crL=document.createElement('label'); crL.style.cssText='font-size:12px'; crL.textContent='評価軸（列・1行に1つ）';
      var crTa=document.createElement('textarea'); crTa.className='sc-block-criteria'; crTa.rows=4;
      crTa.style.cssText='font-family:inherit'; crTa.placeholder='例：コスト\\n機能性\\nサポート\\n実績';
      crTa.value=(cfg.criteria||[]).join('\\n');
      crDiv.appendChild(crL); crDiv.appendChild(crTa);
      var itDiv=document.createElement('div'); itDiv.style.cssText='flex:1;min-width:160px';
      var itL=document.createElement('label'); itL.style.cssText='font-size:12px'; itL.textContent='初期対象（行・1行に1つ）';
      var itTa=document.createElement('textarea'); itTa.className='sc-block-items'; itTa.rows=4;
      itTa.style.cssText='font-family:inherit'; itTa.placeholder='例：自社\\n競合A\\n競合B';
      itTa.value=(cfg.items||[]).join('\\n');
      itDiv.appendChild(itL); itDiv.appendChild(itTa);
      body.appendChild(crDiv); body.appendChild(itDiv); el.appendChild(body);
      box.appendChild(el);
    }}

    function rowHtml(it) {{
      it = it || {{label:'',type:'text',multi:false,required:false,options:[],parent_idx:null,parent_value:null}};
      const opts = (it.options || []).join('\\n');
      const hasBranch = it.parent_idx !== null && it.parent_idx !== undefined;
      return `
      <div class="hitem" draggable="true" style="border:1px solid #d4dae4;border-radius:8px;padding:12px;margin:8px 0;background:#fafbfc">
        <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
          <span class="drag-handle" title="ドラッグで並び替え" style="cursor:grab;color:#bbb;font-size:18px;padding-bottom:6px;user-select:none;line-height:1;flex-shrink:0">⠿</span>
          <div style="flex:2;min-width:200px"><label style="font-size:12px">質問ラベル</label>
            <input class="i-label" value="${{(it.label||'').replace(/"/g,'&quot;')}}" placeholder="例：現状の課題"></div>
          <div style="flex:1;min-width:120px"><label style="font-size:12px">回答形式</label>
            <select class="i-type" onchange="syncRow(this)">
              <option value="text"${{it.type==='text'?' selected':''}}>自由記述（テキスト）</option>
              <option value="number"${{it.type==='number'?' selected':''}}>自由記述（数値のみ）</option>
              <option value="choice"${{it.type==='choice'?' selected':''}}>選択肢</option>
            </select></div>
          <div class="i-multi-wrap" style="min-width:120px;${{it.type==='choice'?'':'display:none'}}">
            <label style="font-size:12px">選択方式</label>
            <select class="i-multi">
              <option value="0"${{!it.multi?' selected':''}}>単一選択</option>
              <option value="1"${{it.multi?' selected':''}}>複数選択</option>
            </select></div>
          <div style="display:flex;align-items:center;gap:4px;padding-bottom:6px">
            <input type="checkbox" class="i-required" ${{it.required?'checked':''}} style="width:14px;height:14px">
            <label style="font-size:12px;margin:0">必須</label></div>
          <button type="button" class="btn sec" style="font-size:11px;padding:4px 8px;background:#fde8e8;color:#c0392b"
            onclick="this.closest('.hitem').remove(); refreshBranchSelectors()">削除</button>
        </div>
        <div class="i-opts-wrap" style="margin-top:8px;${{it.type==='choice'?'':'display:none'}}">
          <label style="font-size:12px">選択肢（1行に1つ）</label>
          <textarea class="i-options" rows="3" placeholder="選択肢1&#10;選択肢2"
            oninput="refreshBranchSelectors()">${{opts}}</textarea>
        </div>
        <div style="margin-top:10px;border-top:1px solid #e8ecf0;padding-top:8px">
          <label style="font-size:12px;cursor:pointer;user-select:none">
            <input type="checkbox" class="i-has-branch" ${{hasBranch?'checked':''}}
              onchange="toggleBranchSection(this); refreshBranchSelectors()" style="width:13px;height:13px;margin-right:4px">
            <span style="color:#666">この質問は別の質問の回答を条件に表示する（分岐）</span>
          </label>
          <div class="i-branch-section" style="margin-top:8px;display:${{hasBranch?'flex':'none'}};gap:12px;flex-wrap:wrap;align-items:flex-end">
            <div style="flex:2;min-width:180px">
              <label style="font-size:12px">分岐元の質問（選択肢型のみ）</label>
              <select class="i-parent-idx" data-init="${{hasBranch ? it.parent_idx : ''}}"
                onchange="onParentIdxChange(this)">
                <option value="">— 選択 —</option>
              </select>
            </div>
            <div style="flex:1;min-width:140px">
              <label style="font-size:12px">この回答のときに表示</label>
              <select class="i-parent-value" data-init="${{hasBranch ? (it.parent_value||'') : ''}}">
                <option value="">— 選択 —</option>
              </select>
            </div>
          </div>
        </div>
      </div>`;
    }}

    function toggleBranchSection(cb) {{
      const sec = cb.closest('.hitem').querySelector('.i-branch-section');
      sec.style.display = cb.checked ? 'flex' : 'none';
    }}

    function syncRow(sel) {{
      const row = sel.closest('.hitem');
      const isChoice = sel.value === 'choice';
      row.querySelector('.i-multi-wrap').style.display = isChoice ? '' : 'none';
      row.querySelector('.i-opts-wrap').style.display = isChoice ? '' : 'none';
    }}

    function refreshBranchSelectors() {{
      // 全hitemの情報を収集
      const rows = Array.from(document.querySelectorAll('#items_box .hitem'));
      const choiceItems = rows.map((row, idx) => {{
        const label = row.querySelector('.i-label').value.trim() || `Q${{idx+1}}`;
        const type  = row.querySelector('.i-type').value;
        const opts  = type==='choice'
          ? row.querySelector('.i-options').value.split('\\n').map(s=>s.trim()).filter(Boolean)
          : [];
        return {{idx, label, type, opts}};
      }});

      rows.forEach((row, currentIdx) => {{
        const parentIdxSel  = row.querySelector('.i-parent-idx');
        const parentValSel  = row.querySelector('.i-parent-value');
        if (!parentIdxSel) return;

        const prevIdxVal  = parentIdxSel.value || parentIdxSel.dataset.init || '';
        const prevValVal  = parentValSel.value  || parentValSel.dataset.init  || '';

        // 分岐元ドロップダウンを再構築
        parentIdxSel.innerHTML = '<option value="">— 選択 —</option>';
        choiceItems.forEach(c => {{
          if (c.idx === currentIdx || c.type !== 'choice') return;
          const opt = document.createElement('option');
          opt.value = c.idx;
          opt.textContent = `Q${{c.idx+1}}: ${{c.label}}`;
          if (String(c.idx) === String(prevIdxVal)) opt.selected = true;
          parentIdxSel.appendChild(opt);
        }});
        parentIdxSel.dataset.init = '';

        // 条件値ドロップダウンを再構築
        const selIdx = parseInt(parentIdxSel.value);
        parentValSel.innerHTML = '<option value="">— 選択 —</option>';
        if (!isNaN(selIdx)) {{
          const parent = choiceItems.find(c => c.idx === selIdx);
          if (parent) {{
            parent.opts.forEach(opt => {{
              const o = document.createElement('option');
              o.value = opt;
              o.textContent = opt;
              if (opt === prevValVal) o.selected = true;
              parentValSel.appendChild(o);
            }});
          }}
        }}
        parentValSel.dataset.init = '';
      }});
    }}

    function onParentIdxChange(sel) {{
      const row = sel.closest('.hitem');
      row.querySelector('.i-parent-value').dataset.init = '';
      refreshBranchSelectors();
    }}

    function addItem(it) {{
      const box = document.getElementById('items_box');
      box.insertAdjacentHTML('beforeend', rowHtml(it));
      refreshBranchSelectors();
    }}

    function serializeItems() {{
      var items = [];
      document.querySelectorAll('#items_box > .hitem, #items_box > .yb-block, #items_box > .ra-block, #items_box > .tl-block, #items_box > .sc-block').forEach(function(el) {{
        if (el.classList.contains('yb-block')) {{
          var ybLabel = el.querySelector('.yb-block-label').value.trim() || '業務プロセス';
          var ybRows = [];
          el.querySelectorAll('.yb-block-pairs > div').forEach(function(row) {{
            var step = row.querySelector('.yb-pair-step').value.trim();
            var dept = row.querySelector('.yb-pair-dept').value.trim();
            if (step || dept) ybRows.push({{step:step, dept:dept}});
          }});
          items.push({{label:ybLabel, type:'yabane', rows:ybRows}});
        }} else if (el.classList.contains('ra-block')) {{
          var raLabel=el.querySelector('.ra-block-label').value.trim()||'DX成熟度評価';
          var axes=el.querySelector('.ra-block-axes').value.split('\\n').map(function(s){{return s.trim();}}).filter(Boolean);
          items.push({{label:raLabel, type:'radar', axes:axes}});
        }} else if (el.classList.contains('tl-block')) {{
          var tlLabel=el.querySelector('.tl-block-label').value.trim()||'導入スケジュール';
          var milestones=el.querySelector('.tl-block-milestones').value.split('\\n').map(function(s){{return s.trim();}}).filter(Boolean);
          items.push({{label:tlLabel, type:'timeline', milestones:milestones}});
        }} else if (el.classList.contains('sc-block')) {{
          var scLabel=el.querySelector('.sc-block-label').value.trim()||'競合比較';
          var criteria=el.querySelector('.sc-block-criteria').value.split('\\n').map(function(s){{return s.trim();}}).filter(Boolean);
          var scItems=el.querySelector('.sc-block-items').value.split('\\n').map(function(s){{return s.trim();}}).filter(Boolean);
          items.push({{label:scLabel, type:'scorecard', criteria:criteria, items:scItems}});
        }} else {{
          var stdLabel = el.querySelector('.i-label').value.trim();
          if (!stdLabel) return;
          var type = el.querySelector('.i-type').value;
          var multi = el.querySelector('.i-multi').value === '1';
          var required = el.querySelector('.i-required').checked;
          var options = type==='choice'
            ? el.querySelector('.i-options').value.split('\\n').map(function(s){{return s.trim();}}).filter(Boolean)
            : [];
          var hasBranch = el.querySelector('.i-has-branch').checked;
          var parentIdxRaw = el.querySelector('.i-parent-idx').value;
          var parentIdx = hasBranch && parentIdxRaw !== '' ? parseInt(parentIdxRaw) : null;
          var parentValue = hasBranch ? (el.querySelector('.i-parent-value').value.trim() || null) : null;
          items.push({{label:stdLabel, type:type, multi:type==='choice'?multi:false, required:required,
            options:options, parent_idx:parentIdx, parent_value:parentValue}});
        }}
      }});
      document.getElementById('items_json').value = JSON.stringify(items);
      return true;
    }}

    (function() {{
      if (_ITEMS.length) {{
        _ITEMS.forEach(function(it) {{
          if (it.type === 'yabane') {{ addYabaneItem(it); }}
          else if (it.type === 'radar') {{ addRadarItem(it); }}
          else if (it.type === 'timeline') {{ addTimelineItem(it); }}
          else if (it.type === 'scorecard') {{ addScorecardItem(it); }}
          else {{ addItem(it); }}
        }});
      }} else {{ addItem(); }}
    }})();

    // ── ドラッグ並び替え ──
    (function() {{
      var box = document.getElementById('items_box');
      var _drag = null;
      box.addEventListener('dragstart', function(e) {{
        var t = e.target;
        if (t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.tagName==='SELECT'||t.tagName==='BUTTON') {{
          e.preventDefault(); return;
        }}
        var item = t.closest('.hitem,.yb-block,.ra-block,.tl-block,.sc-block');
        if (!item) return;
        _drag = item;
        setTimeout(function(){{item.style.opacity='0.4';}}, 0);
      }});
      box.addEventListener('dragend', function() {{
        if (_drag) _drag.style.opacity='';
        _drag = null;
      }});
      box.addEventListener('dragover', function(e) {{
        e.preventDefault();
        if (!_drag) return;
        var over = e.target.closest('.hitem,.yb-block,.ra-block,.tl-block,.sc-block');
        if (!over||over===_drag) return;
        var rect = over.getBoundingClientRect();
        if (e.clientY < rect.top + rect.height/2) box.insertBefore(_drag, over);
        else box.insertBefore(_drag, over.nextSibling);
      }});
    }})();
    </script>"""


def hearing_new_page(con, preselect: str | None = None) -> str:
    """対象（商談 or リード）とテンプレートを選んでヒアリングを開始する画面。"""
    tmpls = sfa_db.list_hearing_templates(con)
    if not tmpls:
        return ('<div class="card"><h2>新規ヒアリング</h2>'
                '<p class="muted">先にテンプレートを作成してください。</p>'
                '<a class="btn" href="/hearing-templates/new">＋テンプレート追加</a></div>')
    tmpl_opts = "".join(
        f'<option value="{t["id"]}">{_esc(t["name"])}</option>' for t in tmpls
    )
    def _sel(v):
        return " selected" if preselect == v else ""
    deals = sfa_db.list_deals(con, status="open")
    deal_opts = ""
    for d in deals:
        val = f"deal:{d['id']}"
        deal_opts += (f'<option value="{val}"{_sel(val)}>商談: '
                      f'{_esc(d.get("account_name") or "")} / {_esc(d.get("deal_name"))}</option>')
    open_leads = [l for l in sfa_db.list_leads(con)
                  if l.get("lead_status") not in ("converted", "lost") and not l.get("deal_id")]
    lead_opts = ""
    for l in open_leads:
        val = f"lead:{l['id']}"
        lead_opts += (f'<option value="{val}"{_sel(val)}>リード: '
                      f'{_esc(l.get("company") or "?")} / {_esc(l.get("name") or "?")}</option>')
    return f"""
    <div class="card" style="max-width:680px">
      <h2>新規ヒアリング</h2>
      <p class="muted" style="margin-bottom:14px">対象とテンプレートを選んでください。リードを選んだ場合は、保存時に自動で商談化されます。</p>
      <form method="get" action="/hearing/start">
        <label>対象（商談 / リード）</label>
        <select name="target" required>
          <option value="">— 選択 —</option>
          <optgroup label="商談">{deal_opts or '<option disabled>なし</option>'}</optgroup>
          <optgroup label="リード（未商談化）">{lead_opts or '<option disabled>なし</option>'}</optgroup>
        </select>
        <label>ヒアリングテンプレート</label>
        <select name="template_id" required>{tmpl_opts}</select>
        <div style="margin-top:16px"><button class="btn" type="submit">ヒアリング入力へ →</button>
        <a class="btn sec" href="/hearings">キャンセル</a></div>
      </form>
    </div>"""


def hearing_input_page(con, *, target_type, target_id, template, target_label,
                       prefill=None, prev_date=None, draft=None,
                       edit_result_id=None, edit_conducted_on=None) -> str:
    """ヒアリング入力画面：ヒアリング項目＋通常の活動履歴入力欄を同一画面に生成。

    draft: get_hearing_draft()の返り値（30秒ごとの自動保存下書き）。存在する場合、
    前回確定結果のprefillよりも優先して復元する（より新しい入力中データのため）。
    edit_result_id: 指定時は既存ヒアリング結果の修正モード。新規の活動履歴は作らず、
    対象のhearing_resultsの内容だけを上書きする。
    """
    prefill = prefill or {}
    draft_data = (draft or {}).get("form_data") or {}
    draft_updated_at = (draft or {}).get("updated_at")

    items = template.get("items") or []
    has_branch = any(
        it.get("parent_idx") is not None and it.get("parent_value") is not None
        for it in items
    )
    fields_html = ""
    for i, it in enumerate(items):
        label = _esc(it.get("label"))
        req = " <span style='color:#c0392b'>*</span>" if it.get("required") else ""
        req_attr = " required" if it.get("required") else ""
        pv = prefill.get(it.get("label"))
        if f"answer_{i}" in draft_data:
            _draft_raw = draft_data[f"answer_{i}"]
            if it.get("type") == "yabane":
                try:
                    pv = json.loads(_draft_raw) if isinstance(_draft_raw, str) else _draft_raw
                except (ValueError, TypeError):
                    pass
            else:
                pv = _draft_raw
        # 分岐設定: data属性でJSに渡す
        parent_idx = it.get("parent_idx")
        parent_value = it.get("parent_value")
        branch_attrs = ""
        branch_class = ""
        if parent_idx is not None and parent_value is not None:
            branch_attrs = f' data-parent-idx="{parent_idx}" data-parent-value="{_esc(parent_value)}"'
            branch_class = " hq-branch"
        if it.get("type") == "radar":
            _ra_axes = it.get("axes") or ["軸1", "軸2", "軸3", "軸4", "軸5"]
            _ra_rows_html = "".join(
                f'<div class="ra-axis-row">'
                f'<input class="ra-axis-label-inp" value="{_esc(_ax)}" placeholder="軸名" onfocus="this.select()">'
                f'<input type="range" class="ra-score-range" min="0" max="5" step="1" value="3"'
                f' oninput="raUpdateScore(this)">'
                f'<span class="ra-score-val">3</span>'
                f'</div>'
                for _ax in _ra_axes
            )
            fields_html += (
                f'<div class="hq-item{branch_class}" style="margin:14px 0"{branch_attrs}>'
                f'<label style="font-weight:700;color:#7c3aed;font-size:13px;margin-bottom:6px;display:block">{label}</label>'
                f'<input type="hidden" name="answer_{i}" id="ra_answer_{i}">'
                f'<div class="ra-wrapper" id="ra_wrapper_{i}" data-ra-idx="{i}">'
                f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">'
                f'<div class="ra-inputs" style="flex:1;min-width:200px">{_ra_rows_html}</div>'
                f'<div style="flex:0 0 200px;min-width:180px;display:flex;flex-direction:column;align-items:center">'
                f'<svg class="ra-svg" width="200" height="200" style="display:block"></svg>'
                f'</div>'
                f'</div>'
                f'</div>'
                f'</div>'
            )
        elif it.get("type") == "timeline":
            _tl_milestones = it.get("milestones") or []
            _tl_events_html = "".join(
                f'<div class="tl-event">'
                f'<div class="tl-dot"></div>'
                f'<div class="tl-event-body">'
                f'<div class="tl-event-header">'
                f'<input type="month" class="tl-date">'
                f'<input class="tl-event-label" value="{_esc(_ms)}" placeholder="マイルストーン名" onfocus="this.select()">'
                f'<button type="button" class="tl-del-btn" onclick="tlDelEvent(this)">✕</button>'
                f'</div>'
                f'<textarea class="tl-note" rows="2" placeholder="詳細・メモ"></textarea>'
                f'</div>'
                f'</div>'
                for _ms in _tl_milestones
            )
            fields_html += (
                f'<div class="hq-item{branch_class}" style="margin:14px 0"{branch_attrs}>'
                f'<label style="font-weight:700;color:#059669;font-size:13px;margin-bottom:6px;display:block">{label}</label>'
                f'<input type="hidden" name="answer_{i}" id="tl_answer_{i}">'
                f'<div class="tl-wrapper" id="tl_wrapper_{i}" data-tl-idx="{i}">'
                f'<div class="tl-track" id="tl_track_{i}">{_tl_events_html}</div>'
                f'<div style="margin-top:10px">'
                f'<button type="button" class="btn sec" style="border-color:#05966960;color:#059669"'
                f' onclick="tlAddEvent({i})">＋マイルストーン追加</button>'
                f'</div>'
                f'</div>'
                f'</div>'
            )
        elif it.get("type") == "scorecard":
            _sc_criteria = it.get("criteria") or []
            _sc_items_list = it.get("items") or []
            _sc_crit_ths = "".join(
                f'<th class="sc-crit-h">'
                f'<input class="sc-crit-name" value="{_esc(_c)}" placeholder="評価軸" onfocus="this.select()">'
                f'<button type="button" class="sc-del-crit-btn" onclick="scDelCrit(this)">✕</button>'
                f'</th>'
                for _c in _sc_criteria
            )
            _sc_item_rows = ""
            for _it_name in _sc_items_list:
                _sc_score_cells = "".join(
                    f'<td class="sc-score-td">'
                    f'<input type="number" class="sc-score-inp" min="1" max="5" placeholder="—"'
                    f' oninput="scUpdateTotal(this)">'
                    f'</td>'
                    for _ in _sc_criteria
                )
                _sc_item_rows += (
                    f'<tr class="sc-item-row">'
                    f'<td class="sc-item-name-td">'
                    f'<input class="sc-item-name" value="{_esc(_it_name)}" placeholder="対象名" onfocus="this.select()">'
                    f'<button type="button" class="sc-del-item-btn" onclick="scDelItem(this)">✕</button>'
                    f'</td>'
                    f'{_sc_score_cells}'
                    f'<td class="sc-total-td">—</td>'
                    f'</tr>'
                )
            fields_html += (
                f'<div class="hq-item{branch_class}" style="margin:14px 0"{branch_attrs}>'
                f'<label style="font-weight:700;color:#d97706;font-size:13px;margin-bottom:6px;display:block">{label}</label>'
                f'<input type="hidden" name="answer_{i}" id="sc_answer_{i}">'
                f'<div class="sc-wrapper" id="sc_wrapper_{i}" data-sc-idx="{i}">'
                f'<div style="overflow-x:auto">'
                f'<table class="sc-table">'
                f'<thead><tr>'
                f'<th class="sc-corner-h">対象 ↓</th>'
                f'{_sc_crit_ths}'
                f'<th class="sc-total-h">合計</th>'
                f'</tr></thead>'
                f'<tbody id="sc_tbody_{i}">{_sc_item_rows}</tbody>'
                f'</table>'
                f'</div>'
                f'<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">'
                f'<button type="button" class="btn sec" onclick="scAddItem({i})">＋対象追加</button>'
                f'<button type="button" class="btn sec" style="border-color:#d9770660;color:#d97706"'
                f' onclick="scAddCrit({i})">＋評価軸追加</button>'
                f'</div>'
                f'</div>'
                f'</div>'
            )
        elif it.get("type") == "yabane":
            _yb_pairs = it.get("rows")
            if _yb_pairs is None:
                # 旧形式（departments/steps）のテンプレートからの変換: 1ステップ1行、部署は空欄
                _yb_pairs = [{"step": _s.get("label", ""), "dept": ""}
                            for _s in (it.get("steps") or [{"label": "ステップ1"}])]
            _yb_prev = pv if isinstance(pv, dict) else None
            if _yb_prev is not None:
                _yb_rows = _yabane_rows_from_answer(_yb_prev)
            else:
                # テンプレートで定義した「ステップ・部署」の並びをそのまま行にする
                # （掛け算はしない。1テンプレ行 = 1ヒアリング行）。
                _yb_rows = [
                    {"step": _p.get("step", ""), "dept": _p.get("dept", ""), "content": "", "output": "",
                     "issue": "", "target": "", "target_number": ""}
                    for _p in (_yb_pairs or [{"step": "", "dept": ""}])
                ]
            if not _yb_rows:
                _yb_rows = [{"step": "", "dept": "", "content": "", "output": "",
                            "issue": "", "target": "", "target_number": ""}]

            def _yb_row_html(_r):
                return (
                    f'<tr class="yb-row">'
                    f'<td class="yb-step-cell"><input class="yb-row-step" value="{_esc(_r.get("step",""))}"'
                    f' placeholder="ステップ名"></td>'
                    f'<td class="yb-dept-cell"><input class="yb-row-dept" value="{_esc(_r.get("dept",""))}"'
                    f' placeholder="部署名"></td>'
                    f'<td class="yb-wide-cell"><textarea class="yb-row-content"'
                    f' placeholder="作業内容">{_esc(_r.get("content",""))}</textarea></td>'
                    f'<td class="yb-wide-cell"><textarea class="yb-row-output"'
                    f' placeholder="アウトプット">{_esc(_r.get("output",""))}</textarea></td>'
                    f'<td class="yb-wide-cell"><textarea class="yb-row-issue"'
                    f' placeholder="現行課題">{_esc(_r.get("issue",""))}</textarea></td>'
                    f'<td class="yb-wide-cell"><textarea class="yb-row-target"'
                    f' placeholder="目指す姿">{_esc(_r.get("target",""))}</textarea></td>'
                    f'<td class="yb-wide-cell"><textarea class="yb-row-target-number"'
                    f' placeholder="目標数値（作業時間等）">{_esc(_r.get("target_number",""))}</textarea></td>'
                    f'<td class="yb-del-cell"><button type="button" class="yb-del-row-btn"'
                    f' onclick="ybDelRow(this)">✕</button></td>'
                    f'</tr>'
                )

            _yb_rows_html = "".join(_yb_row_html(_r) for _r in _yb_rows)
            fields_html += (
                f'<div class="hq-item" style="margin:14px 0">'
                f'<label style="font-weight:700;color:#2f6fed;font-size:13px;margin-bottom:6px;display:block">{label}</label>'
                f'<input type="hidden" name="answer_{i}" id="yb_answer_{i}">'
                f'<div class="yb-wrapper" id="yb_wrapper_{i}" data-yb-idx="{i}">'
                f'<table class="yb-table">'
                f'<thead><tr>'
                f'<th class="yb-step-h">ステップ</th><th class="yb-dept-h">部署</th>'
                f'<th class="yb-wide-h">作業内容</th><th class="yb-wide-h">アウトプット</th>'
                f'<th class="yb-wide-h">現行課題</th><th class="yb-wide-h">目指す姿</th>'
                f'<th class="yb-wide-h">目標数値</th><th class="yb-del-h"></th>'
                f'</tr></thead>'
                f'<tbody id="yb_tbody_{i}">{_yb_rows_html}</tbody>'
                f'</table>'
                f'</div>'
                f'<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">'
                f'<button type="button" class="btn sec" onclick="ybAddRow({i})">＋行追加</button>'
                f'<button type="button" class="btn sec" style="border-color:#3b82f660;color:#3b82f6"'
                f' onclick="ybAddStepBlock({i})">＋ステップ追加（全部署ぶん）</button>'
                f'<button type="button" class="btn sec" style="border-color:#3b82f660;color:#3b82f6"'
                f' onclick="ybAddDeptBlock({i})">＋部署追加（全ステップぶん）</button>'
                f'</div>'
                f'</div>'
            )
        elif it.get("type") == "choice":
            opts = it.get("options") or []
            if it.get("multi"):
                cur = set(pv if isinstance(pv, list) else ([pv] if pv else []))
                boxes = "".join(
                    f'<label style="display:inline-flex;align-items:center;gap:6px;margin:2px 12px 2px 0;font-weight:400">'
                    f'<input type="checkbox" name="answer_{i}" value="{_esc(o)}"'
                    f'{" checked" if o in cur else ""} style="width:14px;height:14px">{_esc(o)}</label>'
                    for o in opts
                )
                fields_html += (f'<div class="hq-item{branch_class}"{branch_attrs} style="margin:10px 0">'
                                f'<label>{label}{req}</label><div>{boxes}</div></div>')
            else:
                radios = "".join(
                    f'<label style="display:inline-flex;align-items:center;gap:6px;margin:2px 12px 2px 0;font-weight:400">'
                    f'<input type="radio" name="answer_{i}" value="{_esc(o)}"'
                    f'{" checked" if pv == o else ""}{req_attr} style="width:14px;height:14px">{_esc(o)}</label>'
                    for o in opts
                )
                fields_html += (f'<div class="hq-item{branch_class}"{branch_attrs} style="margin:10px 0">'
                                f'<label>{label}{req}</label><div>{radios}</div></div>')
        elif it.get("type") == "number":
            val = _esc(str(pv) if pv is not None else "")
            fields_html += (f'<div class="hq-item{branch_class}"{branch_attrs} style="margin:10px 0">'
                            f'<label>{label}{req}</label>'
                            f'<input type="number" name="answer_{i}" value="{val}" step="any"'
                            f' inputmode="numeric" style="max-width:200px"{req_attr}></div>')
        else:
            val = _esc(pv if isinstance(pv, str) else "")
            fields_html += (f'<div class="hq-item{branch_class}"{branch_attrs} style="margin:10px 0">'
                            f'<label>{label}{req}</label>'
                            f'<textarea name="answer_{i}" rows="2"{req_attr}>{val}</textarea></div>')

    prev_note = (f'<p class="muted" style="font-size:12px;margin:0 0 10px">'
                 f'前回ヒアリング（{_esc(prev_date)}）の内容を引用しています。保存すると新しい履歴として追加されます。</p>'
                 if prefill and prev_date and not draft_data else "")
    draft_note = (
        f'<p class="muted" style="font-size:12px;margin:0 0 10px;color:#b45309">'
        f'⚠ 自動保存された下書き（{_esc((draft_updated_at or "")[:16])} 時点）を復元しました。'
        f'内容を確認し、問題なければそのまま保存してください。'
        f'<br>※レーダーチャート／タイムライン／スコアカード形式の項目は自動保存の対象データは残りますが、'
        f'画面への自動復元は未対応です。該当項目があれば再入力してください。</p>'
        if draft_data else ""
    )
    guide_html = (
        '<div style="position:relative;margin-left:auto;font-size:12px"'
        ' onmouseenter="this.querySelector(\'.hq-guide-popup\').style.display=\'block\'"'
        ' onmouseleave="this.querySelector(\'.hq-guide-popup\').style.display=\'none\'">'
        '<div style="cursor:default;color:#2f6fed;font-weight:600;padding:4px 10px;'
        'background:#e8f0fe;border-radius:6px;white-space:nowrap;user-select:none">📋 入力ガイド</div>'
        '<div class="hq-guide-popup" style="display:none;position:absolute;right:0;top:calc(100% + 4px);'
        'z-index:200;background:#fff;border:1px solid #d0e4ff;border-radius:8px;padding:12px 16px;'
        'width:340px;box-shadow:0 4px 16px rgba(0,0,0,.12);line-height:1.75;color:#3a4760">'
        '<p style="margin:0 0 8px;font-weight:700;color:#2f6fed;font-size:13px">このシートの使い方</p>'
        '<ul style="margin:0;padding-left:18px;font-size:12px">'
        '<li><strong>グレーの項目</strong>は<em>分岐（条件付き）質問</em>です。上の質問の回答によって活性化します。</li>'
        '<li>グレーの項目も<strong>直接入力・選択できます</strong>（入力すると分岐元の回答が自動でセットされます）。</li>'
        '<li>分岐元の回答を変えるとグレー項目の入力は<strong>自動でクリア</strong>されます。</li>'
        '<li><span style="color:#c0392b">*</span> 印は必須項目です。</li>'
        '</ul>'
        '</div>'
        '</div>'
    ) if has_branch else ""
    return f"""
    <style>
    .hq-branch {{ transition: opacity .25s, filter .25s; }}
    .hq-branch.hq-inactive {{ opacity: .38; filter: grayscale(.35); }}
    .hq-branch.hq-inactive > label:first-child {{ color: #aaa; }}
    .hq-sticky {{
      position: sticky; top: 0; z-index: 50;
      background: #fff; border-bottom: 1px solid #e2e6ee;
      box-shadow: 0 2px 6px rgba(0,0,0,.06);
      padding: 10px 20px 10px; margin: -20px -16px 16px;
    }}
    .hq-sticky-inner {{
      max-width: 760px; margin: 0 auto;
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
      position: relative;
    }}
    /* ── レーダーチャート ── */
    .ra-wrapper{{margin-top:4px}}
    .ra-axis-row{{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #e8ecf0}}
    .ra-axis-label-inp{{border:none;background:transparent;color:#1e293b;font-weight:600;font-size:13px;width:120px;padding:2px 4px;outline:none;cursor:text;font-family:inherit}}
    .ra-axis-label-inp:focus{{background:rgba(37,99,235,.07);border-radius:3px}}
    .ra-score-range{{flex:1;accent-color:#2563eb;cursor:pointer;height:4px}}
    .ra-score-val{{font-weight:700;font-size:14px;color:#2563eb;min-width:18px;text-align:right}}
    .ra-svg text{{font-family:inherit}}
    /* ── タイムライン ── */
    .tl-wrapper{{margin-top:4px}}
    .tl-track{{position:relative;padding-left:24px}}
    .tl-track::before{{content:'';position:absolute;left:8px;top:0;bottom:0;width:2px;background:#d1fae5}}
    .tl-event{{position:relative;margin-bottom:12px}}
    .tl-dot{{position:absolute;left:-20px;top:14px;width:10px;height:10px;border-radius:50%;background:#059669;border:2px solid #fff;box-shadow:0 0 0 2px #059669}}
    .tl-event-body{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:8px 10px}}
    .tl-event-header{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
    .tl-date{{border:1px solid #d4dae4;border-radius:4px;padding:4px 6px;font-size:12px;color:#1e293b;font-family:inherit}}
    .tl-event-label{{flex:1;min-width:120px;border:none;background:transparent;font-weight:600;font-size:13px;color:#065f46;outline:none;cursor:text;padding:2px 4px}}
    .tl-event-label:focus{{background:rgba(5,150,105,.07);border-radius:3px}}
    .tl-del-btn{{font-size:10px;padding:2px 6px;background:#fde8e8;color:#c0392b;border:none;cursor:pointer;border-radius:3px;flex-shrink:0}}
    .tl-note{{width:100%;resize:vertical;font-size:12px;border:1px solid #bbf7d0;border-radius:4px;padding:5px;color:#1e293b;background:#fff;font-family:inherit}}
    /* ── スコアカード ── */
    .sc-wrapper{{overflow-x:auto;margin-top:4px}}
    .sc-table{{border-collapse:collapse;min-width:300px}}
    .sc-corner-h{{background:#fef3c7;color:#92400e;font-weight:700;padding:8px 10px;border:1px solid #fde68a;text-align:center;font-size:11px;white-space:nowrap;min-width:100px}}
    .sc-crit-h{{background:#fef3c7;border:1px solid #fde68a;padding:5px 6px;text-align:center;vertical-align:middle;min-width:90px}}
    .sc-crit-name{{border:none;background:transparent;color:#92400e;font-weight:700;font-size:12px;text-align:center;width:calc(100% - 20px);padding:2px 4px;outline:none;cursor:text;font-family:inherit}}
    .sc-crit-name:focus{{background:rgba(146,64,14,.07);border-radius:3px}}
    .sc-del-crit-btn{{font-size:10px;padding:1px 4px;background:#fde8e8;color:#c0392b;border:none;cursor:pointer;border-radius:2px;margin-left:4px;vertical-align:middle}}
    .sc-total-h{{background:#fef3c7;border:1px solid #fde68a;padding:6px;text-align:center;font-size:11px;color:#92400e;font-weight:700;white-space:nowrap;min-width:44px}}
    .sc-item-name-td{{padding:4px 8px;border:1px solid #fde68a;vertical-align:middle;background:#fffbeb;white-space:nowrap}}
    .sc-item-name{{border:none;background:transparent;color:#d97706;font-weight:600;font-size:13px;padding:2px 4px;outline:none;cursor:text;font-family:inherit;width:calc(100% - 20px)}}
    .sc-item-name:focus{{background:rgba(217,119,6,.07);border-radius:3px}}
    .sc-del-item-btn{{font-size:10px;padding:1px 4px;background:#fde8e8;color:#c0392b;border:none;cursor:pointer;border-radius:2px;margin-left:4px;vertical-align:middle}}
    .sc-score-td{{padding:4px;border:1px solid #fde68a;text-align:center;background:#fff}}
    .sc-score-inp{{width:52px;text-align:center;border:1px solid #d4dae4;border-radius:4px;padding:4px;font-size:13px;font-family:inherit}}
    .sc-total-td{{padding:4px 8px;border:1px solid #fde68a;text-align:center;font-weight:700;font-size:13px;color:#d97706;background:#fffbeb;white-space:nowrap}}
    /* ── 矢羽（1業務1行のフラット表。ステップ列は横スクロール時に固定） ── */
    .yb-wrapper{{overflow-x:auto;margin-top:4px;max-width:100%}}
    .yb-table{{border-collapse:collapse;min-width:900px}}
    .yb-step-h,.yb-dept-h,.yb-wide-h,.yb-del-h{{
      background:#e8eeff;color:#3730a3;font-weight:700;padding:8px 10px;
      border:1px solid #c7d2fe;text-align:center;font-size:11px;white-space:nowrap;
    }}
    .yb-step-h{{position:sticky;left:0;z-index:2;min-width:120px}}
    .yb-dept-h{{min-width:120px}}
    .yb-wide-h{{min-width:240px}}
    .yb-del-h{{min-width:32px}}
    .yb-step-cell{{position:sticky;left:0;z-index:1;background:#eef2ff;border:1px solid #c7d2fe;padding:5px;vertical-align:top}}
    .yb-dept-cell{{border:1px solid #dde4f0;padding:5px;vertical-align:top;background:#fff;min-width:120px}}
    .yb-wide-cell{{border:1px solid #dde4f0;padding:5px;vertical-align:top;background:#fff;min-width:240px}}
    .yb-del-cell{{border:1px solid #dde4f0;padding:0;text-align:center;vertical-align:middle}}
    .yb-row-step,.yb-row-dept{{width:100%;border:1px solid #d4dae4;border-radius:4px;padding:6px;font-size:13px;font-family:inherit}}
    .yb-row-step{{font-weight:700;color:#3730a3}}
    .yb-wide-cell textarea{{width:100%;min-height:54px;resize:vertical;font-size:13px;margin:0;border:1px solid #d4dae4;border-radius:4px;padding:6px;color:#1e293b;font-family:inherit}}
    .yb-del-row-btn{{font-size:11px;padding:6px 8px;background:#fde8e8;color:#c0392b;border:none;cursor:pointer;height:100%}}
    </style>
    <div class="hq-sticky">
      <div class="hq-sticky-inner">
        <div style="flex:1;min-width:0">
          <div style="font-size:15px;font-weight:700;color:#1d2430;margin-bottom:2px">ヒアリング入力</div>
          <div style="font-size:12px;color:#6b7689;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            <strong style="color:#3a4760">対象:</strong> {_esc(target_label)}
            <span style="margin:0 6px;color:#d4dae4">|</span>
            <strong style="color:#3a4760">テンプレート:</strong> {_esc(template.get('name', ''))}
          </div>
        </div>
        {guide_html}
      </div>
    </div>
    <div class="card" style="max-width:760px">
      {prev_note}
      {draft_note}
      <p id="hq_autosave_status" class="muted" style="font-size:11px;margin:0 0 8px"></p>
      <form method="post" action="/hearing/submit" id="hearing_form">
        <input type="hidden" name="target_type" value="{_esc(target_type)}">
        <input type="hidden" name="target_id" value="{_esc(target_id)}">
        <input type="hidden" name="template_id" value="{template['id']}">
        {f'<input type="hidden" name="edit_result_id" value="{edit_result_id}">' if edit_result_id else ""}

        <div style="background:#f0f6ff;border-radius:8px;padding:14px 16px;margin-bottom:16px">
          <p style="margin:0 0 6px;font-weight:600;color:#2f6fed">ヒアリング項目</p>
          {fields_html or '<p class="muted">このテンプレートには項目がありません。</p>'}
        </div>

        {f'''<div style="border:1px solid #e2e6ee;border-radius:8px;padding:14px 16px">
          <p style="margin:0 0 8px;font-weight:600;color:#555">ヒアリング実施日</p>
          <input type="date" name="occurred_on" value="{_esc(edit_conducted_on)}" required style="max-width:200px">
        </div>
        <div style="margin-top:16px"><button class="btn" type="submit">更新（この内容でヒアリング結果を修正）</button>
        <a class="btn sec" href="/hearing/result/{edit_result_id}">キャンセル</a></div>''' if edit_result_id else f'''
        <div style="border:1px solid #e2e6ee;border-radius:8px;padding:14px 16px">
          <p style="margin:0 0 8px;font-weight:600;color:#555">活動履歴として記録</p>
          <div class="grid">
            <div><label>ヒアリング日</label><input type="date" name="occurred_on" required></div>
            <div><label>種別</label><select name="type">{_opt(sfa_db.get_master_list(con,'activity_types'), '面談')}</select></div>
            <div><label>相手</label><input name="contact_name" placeholder="例：田中部長"></div>
          </div>
          <label>内容・決定事項</label><textarea name="body" rows="3"></textarea>
          <div style="margin-top:10px;padding:12px;background:#f8f9fa;border-radius:6px">
            <p style="margin:0 0 8px;font-size:.9em;font-weight:600;color:#555">商談の現状を更新（任意）</p>
            <div class="grid">
              <div><label>次回MS日</label><input type="date" name="next_milestone_date"></div>
              <div><label>次回MSラベル</label><input name="next_milestone_label"></div>
            </div>
            <label>現状メモ</label><textarea name="update_note" rows="2"></textarea>
          </div>
        </div>
        <div style="margin-top:16px"><button class="btn" type="submit">保存（活動履歴＋ヒアリング結果を記録）</button>
        <a class="btn sec" href="/hearings">キャンセル</a></div>'''}
      </form>
    </div>
    <script>
    // ── レーダーチャート JS ──
    function _svgEsc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
    function raRedraw(wrapper) {{
      var rows=Array.from(wrapper.querySelectorAll('.ra-axis-row'));
      var n=rows.length; if(n<3) return;
      var svg=wrapper.querySelector('.ra-svg');
      var cx=100,cy=100,R=72;
      var html='';
      for(var g=1;g<=5;g++){{
        var gr=R*g/5;
        html+='<circle cx="'+cx+'" cy="'+cy+'" r="'+gr+'" fill="none" stroke="#e2e8f0" stroke-width="1"/>';
      }}
      var angles=[],scores=[];
      for(var i=0;i<n;i++){{
        angles.push(-Math.PI/2+i*2*Math.PI/n);
        scores.push(parseFloat(rows[i].querySelector('.ra-score-range').value)||0);
      }}
      for(var i=0;i<n;i++){{
        var ax=cx+R*Math.cos(angles[i]), ay=cy+R*Math.sin(angles[i]);
        html+='<line x1="'+cx+'" y1="'+cy+'" x2="'+ax+'" y2="'+ay+'" stroke="#cbd5e1" stroke-width="1"/>';
        var lx=cx+(R+15)*Math.cos(angles[i]), ly=cy+(R+15)*Math.sin(angles[i]);
        var anc=lx<cx-8?'end':lx>cx+8?'start':'middle';
        var lbl=rows[i].querySelector('.ra-axis-label-inp')?rows[i].querySelector('.ra-axis-label-inp').value:'';
        html+='<text x="'+lx+'" y="'+(ly+4)+'" text-anchor="'+anc+'" font-size="10" fill="#475569">'+_svgEsc(lbl)+'</text>';
      }}
      var poly='';
      for(var i=0;i<n;i++){{
        var pr=R*scores[i]/5;
        poly+=(cx+pr*Math.cos(angles[i]))+','+(cy+pr*Math.sin(angles[i]))+' ';
      }}
      html+='<polygon points="'+poly.trim()+'" fill="rgba(124,58,237,.2)" stroke="#7c3aed" stroke-width="2"/>';
      for(var i=0;i<n;i++){{
        var pr=R*scores[i]/5;
        html+='<circle cx="'+(cx+pr*Math.cos(angles[i]))+'" cy="'+(cy+pr*Math.sin(angles[i]))+'" r="4" fill="#7c3aed"/>';
      }}
      svg.innerHTML=html;
    }}
    function raUpdateScore(rangeEl) {{
      rangeEl.nextElementSibling.textContent=rangeEl.value;
      raRedraw(rangeEl.closest('[data-ra-idx]'));
    }}
    // ── タイムライン JS ──
    function tlDelEvent(btn){{btn.closest('.tl-event').remove();}}
    function tlAddEvent(idx){{
      var track=document.getElementById('tl_track_'+idx);
      var ev=document.createElement('div'); ev.className='tl-event';
      ev.innerHTML='<div class="tl-dot"></div>'
        +'<div class="tl-event-body">'
        +'<div class="tl-event-header">'
        +'<input type="month" class="tl-date">'
        +'<input class="tl-event-label" placeholder="マイルストーン名" onfocus="this.select()">'
        +'<button type="button" class="tl-del-btn" onclick="tlDelEvent(this)">✕</button>'
        +'</div>'
        +'<textarea class="tl-note" rows="2" placeholder="詳細・メモ"></textarea>'
        +'</div>';
      track.appendChild(ev);
    }}
    // ── スコアカード JS ──
    function scUpdateTotal(inp){{
      var row=inp.closest('.sc-item-row');
      var sum=0;
      row.querySelectorAll('.sc-score-inp').forEach(function(i){{var v=parseFloat(i.value); if(!isNaN(v))sum+=v;}});
      row.querySelector('.sc-total-td').textContent=sum||'—';
    }}
    function scDelItem(btn){{btn.closest('.sc-item-row').remove();}}
    function scDelCrit(btn){{
      var th=btn.closest('.sc-crit-h');
      var table=th.closest('table');
      var colIdx=Array.from(th.closest('tr').querySelectorAll('.sc-crit-h')).indexOf(th);
      th.remove();
      table.querySelectorAll('.sc-item-row').forEach(function(tr){{
        var cells=tr.querySelectorAll('.sc-score-td');
        if(cells[colIdx]) cells[colIdx].remove();
      }});
    }}
    function scAddItem(idx){{
      var wrapper=document.getElementById('sc_wrapper_'+idx);
      var nCols=wrapper.querySelectorAll('thead .sc-crit-h').length;
      var tr=document.createElement('tr'); tr.className='sc-item-row';
      var ntd=document.createElement('td'); ntd.className='sc-item-name-td';
      var ni=document.createElement('input'); ni.className='sc-item-name'; ni.placeholder='対象名'; ni.onfocus=function(){{this.select();}};
      var db=document.createElement('button'); db.type='button'; db.className='sc-del-item-btn';
      db.textContent='✕'; db.onclick=function(){{scDelItem(this);}};
      ntd.appendChild(ni); ntd.appendChild(db); tr.appendChild(ntd);
      for(var c=0;c<nCols;c++){{
        var td=document.createElement('td'); td.className='sc-score-td';
        var inp=document.createElement('input'); inp.type='number'; inp.className='sc-score-inp';
        inp.min=1; inp.max=5; inp.placeholder='—'; inp.oninput=function(){{scUpdateTotal(this);}};
        td.appendChild(inp); tr.appendChild(td);
      }}
      var ttd=document.createElement('td'); ttd.className='sc-total-td'; ttd.textContent='—';
      tr.appendChild(ttd);
      wrapper.querySelector('tbody').appendChild(tr);
    }}
    function scAddCrit(idx){{
      var wrapper=document.getElementById('sc_wrapper_'+idx);
      var hdrRow=wrapper.querySelector('thead tr');
      var totalTh=hdrRow.querySelector('.sc-total-h');
      var th=document.createElement('th'); th.className='sc-crit-h';
      var inp=document.createElement('input'); inp.className='sc-crit-name'; inp.placeholder='評価軸'; inp.onfocus=function(){{this.select();}};
      var db=document.createElement('button'); db.type='button'; db.className='sc-del-crit-btn';
      db.textContent='✕'; db.onclick=function(){{scDelCrit(this);}};
      th.appendChild(inp); th.appendChild(db); hdrRow.insertBefore(th,totalTh);
      wrapper.querySelectorAll('.sc-item-row').forEach(function(tr){{
        var totalTd=tr.querySelector('.sc-total-td');
        var td=document.createElement('td'); td.className='sc-score-td';
        var sinp=document.createElement('input'); sinp.type='number'; sinp.className='sc-score-inp';
        sinp.min=1; sinp.max=5; sinp.placeholder='—'; sinp.oninput=function(){{scUpdateTotal(this);}};
        td.appendChild(sinp); tr.insertBefore(td,totalTd);
      }});
    }}
    // ── 矢羽入力 JS（1業務1行のフラット表） ──
    function ybDelRow(btn) {{ btn.closest('.yb-row').remove(); }}
    function _ybRowHtml(step, dept) {{
      function esc(s) {{ return (s||'').replace(/"/g,'&quot;'); }}
      return '<tr class="yb-row">' +
        '<td class="yb-step-cell"><input class="yb-row-step" value="'+esc(step)+'" placeholder="ステップ名"></td>' +
        '<td class="yb-dept-cell"><input class="yb-row-dept" value="'+esc(dept)+'" placeholder="部署名"></td>' +
        '<td class="yb-wide-cell"><textarea class="yb-row-content" placeholder="作業内容"></textarea></td>' +
        '<td class="yb-wide-cell"><textarea class="yb-row-output" placeholder="アウトプット"></textarea></td>' +
        '<td class="yb-wide-cell"><textarea class="yb-row-issue" placeholder="現行課題"></textarea></td>' +
        '<td class="yb-wide-cell"><textarea class="yb-row-target" placeholder="目指す姿"></textarea></td>' +
        '<td class="yb-wide-cell"><textarea class="yb-row-target-number" placeholder="目標数値（作業時間等）"></textarea></td>' +
        '<td class="yb-del-cell"><button type="button" class="yb-del-row-btn" onclick="ybDelRow(this)">✕</button></td>' +
      '</tr>';
    }}
    function ybAddRow(idx, step, dept) {{
      var tbody=document.getElementById('yb_tbody_'+idx);
      tbody.insertAdjacentHTML('beforeend', _ybRowHtml(step||'', dept||''));
    }}
    function ybAddStepBlock(idx) {{
      var tbody=document.getElementById('yb_tbody_'+idx);
      var depts=Array.from(new Set(Array.from(tbody.querySelectorAll('.yb-row-dept'))
        .map(function(i){{return i.value.trim();}}).filter(Boolean)));
      if (!depts.length) depts=[''];
      depts.forEach(function(d){{ ybAddRow(idx, '', d); }});
    }}
    function ybAddDeptBlock(idx) {{
      var tbody=document.getElementById('yb_tbody_'+idx);
      var steps=Array.from(new Set(Array.from(tbody.querySelectorAll('.yb-row-step'))
        .map(function(i){{return i.value.trim();}}).filter(Boolean)));
      if (!steps.length) steps=[''];
      steps.forEach(function(s){{ ybAddRow(idx, s, ''); }});
    }}
    // 各特殊項目のUI状態を隠しinput(answer_N)へ直列化する。
    // submit時・30秒ごとの自動保存時の両方から呼ぶ。
    function serializeAllAnswers() {{
      // レーダーチャート直列化
      document.querySelectorAll('[data-ra-idx]').forEach(function(wrapper) {{
        var idx=wrapper.getAttribute('data-ra-idx');
        var axes=[];
        wrapper.querySelectorAll('.ra-axis-row').forEach(function(row) {{
          axes.push({{
            label:row.querySelector('.ra-axis-label-inp').value.trim(),
            score:parseFloat(row.querySelector('.ra-score-range').value)||0
          }});
        }});
        var h=document.getElementById('ra_answer_'+idx);
        if(h) h.value=JSON.stringify({{axes:axes}});
      }});
      // タイムライン直列化
      document.querySelectorAll('[data-tl-idx]').forEach(function(wrapper) {{
        var idx=wrapper.getAttribute('data-tl-idx');
        var events=[];
        wrapper.querySelectorAll('.tl-event').forEach(function(ev) {{
          events.push({{
            label:ev.querySelector('.tl-event-label').value.trim(),
            date:ev.querySelector('.tl-date').value,
            note:ev.querySelector('.tl-note').value.trim()
          }});
        }});
        var h=document.getElementById('tl_answer_'+idx);
        if(h) h.value=JSON.stringify({{events:events}});
      }});
      // スコアカード直列化
      document.querySelectorAll('[data-sc-idx]').forEach(function(wrapper) {{
        var idx=wrapper.getAttribute('data-sc-idx');
        var crits=Array.from(wrapper.querySelectorAll('thead .sc-crit-name')).map(function(i){{return i.value.trim();}});
        var items=[];
        wrapper.querySelectorAll('.sc-item-row').forEach(function(tr) {{
          var name=tr.querySelector('.sc-item-name').value.trim();
          var scores={{}};
          tr.querySelectorAll('.sc-score-inp').forEach(function(inp,di) {{
            if(crits[di]!==undefined){{var v=parseFloat(inp.value); scores[crits[di]]=isNaN(v)?null:v;}}
          }});
          items.push({{label:name,scores:scores}});
        }});
        var h=document.getElementById('sc_answer_'+idx);
        if(h) h.value=JSON.stringify({{criteria:crits,items:items}});
      }});
      document.querySelectorAll('[data-yb-idx]').forEach(function(wrapper) {{
        var idx=wrapper.getAttribute('data-yb-idx');
        var rows=[];
        wrapper.querySelectorAll('.yb-row').forEach(function(tr) {{
          rows.push({{
            step: tr.querySelector('.yb-row-step').value.trim(),
            dept: tr.querySelector('.yb-row-dept').value.trim(),
            content: tr.querySelector('.yb-row-content').value,
            output: tr.querySelector('.yb-row-output').value,
            issue: tr.querySelector('.yb-row-issue').value,
            target: tr.querySelector('.yb-row-target').value,
            target_number: tr.querySelector('.yb-row-target-number').value
          }});
        }});
        var hidden=document.getElementById('yb_answer_'+idx);
        if(hidden) hidden.value=JSON.stringify({{rows:rows}});
      }});
    }}
    document.getElementById('hearing_form').addEventListener('submit', function() {{
      serializeAllAnswers();
      if (window._hqAutosaveTimer) clearInterval(window._hqAutosaveTimer);
    }});
    // ── 30秒ごとの自動保存（下書き） ──
    (function() {{
      var form = document.getElementById('hearing_form');
      var statusEl = document.getElementById('hq_autosave_status');
      function autoSave() {{
        serializeAllAnswers();
        var data = new URLSearchParams(new FormData(form));
        fetch('/hearing/autosave', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
          body: data.toString()
        }}).then(function(r) {{ return r.json(); }}).then(function(res) {{
          if (statusEl) {{
            var now = new Date();
            var hh = String(now.getHours()).padStart(2,'0');
            var mm = String(now.getMinutes()).padStart(2,'0');
            statusEl.textContent = res.ok ? ('下書きを自動保存しました（' + hh + ':' + mm + '）') : '自動保存に失敗しました';
          }}
        }}).catch(function() {{
          if (statusEl) statusEl.textContent = '自動保存に失敗しました（通信エラー）';
        }});
      }}
      window._hqAutosaveTimer = setInterval(autoSave, 30000);
    }})();
    // レーダーチャート初期描画
    document.querySelectorAll('[data-ra-idx]').forEach(function(w){{raRedraw(w);}});
    // ── 分岐ロジック ──
    (function() {{
      var form = document.getElementById('hearing_form');
      if (!form) return;
      var _initialLoad = true;

      function getParentValues(parentIdx) {{
        // 複数選択（チェックボックス）では複数チェックされ得るため、配列で全件返す
        // （以前はchecked[0]のみ見ており、2つ目以降の親子関係が機能しなかった）。
        var name = 'answer_' + parentIdx;
        var checked = form.querySelectorAll('[name="' + name + '"]:checked');
        if (checked.length) return Array.from(checked).map(function(c) {{ return c.value; }});
        // radio/checkbox は :checked がなければ未回答
        var firstInp = form.querySelector('[name="' + name + '"]');
        if (firstInp && (firstInp.type === 'radio' || firstInp.type === 'checkbox')) return [];
        // textarea / number input
        var inp = form.querySelector('textarea[name="' + name + '"],input[type="number"][name="' + name + '"]');
        return inp ? [inp.value.trim()] : [];
      }}

      function updateBranch() {{
        form.querySelectorAll('.hq-branch').forEach(function(div) {{
          var pIdx = div.dataset.parentIdx;
          var pVal = div.dataset.parentValue;
          if (pIdx === undefined || pVal === undefined) return;
          var shouldInactive = getParentValues(parseInt(pIdx)).indexOf(pVal) === -1;
          div.classList.toggle('hq-inactive', shouldInactive);
        }});
        _initialLoad = false;
      }}

      function autoSetParent(changedEl) {{
        var branchDiv = changedEl.closest('.hq-branch');
        if (!branchDiv) return;
        var pIdx = branchDiv.dataset.parentIdx;
        var pVal = branchDiv.dataset.parentValue;
        if (pIdx === undefined || pVal === undefined) return;
        var parentName = 'answer_' + pIdx;
        var parentInput = form.querySelector('[name="' + parentName + '"][value="' + CSS.escape(pVal) + '"]');
        if (parentInput && (parentInput.type === 'radio' || parentInput.type === 'checkbox') && !parentInput.checked) {{
          parentInput.checked = true;
          updateBranch();
        }}
      }}

      form.addEventListener('change', function(e) {{
        updateBranch();
        if (e.target.type === 'radio' || e.target.type === 'checkbox') autoSetParent(e.target);
      }});
      form.addEventListener('input', function(e) {{
        if (e.target.tagName === 'TEXTAREA' || e.target.type === 'number') {{
          updateBranch();
          autoSetParent(e.target);
        }}
      }});

      updateBranch();
    }})();
    </script>"""


def _format_answer(ans) -> str:
    if isinstance(ans, list):
        return "、".join(str(a) for a in ans)
    return str(ans) if ans is not None else ""


def _yabane_rows_from_answer(ans: dict) -> list:
    """矢羽回答から行リストを取得する。新形式(rows)優先、旧形式(departments/steps[].cells)は変換。"""
    rows = ans.get("rows")
    if rows is not None:
        return rows
    converted = []
    for s in (ans.get("steps") or []):
        for d in (ans.get("departments") or []):
            content = (s.get("cells") or {}).get(d, "")
            if content:
                converted.append({"step": s.get("label", ""), "dept": d, "content": content})
    return converted


def _format_answer_for_export(a: dict) -> str:
    """一覧プレビュー・CSV/xlsx出力向け: 矢羽など特殊タイプも読める文字列に整形する。"""
    ans = a.get("answer")
    if a.get("type") == "yabane" and isinstance(ans, dict):
        parts = []
        for r in _yabane_rows_from_answer(ans):
            frags = [f"{r.get('step','')}/{r.get('dept','')}"]
            for key, fkey in (("content", "作業内容"), ("output", "アウトプット"), ("issue", "現行課題"),
                              ("target", "目指す姿"), ("target_number", "目標数値")):
                v = r.get(key)
                if v:
                    frags.append(f"{fkey}:{v}")
            parts.append(" ".join(frags))
        return "；".join(parts)
    return _format_answer(ans)


def _radar_result_html(ra: dict) -> str:
    axes = ra.get("axes") or []
    if not axes:
        return '<p class="muted">データなし</p>'
    n = len(axes)
    import math
    cx, cy, R = 100, 100, 72
    circles = "".join(
        f'<circle cx="{cx}" cy="{cy}" r="{R*g//5}" fill="none" stroke="#e2e8f0" stroke-width="1"/>'
        for g in range(1, 6)
    )
    axis_lines = ""
    poly_pts = ""
    dots = ""
    for i, ax in enumerate(axes):
        angle = -math.pi / 2 + i * 2 * math.pi / n
        ax_x = cx + R * math.cos(angle)
        ax_y = cy + R * math.sin(angle)
        axis_lines += f'<line x1="{cx}" y1="{cy}" x2="{ax_x:.1f}" y2="{ax_y:.1f}" stroke="#cbd5e1" stroke-width="1"/>'
        lx = cx + (R + 16) * math.cos(angle)
        ly = cy + (R + 16) * math.sin(angle)
        anc = "end" if lx < cx - 8 else ("start" if lx > cx + 8 else "middle")
        axis_lines += (f'<text x="{lx:.1f}" y="{ly+4:.1f}" text-anchor="{anc}"'
                       f' font-size="10" fill="#475569">{_esc(ax.get("label",""))}</text>')
        score = ax.get("score") or 0
        pr = R * score / 5
        poly_pts += f'{cx+pr*math.cos(angle):.1f},{cy+pr*math.sin(angle):.1f} '
        dots += (f'<circle cx="{cx+pr*math.cos(angle):.1f}" cy="{cy+pr*math.sin(angle):.1f}"'
                 f' r="4" fill="#7c3aed"/>')
    poly = f'<polygon points="{poly_pts.strip()}" fill="rgba(124,58,237,.2)" stroke="#7c3aed" stroke-width="2"/>'
    svg = (f'<svg width="200" height="200" style="display:block;margin:0 auto">'
           f'{circles}{axis_lines}{poly}{dots}</svg>')
    rows = "".join(
        f'<tr>'
        f'<td style="padding:5px 10px;font-weight:600;font-size:13px;border-bottom:1px solid #ede9fe">{_esc(ax.get("label",""))}</td>'
        f'<td style="padding:5px 10px;border-bottom:1px solid #ede9fe">'
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<div style="flex:1;height:8px;background:#ede9fe;border-radius:4px;overflow:hidden">'
        f'<div style="width:{(ax.get("score") or 0)*20}%;height:100%;background:#7c3aed;border-radius:4px"></div>'
        f'</div>'
        f'<span style="font-weight:700;color:#7c3aed;min-width:20px">{ax.get("score") or 0}</span>'
        f'</div>'
        f'</td>'
        f'</tr>'
        for ax in axes
    )
    table = (f'<table style="width:100%;border-collapse:collapse;margin-top:8px;max-width:360px">'
             f'<thead><tr>'
             f'<th style="background:#f5f3ff;color:#7c3aed;padding:5px 10px;font-size:11px;text-align:left;border-bottom:1px solid #ede9fe">軸</th>'
             f'<th style="background:#f5f3ff;color:#7c3aed;padding:5px 10px;font-size:11px;text-align:left;border-bottom:1px solid #ede9fe">スコア（/5）</th>'
             f'</tr></thead><tbody>{rows}</tbody></table>')
    return f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">{svg}{table}</div>'


def _timeline_result_html(tl: dict) -> str:
    events = tl.get("events") or []
    if not events:
        return '<p class="muted">データなし</p>'
    items_html = ""
    for ev in events:
        date_str = ev.get("date") or ""
        ev_label = _esc(ev.get("label") or "")
        ev_note = _esc(ev.get("note") or "")
        _date_badge = (f'<span style="font-size:11px;font-weight:700;color:#059669;background:#d1fae5;'
                       f'padding:1px 8px;border-radius:3px">{_esc(date_str)}</span>') if date_str else ""
        _note_p = (f'<p style="font-size:12px;color:#374151;margin:0;white-space:pre-wrap">{ev_note}</p>'
                   ) if ev_note else ""
        items_html += (
            f'<div style="position:relative;padding-left:24px;margin-bottom:12px">'
            f'<div style="position:absolute;left:4px;top:6px;width:10px;height:10px;border-radius:50%;background:#059669;border:2px solid #fff;box-shadow:0 0 0 2px #059669"></div>'
            f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:8px 12px">'
            f'<div style="display:flex;gap:10px;align-items:center;margin-bottom:4px;flex-wrap:wrap">'
            f'{_date_badge}'
            f'<span style="font-weight:700;font-size:13px;color:#065f46">{ev_label}</span>'
            f'</div>'
            f'{_note_p}'
            f'</div>'
            f'</div>'
        )
    return (f'<div style="position:relative;padding-left:8px">'
            f'<div style="position:absolute;left:12px;top:0;bottom:0;width:2px;background:#d1fae5"></div>'
            f'{items_html}</div>')


def _scorecard_result_html(sc: dict) -> str:
    criteria = sc.get("criteria") or []
    items = sc.get("items") or []
    if not criteria or not items:
        return '<p class="muted">データなし</p>'
    crit_ths = "".join(
        f'<th style="background:#fef3c7;color:#92400e;padding:6px 10px;font-size:11px;font-weight:700;border:1px solid #fde68a;text-align:center">{_esc(c)}</th>'
        for c in criteria
    )
    rows_html = ""
    for it in items:
        scores = it.get("scores") or {}
        total = sum(v for v in scores.values() if isinstance(v, (int, float)))
        score_tds = "".join(
            f'<td style="padding:6px 10px;border:1px solid #fde68a;text-align:center;font-size:13px;font-weight:700;color:#d97706">'
            f'{scores.get(c, "—")}</td>'
            for c in criteria
        )
        rows_html += (
            f'<tr>'
            f'<td style="padding:6px 10px;border:1px solid #fde68a;font-weight:600;background:#fffbeb;color:#d97706">{_esc(it.get("label",""))}</td>'
            f'{score_tds}'
            f'<td style="padding:6px 10px;border:1px solid #fde68a;text-align:center;font-weight:700;color:#92400e;background:#fef3c7">{total or "—"}</td>'
            f'</tr>'
        )
    return (f'<div style="overflow-x:auto">'
            f'<table style="border-collapse:collapse;min-width:300px">'
            f'<thead><tr>'
            f'<th style="background:#fef3c7;color:#92400e;padding:6px 10px;font-size:11px;font-weight:700;border:1px solid #fde68a">対象</th>'
            f'{crit_ths}'
            f'<th style="background:#fef3c7;color:#92400e;padding:6px 10px;font-size:11px;font-weight:700;border:1px solid #fde68a;text-align:center">合計</th>'
            f'</tr></thead><tbody>{rows_html}</tbody></table></div>')


def _yabane_result_table(yb: dict) -> str:
    """矢羽回答をフラット表（1業務1行）のHTMLに変換。旧形式（departments/steps[].cells）も自動変換して表示する。"""
    rows = _yabane_rows_from_answer(yb)
    if not rows:
        return '<p class="muted">データなし</p>'
    headers = ["ステップ", "部署", "作業内容", "アウトプット", "現行課題", "目指す姿", "目標数値"]
    ths = "".join(
        f'<th style="{"position:sticky;left:0;z-index:1;" if idx == 0 else ""}'
        f'background:#e8eeff;color:#3730a3;font-weight:700;padding:8px 10px;'
        f'border:1px solid #c7d2fe;white-space:nowrap;font-size:12px">{h}</th>'
        for idx, h in enumerate(headers)
    )
    body_rows = ""
    for r in rows:
        tds = (
            f'<td style="position:sticky;left:0;background:#eef2ff;font-weight:700;color:#3730a3;'
            f'padding:8px 10px;border:1px solid #c7d2fe;white-space:nowrap">{_esc(r.get("step",""))}</td>'
            f'<td style="padding:8px 10px;border:1px solid #dde4f0;white-space:nowrap;'
            f'font-weight:600;color:#3730a3">{_esc(r.get("dept",""))}</td>'
        )
        for key in ("content", "output", "issue", "target", "target_number"):
            tds += (
                f'<td style="padding:8px;border:1px solid #dde4f0;vertical-align:top;'
                f'white-space:pre-wrap;font-size:13px;color:#1e293b;min-width:200px">{_esc(r.get(key, ""))}</td>'
            )
        body_rows += f'<tr>{tds}</tr>'
    return (
        f'<div style="overflow-x:auto;margin-top:8px">'
        f'<table style="border-collapse:collapse;width:100%;min-width:900px">'
        f'<thead><tr>{ths}</tr></thead>'
        f'<tbody>{body_rows}</tbody>'
        f'</table></div>'
    )


def hearing_result_page(con, result: dict) -> str:
    """個別ヒアリング結果の表示（Q&A・矢羽混在対応）。"""
    answers = result.get("answers") or []
    result_html_parts = []
    for a in answers:
        _atype = a.get("type")
        if _atype == "yabane":
            result_html_parts.append(
                f'<div style="margin:16px 0">'
                f'<div style="font-size:12px;font-weight:700;color:#3730a3;margin-bottom:4px;'
                f'padding:4px 0;border-bottom:1px solid #e0e7ff">{_esc(a.get("label") or "業務プロセス")}</div>'
                f'{_yabane_result_table(a.get("answer") or {{}})}'
                f'</div>'
            )
        elif _atype == "radar":
            result_html_parts.append(
                f'<div style="margin:16px 0">'
                f'<div style="font-size:12px;font-weight:700;color:#7c3aed;margin-bottom:8px;'
                f'padding:4px 0;border-bottom:1px solid #ede9fe">{_esc(a.get("label") or "レーダーチャート")}</div>'
                f'{_radar_result_html(a.get("answer") or {{}})}'
                f'</div>'
            )
        elif _atype == "timeline":
            result_html_parts.append(
                f'<div style="margin:16px 0">'
                f'<div style="font-size:12px;font-weight:700;color:#059669;margin-bottom:8px;'
                f'padding:4px 0;border-bottom:1px solid #bbf7d0">{_esc(a.get("label") or "タイムライン")}</div>'
                f'{_timeline_result_html(a.get("answer") or {{}})}'
                f'</div>'
            )
        elif _atype == "scorecard":
            result_html_parts.append(
                f'<div style="margin:16px 0">'
                f'<div style="font-size:12px;font-weight:700;color:#d97706;margin-bottom:8px;'
                f'padding:4px 0;border-bottom:1px solid #fde68a">{_esc(a.get("label") or "スコアカード")}</div>'
                f'{_scorecard_result_html(a.get("answer") or {{}})}'
                f'</div>'
            )
        else:
            result_html_parts.append(
                f'<table style="width:100%;border-collapse:collapse;margin:10px 0">'
                f'<tr><td style="white-space:nowrap;font-weight:600;vertical-align:top;'
                f'padding:7px 10px;width:30%;border-bottom:1px solid #f0f4ff">{_esc(a.get("label"))}</td>'
                f'<td style="white-space:pre-wrap;padding:7px 10px;border-bottom:1px solid #f0f4ff">'
                f'{_esc(_format_answer(a.get("answer")))}</td></tr>'
                f'</table>'
            )
    if not result_html_parts:
        result_html_parts = ['<p class="muted">回答なし</p>']
    result_html = "\n".join(result_html_parts)
    other = sfa_db.list_hearing_results(con, result["deal_id"])
    history = ""
    if len(other) > 1:
        links = "".join(
            f'<a href="/hearing/result/{o["id"]}" style="margin-right:10px;font-size:12px'
            f'{";font-weight:700" if o["id"]==result["id"] else ""}">'
            f'{_esc(o.get("conducted_on") or "?")}（{_esc(o.get("template_name") or "")}）</a>'
            for o in other
        )
        history = f'<p class="muted" style="margin-top:12px;font-size:12px">この商談のヒアリング履歴: {links}</p>'
    return f"""
    <div class="card" style="max-width:960px">
      <h2>ヒアリング結果</h2>
      <p style="margin:0 0 4px"><strong>商談:</strong> <a href="/deal/{result['deal_id']}">{_esc(result.get('account_name') or '')} / {_esc(result.get('deal_name') or '')}</a></p>
      <p class="muted" style="margin:0 0 12px"><strong>テンプレート:</strong> {_esc(result.get('template_name') or '')}　<strong>ヒアリング日:</strong> {_esc(result.get('conducted_on') or '—')}</p>
      {result_html}
      {history}
      <div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap">
        <a class="btn sec" href="/deal/{result['deal_id']}">商談へ戻る</a>
        <a class="btn sec" href="/hearings">ヒアリング一覧</a>
        {f'<a class="btn sec" href="/hearing/result/{result["id"]}/edit">編集</a>' if result.get('template_id') else ''}
        <form method="post" action="/hearing/result/{result['id']}/delete" style="display:inline;margin:0">
          <button class="btn" style="background:#c53030;border-color:#c53030;color:#fff"
            onclick="return confirm('このヒアリング結果を削除しますか？この操作は取り消せません。')">削除</button>
        </form>
      </div>
    </div>"""


def hearings_page(con, template_id: int | None = None) -> str:
    """ヒアリングタブ：実施済み一覧 + xlsx一括DL。template_id指定時はそのテンプレートのみに絞り込む。"""
    results = sfa_db.list_all_hearing_results(con, template_id=template_id)
    tmpl = sfa_db.get_hearing_template(con, template_id) if template_id else None
    rows = ""
    for r in results:
        preview = "　".join(
            f'{_esc(a.get("label"))}: {_esc(_format_answer_for_export(a))}'
            for a in (r.get("answers") or [])[:2]
        )
        _nav = f"location.href='/hearing/result/{r['id']}'"
        rows += (
            f'<tr>'
            f'<td style="width:32px"><input type="checkbox" name="ids" value="{r["id"]}"></td>'
            f'<td style="cursor:pointer" onclick="{_nav}">{_esc(r.get("conducted_on") or "—")}</td>'
            f'<td style="cursor:pointer" onclick="{_nav}"><a href="/deal/{r["deal_id"]}" onclick="event.stopPropagation()">{_esc(r.get("account_name") or "")}</a></td>'
            f'<td style="cursor:pointer" onclick="{_nav}">{_esc(r.get("deal_name") or "")}</td>'
            f'<td style="cursor:pointer" onclick="{_nav}">{_esc(r.get("template_name") or "")}</td>'
            f'<td class="muted" style="font-size:12px;cursor:pointer" onclick="{_nav}">{preview}</td>'
            f'</tr>'
        )
    header_actions = []
    if template_id and tmpl:
        header_actions.append('<a class="btn sec" href="/hearings">全テンプレート表示に戻る</a>')
        if results:
            header_actions.append(
                f'<a class="btn sec" href="/hearings/export.csv?template_id={template_id}">📥 このテンプレートのみCSVダウンロード</a>'
            )
    elif results:
        header_actions.append('<a class="btn sec" href="/hearings/export">📥 xlsx一括ダウンロード</a>')
    header_actions.append('<a class="btn sec" href="/hearing-templates">テンプレート管理</a>')
    header_actions.append('<a class="btn" href="/hearing/new">＋新規ヒアリング</a>')
    title = f"ヒアリング（{_esc(tmpl['name'])}）" if tmpl else "ヒアリング"
    desc = ("このテンプレートで実施されたヒアリングのみ表示しています。"
            if tmpl else "実施済みのヒアリング結果一覧です。xlsxはテンプレートごとにシートが分かれます。")
    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>{title}</span>
        <span style="display:flex;gap:8px;flex-wrap:wrap">
          {"".join(header_actions)}
        </span>
      </h2>
      <p class="muted" style="margin-bottom:14px">{desc}</p>
      <form id="hearing_bulk_form" method="post" action="/hearings/bulk_delete">
      <table>
        <tr><th style="width:32px"><input type="checkbox" id="hearing_chk_all" title="全選択"
              onchange="document.querySelectorAll('#hearing_bulk_form [name=ids]').forEach(c=>c.checked=this.checked)"></th>
            <th>ヒアリング日</th><th>アカウント</th><th>案件名</th><th>テンプレート</th><th>回答プレビュー</th></tr>
        {rows or '<tr><td colspan=6 class="muted">まだヒアリング結果がありません。</td></tr>'}
      </table>
      {f'''<div style="margin-top:10px">
        <button class="btn" type="button" onclick="hearingBulkDelete()"
          style="background:#c53030;border-color:#c53030;color:#fff">選択した件を削除</button>
      </div>''' if results else ''}
      </form>
    </div>
    <script>
    function hearingBulkDelete() {{
      var ids = Array.from(document.querySelectorAll('#hearing_bulk_form [name=ids]:checked')).map(function(c){{return c.value;}});
      if (!ids.length) {{ alert('削除するヒアリング結果を選択してください。'); return; }}
      if (!confirm(ids.length + '件のヒアリング結果を削除します。この操作は取り消せません。よろしいですか？')) return;
      document.getElementById('hearing_bulk_form').submit();
    }}
    </script>"""


def build_hearings_xlsx(con) -> bytes:
    """全ヒアリング結果を、テンプレートごとに1シートのxlsxにまとめる。"""
    import openpyxl
    from io import BytesIO
    results = sfa_db.list_all_hearing_results(con)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # デフォルトシート削除

    # テンプレート名スナップショット単位でグループ化
    groups: dict = {}
    for r in results:
        key = r.get("template_name") or "（テンプレート不明）"
        groups.setdefault(key, []).append(r)

    def safe_sheet_name(name: str, used: set) -> str:
        for ch in r'\/?*[]:':
            name = name.replace(ch, "_")
        name = (name or "Sheet")[:31]
        base, n = name, 1
        while name in used:
            suffix = f"_{n}"
            name = base[:31 - len(suffix)] + suffix
            n += 1
        used.add(name)
        return name

    used_names: set = set()
    if not groups:
        ws = wb.create_sheet(safe_sheet_name("ヒアリング", used_names))
        ws.append(["（データなし）"])
    for tmpl_name, items in groups.items():
        # この群に出現する全項目ラベルを出現順に収集
        labels: list = []
        for r in items:
            for a in (r.get("answers") or []):
                lbl = a.get("label")
                if lbl and lbl not in labels:
                    labels.append(lbl)
        ws = wb.create_sheet(safe_sheet_name(tmpl_name, used_names))
        ws.append(["商談ID", "アカウント", "案件名", "ヒアリング日"] + labels)
        for r in items:
            amap = {a.get("label"): _format_answer_for_export(a) for a in (r.get("answers") or [])}
            ws.append([
                r.get("deal_id"), r.get("account_name") or "", r.get("deal_name") or "",
                r.get("conducted_on") or "",
            ] + [amap.get(lbl, "") for lbl in labels])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_hearing_results_csv(con, template_id: int) -> bytes:
    """指定テンプレートのヒアリング結果のみをCSVにまとめる（テンプレート管理画面の絞り込みDL用）。"""
    import csv
    import io
    results = sfa_db.list_all_hearing_results(con, template_id=template_id)
    labels: list = []
    for r in results:
        for a in (r.get("answers") or []):
            lbl = a.get("label")
            if lbl and lbl not in labels:
                labels.append(lbl)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["商談ID", "アカウント", "案件名", "ヒアリング日"] + labels)
    for r in results:
        amap = {a.get("label"): _format_answer_for_export(a) for a in (r.get("answers") or [])}
        writer.writerow([
            r.get("deal_id"), r.get("account_name") or "", r.get("deal_name") or "",
            r.get("conducted_on") or "",
        ] + [amap.get(lbl, "") for lbl in labels])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def leads_page(con, *, status=None, source=None, q=None) -> str:
    # デフォルトは商談化済・見込みなしを除外
    if status is None:
        leads = [l for l in sfa_db.list_leads(con, source=source, q=q)
                 if l.get("lead_status") not in ("converted", "lost")]
    else:
        leads = sfa_db.list_leads(con, status=status, source=source, q=q)

    status_opts = ('<option value="">全ステージ</option>'
                   + "".join(
                       f'<option value="{s}"{" selected" if s == status else ""}>'
                       f'{sfa_db.LEAD_STATUS_LABELS[s]}</option>'
                       for s in sfa_db.LEAD_STATUSES))
    source_opts = ('<option value="">全経路</option>'
                   + "".join(
                       f'<option value="{s}"{" selected" if s == source else ""}>'
                       f'{sfa_db.LEAD_SOURCE_LABELS[s]}</option>'
                       for s in sfa_db.LEAD_SOURCES))

    filter_form = f"""<form method="get" action="/leads" class="filter-row">
      <select name="status">{status_opts}</select>
      <select name="source">{source_opts}</select>
      <input name="q" placeholder="氏名・会社検索" value="{_esc(q)}" style="min-width:150px">
      <button class="btn sec" type="submit">絞り込み</button>
      <a class="btn sec" href="/leads">リセット</a>
    </form>"""

    # マスタデータ取得（インライン編集・バルク編集用）
    owners_list = sfa_db.get_master_list(con, "owners")
    industries_list = sfa_db.get_master_list(con, "industries")
    company_sizes_list = sfa_db.get_master_list(con, "company_sizes")
    # バルク編集用JS オブジェクト構築
    bulk_options = {
        "source": [["", "（変更なし）"]] + [[s, sfa_db.LEAD_SOURCE_LABELS[s]] for s in sfa_db.LEAD_SOURCES],
        "assigned_to": [["", "（変更なし）"]] + [[o, o] for o in owners_list],
        "industry": [["", "（変更なし）"]] + [[i, i] for i in industries_list],
        "company_size": [["", "（変更なし）"]] + [[cs, cs] for cs in company_sizes_list],
        "lead_status": [["", "（変更なし）"]] + [[s, sfa_db.LEAD_STATUS_LABELS[s]] for s in sfa_db.LEAD_STATUSES],
    }
    bulk_options_json = json.dumps(bulk_options, ensure_ascii=False)

    def _inline_select_source(lead_id, current):
        opts = "".join(
            f'<option value="{html.escape(s)}"{" selected" if s == current else ""}>{html.escape(sfa_db.LEAD_SOURCE_LABELS[s])}</option>'
            for s in sfa_db.LEAD_SOURCES
        )
        return (f'<select onchange="updateLeadField({lead_id}, \'source\', this.value)"'
                f' style="font-size:12px;padding:2px 4px;max-width:120px">'
                f'<option value=""></option>{opts}</select>')

    def _inline_select_master(lead_id, field, values, current):
        opts = "".join(
            f'<option value="{html.escape(v)}"{" selected" if v == current else ""}>{html.escape(v)}</option>'
            for v in values
        )
        return (f'<select onchange="updateLeadField({lead_id}, \'{field}\', this.value)"'
                f' style="font-size:12px;padding:2px 4px;max-width:120px">'
                f'<option value=""></option>{opts}</select>')

    def _inline_select_status(lead_id, current):
        opts = "".join(
            f'<option value="{html.escape(s)}"{" selected" if s == current else ""}>{html.escape(sfa_db.LEAD_STATUS_LABELS[s])}</option>'
            for s in sfa_db.LEAD_STATUSES
        )
        return (f'<select onchange="updateLeadField({lead_id}, \'lead_status\', this.value)"'
                f' style="font-size:12px;padding:2px 4px;max-width:110px">'
                f'<option value=""></option>{opts}</select>')

    rows = []
    for ld in leads:
        sc = f's-{ld.get("lead_status", "new")}'
        sl = sfa_db.LEAD_STATUS_LABELS.get(ld.get("lead_status", "new"), "")
        deal_badge = (f' <a href="/deal/{ld["deal_id"]}" title="紐付け商談">🔗</a>'
                      if ld.get("deal_id") else "")
        sel_status = _inline_select_status(ld["id"], ld.get("lead_status", "new"))
        sel_source = _inline_select_source(ld["id"], ld.get("source", "other"))
        sel_owner = _inline_select_master(ld["id"], "assigned_to", owners_list, ld.get("assigned_to") or "")
        sel_industry = _inline_select_master(ld["id"], "industry", industries_list, ld.get("industry") or "")
        sel_company_size = _inline_select_master(ld["id"], "company_size", company_sizes_list, ld.get("company_size") or "")
        rows.append(
            f'<tr>'
            f'<td style="width:32px"><input type="checkbox" name="ids" value="{ld["id"]}"></td>'
            f'<td><a href="/leads/{ld["id"]}">{_esc(ld["name"])}</a>{deal_badge}<br>'
            f'<span class="muted">{_esc(ld.get("company"))}</span></td>'
            f'<td>{sel_status}</td>'
            f'<td class="hide-sm">{sel_source}</td>'
            f'<td class="hide-sm">{sel_owner}</td>'
            f'<td class="hide-sm">{sel_industry}</td>'
            f'<td class="hide-sm">{sel_company_size}</td>'
            f'<td class="muted">{_esc((ld.get("updated_at") or "")[:10])}</td>'
            f'</tr>'
        )

    return f"""
    <div class="card">
      <h2 style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span>リード一覧 <span class="muted" style="font-weight:normal">({len(leads)}件)</span></span>
        <span style="display:flex;gap:8px">
          <a class="btn sec" href="/email-draft">メールドラフト</a>
          <a class="btn sec" href="/leads/import">CSV取込</a>
          <a class="btn" href="/leads/new">＋新規リード</a>
        </span>
      </h2>
      {filter_form}
      <form id="bulk_form" method="post" action="/leads/bulk_edit">
      <div style="overflow-x:auto">
      <table>
        <tr><th style="width:32px"><input type="checkbox" id="chk_all" title="全選択"
              onchange="document.querySelectorAll('[name=ids]').forEach(c=>c.checked=this.checked)"></th>
            <th>氏名 / 会社</th><th>ステータス</th>
            <th class="hide-sm">経路</th>
            <th class="hide-sm">担当</th>
            <th class="hide-sm">業界</th>
            <th class="hide-sm">企業規模</th>
            <th>更新日</th></tr>
        {''.join(rows) or '<tr><td colspan=8 class=muted>リードがありません。「＋新規リード」から追加、またはCSV取込してください。</td></tr>'}
      </table>
      <div style="display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap">
        <select id="bulk_field" name="field" style="width:auto">
          <option value="lead_status">ステータス</option>
          <option value="source">経路</option>
          <option value="assigned_to">担当</option>
          <option value="industry">業界</option>
          <option value="company_size">企業規模</option>
        </select>
        <select id="bulk_value" name="value" style="width:auto"></select>
        <button class="btn sec" type="submit">選択した件を一括変更</button>
        <button class="btn" type="button" onclick="bulkDelete()"
          style="background:#c53030;border-color:#c53030;color:#fff;margin-left:8px">選択した件を削除</button>
      </div>
      </div>
      </form>
    </div>
    <script>
    const BULK_OPTIONS = {bulk_options_json};
    function updateLeadField(id, field, value) {{
      fetch('/leads/' + id + '/field', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
        body: 'field=' + encodeURIComponent(field) + '&value=' + encodeURIComponent(value)
      }}).then(r => r.json()).then(d => {{
        if (!d.ok) {{ alert('更新エラー: ' + (d.error || '')); }}
      }}).catch(() => alert('通信エラー'));
    }}
    function repopulateBulkValue() {{
      var field = document.getElementById('bulk_field').value;
      var opts = BULK_OPTIONS[field] || [];
      var sel = document.getElementById('bulk_value');
      sel.innerHTML = opts.map(function(pair) {{
        return '<option value="' + pair[0] + '">' + pair[1] + '</option>';
      }}).join('');
    }}
    function bulkDelete() {{
      var ids = Array.from(document.querySelectorAll('[name=ids]:checked')).map(c => c.value);
      if (!ids.length) {{ alert('削除するリードを選択してください。'); return; }}
      if (!confirm(ids.length + '件のリードを削除します。この操作は取り消せません。よろしいですか？')) return;
      var form = document.createElement('form');
      form.method = 'post';
      form.action = '/leads/bulk_delete';
      ids.forEach(function(id) {{
        var inp = document.createElement('input');
        inp.type = 'hidden'; inp.name = 'ids'; inp.value = id;
        form.appendChild(inp);
      }});
      document.body.appendChild(form);
      form.submit();
    }}
    document.getElementById('bulk_field').addEventListener('change', repopulateBulkValue);
    repopulateBulkValue();
    </script>"""


def lead_form(con, lead=None) -> str:
    lead = lead or {}
    accounts = sfa_db.list_accounts(con)
    acc_datalist = "".join(
        f'<option value="{html.escape(a["name"])}"></option>' for a in accounts
    )
    status_items = [(s, sfa_db.LEAD_STATUS_LABELS[s]) for s in sfa_db.LEAD_STATUSES]
    source_items = [(s, sfa_db.LEAD_SOURCE_LABELS[s]) for s in sfa_db.LEAD_SOURCES]

    status_btns = ""
    convert_btn = ""
    deal_link = ""
    activities_html = ""

    if lead.get("id"):
        cur_status = lead.get("lead_status", "new")
        btns = []
        for s in sfa_db.LEAD_STATUSES:
            active_style = ("font-weight:700;box-shadow:inset 0 0 0 2px #2f6fed"
                            if s == cur_status else "opacity:0.55")
            btns.append(
                f'<form method="post" action="/leads/{lead["id"]}/status"'
                f' style="display:inline;margin:0 4px 4px 0">'
                f'<input type="hidden" name="status" value="{s}">'
                f'<button class="btn sec" style="{active_style}">'
                f'{sfa_db.LEAD_STATUS_LABELS[s]}</button></form>'
            )
        status_btns = f'<div style="margin:0 0 14px">{"".join(btns)}</div>'

        acts = sfa_db.list_lead_activities(con, lead["id"])
        act_rows = ""
        for a in acts:
            tl = sfa_db.LEAD_ACTIVITY_LABELS.get(a.get("type", "note"), a.get("type", ""))
            act_rows += (
                f'<tr><td class="muted" style="white-space:nowrap">{_esc((a.get("created_at") or "")[:16])}</td>'
                f'<td>{tl}</td><td>{_esc(a.get("author"))}</td>'
                f'<td style="white-space:pre-wrap">{_esc(a.get("content"))}</td></tr>'
            )
        act_type_opts = "".join(
            f'<option value="{t}">{sfa_db.LEAD_ACTIVITY_LABELS[t]}</option>'
            for t in sfa_db.LEAD_ACTIVITY_TYPES
        )
        activities_html = f"""
        <div class="card"><h2>活動ログ</h2>
        <table><tr><th>日時</th><th>種別</th><th>担当</th><th>内容</th></tr>
        {act_rows or '<tr><td colspan=4 class=muted>活動なし</td></tr>'}
        </table>
        <form method="post" action="/leads/{lead['id']}/activity" style="margin-top:14px">
          <div class="grid">
            <div><label>種別</label><select name="type">{act_type_opts}</select></div>
            <div><label>担当者</label><select name="author">{_opt(sfa_db.get_master_list(con,'owners'), None)}</select></div>
          </div>
          <label>内容 *</label><textarea name="content" rows="2" required></textarea>
          <p><button class="btn sec">活動を追加</button></p>
        </form></div>"""

        can_convert = (cur_status not in ("converted", "lost") and not lead.get("deal_id"))
        if can_convert:
            convert_btn = (
                f'<form method="post" action="/leads/{lead["id"]}/convert" style="display:inline">'
                f'<button class="btn sync"'
                f' onclick="return confirm(\'アポ獲得後に商談化します。\\nリードはクローズされ、商談が作成されます。\')">'
                f'アポ獲得 → 商談化</button></form>')
        if lead.get("deal_id"):
            deal_link = f'<a class="btn sec" href="/deal/{lead["deal_id"]}">紐付け商談を見る 🔗</a>'

    delete_btn = ""
    if lead.get("id"):
        delete_btn = (
            f'<form method="post" action="/leads/{lead["id"]}/delete" style="display:inline;margin:0">'
            '<button class="btn" style="background:#ef4444"'
            ' onclick="return confirm(\'このリードを削除しますか？この操作は元に戻せません。\')">削除</button></form>'
        )

    return f"""
    <div class="card">
      <h2>{'リード編集' if lead.get('id') else '新規リード'}</h2>
      {status_btns}
      <form method="post" action="/leads/save">
        <input type="hidden" name="id" value="{_esc(lead.get('id'))}">
        <div class="grid">
          <div><label>氏名 *</label>
            <input name="name" required value="{_esc(lead.get('name'))}"></div>
          <div><label>会社名 * <span class="muted">（既存アカウントから選択または新規入力）</span></label>
            <input name="company" required value="{_esc(lead.get('company'))}" list="acc_list" autocomplete="off">
            <datalist id="acc_list">{acc_datalist}</datalist></div>
          <div><label>業界</label>
            <select name="industry">{_opt(sfa_db.get_master_list(con,'industries'), lead.get('industry'))}</select></div>
          <div><label>企業規模</label>
            <select name="company_size">{_opt(sfa_db.get_master_list(con,'company_sizes'), lead.get('company_size'))}</select></div>
          <div><label>役職</label>
            <input name="title" value="{_esc(lead.get('title'))}"></div>
          <div><label>メール</label>
            <input name="email" type="email" value="{_esc(lead.get('email'))}"></div>
          <div><label>電話</label>
            <input name="phone" value="{_esc(lead.get('phone'))}"></div>
          <div><label>担当者</label>
            <select name="assigned_to">{_opt(sfa_db.get_master_list(con,'owners'), lead.get('assigned_to'))}</select></div>
          <div><label>獲得経路</label>
            <select name="source">{_opt_kv(source_items, lead.get('source') or 'other')}</select></div>
          <div><label>ステータス</label>
            <select name="lead_status">{_opt_kv(status_items, lead.get('lead_status') or 'new')}</select></div>
        </div>
        <label>メモ</label><textarea name="notes" rows="2">{_esc(lead.get('notes'))}</textarea>
        <p style="display:flex;flex-wrap:wrap;gap:8px">
          <button class="btn">保存</button>
          <a class="btn sec" href="/leads">一覧へ</a>
          {deal_link}
        </p>
      </form>
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">
        {convert_btn}
        {delete_btn}
      </div>
    </div>
    {activities_html}"""


def leads_import_page(con, result: str = "") -> str:
    result_html = f'<div class="flash">{html.escape(result)}</div>' if result else ""
    header_line = ",".join(leads_csv.TEMPLATE_HEADERS)
    example_lines = "\n".join(",".join(row) for row in leads_csv.TEMPLATE_EXAMPLE_ROWS)

    owners_list = sfa_db.get_master_list(con, "owners")
    industries_list = sfa_db.get_master_list(con, "industries")
    sizes_list = sfa_db.get_master_list(con, "company_sizes")
    theme_names = [t["name"] for t in sfa_db.list_pitch_themes(con, active_only=True)]
    source_chips = _chips([f"{s}({sfa_db.LEAD_SOURCE_LABELS[s]})" for s in sfa_db.LEAD_SOURCES])
    status_chips = _chips([f"{s}({sfa_db.LEAD_STATUS_LABELS[s]})" for s in sfa_db.LEAD_STATUSES])

    choices_html = f"""
    <details style="margin:12px 0;background:#f8f9fb;border-radius:6px;padding:10px 14px" open>
      <summary style="cursor:pointer;font-weight:600;color:#3a4760">
        📋 入力選択肢一覧（この中から選んでください。表記が完全一致しないと空欄で取り込まれます）
      </summary>
      <div style="margin-top:10px;font-size:13px;line-height:1.9">
        <div><strong>業界</strong>: {_chips(industries_list)}</div>
        <div><strong>企業規模</strong>: {_chips(sizes_list)}</div>
        <div><strong>獲得経路</strong>（コードで入力。一致しない場合は自動的に other）: {source_chips}</div>
        <div><strong>ステータス</strong>（コードで入力。一致しない場合は自動的に new）: {status_chips}</div>
        <div><strong>ピッチテーマ</strong>: {_chips(theme_names)}</div>
        <div><strong>担当者</strong>: {_chips(owners_list)}</div>
      </div>
    </details>"""

    return f"""
    <div class="card"><h2>リード一括取込</h2>
    {result_html}

    <h3 style="margin:0 0 8px;font-size:14px;color:#3a4760">📇 名刺データ（xlsx）アップロード</h3>
    <p class="muted">名刺管理アプリ（Eight / CAMCARD / Sansan等）からエクスポートしたxlsxを<strong>そのまま</strong>アップロードします（列構成は名刺アプリ側の固定形式）。<br>
    会社名・業界などはAIがWebリサーチで補強します（ANTHROPIC_API_KEY 設定時）。<br>
    <strong>下記テンプレートに沿って作成したCSV/xlsxはこちらではなく、下の「CSVファイルアップロード」欄を使ってください。</strong></p>
    <form method="post" action="/leads/upload_meishi" enctype="multipart/form-data" style="margin-bottom:20px">
      <label>名刺xlsxファイル</label>
      <input type="file" name="meishi_file" accept=".xlsx,.xls,.csv" required style="padding:4px">
      <p style="margin-top:8px"><button class="btn">アップロードして取込</button>
         <a class="btn sec" href="/leads">キャンセル</a></p>
    </form>

    <hr style="margin:20px 0">
    <h3 style="margin:0 0 8px;font-size:14px;color:#3a4760">📋 CSVファイルアップロード／ペースト取込（こちらがメインの取込方法です）</h3>
    <p style="margin:10px 0">
      <a class="btn sec" href="/leads/import/template.csv">📥 テンプレートCSVをダウンロード</a>
    </p>
    <p class="muted">運用ルール: 入力は必ずこのテンプレートに列を揃えて作成してください（列の追加・削除・並び替えは不可、値のみ入力）。</p>

    <form method="post" action="/leads/import" enctype="multipart/form-data"
          style="background:#f0f7ff;border:1px solid #cfe0fb;border-radius:8px;padding:14px;margin:14px 0">
      <label>👇 CSVファイル（テンプレートに沿ったファイルをそのままアップロード。xlsxも可）</label>
      <input type="file" name="csv_file" accept=".csv,.xlsx,.xls" style="padding:4px">
      <label style="margin-top:12px">またはCSVデータを直接ペースト</label>
      <textarea name="csv_text" rows="8"
        style="font-family:monospace;font-size:12px"></textarea>
      <p class="muted" style="margin-top:4px">両方入力した場合はアップロードしたファイルを優先します。</p>
      <p><button class="btn">取込実行</button>
         <a class="btn sec" href="/leads">キャンセル</a></p>
    </form>

    <p class="muted" style="margin-top:12px">CSVフォーマット（1行目はヘッダ行、空行はスキップ）:</p>
    <pre style="background:#f4f6f9;padding:10px;border-radius:6px">{html.escape(header_line)}
{html.escape(example_lines)}</pre>
    <p class="muted" style="margin-top:4px">
      業界 / 企業規模 / 担当者 / ピッチテーマは、下の「入力選択肢一覧」と完全一致した場合のみ取り込まれます
      （一致しない場合は空欄になります）。業界・企業規模が空欄の行はAIが会社名から自動推定します
      （ANTHROPIC_API_KEY設定時）。
    </p>

    {_ai_prompt_block(leads_csv.build_ai_prompt(con), "/leads/import/ai_prompt.md")}

    {choices_html}
    </div>"""


def deals_import_page(con, result: str = "") -> str:
    result_html = f'<div class="flash">{html.escape(result)}</div>' if result else ""
    header_line = ",".join(deals_csv.TEMPLATE_HEADERS)
    example_lines = "\n".join(",".join(row) for row in deals_csv.TEMPLATE_EXAMPLE_ROWS)

    stages = sfa_db.get_master_list(con, "deal_stages")
    biz_l1_list = sfa_db.get_master_list(con, "business_type_l1")
    owners_list = sfa_db.get_master_list(con, "owners")
    patterns_list = sfa_db.get_master_list(con, "lead_patterns")
    sizes_list = sfa_db.get_master_list(con, "company_sizes")
    industries_list = sfa_db.get_master_list(con, "industries")

    biz_l2_html = "".join(
        f'<div style="margin:4px 0 4px 12px">└ <strong>{html.escape(l1)}</strong>: '
        f'{_chips(sfa_db.BUSINESS_TYPE_L2_BY_L1.get(l1, []))}</div>'
        for l1 in biz_l1_list
    )

    choices_html = f"""
    <details style="margin:12px 0;background:#f8f9fb;border-radius:6px;padding:10px 14px" open>
      <summary style="cursor:pointer;font-weight:600;color:#3a4760">
        📋 入力選択肢一覧（この中から選んでください。表記が完全一致しないと空欄で取り込まれます）
      </summary>
      <div style="margin-top:10px;font-size:13px;line-height:1.9">
        <div><strong>種別</strong>: {_chips(["商談", "アカウント"])}</div>
        <div><strong>ステージ</strong>: {_chips(stages)}</div>
        <div><strong>事業種別L1 / L2</strong>: {_chips(biz_l1_list)}
          {biz_l2_html}
        </div>
        <div><strong>リード経路</strong>: {_chips(patterns_list)}</div>
        <div><strong>担当者・サブ担当</strong>: {_chips(owners_list)}</div>
        <div><strong>重要度</strong>: {_chips(sfa_db.IMPORTANCE_OPTIONS)}</div>
        <div><strong>企業規模</strong>: {_chips(sizes_list)}</div>
        <div><strong>業界</strong>（参考。自由入力欄のためこの一覧以外の表記も取り込めます）: {_chips(industries_list)}</div>
      </div>
    </details>"""

    return f"""
    <div class="card"><h2>商談一括取込</h2>
    {result_html}

    <p class="muted">
      1行 = 1商談 または 1アカウント。<strong>種別</strong>列に「商談」「アカウント」のどちらかを指定します
      （省略した場合、商談名があれば商談、無ければアカウントとして扱います）。<br>
      会社名が未登録の場合、商談・アカウントどちらの取込でも<strong>アカウントを自動作成</strong>します
      （既存アカウントは業界・企業規模が空欄の項目のみ補完し、上書きはしません）。
    </p>
    <p style="margin:10px 0">
      <a class="btn sec" href="/deals/import/template.csv">📥 テンプレートCSVをダウンロード</a>
    </p>
    <p class="muted">運用ルール: 入力は必ずこのテンプレートに列を揃えて作成してください（列の追加・削除・並び替えは不可、値のみ入力）。</p>

    <form method="post" action="/deals/import" enctype="multipart/form-data"
          style="background:#f0f7ff;border:1px solid #cfe0fb;border-radius:8px;padding:14px;margin:14px 0">
      <label>👇 CSVファイル（テンプレートに沿ったファイルをそのままアップロード。xlsxも可）</label>
      <input type="file" name="csv_file" accept=".csv,.xlsx,.xls" style="padding:4px">
      <label style="margin-top:12px">またはCSVデータを直接ペースト</label>
      <textarea name="csv_text" rows="8"
        style="font-family:monospace;font-size:12px"></textarea>
      <p class="muted" style="margin-top:4px">両方入力した場合はアップロードしたファイルを優先します。</p>
      <p><button class="btn">取込実行</button>
         <a class="btn sec" href="/deals">キャンセル</a></p>
    </form>

    <p class="muted" style="margin-top:12px">CSVフォーマット（1行目はヘッダ行、空行はスキップ）:</p>
    <pre style="background:#f4f6f9;padding:10px;border-radius:6px">{html.escape(header_line)}
{html.escape(example_lines)}</pre>
    <p class="muted" style="margin-top:4px">
      ステージ / 事業種別L1 / 事業種別L2（L1に対応する値のみ） / リード経路 / 担当者 / 重要度 / 企業規模は、
      下の「入力選択肢一覧」と完全一致した場合のみ取り込まれます（一致しない場合は空欄になります）。<br>
      業界は自由入力欄のためどんな文字列でも取り込めます。事前に業界・企業規模を調査済みであれば、
      その場でCSVに直接入力してください（AI推定より確実です）。
    </p>

    {_ai_prompt_block(deals_csv.build_ai_prompt(con), "/deals/import/ai_prompt.md")}

    {choices_html}
    </div>"""




# ── HTTPハンドラ ───────────────────────────────────────────────────────────────

def _make_handler(db_path: str, theme_client: ThemeDBClient | None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音
            pass

        def _send(self, body: bytes, status=200, ctype="text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_cors_json(self, body: bytes, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            self._form_raw = self.rfile.read(n).decode("utf-8", errors="replace")
            d = urllib.parse.parse_qs(self._form_raw, keep_blank_values=True)
            return {k: (v[0] if v else "") for k, v in d.items()}

        def _form_list(self, key: str) -> list[str]:
            """_form() 呼び出し後に特定キーの全値リストを返す。"""
            d = urllib.parse.parse_qs(getattr(self, "_form_raw", ""), keep_blank_values=True)
            return d.get(key, [])

        def _form_multi(self) -> dict:
            """multipart/form-data対応。ファイルは(filename, バイト列)のタプルで返す。

            Python 3.13でcgiモジュールが削除された（PEP 594）ため、標準ライブラリのみで
            簡易的なmultipartパーサを自前実装している。
            """
            ctype = self.headers.get("Content-Type", "")
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)

            m = re.search(r'boundary="?([^";]+)"?', ctype)
            if not m:
                return {}
            boundary = ("--" + m.group(1)).encode("utf-8")

            result = {}
            for part in body.split(boundary):
                part = part.strip(b"\r\n")
                if not part or part == b"--":
                    continue
                if b"\r\n\r\n" not in part:
                    continue
                header_blob, content = part.split(b"\r\n\r\n", 1)
                content = content[:-2] if content.endswith(b"\r\n") else content
                headers = {}
                for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                disposition = headers.get("content-disposition", "")
                name_m = re.search(r'name="([^"]*)"', disposition)
                if not name_m:
                    continue
                field_name = name_m.group(1)
                filename_m = re.search(r'filename="([^"]*)"', disposition)
                if filename_m:
                    result[field_name] = (filename_m.group(1), content)
                else:
                    result[field_name] = content.decode("utf-8", errors="replace")
            return result

        def _qs(self) -> dict:
            qs_raw = self.path.split("?")[1] if "?" in self.path else ""
            return urllib.parse.parse_qs(qs_raw)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            con = sfa_db.connect(db_path)
            try:
                if path == "/health":
                    self._send(b'{"status":"ok"}', ctype="application/json")
                elif path == "/api/deals":
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send(b'{"error":"unauthorized"}', status=401, ctype="application/json")
                    else:
                        status_q = (qs.get("status", ["open"])[0] or "open")
                        effective = None if status_q == "all" else status_q
                        deals = sfa_db.list_deals(con, status=effective)
                        self._send(json.dumps([dict(d) for d in deals], ensure_ascii=False, default=str).encode(), ctype="application/json")
                elif path == "/api/memo/list":
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        theme_id_q = qs.get("theme_id", [None])[0]
                        if theme_id_q:
                            notes = con.execute(
                                "SELECT * FROM meeting_notes WHERE theme_id=? ORDER BY note_date ASC, created_at ASC LIMIT 100",
                                (int(theme_id_q),)
                            ).fetchall()
                        else:
                            notes = con.execute(
                                "SELECT * FROM meeting_notes ORDER BY note_date ASC, created_at ASC LIMIT 100"
                            ).fetchall()
                        self._send_cors_json(json.dumps([dict(r) for r in notes], ensure_ascii=False, default=str).encode())
                elif path == "/api/theme_deal_map":
                    # ダッシュボード用: theme_id → SFA deal_id マッピング
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        rows = con.execute(
                            "SELECT id, theme_id FROM deals WHERE theme_id IS NOT NULL"
                        ).fetchall()
                        result = {str(row["theme_id"]): row["id"] for row in rows}
                        self._send_cors_json(json.dumps(result, ensure_ascii=False).encode())
                elif path == "/api/memo/list_all":
                    # スプシ出力用: 全メモ + deals/accounts JOIN
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        rows = con.execute("""
                            SELECT m.id, m.note_date, m.body, m.task, m.task_owner,
                                   m.task_due, m.task_done, m.created_at,
                                   d.deal_name, a.name AS account_name
                            FROM meeting_notes m
                            LEFT JOIN deals d ON d.theme_id = m.theme_id
                            LEFT JOIN accounts a ON a.id = d.account_id
                            ORDER BY m.note_date DESC, m.created_at DESC
                        """).fetchall()
                        self._send_cors_json(json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str).encode())
                elif path == "/":
                    self._send(render(dashboard_page(con)))
                # ── メールパターン ──
                elif path == "/email-patterns":
                    self._send(render(email_patterns_page(con)))
                elif path == "/email-patterns/new":
                    self._send(render(email_pattern_form(con)))
                elif path == "/email-draft":
                    qs = self._qs()
                    self._send(render(email_draft_page(
                        con,
                        status_filter=(qs.get("status", [None])[0] or None),
                        q=(qs.get("q", [None])[0] or None),
                    )))
                elif path == "/email-draft/eml":
                    qs = self._qs()
                    try:
                        lid = int((qs.get("lead_id", [None])[0]) or 0)
                        pid = int((qs.get("pattern_id", [None])[0]) or 0)
                        lead = sfa_db.get_lead(con, lid)
                        p = sfa_db.get_email_pattern(con, pid)
                        if lead and p:
                            eml = build_eml_bytes(p, lead)
                            self.send_response(200)
                            self.send_header("Content-Type", "message/rfc822")
                            self.send_header("Content-Disposition", 'attachment; filename="draft.eml"')
                            self.send_header("Content-Length", str(len(eml)))
                            self.end_headers()
                            self.wfile.write(eml)
                        else:
                            self._send(b"Not found", 404)
                    except (ValueError, TypeError):
                        self._send(b"Bad request", 400)
                elif path.startswith("/email-patterns/") and path.endswith("/edit"):
                    try:
                        pid = int(path.split("/")[2])
                        p = sfa_db.get_email_pattern(con, pid)
                        self._send(render(email_pattern_form(con, p) if p else "<div class=card>見つかりません</div>"))
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>見つかりません</div>"), 404)
                # ── 初回ヒアリング ──
                elif path == "/hearings/export":
                    try:
                        data = build_hearings_xlsx(con)
                        self.send_response(200)
                        self.send_header("Content-Type",
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                        self.send_header("Content-Disposition", 'attachment; filename="hearings.xlsx"')
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    except Exception as _ex:
                        print(f"[hearings/export] {_ex}", flush=True)
                        import traceback as _tb; _tb.print_exc()
                        self._send(render("<div class=card>エクスポートに失敗しました</div>"), 500)
                elif path == "/hearings/export.csv":
                    qs = self._qs()
                    try:
                        tid = int(qs.get("template_id", ["0"])[0] or 0)
                    except ValueError:
                        tid = 0
                    if not tid:
                        self._send(render("<div class=card>テンプレートが指定されていません</div>"), 400)
                    else:
                        data = build_hearing_results_csv(con, tid)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/csv; charset=utf-8")
                        self.send_header("Content-Disposition", f'attachment; filename="hearing_results_{tid}.csv"')
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                elif path == "/hearings":
                    qs = self._qs()
                    tid_raw = qs.get("template_id", [None])[0]
                    try:
                        tid = int(tid_raw) if tid_raw else None
                    except ValueError:
                        tid = None
                    self._send(render(hearings_page(con, template_id=tid)))
                elif path == "/hearing-templates/new":
                    self._send(render(hearing_template_form(con)))
                elif path == "/hearing-templates":
                    self._send(render(hearing_templates_page(con)))
                elif path.startswith("/hearing-templates/") and path.endswith("/edit"):
                    try:
                        tid = int(path.split("/")[2])
                        t = sfa_db.get_hearing_template(con, tid)
                        self._send(render(hearing_template_form(con, t) if t
                                          else "<div class=card>見つかりません</div>"))
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>見つかりません</div>"), 404)
                elif path == "/hearing/new":
                    qs = self._qs()
                    self._send(render(hearing_new_page(con, preselect=(qs.get("target", [None])[0]))))
                elif path == "/hearing/start":
                    qs = self._qs()
                    target = (qs.get("target", [""])[0] or "")
                    try:
                        tid = int(qs.get("template_id", ["0"])[0] or 0)
                    except ValueError:
                        tid = 0
                    tmpl = sfa_db.get_hearing_template(con, tid) if tid else None
                    if not tmpl or ":" not in target:
                        self._send(render("<div class=card>対象またはテンプレートが不正です。"
                                          "<a href='/hearing/new'>戻る</a></div>"), 400)
                    else:
                        ttype, _, tval = target.partition(":")
                        try:
                            tval_id = int(tval)
                        except ValueError:
                            tval_id = 0
                        prefill, prev_date, label = None, None, ""
                        if ttype == "deal":
                            d = sfa_db.get_deal(con, tval_id)
                            if d:
                                label = f"{d.get('account_name') or ''} / {d.get('deal_name')}"
                                # 2回目以降: 同テンプレの直近結果をプリフィル
                                prev = [r for r in sfa_db.list_hearing_results(con, tval_id)
                                        if r.get("template_id") == tmpl["id"]]
                                if prev:
                                    prefill = {a.get("label"): a.get("answer")
                                               for a in (prev[0].get("answers") or [])}
                                    prev_date = prev[0].get("conducted_on")
                        elif ttype == "lead":
                            ld = sfa_db.get_lead(con, tval_id)
                            if ld:
                                label = f"{ld.get('company') or '?'} / {ld.get('name') or '?'}（リード）"
                        if not label:
                            self._send(render("<div class=card>対象が見つかりません</div>"), 404)
                        else:
                            draft = sfa_db.get_hearing_draft(
                                con, target_type=ttype, target_id=tval_id, template_id=tmpl["id"],
                            )
                            self._send(render(hearing_input_page(
                                con, target_type=ttype, target_id=tval_id, template=tmpl,
                                target_label=label, prefill=prefill, prev_date=prev_date,
                                draft=draft,
                            )))
                elif path.startswith("/hearing/result/") and path.endswith("/edit"):
                    try:
                        rid = int(path.split("/")[3])
                    except (ValueError, IndexError):
                        rid = 0
                    r = sfa_db.get_hearing_result(con, rid) if rid else None
                    tmpl = sfa_db.get_hearing_template(con, r["template_id"]) if r and r.get("template_id") else None
                    if not r or not tmpl:
                        self._send(render("<div class=card>編集対象が見つかりません"
                                          "（テンプレートが削除されている可能性があります）。"
                                          "<a href='/hearings'>一覧へ戻る</a></div>"), 404)
                    else:
                        label = f"{r.get('account_name') or ''} / {r.get('deal_name') or ''}"
                        prefill = {a.get("label"): a.get("answer") for a in (r.get("answers") or [])}
                        draft = sfa_db.get_hearing_draft(
                            con, target_type="hearing_edit", target_id=rid, template_id=tmpl["id"],
                        )
                        self._send(render(hearing_input_page(
                            con, target_type="deal", target_id=r["deal_id"], template=tmpl,
                            target_label=label, prefill=prefill, prev_date=None, draft=draft,
                            edit_result_id=rid, edit_conducted_on=r.get("conducted_on"),
                        )))
                elif path.startswith("/hearing/result/"):
                    try:
                        rid = int(path.split("/")[3])
                        r = sfa_db.get_hearing_result(con, rid)
                        self._send(render(hearing_result_page(con, r) if r
                                          else "<div class=card>ヒアリング結果が見つかりません</div>"),
                                   200 if r else 404)
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>ページが見つかりません</div>"), 404)
                elif path == "/deals":
                    qs = self._qs()
                    def qs1(k): return (qs.get(k, [None])[0] or None)
                    self._send(render(home_page(con, owner=qs1("owner"), status_filter=qs1("status"), stage_filter=qs1("stage"))))
                elif path == "/deals/import":
                    self._send(render(deals_import_page(con)))
                elif path == "/deals/import/template.csv":
                    body = deals_csv.build_template_csv()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="deals_import_template.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/deals/import/ai_prompt.md":
                    body = deals_csv.build_ai_prompt(con).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="deals_import_ai_prompt.md"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/masters":
                    self._send(render(masters_page(con)))
                elif path == "/activity/new":
                    self._send(render(activity_deal_picker(con)))
                elif path == "/deal/new":
                    self._send(render(deal_form(con)))
                # ── 開発案件 ──
                elif path == "/dev-projects":
                    self._send(render(dev_projects_list_page(con)))
                elif path == "/dev-projects/new":
                    qs = self._qs()
                    did_raw = qs.get("deal_id", [None])[0]
                    try:
                        did = int(did_raw) if did_raw else None
                    except ValueError:
                        did = None
                    self._send(render(dev_project_form(con, deal_id=did)))
                elif path.startswith("/dev-project/") and path.endswith("/edit"):
                    try:
                        pid = int(path.split("/")[2])
                        proj = sfa_db.get_dev_project(con, pid)
                        self._send(
                            render(dev_project_form(con, proj) if proj
                                   else "<div class=card>開発案件が見つかりません</div>"),
                            200 if proj else 404,
                        )
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>ページが見つかりません</div>"), 404)
                elif path == "/accounts":
                    self._send(render(accounts_page(con)))
                elif path == "/account/new":
                    self._send(render(account_form(con)))
                # ── リード ──
                elif path == "/leads":
                    qs = self._qs()
                    def qs1(k): return (qs.get(k, [None])[0] or None)
                    self._send(render(leads_page(
                        con, status=qs1("status"), source=qs1("source"), q=qs1("q"),
                    )))
                elif path == "/leads/new":
                    self._send(render(lead_form(con)))
                elif path == "/leads/import":
                    self._send(render(leads_import_page(con)))
                elif path == "/leads/import/template.csv":
                    body = leads_csv.build_template_csv()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="leads_import_template.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/leads/import/ai_prompt.md":
                    body = leads_csv.build_ai_prompt(con).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="leads_import_ai_prompt.md"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path.startswith("/leads/"):
                    try:
                        lid = int(path.split("/")[2])
                        lead = sfa_db.get_lead(con, lid)
                        if lead:
                            self._send(render(lead_form(con, lead)))
                        else:
                            self._send(render("<div class=card>リードが見つかりません</div>"), 404)
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>ページが見つかりません</div>"), 404)
                # ── 商談・アカウント ──
                elif path.startswith("/deal/"):
                    did = int(path.split("/")[2])
                    deal = sfa_db.get_deal(con, did)
                    self._send(
                        render(deal_form(con, deal)) if deal
                        else render("<div class=card>商談が見つかりません</div>"),
                        200 if deal else 404,
                    )
                elif path.startswith("/account/"):
                    parts = path.split("/")
                    aid = int(parts[2])
                    acc = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
                    if len(parts) >= 4 and parts[3] == "edit":
                        self._send(render(account_form(con, dict(acc) if acc else None)))
                    else:
                        self._send(
                            render(account_detail(con, dict(acc))) if acc
                            else render("<div class=card>アカウントが見つかりません</div>"),
                            200 if acc else 404,
                        )
                else:
                    self._send(render("<div class=card>ページが見つかりません</div>"), 404)
            finally:
                con.close()

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            con = sfa_db.connect(db_path)
            ctype = self.headers.get("Content-Type", "")
            try:
                if "multipart/form-data" in ctype:
                    f = self._form_multi()
                    f_list = {}  # multipart returns single values; list values handled separately
                else:
                    n = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(n).decode("utf-8")
                    import urllib.parse as _up
                    d = _up.parse_qs(raw, keep_blank_values=True)
                    f_list = {k: v for k, v in d.items()}
                    f = {k: (v[0] if v else "") for k, v in d.items()}

                # ── マスタ ──
                if path == "/masters/save":
                    for key in sfa_db.MASTER_KEYS:
                        values = f_list.get(f"{key}[]", [])
                        values = [v.strip() for v in values if v.strip()]
                        sfa_db.set_master_list(con, key, values)
                    self._redirect("/")

                # ── アカウント ──
                elif path == "/account/save":
                    saved_acc_id = sfa_db.upsert_account(
                        con, id=int(f["id"]) if f.get("id") else None,
                        name=f.get("name") or "(無名)",
                        industry=f.get("industry") or None,
                        company_size=f.get("company_size") or None,
                        note=f.get("note") or None,
                    )
                    self._redirect(f"/account/{saved_acc_id}")

                elif path.startswith("/account/") and path.endswith("/delete"):
                    parts = path.split("/")
                    if len(parts) == 4 and parts[3] == "delete" and parts[2].isdigit():
                        con.execute("DELETE FROM accounts WHERE id=?", (int(parts[2]),))
                        con.commit()
                    self._redirect("/accounts")

                elif path == "/accounts/bulk_delete":
                    ids = f_list.get("ids", [])
                    for acc_id in ids:
                        if str(acc_id).isdigit():
                            con.execute("DELETE FROM accounts WHERE id=?", (int(acc_id),))
                    if ids:
                        con.commit()
                    self._redirect("/accounts")

                # ── 商談一括編集 ──
                elif path == "/deals/bulk_edit":
                    _DEAL_ALLOWED = {"stage", "owner", "sub_owner", "business_type_l1"}
                    ids = f_list.get("ids", [])
                    field = f.get("field", "")
                    value = f.get("value", "")
                    if field in _DEAL_ALLOWED and ids:
                        if field == "stage":
                            valid = sfa_db.get_master_list(con, "deal_stages")
                            if value and value not in valid:
                                self._redirect("/deals")
                                return
                        for did in ids:
                            if str(did).isdigit():
                                con.execute(
                                    f"UPDATE deals SET {field}=?, updated_at=datetime('now') WHERE id=?",
                                    (value or None, int(did)),
                                )
                        con.commit()
                        if field == "stage" and theme_client is not None:
                            for did in ids:
                                if str(did).isdigit():
                                    try:
                                        theme_link.sync_deal(theme_client, con, int(did))
                                    except Exception:
                                        pass
                    self._redirect("/deals")

                # ── 商談 ──
                elif path == "/deal/save":
                    def num(k):
                        v = f.get(k, "").strip()
                        try:
                            return float(v) if v else None
                        except ValueError:
                            return None
                    # 新規アカウント自動作成（deal/new フォームで「新規アカウントを追加」チェック時）
                    deal_account_id = int(f["account_id"]) if f.get("account_id") else None
                    new_acc_name = (f.get("new_account_name") or "").strip()
                    if new_acc_name and not deal_account_id:
                        existing_acc = con.execute(
                            "SELECT id FROM accounts WHERE name=?", (new_acc_name,)
                        ).fetchone()
                        if existing_acc:
                            deal_account_id = existing_acc["id"]
                        else:
                            industries_m = sfa_db.get_master_list(con, "industries")
                            sizes_m = sfa_db.get_master_list(con, "company_sizes")
                            try:
                                est = leads_csv.estimate_companies([new_acc_name], industries_m, sizes_m)
                                est1 = est.get(new_acc_name, {})
                            except Exception:
                                est1 = {}
                            deal_account_id = sfa_db.upsert_account(
                                con, name=new_acc_name,
                                industry=est1.get("industry"),
                                company_size=est1.get("company_size"),
                            )
                    did = sfa_db.upsert_deal(
                        con, id=int(f["id"]) if f.get("id") else None,
                        account_id=deal_account_id,
                        deal_name=f.get("deal_name") or "(無題)",
                        stage=f.get("stage") or None,
                        business_type_l1=f.get("business_type_l1") or None,
                        business_type_l2=f.get("business_type_l2") or None,
                        lead_pattern=f.get("lead_pattern") or None,
                        owner=f.get("owner") or None,
                        sub_owner=f.get("sub_owner") or None,
                        value_lumpsum=num("value_lumpsum"),
                        value_lumpsum_monthly=num("value_lumpsum_monthly"),
                        value_recurring=num("value_recurring"),
                        client_budget=f.get("client_budget") or None,
                        next_milestone_date=f.get("next_milestone_date") or None,
                        next_milestone_label=f.get("next_milestone_label") or None,
                        note=f.get("note") or None,
                        goal=f.get("goal") or None,
                        importance=f.get("importance") or None,
                        status=f.get("status") or "open",
                        cost_stage=f.get("cost_stage") or None,
                        approach_value=num("approach_value"),
                        approach_rate=num("approach_rate"),
                        reduction_rate=num("reduction_rate"),
                        fee_rate=num("fee_rate"),
                        diagnosis_cost=num("diagnosis_cost"),
                    )
                    if theme_client is not None:
                        try:
                            theme_link.sync_deal(theme_client, con, did)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[theme_link] sync_deal failed: {exc}")
                    self._redirect(f"/deal/{did}")

                # ── 開発案件 ──
                elif path == "/dev-project/new":
                    deal_id_val = int(f["deal_id"]) if f.get("deal_id") else None
                    if not deal_id_val:
                        self._redirect("/dev-projects")
                        return
                    pid = sfa_db.upsert_dev_project(
                        con, id=None, deal_id=deal_id_val,
                        theme=f.get("theme") or "(無題)",
                        theme_detail=f.get("theme_detail") or None,
                        status=f.get("status") or None,
                        stage=f.get("stage") or None,
                        resolution=f.get("resolution") or None,
                        budget_confirmed=f.get("budget_confirmed") or None,
                        difficulty=f.get("difficulty") or None,
                        has_backend=f.get("has_backend") or None,
                        dev_owner=f.get("dev_owner") or None,
                        tech_support=f.get("tech_support") or None,
                        dev_milestone=f.get("dev_milestone") or None,
                        deadline=f.get("deadline") or None,
                        dev_policy=f.get("dev_policy") or None,
                    )
                    if theme_client is not None:
                        try:
                            dev_project_link.sync_dev_project(theme_client, con, pid)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[dev_project_link] sync_dev_project failed: {exc}")
                    self._redirect(f"/deal/{deal_id_val}")

                elif path.startswith("/dev-project/") and path.endswith("/edit"):
                    try:
                        pid = int(path.split("/")[2])
                    except (ValueError, IndexError):
                        self._redirect("/dev-projects")
                        return
                    existing = sfa_db.get_dev_project(con, pid)
                    if not existing:
                        self._redirect("/dev-projects")
                        return
                    sfa_db.upsert_dev_project(
                        con, id=pid, deal_id=existing["deal_id"],
                        theme=f.get("theme") or "(無題)",
                        theme_detail=f.get("theme_detail") or None,
                        status=f.get("status") or None,
                        stage=f.get("stage") or None,
                        resolution=f.get("resolution") or None,
                        budget_confirmed=f.get("budget_confirmed") or None,
                        difficulty=f.get("difficulty") or None,
                        has_backend=f.get("has_backend") or None,
                        dev_owner=f.get("dev_owner") or None,
                        tech_support=f.get("tech_support") or None,
                        dev_milestone=f.get("dev_milestone") or None,
                        deadline=f.get("deadline") or None,
                        dev_policy=f.get("dev_policy") or None,
                    )
                    if theme_client is not None:
                        try:
                            dev_project_link.sync_dev_project(theme_client, con, pid)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[dev_project_link] sync_dev_project failed: {exc}")
                    self._redirect(f"/deal/{existing['deal_id']}")

                elif path.startswith("/dev-project/") and path.endswith("/delete"):
                    try:
                        pid = int(path.split("/")[2])
                    except (ValueError, IndexError):
                        self._redirect("/dev-projects")
                        return
                    existing = sfa_db.get_dev_project(con, pid)
                    if existing:
                        sfa_db.delete_dev_project(con, pid)
                        if theme_client is not None and existing.get("hisho_id"):
                            try:
                                dev_project_link.delete_dev_project_remote(theme_client, existing["hisho_id"])
                            except Exception as exc:  # noqa: BLE001
                                print(f"[dev_project_link] delete_dev_project_remote failed: {exc}")
                    self._redirect(f"/deal/{existing['deal_id']}" if existing else "/dev-projects")

                elif path == "/activity/add":
                    did = int(f["deal_id"])
                    sfa_db.add_activity(
                        con, deal_id=did,
                        type=f.get("type") or None,
                        occurred_on=f.get("occurred_on") or None,
                        contact_name=f.get("contact_name") or None,
                        body=f.get("body") or None,
                    )
                    # 商談の現状メモ・次回MSを同時更新（入力があった場合のみ）
                    update_note = f.get("update_note", "").strip()
                    ms_date = f.get("next_milestone_date", "").strip()
                    ms_label = f.get("next_milestone_label", "").strip()
                    if update_note or ms_date or ms_label:
                        deal = sfa_db.get_deal(con, did)
                        if deal:
                            sfa_db.upsert_deal(
                                con, id=did,
                                account_id=deal["account_id"],
                                theme_id=deal.get("theme_id"),
                                deal_name=deal["deal_name"],
                                stage=deal.get("stage"),
                                business_type_l1=deal.get("business_type_l1"),
                                business_type_l2=deal.get("business_type_l2"),
                                lead_pattern=deal.get("lead_pattern"),
                                owner=deal.get("owner"),
                                sub_owner=deal.get("sub_owner"),
                                value_lumpsum=deal.get("value_lumpsum"),
                                value_lumpsum_monthly=deal.get("value_lumpsum_monthly"),
                                value_recurring=deal.get("value_recurring"),
                                client_budget=deal.get("client_budget"),
                                next_milestone_date=ms_date or deal.get("next_milestone_date"),
                                next_milestone_label=ms_label or deal.get("next_milestone_label"),
                                note=update_note or deal.get("note"),
                                goal=deal.get("goal"),
                                status=deal.get("status"),
                            )
                    self._redirect(f"/deal/{did}")

                # ── 商談インライン編集 ──
                elif path.startswith("/deal/") and path.endswith("/field"):
                    _DEAL_ALLOWED_FIELDS = {"stage", "owner", "sub_owner", "business_type_l1", "business_type_l2", "client_budget", "value_lumpsum"}
                    parts = path.split("/")
                    _ok = False
                    _err = ""
                    if len(parts) == 4 and parts[3] == "field" and parts[2].isdigit():
                        deal_id = int(parts[2])
                        field = f.get("field", "")
                        value = f.get("value", "")
                        if field not in _DEAL_ALLOWED_FIELDS:
                            _err = "不正なフィールド"
                        elif field == "stage":
                            valid_stages = sfa_db.get_master_list(con, "deal_stages")
                            if value and value not in valid_stages:
                                _err = "不正なステージ値"
                            else:
                                con.execute(
                                    "UPDATE deals SET stage=?, updated_at=datetime('now') WHERE id=?",
                                    (value or None, deal_id),
                                )
                                con.commit()
                                _ok = True
                                if theme_client is not None:
                                    try:
                                        theme_link.sync_deal(theme_client, con, deal_id)
                                    except Exception as _exc:
                                        print(f"[theme_link] sync_deal failed: {_exc}")
                        else:
                            con.execute(
                                f"UPDATE deals SET {field}=?, updated_at=datetime('now') WHERE id=?",
                                (value or None, deal_id),
                            )
                            con.commit()
                            _ok = True
                    else:
                        _err = "不正なリクエスト"
                    _resp = json.dumps({"ok": _ok} if _ok else {"ok": False, "error": _err}).encode("utf-8")
                    self._send(_resp, ctype="application/json")

                # ── メールパターン ──
                elif path == "/email-patterns/save":
                    cc_list = f_list.get("cc", [])
                    sfa_db.save_email_pattern(
                        con,
                        name=f.get("name", ""),
                        subject=f.get("subject", ""),
                        body=f.get("body", ""),
                        from_address=f.get("from_address") or None,
                        cc_addresses=",".join(cc_list) if cc_list else None,
                    )
                    self._redirect("/email-patterns")
                elif path.startswith("/email-patterns/") and path.endswith("/save"):
                    try:
                        pid = int(path.split("/")[2])
                        cc_list = f_list.get("cc", [])
                        sfa_db.save_email_pattern(
                            con, id=pid,
                            name=f.get("name", ""),
                            subject=f.get("subject", ""),
                            body=f.get("body", ""),
                            from_address=f.get("from_address") or None,
                            cc_addresses=",".join(cc_list) if cc_list else None,
                        )
                        self._redirect("/email-patterns")
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>不正なリクエスト</div>"), 400)
                elif path.startswith("/email-patterns/") and path.endswith("/delete"):
                    try:
                        pid = int(path.split("/")[2])
                        sfa_db.delete_email_pattern(con, pid)
                        self._redirect("/email-patterns")
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>不正なリクエスト</div>"), 400)

                # ── 初回ヒアリング ──
                elif path == "/hearing-templates/save" or (
                        path.startswith("/hearing-templates/") and path.endswith("/save")):
                    try:
                        items = json.loads(f.get("items_json") or "[]")
                        if not isinstance(items, (list, dict)):
                            items = []
                    except (ValueError, TypeError):
                        items = []
                    tid = None
                    if path != "/hearing-templates/save":
                        tid = int(path.split("/")[2])
                    sfa_db.save_hearing_template(
                        con, id=tid,
                        name=f.get("name", "") or "(無題)",
                        description=f.get("description") or None,
                        items=items,
                    )
                    self._redirect("/hearing-templates")
                elif path.startswith("/hearing-templates/") and path.endswith("/delete"):
                    try:
                        tid = int(path.split("/")[2])
                        sfa_db.delete_hearing_template(con, tid)
                        self._redirect("/hearing-templates")
                    except (ValueError, IndexError):
                        self._send(render("<div class=card>不正なリクエスト</div>"), 400)
                elif path.startswith("/hearing/result/") and path.endswith("/delete"):
                    parts = path.split("/")
                    if len(parts) == 5 and parts[4] == "delete" and parts[3].isdigit():
                        rid = int(parts[3])
                        r = sfa_db.get_hearing_result(con, rid)
                        sfa_db.delete_hearing_result(con, rid)
                        self._redirect(f"/deal/{r['deal_id']}" if r else "/hearings")
                    else:
                        self._redirect("/hearings")

                elif path == "/hearings/bulk_delete":
                    ids = f_list.get("ids", [])
                    for rid in ids:
                        if str(rid).isdigit():
                            sfa_db.delete_hearing_result(con, int(rid))
                    self._redirect("/hearings")

                elif path == "/hearing/autosave":
                    try:
                        ttype = f.get("target_type", "")
                        tval_id = int(f.get("target_id") or 0)
                        tmpl_id = int(f.get("template_id") or 0)
                        edit_rid = f.get("edit_result_id", "")
                        if edit_rid:
                            # 修正モードの下書きは、新規ヒアリングの下書きと衝突しないよう別名前空間に保存
                            ttype, tval_id = "hearing_edit", int(edit_rid)
                        if not (ttype and tval_id and tmpl_id):
                            raise ValueError("missing target/template")
                        form_data = {k: (v[0] if len(v) == 1 else v) for k, v in f_list.items()}
                        sfa_db.save_hearing_draft(
                            con, target_type=ttype, target_id=tval_id, template_id=tmpl_id,
                            form_data=form_data,
                        )
                        self._send_cors_json(json.dumps({"ok": True}).encode())
                    except Exception as exc:  # noqa: BLE001
                        self._send_cors_json(
                            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode(),
                            status=400,
                        )

                elif path == "/hearing/submit":
                    try:
                        ttype = f.get("target_type", "")
                        tval_id = int(f.get("target_id") or 0)
                        tmpl_id = int(f.get("template_id") or 0)
                    except ValueError:
                        self._send(render("<div class=card>不正なリクエスト</div>"), 400)
                        return
                    tmpl = sfa_db.get_hearing_template(con, tmpl_id) if tmpl_id else None
                    # 対象 deal_id を確定（リードは商談化）
                    deal_id = None
                    if ttype == "lead":
                        lead = sfa_db.get_lead(con, tval_id)
                        if lead:
                            try:
                                deal_id = convert_lead_to_deal(con, lead)
                            except Exception as _e:
                                print(f"[hearing/submit] convert failed: {_e}", flush=True)
                    elif ttype == "deal":
                        d = sfa_db.get_deal(con, tval_id)
                        deal_id = d["id"] if d else None
                    if not deal_id or not tmpl:
                        self._send(render("<div class=card>対象またはテンプレートが見つかりません</div>"), 404)
                        return
                    # 回答を組み立て（Q&A・矢羽混在対応）
                    answers = []
                    for i, it in enumerate(tmpl.get("items") or []):
                        if it.get("type") == "yabane":
                            try:
                                yb_ans = json.loads(f.get(f"answer_{i}") or "{}")
                            except (ValueError, TypeError):
                                yb_ans = {}
                            answers.append({"label": it.get("label") or "業務プロセス",
                                            "type": "yabane", "answer": yb_ans})
                        elif it.get("type") == "choice" and it.get("multi"):
                            ans = [v for v in f_list.get(f"answer_{i}", []) if v]
                            answers.append({"label": it.get("label"),
                                            "type": it.get("type"), "answer": ans})
                        else:
                            ans = (f.get(f"answer_{i}", "") or "").strip()
                            answers.append({"label": it.get("label"),
                                            "type": it.get("type"), "answer": ans})
                    conducted_on = f.get("occurred_on") or None

                    # 修正モード: 新規の活動履歴は作らず、既存hearing_resultsを上書きするだけ
                    edit_rid_raw = f.get("edit_result_id", "")
                    if edit_rid_raw:
                        try:
                            edit_rid = int(edit_rid_raw)
                        except ValueError:
                            self._send(render("<div class=card>不正なリクエスト</div>"), 400)
                            return
                        sfa_db.update_hearing_result(con, edit_rid, conducted_on=conducted_on, answers=answers)
                        sfa_db.delete_hearing_draft(
                            con, target_type="hearing_edit", target_id=edit_rid, template_id=tmpl_id,
                        )
                        self._redirect(f"/hearing/result/{edit_rid}")
                        return

                    # 活動履歴を1件追加
                    act_id = sfa_db.add_activity(
                        con, deal_id=deal_id,
                        type=f.get("type") or None,
                        occurred_on=conducted_on,
                        contact_name=f.get("contact_name") or None,
                        body=f.get("body") or None,
                    )
                    # ヒアリング結果を保存（活動履歴と相互リンク）
                    sfa_db.add_hearing_result(
                        con, deal_id=deal_id, template_id=tmpl["id"],
                        template_name=tmpl.get("name"), conducted_on=conducted_on,
                        answers=answers, activity_id=act_id,
                    )
                    # 確定保存できたので自動保存の下書きは不要（元のtarget_type/idで保存されているため）
                    sfa_db.delete_hearing_draft(con, target_type=ttype, target_id=tval_id, template_id=tmpl_id)
                    # 商談の現状メモ・次回MSを更新（入力があった場合のみ）
                    update_note = (f.get("update_note") or "").strip()
                    ms_date = (f.get("next_milestone_date") or "").strip()
                    ms_label = (f.get("next_milestone_label") or "").strip()
                    if update_note or ms_date or ms_label:
                        deal = sfa_db.get_deal(con, deal_id)
                        if deal:
                            sfa_db.upsert_deal(
                                con, id=deal_id,
                                account_id=deal["account_id"], theme_id=deal.get("theme_id"),
                                deal_name=deal["deal_name"], stage=deal.get("stage"),
                                business_type_l1=deal.get("business_type_l1"),
                                business_type_l2=deal.get("business_type_l2"),
                                lead_pattern=deal.get("lead_pattern"), owner=deal.get("owner"),
                                sub_owner=deal.get("sub_owner"),
                                value_lumpsum=deal.get("value_lumpsum"),
                                value_lumpsum_monthly=deal.get("value_lumpsum_monthly"),
                                value_recurring=deal.get("value_recurring"),
                                client_budget=deal.get("client_budget"),
                                next_milestone_date=ms_date or deal.get("next_milestone_date"),
                                next_milestone_label=ms_label or deal.get("next_milestone_label"),
                                note=update_note or deal.get("note"),
                                goal=deal.get("goal"), status=deal.get("status"),
                            )
                    if theme_client is not None:
                        try:
                            theme_link.sync_deal(theme_client, con, deal_id)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[theme_link] sync_deal failed: {exc}")
                    self._redirect(f"/deal/{deal_id}")

                # ── リード ──
                elif path == "/leads/save":
                    existing_id = int(f["id"]) if f.get("id") else None
                    existing_deal_id = None
                    existing_pitch_theme_id = None
                    if existing_id:
                        existing = sfa_db.get_lead(con, existing_id)
                        existing_deal_id = existing.get("deal_id") if existing else None
                        existing_pitch_theme_id = existing.get("pitch_theme_id") if existing else None
                    company_name = f.get("company") or "(未設定)"
                    industry = f.get("industry") or None
                    company_size = f.get("company_size") or None
                    lid = sfa_db.upsert_lead(
                        con, id=existing_id,
                        name=f.get("name") or "(無名)",
                        company=company_name,
                        industry=industry,
                        company_size=company_size,
                        title=f.get("title") or None,
                        email=f.get("email") or None,
                        phone=f.get("phone") or None,
                        source=f.get("source") or "other",
                        pitch_theme_id=existing_pitch_theme_id,
                        lead_status=f.get("lead_status") or "new",
                        notes=f.get("notes") or None,
                        assigned_to=f.get("assigned_to") or None,
                        deal_id=existing_deal_id,
                    )
                    # アカウント自動追加・補完
                    existing_acc = con.execute(
                        "SELECT id, industry, company_size FROM accounts WHERE name=?",
                        (company_name,)
                    ).fetchone()
                    if existing_acc is None:
                        sfa_db.upsert_account(
                            con, name=company_name,
                            industry=industry,
                            company_size=company_size,
                        )
                    else:
                        acc = dict(existing_acc)
                        updates = {}
                        if industry and not acc.get("industry"):
                            updates["industry"] = industry
                        if company_size and not acc.get("company_size"):
                            updates["company_size"] = company_size
                        if updates:
                            set_clause = ", ".join(f"{k}=?" for k in updates)
                            con.execute(
                                f"UPDATE accounts SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                                (*updates.values(), acc["id"]),
                            )
                            con.commit()
                    self._redirect(f"/leads/{lid}")

                elif path == "/leads/upload_meishi":
                    file_item = f.get("meishi_file")
                    if not file_item or not isinstance(file_item, tuple):
                        self._send(render(leads_import_page(con), flash="ファイルが選択されていません。"))
                    else:
                        filename, data = file_item
                        try:
                            from . import meishi_import
                            added, skipped, errors = meishi_import.import_meishi_file(con, data, filename)
                            msg = f"取込完了: {added}件追加、{skipped}件スキップ。"
                            if errors:
                                msg += " エラー: " + "; ".join(errors[:3])
                            self._send(render(leads_import_page(con, result=msg)))
                        except ImportError:
                            self._send(render(leads_import_page(con), flash="meishi_importモジュールが見つかりません。"))
                        except Exception as exc:
                            self._send(render(leads_import_page(con), flash=f"取込エラー: {exc}"))

                elif path == "/leads/import":
                    try:
                        csv_text = _decode_uploaded_csv(f.get("csv_file")) or f.get("csv_text", "")
                        ok, skip = leads_csv.import_leads(
                            con, csv_text,
                            industries=sfa_db.get_master_list(con, "industries"),
                            company_sizes=sfa_db.get_master_list(con, "company_sizes"),
                        )
                        self._send(render(
                            leads_import_page(con),
                            flash=f"取込完了: {ok}件追加。" + (f"スキップ {skip}件。" if skip else ""),
                        ))
                    except Exception as exc:
                        self._send(render(leads_import_page(con), flash=f"取込エラー: {exc}"))

                elif path == "/deals/import":
                    try:
                        csv_text = _decode_uploaded_csv(f.get("csv_file")) or f.get("csv_text", "")
                        ok_deals, ok_accounts, dup_skip, skip = deals_csv.import_deals(
                            con, csv_text,
                            industries=sfa_db.get_master_list(con, "industries"),
                            company_sizes=sfa_db.get_master_list(con, "company_sizes"),
                        )
                        msg = f"取込完了: 商談{ok_deals}件・アカウント{ok_accounts}件を追加。"
                        if dup_skip:
                            msg += f" 重複スキップ {dup_skip}件（同一内容の商談が既存）。"
                        if skip:
                            msg += f" エラースキップ {skip}件。"
                        if ok_deals and theme_client is not None:
                            import threading as _threading
                            _threading.Thread(
                                target=theme_link.sync_all_pending, args=(theme_client, db_path),
                                daemon=True,
                            ).start()
                            msg += " テーマDBへの同期をバックグラウンドで開始しました。"
                        self._send(render(deals_import_page(con), flash=msg))
                    except Exception as exc:
                        self._send(render(deals_import_page(con), flash=f"取込エラー: {exc}"))

                elif path == "/deals/sync_pending":
                    if theme_client is None:
                        self._send(render(home_page(con), flash="テーマDB連携が無効です（THEME_API_TOKEN未設定）。"))
                    else:
                        pending = con.execute("SELECT COUNT(*) c FROM deals WHERE theme_id IS NULL").fetchone()["c"]
                        import threading as _threading
                        _threading.Thread(
                            target=theme_link.sync_all_pending, args=(theme_client, db_path),
                            daemon=True,
                        ).start()
                        self._send(render(
                            home_page(con),
                            flash=f"テーマDB未同期の商談{pending}件の同期をバックグラウンドで開始しました。"
                                  f"数分後に商談一覧の「連携」列で確認できます。",
                        ))

                elif path == "/leads/bulk_source":
                    ids = f_list.get("ids", [])
                    source = f.get("source", "")
                    if source in sfa_db.LEAD_SOURCES and ids:
                        for lead_id in ids:
                            if lead_id.isdigit():
                                con.execute(
                                    "UPDATE leads SET source=?, updated_at=datetime('now') WHERE id=?",
                                    (source, int(lead_id)),
                                )
                        con.commit()
                    self._redirect("/leads")

                elif path == "/leads/bulk_edit":
                    _LEAD_ALLOWED_FIELDS = {"source", "assigned_to", "industry", "company_size", "lead_status"}
                    ids = f_list.get("ids", [])
                    field = f.get("field", "")
                    value = f.get("value", "")
                    if field in _LEAD_ALLOWED_FIELDS and ids:
                        if field == "source" and value and value not in sfa_db.LEAD_SOURCES:
                            pass  # invalid, skip
                        elif field == "lead_status" and value and value not in sfa_db.LEAD_STATUSES:
                            pass  # invalid, skip
                        else:
                            for lead_id in ids:
                                if str(lead_id).isdigit():
                                    con.execute(
                                        f"UPDATE leads SET {field}=?, updated_at=datetime('now') WHERE id=?",
                                        (value or None, int(lead_id)),
                                    )
                            con.commit()
                    self._redirect("/leads")

                elif path == "/leads/bulk_delete":
                    ids = f_list.get("ids", [])
                    for lead_id in ids:
                        if str(lead_id).isdigit():
                            con.execute("DELETE FROM leads WHERE id=?", (int(lead_id),))
                    if ids:
                        con.commit()
                    self._redirect("/leads")

                elif path.startswith("/leads/") and path.endswith("/set_pattern"):
                    parts = path.split("/")
                    _ok = False
                    _err = ""
                    if len(parts) == 4 and parts[2].isdigit():
                        lid = int(parts[2])
                        pid_str = f.get("pattern_id", "")
                        pid = int(pid_str) if pid_str and pid_str.isdigit() else None
                        sfa_db.set_lead_email_pattern(con, lid, pid)
                        _ok = True
                    else:
                        _err = "不正なリクエスト"
                    self._send(json.dumps({"ok": _ok} if _ok else {"ok": False, "error": _err}).encode(), ctype="application/json")
                elif path.startswith("/leads/") and path.endswith("/field"):
                    _LEAD_ALLOWED_FIELDS = {"source", "assigned_to", "industry", "company_size", "lead_status"}
                    parts = path.split("/")
                    _ok = False
                    _err = ""
                    if len(parts) == 4 and parts[3] == "field" and parts[2].isdigit():
                        lid = int(parts[2])
                        field = f.get("field", "")
                        value = f.get("value", "")
                        if field not in _LEAD_ALLOWED_FIELDS:
                            _err = "不正なフィールド"
                        elif field == "source" and value and value not in sfa_db.LEAD_SOURCES:
                            _err = "不正な経路値"
                        elif field == "lead_status" and value and value not in sfa_db.LEAD_STATUSES:
                            _err = "不正なステータス値"
                        else:
                            con.execute(
                                f"UPDATE leads SET {field}=?, updated_at=datetime('now') WHERE id=?",
                                (value or None, lid),
                            )
                            con.commit()
                            _ok = True
                    else:
                        _err = "不正なリクエスト"
                    _resp = json.dumps({"ok": _ok} if _ok else {"ok": False, "error": _err}).encode("utf-8")
                    self._send(_resp, ctype="application/json")

                elif path.startswith("/leads/") and path.endswith("/delete"):
                    parts = path.split("/")
                    if len(parts) == 4 and parts[3] == "delete" and parts[2].isdigit():
                        lid = int(parts[2])
                        con.execute("DELETE FROM leads WHERE id=?", (lid,))
                        con.commit()
                    self._redirect("/leads")

                elif path.startswith("/leads/") and path.endswith("/activity"):
                    lid = int(path.split("/")[2])
                    sfa_db.create_lead_activity(
                        con, lead_id=lid,
                        type=f.get("type") or "note",
                        content=f.get("content") or "(内容なし)",
                        author=f.get("author") or None,
                    )
                    self._redirect(f"/leads/{lid}")

                elif path.startswith("/leads/") and path.endswith("/status"):
                    lid = int(path.split("/")[2])
                    new_status = f.get("status", "")
                    if new_status in sfa_db.LEAD_STATUSES:
                        con.execute(
                            "UPDATE leads SET lead_status=?, updated_at=datetime('now') WHERE id=?",
                            (new_status, lid),
                        )
                        con.commit()
                    self._redirect(f"/leads/{lid}")

                elif path.startswith("/leads/") and path.endswith("/convert"):
                    lid = int(path.split("/")[2])
                    lead = sfa_db.get_lead(con, lid)
                    if not lead:
                        self._redirect("/leads")
                    else:
                        try:
                            deal_id = convert_lead_to_deal(con, lead)
                            self._redirect(f"/deal/{deal_id}")
                        except Exception as _conv_e:
                            print(f"[convert] error lid={lid}: {_conv_e}", flush=True)
                            import traceback as _tb; _tb.print_exc()
                            self._redirect(f"/leads/{lid}")

                # ── 商談 → リード戻し ──
                elif path.endswith("/revert_to_lead") and "/deal/" in path:
                    deal_id_str = path.split("/deal/")[1].split("/")[0]
                    _redirect_to = "/deals"
                    if deal_id_str.isdigit():
                        _did = int(deal_id_str)
                        _deal = sfa_db.get_deal(con, _did)
                        if _deal and _deal.get("status") != "closed":
                            _lid = None
                            # 既存リード検索（deal_id が紐付いているもの）
                            _lead_row = con.execute(
                                "SELECT * FROM leads WHERE deal_id=? LIMIT 1", (_did,)
                            ).fetchone()
                            if _lead_row:
                                _lid = dict(_lead_row)["id"]
                                con.execute(
                                    "UPDATE leads SET lead_status='following', deal_id=NULL, "
                                    "updated_at=datetime('now') WHERE id=?", (_lid,)
                                )
                                con.execute(
                                    "INSERT INTO lead_activities (lead_id,type,content,author) VALUES (?,?,?,?)",
                                    (_lid, "note", "アポ未獲得のため商談からリードへ戻す（フォロー中に変更）。", "システム"),
                                )
                            else:
                                # 既存リードがなければアカウントから新規作成
                                _acct_row = con.execute(
                                    "SELECT * FROM accounts WHERE id=?", (_deal.get("account_id"),)
                                ).fetchone()
                                _acct = dict(_acct_row) if _acct_row else {}
                                _lid = sfa_db.upsert_lead(
                                    con, name=_acct.get("name", "（不明）"),
                                    company=_acct.get("name", "（不明）"),
                                    lead_status="following",
                                    notes=f"アポ未獲得のため商談 #{_did} ({_deal.get('deal_name','')}) からリードに戻す",
                                    assigned_to=_deal.get("owner"),
                                )
                            # 商談をクローズ
                            con.execute(
                                "UPDATE deals SET status='closed', "
                                "note=CASE WHEN note IS NULL OR note='' THEN ? ELSE note||char(10)||? END, "
                                "updated_at=datetime('now') WHERE id=?",
                                ("アポ未獲得のためクローズ（リードに戻す）",
                                 "アポ未獲得のためクローズ（リードに戻す）", _did),
                            )
                            con.commit()
                            if _lid:
                                _redirect_to = f"/lead/{_lid}"
                    self._redirect(_redirect_to)

                # ── メモ保存 ──
                elif path == "/api/memo/save":
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            data = f
                        tid = data.get("theme_id")
                        note_id = con.execute(
                            "INSERT INTO meeting_notes(theme_id,note_date,body,task,task_owner,task_due) VALUES(?,?,?,?,?,?)",
                            (int(tid) if tid else None, data.get("note_date") or None,
                             data.get("body") or None, data.get("task") or None,
                             data.get("task_owner") or None, data.get("task_due") or None),
                        ).lastrowid
                        con.commit()
                        self._send_cors_json(json.dumps({"ok": True, "id": note_id}, ensure_ascii=False).encode())

                # ── メモ削除 ──
                elif path == "/api/memo/delete":
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            data = f
                        note_id = data.get("id")
                        con.execute("DELETE FROM meeting_notes WHERE id=?", (int(note_id),))
                        con.commit()
                        self._send_cors_json(json.dumps({"ok": True}, ensure_ascii=False).encode())

                # ── タスク完了トグル ──
                elif path == "/api/memo/toggle_task":
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            data = f
                        note_id = data.get("id")
                        done = 1 if data.get("done") else 0
                        con.execute("UPDATE meeting_notes SET task_done=? WHERE id=?", (done, int(note_id)))
                        con.commit()
                        self._send_cors_json(json.dumps({"ok": True}, ensure_ascii=False).encode())

                # ── 翌日アポSlack通知の重複防止マーカー ──
                elif path == "/api/deals/mark_notified":
                    qs = self._qs()
                    token = (qs.get("token", [None])[0] or "")
                    if SFA_API_TOKEN and token != SFA_API_TOKEN:
                        self._send_cors_json(b'{"error":"unauthorized"}', status=401)
                    else:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            data = f
                        deal_id = data.get("deal_id")
                        notified_date = data.get("date")
                        if deal_id and notified_date:
                            con.execute(
                                "UPDATE deals SET slack_notified_date=? WHERE id=?",
                                (notified_date, int(deal_id)),
                            )
                            con.commit()
                            self._send_cors_json(json.dumps({"ok": True}, ensure_ascii=False).encode())
                        else:
                            self._send_cors_json(b'{"error":"deal_id and date required"}', status=400)

                # ── Slack Events API ──
                elif path == "/slack/events":
                    import threading as _threading
                    # body は do_POST 先頭の raw 変数で読み込み済み（rfile は再読不可）
                    try:
                        data = json.loads(raw)
                    except Exception:
                        self._send("<error/>", 400)
                        return

                    # URL検証チャレンジ
                    if data.get("type") == "url_verification":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"challenge": data["challenge"]}).encode())
                        return

                    # Slackに即時200を返してからバックグラウンド処理
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"ok")

                    # イベントをバックグラウンドで処理（conはスレッドセーフのため再接続）
                    def _process():
                        _con = sfa_db.connect(db_path)
                        try:
                            from cowork import slack_bot
                            slack_bot.handle_event(data, _con, theme_client)
                        except Exception as _e:
                            print(f"[slack_events] error: {_e}")
                        finally:
                            _con.close()
                    _threading.Thread(target=_process, daemon=True).start()
                    return

                else:
                    self._send(render("<div class=card>不明な操作</div>"), 404)
            finally:
                con.close()

    return H


def start(db_path: str = sfa_db.DEFAULT_DB_PATH, port: int = 8787,
          theme_client: ThemeDBClient | None = None) -> None:
    sfa_db.init_db(db_path)
    handler = _make_handler(db_path, theme_client)
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"Inproc Salesforce: http://localhost:{port}  (DB={db_path})")
    srv.serve_forever()
