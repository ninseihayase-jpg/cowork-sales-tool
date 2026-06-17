"""フェーズ2-1：営業情報DBのブラウザ入力画面（標準ライブラリのみ）。

アカウント・商談・活動を入力／一覧し、商談をテーマDBへ同期できる。
入力負荷を抑えるためステージ等はプルダウン。挙動安定を優先し外部依存なし。

起動: python scripts/run_webapp.py  → http://localhost:8787
"""

from __future__ import annotations

import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import sfa_db
from .theme_db import ThemeDBClient
from . import theme_link


def _opt(values: list[str], selected: str | None) -> str:
    out = ['<option value=""></option>']
    for v in values:
        sel = " selected" if v == selected else ""
        out.append(f'<option value="{html.escape(v)}"{sel}>{html.escape(v)}</option>')
    return "".join(out)


def _esc(v) -> str:
    return "" if v is None else html.escape(str(v))


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cowork 営業支援</title>
<style>
 body{{font-family:system-ui,'Segoe UI','Hiragino Kaku Gothic ProN',sans-serif;margin:0;background:#f4f6f9;color:#1d2430}}
 header{{background:#1f2a44;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:16px}}
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
</style></head><body>
<header><h1>Cowork 営業支援</h1><a href="/">商談一覧</a><a href="/deal/new">＋新規商談</a><a href="/account/new">＋新規アカウント</a></header>
<main>{flash}{body}</main></body></html>"""


def render(body: str, flash: str = "") -> bytes:
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return PAGE.format(body=body, flash=flash_html).encode("utf-8")


def home_page(con) -> str:
    deals = sfa_db.list_deals(con, status=None)
    rows = []
    for d in deals:
        val = d.get("value_lumpsum") or d.get("value_recurring") or ""
        linked = "🔗" if d.get("theme_id") else "—"
        rows.append(
            f'<tr><td><a href="/deal/{d["id"]}">{_esc(d.get("account_name"))}</a></td>'
            f'<td>{_esc(d.get("deal_name"))}</td>'
            f'<td><span class="stage">{_esc(d.get("stage"))}</span></td>'
            f'<td>{_esc(d.get("owner"))}</td><td class="right">{_esc(val)}</td>'
            f'<td class="right" title="テーマDB連携">{linked}</td></tr>'
        )
    accounts = sfa_db.list_accounts(con)
    acc_rows = "".join(
        f'<tr><td><a href="/account/{a["id"]}">{_esc(a["name"])}</a></td>'
        f'<td>{_esc(a.get("industry"))}</td><td>{_esc(a.get("company_size"))}</td></tr>'
        for a in accounts
    )
    return f"""
    <div class="card"><h2>商談 ({len(deals)})</h2>
    <table><tr><th>アカウント</th><th>案件名</th><th>ステージ</th><th>担当</th><th class="right">金額(万円)</th><th class="right">連携</th></tr>
    {''.join(rows) or '<tr><td colspan=6 class=muted>まだ商談がありません。「＋新規商談」から追加してください。</td></tr>'}</table></div>
    <div class="card"><h2>アカウント ({len(accounts)})</h2>
    <table><tr><th>企業名</th><th>業界</th><th>企業規模</th></tr>
    {acc_rows or '<tr><td colspan=3 class=muted>まだアカウントがありません。</td></tr>'}</table></div>
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
        <div><label>企業規模</label><select name="company_size">{_opt(sfa_db.COMPANY_SIZES, acc.get('company_size'))}</select></div>
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
    activities_html = ""
    sync_btn = ""
    if deal.get("id"):
        acts = sfa_db.list_activities(con, deal["id"])
        act_rows = "".join(
            f'<tr><td>{_esc(a.get("occurred_on"))}</td><td>{_esc(a.get("type"))}</td><td>{_esc(a.get("body"))}</td></tr>'
            for a in acts
        ) or '<tr><td colspan=3 class=muted>活動なし</td></tr>'
        activities_html = f"""
        <div class="card"><h2>活動履歴</h2>
        <table><tr><th>日付</th><th>種別</th><th>内容</th></tr>{act_rows}</table>
        <form method="post" action="/activity/add" style="margin-top:12px">
          <input type="hidden" name="deal_id" value="{deal['id']}">
          <div class="grid">
            <div><label>日付</label><input type="date" name="occurred_on"></div>
            <div><label>種別</label><select name="type">{_opt(sfa_db.ACTIVITY_TYPES, '面談')}</select></div>
          </div>
          <label>内容</label><textarea name="body" rows="2"></textarea>
          <p><button class="btn sec">活動を追加</button></p>
        </form></div>"""
        sync_btn = f"""<form method="post" action="/deal/{deal['id']}/sync" style="display:inline">
          <button class="btn sync">テーマDB／ダッシュボードへ同期</button></form>
          <span class="muted">{'連携済 theme_id='+str(deal.get('theme_id')) if deal.get('theme_id') else '未連携'}</span>"""
    return f"""
    <div class="card"><h2>{'商談編集' if deal.get('id') else '新規商談'}</h2>
    <form method="post" action="/deal/save">
      <input type="hidden" name="id" value="{_esc(deal.get('id'))}">
      <div class="grid">
        <div><label>アカウント *</label><select name="account_id" required>{''.join(acc_opts)}</select></div>
        <div><label>案件名 *</label><input name="deal_name" required value="{_esc(deal.get('deal_name'))}"></div>
        <div><label>ステージ</label><select name="stage">{_opt(sfa_db.DEAL_STAGES, deal.get('stage'))}</select></div>
        <div><label>担当</label><input name="owner" value="{_esc(deal.get('owner'))}"></div>
        <div><label>事業種別L1</label><select name="business_type_l1">{_opt(sfa_db.BUSINESS_TYPE_L1, deal.get('business_type_l1'))}</select></div>
        <div><label>リード経路</label><select name="lead_pattern">{_opt(sfa_db.LEAD_PATTERNS, deal.get('lead_pattern'))}</select></div>
        <div><label>単発総額(万円)</label><input name="value_lumpsum" value="{_esc(deal.get('value_lumpsum'))}"></div>
        <div><label>継続月額(万円)</label><input name="value_recurring" value="{_esc(deal.get('value_recurring'))}"></div>
        <div><label>クライアント予算</label><input name="client_budget" value="{_esc(deal.get('client_budget'))}"></div>
        <div><label>ステータス</label><select name="status">{_opt(['open','closed'], deal.get('status') or 'open')}</select></div>
        <div><label>次回MS日</label><input type="date" name="next_milestone_date" value="{_esc(deal.get('next_milestone_date'))}"></div>
        <div><label>次回MSラベル</label><input name="next_milestone_label" value="{_esc(deal.get('next_milestone_label'))}"></div>
      </div>
      <label>現状メモ</label><textarea name="note" rows="2">{_esc(deal.get('note'))}</textarea>
      <label>ゴール</label><textarea name="goal" rows="2">{_esc(deal.get('goal'))}</textarea>
      <p><button class="btn">保存</button> <a class="btn sec" href="/">一覧へ</a> {sync_btn}</p>
    </form></div>
    {activities_html}"""


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

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            con = sfa_db.connect(db_path)
            try:
                if path == "/health":
                    self._send(b'{"status":"ok"}', ctype="application/json")
                elif path == "/":
                    self._send(render(home_page(con)))
                elif path == "/deal/new":
                    self._send(render(deal_form(con)))
                elif path == "/account/new":
                    self._send(render(account_form(con)))
                elif path.startswith("/deal/"):
                    did = int(path.split("/")[2])
                    deal = sfa_db.get_deal(con, did)
                    self._send(render(deal_form(con, deal)) if deal else render("<div class=card>商談が見つかりません</div>", ), 200 if deal else 404)
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
            try:
                f = self._form()
                if path == "/account/save":
                    sfa_db.upsert_account(
                        con, id=int(f["id"]) if f.get("id") else None,
                        name=f.get("name") or "(無名)", industry=f.get("industry") or None,
                        company_size=f.get("company_size") or None, note=f.get("note") or None)
                    self._redirect("/")
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
                        deal_name=f.get("deal_name") or "(無題)", stage=f.get("stage") or None,
                        business_type_l1=f.get("business_type_l1") or None,
                        lead_pattern=f.get("lead_pattern") or None, owner=f.get("owner") or None,
                        value_lumpsum=num("value_lumpsum"), value_recurring=num("value_recurring"),
                        client_budget=f.get("client_budget") or None,
                        next_milestone_date=f.get("next_milestone_date") or None,
                        next_milestone_label=f.get("next_milestone_label") or None,
                        note=f.get("note") or None, goal=f.get("goal") or None,
                        status=f.get("status") or "open")
                    self._redirect(f"/deal/{did}")
                elif path == "/activity/add":
                    sfa_db.add_activity(
                        con, deal_id=int(f["deal_id"]), type=f.get("type") or None,
                        occurred_on=f.get("occurred_on") or None, body=f.get("body") or None)
                    self._redirect(f"/deal/{f['deal_id']}")
                elif path.startswith("/deal/") and path.endswith("/sync"):
                    did = int(path.split("/")[2])
                    if theme_client is None:
                        self._send(render(deal_form(con, sfa_db.get_deal(con, did)),
                                          flash="同期はテーマDBトークン未設定のため無効です（.env の THEME_API_TOKEN を設定）。"))
                    else:
                        try:
                            res = theme_link.sync_deal(theme_client, con, did)
                            self._send(render(deal_form(con, sfa_db.get_deal(con, did)),
                                              flash=f"テーマDBへ同期しました（{res['action']} / theme_id={res['theme_id']}）。ダッシュボードに反映されます。"))
                        except Exception as exc:  # noqa: BLE001
                            self._send(render(deal_form(con, sfa_db.get_deal(con, did)),
                                              flash=f"同期エラー: {exc}"))
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
