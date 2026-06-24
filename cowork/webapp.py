"""フェーズ2-1：営業情報DBのブラウザ入力画面（標準ライブラリのみ）。

アカウント・商談・活動、リード・ピッチテーマを入力／一覧し、商談をテーマDBへ同期できる。
入力負荷を抑えるためステージ等はプルダウン。挙動安定を優先し外部依存なし。

起動: python scripts/run_webapp.py  → http://localhost:8787
"""

from __future__ import annotations

import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import sfa_db
from . import leads_csv
from .theme_db import ThemeDBClient
from . import theme_link


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


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cowork 営業支援</title>
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
  <h1>Cowork 営業支援</h1>
  <a href="/">ホーム</a>
  <a href="/deals">商談一覧</a>
  <a href="/leads">リード</a>
  <a href="/masters" style="opacity:.65;font-size:12px">⚙ マスタ編集</a>
</header>
<main>{flash}{body}</main></body></html>"""


def render(body: str, flash: str = "") -> bytes:
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return PAGE.format(body=body, flash=flash_html).encode("utf-8")


# ── ダッシュボード ──────────────────────────────────────────────────────────────

def dashboard_page(con) -> str:
    deals = sfa_db.list_deals(con, status="open")
    accounts = sfa_db.list_accounts(con)
    leads = sfa_db.list_leads(con)
    sheet_id = os.environ.get("SALES_SHEET_ID", "")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else "#"
    hisho_url = os.environ.get("THEME_API_URL", "https://hisho-ohxe.onrender.com").rstrip("/") + "/dashboard"

    # 直近の活動がある商談（最大3件）
    recent_rows = ""
    recent_deals = deals[:3]
    for d in recent_deals:
        ms = _esc(d.get("next_milestone_date") or "—")
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
          <a class="btn sec" href="/deals">一覧</a>
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
      <h2>進行中の商談（直近）</h2>
      <table>
        <tr><th>アカウント</th><th>案件名</th><th>ステージ</th><th>次回MS</th></tr>
        {recent_rows or '<tr><td colspan=4 class=muted>進行中の商談がありません</td></tr>'}
      </table>
        <p style="margin-top:10px">
        <a class="btn sec" href="/deals">すべての商談を見る</a>
        <a class="btn ext" href="{hisho_url}" target="_blank" style="margin-left:8px">Salesダッシュボード ↗</a>
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
    deals = sfa_db.list_deals(con, status=status_filter, owner=owner, stage=stage_filter)
    owners = sfa_db.get_master_list(con, "owners")
    stages = sfa_db.get_master_list(con, "deal_stages")
    biz_l1_list = sfa_db.get_master_list(con, "business_type_l1")
    owner_opts = '<option value="">全担当</option>' + "".join(
        f'<option value="{html.escape(o)}"{" selected" if o == owner else ""}>{html.escape(o)}</option>'
        for o in owners
    )
    status_opts = (
        '<option value="">全ステータス</option>'
        + f'<option value="open"{"  selected" if status_filter=="open" else ""}>進行中</option>'
        + f'<option value="closed"{" selected" if status_filter=="closed" else ""}>クローズ済</option>'
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
    def _deal_inline_select(deal_id, field, values, current):
        opts = "".join(
            f'<option value="{html.escape(v)}"{" selected" if v == current else ""}>{html.escape(v)}</option>'
            for v in values
        )
        return (f'<select onchange="updateDealField({deal_id}, \'{field}\', this.value)"'
                f' style="font-size:11px;padding:1px 2px;max-width:90px">'
                f'<option value=""></option>{opts}</select>')

    # バルク編集用JSオブジェクト構築
    deal_bulk_options = {
        "stage": [["", "（変更なし）"]] + [[s, s] for s in stages],
        "owner": [["", "（変更なし）"]] + [[o, o] for o in owners],
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
        sel_biz_l1 = _deal_inline_select(d["id"], "business_type_l1", biz_l1_list, d.get("business_type_l1") or "")
        biz_l2_values = sfa_db.BUSINESS_TYPE_L2_BY_L1.get(d.get("business_type_l1") or "", [])
        sel_biz_l2 = _deal_inline_select(d["id"], "business_type_l2", biz_l2_values, d.get("business_type_l2") or "")
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
    return f"""
    <div class="card"><h2 style="display:flex;justify-content:space-between;align-items:center">
      <span>商談 ({len(deals)})</span>
      <a class="btn" href="/deal/new">＋商談追加</a>
    </h2>
    {filter_row}
    <form id="deal_bulk_form" method="post" action="/deals/bulk_edit">
    <div style="overflow-x:auto">
    <table style="min-width:900px"><tr>
      <th style="width:28px"><input type="checkbox" id="deal_chk_all" title="全選択"
            onchange="document.querySelectorAll('#deal_bulk_form [name=ids]').forEach(c=>c.checked=this.checked)"></th>
      <th>#</th><th>アカウント</th><th>案件名</th><th>ステージ</th><th>担当</th>
      <th>種別L1</th><th>種別L2</th>
      <th>予算<br><span style="font-size:10px;font-weight:normal;color:#8893a8">(万円)</span></th>
      <th>WT総額<br><span style="font-size:10px;font-weight:normal;color:#8893a8">(提案,万円)</span></th>
      <th>次回MS</th><th class="right">連携</th></tr>
    {''.join(rows) or '<tr><td colspan=12 class=muted>商談がありません。</td></tr>'}
    </table></div>
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap">
      <select id="deal_bulk_field" name="field" style="width:auto">
        <option value="stage">ステージ</option>
        <option value="owner">担当</option>
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
    return f"""
    <div class="card"><h2>{'アカウント編集' if acc else '新規アカウント'}</h2>
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
      <p><button class="btn">保存</button> <a class="btn sec" href="/">キャンセル</a></p>
    </form></div>"""


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
    revert_btn = ""
    if deal.get("id") and deal.get("status") == "open":
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
        <div><label>アカウント *</label>
          <select name="account_id" required>{''.join(acc_opts)}</select></div>
        <div><label>案件名 *</label>
          <input name="deal_name" required value="{_esc(deal.get('deal_name'))}"></div>
        <div><label>ステージ</label>
          <select name="stage">{_opt(sfa_db.get_master_list(con,'deal_stages'), deal.get('stage'))}</select></div>
        <div><label>担当</label>
          <select name="owner">{_opt(sfa_db.get_master_list(con,'owners'), deal.get('owner'))}</select></div>
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
    {activities_html}"""


# ── リード / ピッチテーマ ページ（CRM吸収）─────────────────────────────────────

_SOURCE_TO_LP = {"exhibition": "Exh.", "referral": "Connection", "inbound": "HP", "other": "na"}


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
          <a class="btn sec" href="/leads/import">CSV取込</a>
          <a class="btn" href="/leads/new">＋新規リード</a>
        </span>
      </h2>
      {filter_form}
      <form id="bulk_form" method="post" action="/leads/bulk_edit">
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
          {convert_btn}
          {deal_link}
        </p>
      </form>
      {delete_btn}
    </div>
    {activities_html}"""


def leads_import_page(result: str = "") -> str:
    result_html = f'<div class="flash">{html.escape(result)}</div>' if result else ""
    return f"""
    <div class="card"><h2>リード一括取込</h2>
    {result_html}

    <h3 style="margin:0 0 8px;font-size:14px;color:#3a4760">📇 名刺データ（xlsx）アップロード</h3>
    <p class="muted">名刺管理アプリ（Eight / CAMCARD / Sansan等）からエクスポートしたxlsxをアップロードします。<br>
    会社名・業界などはAIがWebリサーチで補強します（ANTHROPIC_API_KEY 設定時）。</p>
    <form method="post" action="/leads/upload_meishi" enctype="multipart/form-data" style="margin-bottom:20px">
      <label>名刺xlsxファイル</label>
      <input type="file" name="meishi_file" accept=".xlsx,.xls,.csv" required style="padding:4px">
      <p style="margin-top:8px"><button class="btn">アップロードして取込</button>
         <a class="btn sec" href="/leads">キャンセル</a></p>
    </form>

    <hr style="margin:20px 0">
    <h3 style="margin:0 0 8px;font-size:14px;color:#3a4760">📋 CSVペースト取込</h3>
    <p class="muted">下記フォーマットのCSVを貼り付けてください（1行目はヘッダ行、空行はスキップ）。</p>
    <pre style="background:#f4f6f9;padding:10px;border-radius:6px">名前,会社名,役職,メール,電話,獲得経路,ステータス,メモ,担当者
田中 太郎,株式会社○○,営業部長,tanaka@example.com,090-xxx-xxxx,exhibition,new,展示会で名刺交換,</pre>
    <p class="muted" style="margin-top:4px">
      獲得経路: exhibition（展示会）/ referral（紹介）/ inbound（インバウンド）/ other
    </p>
    <form method="post" action="/leads/import">
      <label>CSVデータ（ペースト）</label>
      <textarea name="csv_text" rows="8"
        style="font-family:monospace;font-size:12px" required></textarea>
      <p><button class="btn">取込実行</button>
         <a class="btn sec" href="/leads">キャンセル</a></p>
    </form></div>"""




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

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8")
            d = urllib.parse.parse_qs(raw, keep_blank_values=True)
            return {k: (v[0] if v else "") for k, v in d.items()}

        def _form_multi(self) -> dict:
            """multipart/form-data対応。ファイルはバイト列で返す。"""
            import cgi, io
            ctype = self.headers.get("Content-Type", "")
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            environ = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(n)}
            fs = cgi.FieldStorage(fp=io.BytesIO(body), environ=environ, keep_blank_values=True)
            result = {}
            for key in fs.keys():
                item = fs[key]
                if hasattr(item, "file") and item.filename:
                    result[key] = (item.filename, item.file.read())
                else:
                    result[key] = item.value
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
                elif path == "/":
                    self._send(render(dashboard_page(con)))
                elif path == "/deals":
                    qs = self._qs()
                    def qs1(k): return (qs.get(k, [None])[0] or None)
                    self._send(render(home_page(con, owner=qs1("owner"), status_filter=qs1("status"), stage_filter=qs1("stage"))))
                elif path == "/masters":
                    self._send(render(masters_page(con)))
                elif path == "/activity/new":
                    self._send(render(activity_deal_picker(con)))
                elif path == "/deal/new":
                    self._send(render(deal_form(con)))
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
                    self._send(render(leads_import_page()))
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
                    aid = int(path.split("/")[2])
                    acc = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
                    self._send(render(account_form(con, dict(acc) if acc else None)))
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
                    sfa_db.upsert_account(
                        con, id=int(f["id"]) if f.get("id") else None,
                        name=f.get("name") or "(無名)",
                        industry=f.get("industry") or None,
                        company_size=f.get("company_size") or None,
                        note=f.get("note") or None,
                    )
                    self._redirect("/")

                # ── 商談一括編集 ──
                elif path == "/deals/bulk_edit":
                    _DEAL_ALLOWED = {"stage", "owner", "business_type_l1"}
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
                    did = sfa_db.upsert_deal(
                        con, id=int(f["id"]) if f.get("id") else None,
                        account_id=int(f["account_id"]) if f.get("account_id") else None,
                        deal_name=f.get("deal_name") or "(無題)",
                        stage=f.get("stage") or None,
                        business_type_l1=f.get("business_type_l1") or None,
                        business_type_l2=f.get("business_type_l2") or None,
                        lead_pattern=f.get("lead_pattern") or None,
                        owner=f.get("owner") or None,
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
                    _DEAL_ALLOWED_FIELDS = {"stage", "owner", "business_type_l1", "business_type_l2", "client_budget", "value_lumpsum"}
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

                # ── リード ──
                elif path == "/leads/save":
                    existing_id = int(f["id"]) if f.get("id") else None
                    existing_deal_id = None
                    if existing_id:
                        existing = sfa_db.get_lead(con, existing_id)
                        existing_deal_id = existing.get("deal_id") if existing else None
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
                        self._send(render(leads_import_page(), flash="ファイルが選択されていません。"))
                    else:
                        filename, data = file_item
                        try:
                            from . import meishi_import
                            added, skipped, errors = meishi_import.import_meishi_file(con, data, filename)
                            msg = f"取込完了: {added}件追加、{skipped}件スキップ。"
                            if errors:
                                msg += " エラー: " + "; ".join(errors[:3])
                            self._send(render(leads_import_page(result=msg)))
                        except ImportError:
                            self._send(render(leads_import_page(), flash="meishi_importモジュールが見つかりません。"))
                        except Exception as exc:
                            self._send(render(leads_import_page(), flash=f"取込エラー: {exc}"))

                elif path == "/leads/import":
                    ok, skip = leads_csv.import_leads(
                        con, f.get("csv_text", ""),
                        industries=sfa_db.get_master_list(con, "industries"),
                        company_sizes=sfa_db.get_master_list(con, "company_sizes"),
                    )
                    self._send(render(
                        leads_import_page(),
                        flash=f"取込完了: {ok}件追加。" + (f"スキップ {skip}件。" if skip else ""),
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
                    if not lead or lead.get("deal_id"):
                        self._redirect(f"/leads/{lid}")
                    else:
                        # 1. アカウントを検索または作成
                        existing_acc = con.execute(
                            "SELECT id FROM accounts WHERE name=?", (lead["company"],)
                        ).fetchone()
                        account_id = (dict(existing_acc)["id"] if existing_acc
                                      else sfa_db.upsert_account(
                                          con, name=lead["company"],
                                          industry=lead.get("industry"),
                                          company_size=lead.get("company_size"),
                                      ))
                        # 2. コンタクト作成（重複チェック）
                        if not con.execute(
                            "SELECT id FROM contacts WHERE account_id=? AND name=?",
                            (account_id, lead["name"]),
                        ).fetchone():
                            con.execute(
                                "INSERT INTO contacts (account_id,name,title,email,phone)"
                                " VALUES (?,?,?,?,?)",
                                (account_id, lead["name"], lead.get("title"),
                                 lead.get("email"), lead.get("phone")),
                            )
                            con.commit()
                        # 3. 商談作成
                        stage_map = {
                            "new": "初回アポ実施", "following": "初回アポ実施",
                            "appointed": "初回アポ実施",
                        }
                        stage = stage_map.get(lead.get("lead_status", "new"), "初回アポ実施")
                        deal_name = lead["company"]
                        deal_id = sfa_db.upsert_deal(
                            con, account_id=account_id,
                            deal_name=deal_name, stage=stage,
                            lead_pattern=_SOURCE_TO_LP.get(lead.get("source", "other"), "na"),
                            owner=lead.get("assigned_to"),
                            note=lead.get("notes"),
                        )
                        # 4. リードをクローズ（商談化済）してdeal_idをセット
                        con.execute(
                            "UPDATE leads SET deal_id=?, lead_status='converted', updated_at=datetime('now') WHERE id=?",
                            (deal_id, lid),
                        )
                        con.commit()
                        self._redirect(f"/deal/{deal_id}")

                # ── 商談 → リード戻し ──
                elif path.endswith("/revert_to_lead") and "/deal/" in path:
                    deal_id_str = path.split("/deal/")[1].split("/")[0]
                    _redirect_to = "/deals"
                    if deal_id_str.isdigit():
                        _did = int(deal_id_str)
                        _deal = sfa_db.get_deal(con, _did)
                        if _deal and _deal.get("status") == "open":
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
    print(f"Cowork 営業支援: http://localhost:{port}  (DB={db_path})")
    srv.serve_forever()
