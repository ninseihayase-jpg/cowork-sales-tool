"""週次営業レポートの「数字パック」計算脳。

設計の要:
- ストック指標（現ファネルの件数/金額・パイプライン・open商談数・稼働リード数）は
  DBが「現在の状態」しか持たないため、前週比を出すには週次スナップショット
  （sfa_db.weekly_snapshots）が要る。record_snapshot() を毎週呼んで蓄積する。
- フロー指標（今週の面談数・新規商談・新規リード）は activities.occurred_on /
  created_at の日付から任意の週を直接集計できるため、スナップショット不要。
  前週比も「今週分」と「先週分」を都度計算して出す。

webサービス側（DBを持つ）から呼ぶこと。Renderのcronは永続ディスクに触れない。
"""

from __future__ import annotations

from datetime import date, timedelta

from . import sfa_db

_OPEN = "(d.status='open' OR d.status IS NULL)"


def _week_bounds(as_of: date | None = None) -> tuple[str, str, str]:
    """(week_start=月曜, week_end=日曜, prev_week_start) を返す。"""
    d = as_of or date.today()
    monday = d - timedelta(days=d.weekday())
    return (monday.isoformat(),
            (monday + timedelta(days=6)).isoformat(),
            (monday - timedelta(days=7)).isoformat())


# ---- スナップショット（ストック指標） ----

def compute_snapshot_metrics(con) -> dict:
    """現時点のストック指標（前週比対象）を算出して返す。"""
    m: dict = {}
    # パイプライン（件数・金額）は「要件詰め以降」に限定（#27）。
    _pipe_ph = ", ".join("?" for _ in sfa_db.PIPELINE_STAGES)
    row = con.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(value_lumpsum),0) lump, "
        f"COALESCE(SUM(value_recurring),0) rec FROM deals d "
        f"WHERE {_OPEN} AND d.stage IN ({_pipe_ph})", list(sfa_db.PIPELINE_STAGES)).fetchone()
    m["open_deals"] = row["n"]
    m["pipeline_lump"] = row["lump"]
    m["pipeline_recurring"] = row["rec"]
    m["leads_active"] = con.execute(
        "SELECT COUNT(*) n FROM leads WHERE lead_status NOT IN ('converted','lost')").fetchone()["n"]
    # ステージ別 open商談件数
    by_stage = {r["s"]: r["n"] for r in con.execute(
        f"SELECT COALESCE(stage,'未設定') s, COUNT(*) n FROM deals d WHERE {_OPEN} GROUP BY stage")}
    for stage in sfa_db.DEAL_STAGES:
        m[f"stage_count:{stage}"] = by_stage.get(stage, 0)
    return m


def record_snapshot(con, as_of: date | None = None) -> str:
    """今週分のスナップショットをupsert（同週再実行は上書き）。week_startを返す。"""
    week_start, _, _ = _week_bounds(as_of)
    sfa_db.save_weekly_snapshot(con, week_start, compute_snapshot_metrics(con))
    return week_start


# ---- フロー指標（日付から任意週を直接集計） ----

def _flow_for_week(con, wk_start: str, wk_end: str) -> dict:
    # 面談は「同一商談(SFA#)×同一日」を1件に重複排除（同じ日に複数の活動を登録しても1面談）。
    # deal_idが無い活動は活動id単位で個別カウント（潰さない）。
    meetings = con.execute(
        "SELECT COUNT(DISTINCT CASE WHEN deal_id IS NOT NULL "
        "THEN deal_id || '|' || occurred_on ELSE 'a' || id END) n "
        "FROM activities WHERE type='面談' AND occurred_on BETWEEN ? AND ?",
        (wk_start, wk_end)).fetchone()["n"]
    companies = con.execute(
        "SELECT COUNT(DISTINCT d.account_id) n FROM activities a JOIN deals d ON d.id=a.deal_id "
        "WHERE a.type='面談' AND a.occurred_on BETWEEN ? AND ?", (wk_start, wk_end)).fetchone()["n"]
    # 新規商談＝「その週に初めて“面談”した商談」（#27）。
    # type='面談' の活動のうち最古(occurred_on最小)が週内にある商談を数える。
    # メモ/メール等の非面談活動が初回だと新規に数えない（例: 継続案件のコストデータ受領メモ）。
    new_deals = con.execute(
        "SELECT COUNT(*) n FROM ("
        "  SELECT a.deal_id, MIN(a.occurred_on) first_act FROM activities a"
        "  WHERE a.type='面談' AND a.occurred_on IS NOT NULL AND a.occurred_on != ''"
        "  GROUP BY a.deal_id HAVING first_act BETWEEN ? AND ?"
        ")", (wk_start, wk_end)).fetchone()["n"]
    # リードは活動を持たないため従来どおり created_at(登録日) 基準。
    new_leads = con.execute(
        "SELECT COUNT(*) n FROM leads WHERE substr(created_at,1,10) BETWEEN ? AND ?",
        (wk_start, wk_end)).fetchone()["n"]
    return {"meetings": meetings, "meeting_companies": companies,
            "new_deals": new_deals, "new_leads": new_leads}


# ---- 数字パック本体 ----

def compute_weekly_numbers(con, as_of: date | None = None) -> dict:
    """①〜④の②に載せる数字パックを構造化dictで返す（HTML/整形は呼び出し側）。"""
    wk_start, wk_end, prev_start = _week_bounds(as_of)
    prev_end = (date.fromisoformat(prev_start) + timedelta(days=6)).isoformat()

    flow = _flow_for_week(con, wk_start, wk_end)
    flow_prev = _flow_for_week(con, prev_start, prev_end)
    new_leads_by_source = {r["s"]: r["n"] for r in con.execute(
        "SELECT COALESCE(source,'未設定') s, COUNT(*) n FROM leads "
        "WHERE substr(created_at,1,10) BETWEEN ? AND ? GROUP BY source", (wk_start, wk_end))}
    activity_breakdown = {r["t"]: r["n"] for r in con.execute(
        "SELECT COALESCE(type,'未設定') t, COUNT(*) n FROM activities "
        "WHERE occurred_on BETWEEN ? AND ? GROUP BY type", (wk_start, wk_end))}

    # ストック（現在断面）
    funnel = [{"stage": r["s"], "count": r["n"], "lump": r["lump"], "recurring": r["rec"]}
              for r in con.execute(
        f"SELECT COALESCE(stage,'未設定') s, COUNT(*) n, COALESCE(SUM(value_lumpsum),0) lump, "
        f"COALESCE(SUM(value_recurring),0) rec FROM deals d WHERE {_OPEN} "
        f"GROUP BY stage ORDER BY n DESC")]
    _pipe_ph = ", ".join("?" for _ in sfa_db.PIPELINE_STAGES)
    stock_row = con.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(value_lumpsum),0) lump, "
        f"COALESCE(SUM(value_recurring),0) rec FROM deals d "
        f"WHERE {_OPEN} AND d.stage IN ({_pipe_ph})", list(sfa_db.PIPELINE_STAGES)).fetchone()
    closing = [dict(r) for r in con.execute(
        f"SELECT a.name AS account, d.deal_name, d.value_lumpsum AS lump, "
        f"d.next_milestone_date AS ms_date, d.owner FROM deals d "
        f"LEFT JOIN accounts a ON a.id=d.account_id "
        f"WHERE {_OPEN} AND d.stage='クロージング' ORDER BY d.next_milestone_date")]

    # コホート（展示会ファネル。lead_pattern='Exh.'の商談＝展示会由来を、新ファネル(#27)で段階分け）
    exhibition = _exhibition_funnel(con, today=(as_of or date.today()).isoformat())

    # 前週比（ストックはスナップショット差分。<2週なら未確定）
    wow = _stock_wow(con, wk_start, prev_start)

    return {
        "as_of": (as_of or date.today()).isoformat(),
        "week_start": wk_start, "week_end": wk_end, "prev_week_start": prev_start,
        "flow": {**flow, "prev": flow_prev,
                 "new_leads_by_source": new_leads_by_source,
                 "activity_breakdown": activity_breakdown},
        "stock": {"open_deals": stock_row["n"], "pipeline_lump": stock_row["lump"],
                  "pipeline_recurring": stock_row["rec"], "funnel": funnel,
                  "closing_deals": closing},
        "cohort": {"exhibition": exhibition},
        "wow": wow,
    }


def _num(v) -> str:
    """整数はカンマ区切り、floatは端数を落として整形。"""
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "0"


def _wow_ar(delta) -> str:
    """前週比を矢印付きの小さなラベルにする（.ar クラス）。0や不明は控えめに。"""
    if delta is None:
        return ""
    try:
        delta = int(round(float(delta)))
    except (TypeError, ValueError):
        return ""
    if delta > 0:
        return f'<span class="ar">▲{delta:,}</span>'
    if delta < 0:
        return f'<span class="ar">▼{abs(delta):,}</span>'
    return '<span class="ar">±0</span>'


def render_number_rail(nums: dict) -> str:
    """compute_weekly_numbers() の dict から、記事レール（数字パート）のHTML断片を生成する。
    _REPORT_ARTICLE_CSS の .rail-h / .stat / .fn クラスに一致。記事本文の <!--NUMBERS--> と差し替える。
    ※ここが「数字は自動集計・人は手打ちしない」の中核（#39）。"""
    flow = nums.get("flow", {}) or {}
    prev = flow.get("prev", {}) or {}
    stock = nums.get("stock", {}) or {}
    exh = (nums.get("cohort", {}) or {}).get("exhibition", {}) or {}
    wow = nums.get("wow", {}) or {}
    wow_ok = bool(wow.get("available"))

    def stat(k, v, unit="", ar=""):
        u = f'<span class="u">{unit}</span>' if unit else ""
        return f'<div class="stat"><div class="k">{k}</div><div class="v">{v}{u} {ar}</div></div>'

    def fn(label, val):
        return f'<div class="fn"><span>{label}</span><b>{val}</b></div>'

    def flow_d(key):
        if key in flow and key in prev:
            return _wow_ar(flow.get(key, 0) - prev.get(key, 0))
        return ""

    parts = ['<div class="rail-h">今週の動き</div>']
    parts.append(stat("面談",
                      f'{_num(flow.get("meetings", 0))}<span class="ar">/{_num(flow.get("meeting_companies", 0))}社</span>',
                      "件", flow_d("meetings")))
    parts.append(stat("新規商談", _num(flow.get("new_deals", 0)), "件", flow_d("new_deals")))

    parts.append('<div class="rail-h sub">パイプライン</div>')
    parts.append(stat("商談（要件詰め〜）", _num(stock.get("open_deals", 0)), "件",
                      _wow_ar(wow.get("open_deals")) if wow_ok else ""))
    parts.append(stat("金額（一括）", _num(stock.get("pipeline_lump", 0)), "万円",
                      _wow_ar(wow.get("pipeline_lump")) if wow_ok else ""))
    if stock.get("pipeline_recurring"):
        parts.append(stat("金額（継続/月）", _num(stock.get("pipeline_recurring", 0)), "万円",
                          _wow_ar(wow.get("pipeline_recurring")) if wow_ok else ""))

    if exh:
        _c = exh.get("counts", {}) or {}
        parts.append('<div class="rail-h sub">展示会ファネル</div>')
        parts.append(fn("総数", _num(exh.get("total", 0))))
        parts.append(fn("初回面談待ち", _num(_c.get("waiting", 0))))
        parts.append(fn("不成立（面談なし）", _num(_c.get("no_deal", 0))))
        parts.append(fn("1次どまり（終了）", _num(_c.get("first_closed", 0))))
        parts.append(fn("2次以降→提案", _num(_c.get("proposal", 0))))
        parts.append(fn("2次以降→クロージング", _num(_c.get("closing", 0))))
        parts.append(fn("2次以降→受注", _num(_c.get("won", 0))))
        parts.append(fn("2次以降→失注", _num(_c.get("lost", 0))))

    funnel = stock.get("funnel") or []
    if funnel:
        parts.append('<div class="rail-h sub">ステージ別（進行中）</div>')
        wow_funnel = wow.get("funnel", {}) if wow_ok else {}
        for row in funnel:
            stg = row.get("stage", "未設定")
            ar = _wow_ar(wow_funnel.get(stg)) if stg in wow_funnel else ""
            parts.append(fn(stg, f'{_num(row.get("count", 0))} {ar}'.strip()))

    if not wow_ok:
        parts.append('<div class="fn" style="border:0;color:var(--faint)">'
                     '<span>※前週比はスナップショット2週分から表示</span><b></b></div>')
    return "".join(parts)


def exhibition_deal_rows(con) -> list[dict]:
    """展示会由来(lead_pattern='Exh.')商談＋面談回数(同一日=1)＋開発案件有無＋status/stage/理由/次回MS。
    ファネル分類と監査ドリルダウンの共通ソース。
    面談回数は『日付(occurred_on)のある面談』のみを同一日=1で数える（日付なしはカウントしない）。"""
    rows = con.execute(
        "SELECT d.id, d.deal_name, d.stage, d.status, d.close_reason, d.next_milestone_date, "
        "acc.name acc, "
        "(SELECT COUNT(DISTINCT a.occurred_on) FROM activities a "
        "   WHERE a.deal_id=d.id AND a.type='面談' "
        "     AND a.occurred_on IS NOT NULL AND a.occurred_on != '') mtg, "
        "EXISTS(SELECT 1 FROM dev_projects dp WHERE dp.deal_id=d.id) has_dev "
        "FROM deals d LEFT JOIN accounts acc ON acc.id=d.account_id "
        "WHERE d.lead_pattern='Exh.' ORDER BY mtg DESC, d.id").fetchall()
    return [dict(r) for r in rows]


# 新ファネルのバケット定義（#27でユーザー確定）。ファネルの進行順（浅い→深い→成果）で並べる。
# この順が監査ドリルダウンの並び順にもなる。
EXH_BUCKETS = [
    ("waiting", "① 初回面談待ち（面談0・進行中）"),
    ("no_deal", "② 不成立（面談0・要検証）"),
    ("first_open", "③ 1次面談どまり・継続中"),
    ("first_closed", "④ 1次面談どまり・終了（理由別）"),
    ("second_open_other", "⑤ 2次以降・進行中（要件詰め等）"),
    ("proposal", "⑥ 2次以降・提案フェーズ"),
    ("closing", "⑦ 2次以降・クロージング"),
    ("won", "⑧ 2次以降・受注"),
    ("lost", "⑨ 2次以降・失注"),
    ("second_closed_other", "⑩ 2次以降・終了（失注以外）"),
]


def classify_exhibition_deal(row: dict, today: str) -> str:
    """展示会商談を新ファネルのバケットkeyに分類。today は 'YYYY-MM-DD'（JST基準を渡す）。"""
    mtg = row.get("mtg") or 0
    status = row.get("status") or "open"
    stage = row.get("stage") or ""
    cr = row.get("close_reason") or ""
    nms = row.get("next_milestone_date") or ""
    closed = (status == "closed")
    if mtg == 0:
        # 面談0回: 次回MSが当日以降(これから面談予定)＝初回面談待ち、それ以外＝不成立(要検証)。
        # ※当日ちょうどのMS(これから面談)も待ちに含める（>=）。過去日/未設定/クローズは不成立。
        if not closed and nms and nms >= today:
            return "waiting"
        return "no_deal"
    if mtg == 1:
        return "first_closed" if closed else "first_open"
    # mtg >= 2（2次面談以降）
    if stage == "受注":
        return "won"
    if closed:
        return "lost" if cr == "失注" else "second_closed_other"
    if stage == "クロージング":
        return "closing"
    if stage == "提案":
        return "proposal"
    return "second_open_other"


def _exhibition_funnel(con, today: str | None = None) -> dict:
    """展示会由来商談の新ファネル（#27）。バケット別件数＋補助内訳を返す。
    旧キー(total/valid_total/first_meeting/second_meeting/won)も後方互換で維持。"""
    today = today or date.today().isoformat()
    rows = exhibition_deal_rows(con)
    counts = {k: 0 for k, _ in EXH_BUCKETS}
    first_closed_by_reason: dict = {}
    dev_yes = dev_no = 0
    for r in rows:
        b = classify_exhibition_deal(r, today)
        counts[b] += 1
        if b == "first_closed":
            cr = r.get("close_reason") or "（理由未設定）"
            first_closed_by_reason[cr] = first_closed_by_reason.get(cr, 0) + 1
        if (r.get("mtg") or 0) >= 2:
            if r.get("has_dev"):
                dev_yes += 1
            else:
                dev_no += 1
    total = len(rows)
    first_meeting = sum(1 for r in rows if (r.get("mtg") or 0) >= 1)
    second_meeting = sum(1 for r in rows if (r.get("mtg") or 0) >= 2)
    won = counts["won"]
    return {
        "total": total,
        "counts": counts,
        "first_closed_by_reason": first_closed_by_reason,
        "dev_yes": dev_yes, "dev_no": dev_no,
        # 後方互換
        "valid_total": total - counts["no_deal"],
        "first_meeting": first_meeting, "second_meeting": second_meeting, "won": won,
    }


def _stock_wow(con, week_start: str, prev_start: str) -> dict:
    """ストック指標の前週比。先週スナップショットが無ければ available=False。"""
    cur = sfa_db.get_weekly_snapshot(con, week_start)
    prev = sfa_db.get_weekly_snapshot(con, prev_start)
    if not prev:
        return {"available": False}
    keys = ("open_deals", "pipeline_lump", "pipeline_recurring", "leads_active")
    delta = {k: (cur.get(k) or 0) - (prev.get(k) or 0) for k in keys if k in cur or k in prev}
    funnel_delta = {}
    for stage in sfa_db.DEAL_STAGES:
        k = f"stage_count:{stage}"
        if k in cur or k in prev:
            funnel_delta[stage] = (cur.get(k) or 0) - (prev.get(k) or 0)
    return {"available": True, "prev_week_start": prev_start, **delta, "funnel": funnel_delta}
