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

from datetime import date, datetime, timedelta, timezone

from . import sfa_db

_OPEN = "(d.status='open' OR d.status IS NULL)"

_JST = timezone(timedelta(hours=9))


def _today_jst() -> date:
    """今日(JST)。KPIの『今日』基準はJSTで統一する。"""
    return datetime.now(_JST).date()


def _pdate(s):
    """ISO 'YYYY-MM-DD'（先頭10字）を date に。空/不正は None。"""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


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
    # 経営KPI（A/C/G）の前週比対象を保存。C rateはpp差分、A件数/金額・G件数/月額は件数差分で見る。
    kpi = compute_kpi_pack(con)
    a, c, g = kpi["A"], kpi["C"], kpi["G"]
    m["pos_pipe"] = a["nPipe"]
    m["pos_won"] = a["nWon"]
    m["pos_lost"] = a["nLost"]
    m["pos_cost"] = a["nCost"]
    m["pipeline_annual"] = a["pipeline_annual"]
    m["cq_budget_rate"] = c["budget"]["rate"]
    m["cq_ms_rate"] = c["ms"]["rate"]
    m["cq_close30_rate"] = c["close30"]["rate"]
    m["cq_mtgcap_rate"] = c["mtgcap"]["rate"]
    m["delivery_active_count"] = g["active_count"]
    m["delivery_recurring"] = g["recurring"]
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

    # 経営KPI（A/C/F/G。SFA自身のDBから自前計算）。todayはJST基準（as_of優先）。
    kpi = compute_kpi_pack(con, today=(as_of or _today_jst()))

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
        "kpi": kpi,
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


def _wow_pp(delta) -> str:
    """達成率など『ポイント差(pp)』の前週比ラベル（.ar クラス）。"""
    if delta is None:
        return ""
    try:
        d = round(float(delta), 1)
    except (TypeError, ValueError):
        return ""
    if d > 0:
        return f'<span class="ar">▲{d:g}pt</span>'
    if d < 0:
        return f'<span class="ar">▼{abs(d):g}pt</span>'
    return '<span class="ar">±0pt</span>'


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
        # 全11バケットを進行順(EXH_BUCKETS)で出す。7つに間引くと各項目の合計が総数と合わない
        # ため、必ず全バケットを表示して「総数＝各項目の合計」が成立するようにする。
        for _k, _ in EXH_BUCKETS:
            parts.append(fn(_EXH_RAIL_LABELS.get(_k, _k), _num(_c.get(_k, 0))))

    funnel = stock.get("funnel") or []
    if funnel:
        wow_funnel = wow.get("funnel", {}) if wow_ok else {}
        _by_stage = {row.get("stage", "未設定"): row.get("count", 0) for row in funnel}
        _won_open = _by_stage.get("受注", 0)  # 受注は「進行中」と分けて別枠で提示
        parts.append('<div class="rail-h sub">ステージ別（進行中）</div>')
        # 進行順（初回アポ実施→要件詰め→提案→クロージング→保留中）で表示。受注は除外。
        _shown = set()
        for stg in sfa_db.DEAL_STAGES:
            if stg == "受注" or stg not in _by_stage:
                continue
            ar = _wow_ar(wow_funnel.get(stg)) if stg in wow_funnel else ""
            parts.append(fn(stg, f'{_num(_by_stage[stg])} {ar}'.strip()))
            _shown.add(stg)
        # DEAL_STAGESに無いステージ（表記ゆれ・レガシー）は末尾に※付きで（データ整備の気づき用）
        for stg, cnt in _by_stage.items():
            if stg == "受注" or stg in _shown:
                continue
            parts.append(fn(f"{stg}※(要確認)", _num(cnt)))
        # 受注（クローズされずopenに残っている分）は進行中と別枠で提示
        if _won_open:
            parts.append('<div class="rail-h sub">受注（進行中に残存）</div>')
            parts.append(fn("受注", f'{_num(_won_open)}'
                             + (f' {_wow_ar(wow_funnel.get("受注"))}' if "受注" in wow_funnel else '')))

    # ---- 経営KPI（A/C/F/G。SFA自身のDBから自前計算） ----
    kpi = nums.get("kpi", {}) or {}
    if kpi:
        A = kpi.get("A", {}) or {}
        parts.append('<div class="rail-h sub">会社全体の商談ポジション</div>')
        parts.append(stat("パイプライン（進行中）", _num(A.get("nPipe", 0)), "件",
                          _wow_ar(wow.get("pos_pipe")) if wow_ok else ""))
        parts.append(stat("受注（累計）", _num(A.get("nWon", 0)), "件",
                          _wow_ar(wow.get("pos_won")) if wow_ok else ""))
        parts.append(stat("失注", _num(A.get("nLost", 0)), "件",
                          _wow_ar(wow.get("pos_lost")) if wow_ok else ""))
        parts.append(stat("コスト削減（進行中）", _num(A.get("nCost", 0)), "件",
                          _wow_ar(wow.get("pos_cost")) if wow_ok else ""))
        parts.append(stat("パイプライン金額（年額換算）", _num(A.get("pipeline_annual", 0)), "万円",
                          _wow_ar(wow.get("pipeline_annual")) if wow_ok else ""))

        C = kpi.get("C", {}) or {}

        def cq(label, d, wowkey):
            extra = f'<span class="ar">（未達{d.get("miss", 0)}/{d.get("denom", 0)}）</span>'
            ar = (_wow_pp(wow.get(wowkey)) if wow_ok else "") + extra
            return stat(label, f'{d.get("rate", 0):g}', "%", ar)

        parts.append('<div class="rail-h sub">プロセス品質（達成率）</div>')
        parts.append(cq("予算把握率", C.get("budget", {}) or {}, "cq_budget_rate"))
        parts.append(cq("面談→次回MS 1週内", C.get("ms", {}) or {}, "cq_ms_rate"))
        parts.append(cq("初回面談→1ヶ月内クローズ", C.get("close30", {}) or {}, "cq_close30_rate"))
        parts.append(cq("ステージ別 面談回数上限内", C.get("mtgcap", {}) or {}, "cq_mtgcap_rate"))

        F = kpi.get("F", {}) or {}
        parts.append('<div class="rail-h sub">アウトカム転換率（母数小・参考）</div>')
        parts.append(fn(f'初回面談→提案（n={_num(F.get("reachedFM", 0))}）', f'{F.get("cv1", 0):g}%'))
        parts.append(fn(f'提案→受注（n={_num(F.get("reachedProp", 0))}）', f'{F.get("cv2", 0):g}%'))
        parts.append('<div class="fn" style="border:0;color:var(--faint)">'
                     '<span>※母数が小さいため参考値（到達ベース）</span><b></b></div>')

        G = kpi.get("G", {}) or {}
        parts.append('<div class="rail-h sub">デリバリー（進行中）</div>')
        parts.append(stat("進行中案件", _num(G.get("active_count", 0)), "件",
                          _wow_ar(wow.get("delivery_active_count")) if wow_ok else ""))
        parts.append(stat("継続月額合計", _num(G.get("recurring", 0)), "万円/月",
                          _wow_ar(wow.get("delivery_recurring")) if wow_ok else ""))

    if not wow_ok:
        parts.append('<div class="fn" style="border:0;color:var(--faint)">'
                     '<span>※前週比はスナップショット2週分から表示</span><b></b></div>')
    return "".join(parts)


def exhibition_deal_rows(con, all_deals: bool = False) -> list[dict]:
    """展示会由来(lead_pattern='Exh.')商談＋面談回数(同一日=1)＋開発案件有無＋status/stage/理由/次回MS。
    ファネル分類と監査ドリルダウンの共通ソース。重要度/種別L1/L2/経路(lead_pattern)も併せて返す。
    面談回数は『日付(occurred_on)のある面談』のみを同一日=1で数える（日付なしはカウントしない）。
    all_deals=True で lead_pattern を問わず全商談(open+closed)を対象にする（全案件ファネル用）。"""
    _where = "" if all_deals else "WHERE d.lead_pattern='Exh.'"
    rows = con.execute(
        "SELECT d.id, d.deal_name, d.stage, d.status, d.close_reason, d.next_milestone_date, "
        "d.next_milestone_type, d.exhibition_name, d.importance, d.business_type_l1, d.business_type_l2, "
        "d.lead_pattern, d.value_lumpsum, d.value_recurring, acc.name acc, "
        "(SELECT COUNT(DISTINCT a.occurred_on) FROM activities a "
        "   WHERE a.deal_id=d.id AND a.type='面談' "
        "     AND a.occurred_on IS NOT NULL AND a.occurred_on != '') mtg, "
        "EXISTS(SELECT 1 FROM dev_projects dp WHERE dp.deal_id=d.id) has_dev "
        "FROM deals d LEFT JOIN accounts acc ON acc.id=d.account_id "
        f"{_where} ORDER BY mtg DESC, d.id").fetchall()
    return [dict(r) for r in rows]


# 新ファネルのバケット定義（#27でユーザー確定）。ファネルの進行順（浅い→深い→成果）で並べる。
# この順が監査ドリルダウンの並び順にもなる。
# ⑤以降(2次面談アポ取得以降)はステージ管理のみで分類する（#27でユーザー確定）。
EXH_BUCKETS = [
    ("waiting", "① 初回面談待ち（面談0・進行中）"),
    ("no_deal", "② 不成立（面談0・要検証）"),
    ("first_open", "③ 1次面談どまり・継続中"),
    ("first_closed", "④ 1次面談どまり・終了（理由別）"),
    ("req", "⑤ 2次以降・要件詰め"),
    ("proposal", "⑥ 2次以降・提案"),
    ("closing", "⑦ 2次以降・クロージング"),
    ("won", "⑧ 2次以降・受注"),
    ("lost", "⑨ 失注（面談回数を問わない）"),
    ("adv_ended", "⑩ 2次以降・終了（失注以外）"),
    ("review", "⑪ 要検証（2次以降だがステージが初回アポ実施/保留/未設定）"),
]

# 記事レール(render_number_rail)用の短縮ラベル。EXH_BUCKETS と同一キー・同一順で全11件を出す。
_EXH_RAIL_LABELS = {
    "waiting": "① 初回面談待ち",
    "no_deal": "② 不成立（面談0）",
    "first_open": "③ 1次どまり・継続",
    "first_closed": "④ 1次どまり・終了",
    "req": "⑤ 2次〜要件詰め",
    "proposal": "⑥ 2次〜提案",
    "closing": "⑦ 2次〜クロージング",
    "won": "⑧ 2次〜受注",
    "lost": "⑨ 失注",
    "adv_ended": "⑩ 2次〜終了(失注以外)",
    "review": "⑪ 要検証",
}


def classify_exhibition_deal(row: dict, today: str) -> str:
    """展示会商談を新ファネルのバケットkeyに分類。today は 'YYYY-MM-DD'（JST基準を渡す）。"""
    mtg = row.get("mtg") or 0
    status = row.get("status") or "open"
    stage = row.get("stage") or ""
    cr = row.get("close_reason") or ""
    nms = row.get("next_milestone_date") or ""
    nms_type = row.get("next_milestone_type") or ""
    closed = (status == "closed")
    # 受注は面談回数に関わらず最優先で「受注」に分類する。
    # （面談を活動履歴に残さず受注したケースを②不成立/④どまりへ取りこぼさないため。実事故あり）
    if stage == "受注":
        return "won"
    # 失注も面談回数を問わず「失注」に統合する（ユーザー確定）。
    # 面談0/1回の失注が②不成立・④1次どまりに散らばるのを防ぎ、失注は⑨に一本化する。
    if closed and cr == "失注":
        return "lost"
    if mtg == 0:
        # 面談0回: 次回MSが当日以降(これから面談予定)＝初回面談待ち、それ以外＝不成立(要検証)。
        # ※当日ちょうどのMS(これから面談)も待ちに含める（>=）。過去日/未設定/クローズは不成立。
        if not closed and nms and nms >= today:
            return "waiting"
        return "no_deal"
    # 「2次面談アポ取得以降」の進捗判定（#27）:
    #   面談2回以上実施済み、または 1次実施済み＆次回MSが『アポ』(=2次商談)で当日以降。
    progressed = (mtg >= 2) or (mtg == 1 and not closed and nms_type == "アポ" and nms and nms >= today)
    if not progressed:
        # 面談1回で、2次アポ未取得（次回MSがアポでない/過去/未設定）＝1次面談どまり。
        return "first_closed" if closed else "first_open"
    # ⑤以降はステージ管理のみで分類。基本は要件詰め以降のはず。
    if stage == "受注":
        return "won"
    if closed:
        return "lost" if cr == "失注" else "adv_ended"
    if stage == "クロージング":
        return "closing"
    if stage == "提案":
        return "proposal"
    if stage == "要件詰め":
        return "req"
    # 進捗しているのにステージが初回アポ実施/保留中/未設定のまま＝要検証。
    return "review"


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


# 理由ブレイクダウンの対象バケット（②不成立 / ④1次どまり終了 / ⑩2次以降終了）。
EXH_BREAKDOWN_BUCKETS = ("no_deal", "first_closed", "adv_ended")


def exhibition_funnel_export_rows(con, today: str | None = None, exhibition: str | None = None) -> list[dict]:
    """展示会ファネルを『区分→内訳(理由)』のカスケード構造でエクスポート用に返す（Excel転記用）。
    各行 = {"kubun": <区分ラベル or ''>, "reason": <内訳ラベル or ''>, "count": int}。
    kubunが埋まる行=区分の小計、reasonが埋まる行=その区分の理由内訳（1列右にずらす）。
    exhibition: None=全展示会 / '__none__'=展示会名未設定 / それ以外=その展示会名のみ。"""
    today = today or date.today().isoformat()
    rows = exhibition_deal_rows(con)
    if exhibition is not None:
        if exhibition == "__none__":
            rows = [r for r in rows if not (r.get("exhibition_name") or "")]
        elif exhibition != "__all__":
            rows = [r for r in rows if (r.get("exhibition_name") or "") == exhibition]
    by_bucket: dict = {k: [] for k, _ in EXH_BUCKETS}
    for r in rows:
        by_bucket[classify_exhibition_deal(r, today)].append(r)
    out: list = []
    for k, lbl in EXH_BUCKETS:
        grp = by_bucket.get(k, [])
        out.append({"kubun": lbl, "reason": "", "count": len(grp)})
        if k in EXH_BREAKDOWN_BUCKETS and grp:
            byr: dict = {}
            for r in grp:
                if (r.get("status") or "open") == "closed":
                    cr = r.get("close_reason") or "（理由未設定）"
                else:
                    cr = "（未クローズ）"
                byr[cr] = byr.get(cr, 0) + 1
            for cr in sorted(byr, key=lambda x: (-byr[x], x)):
                out.append({"kubun": "", "reason": cr, "count": byr[cr]})
    out.append({"kubun": "合計", "reason": "", "count": len(rows)})
    return out


def exhibition_funnel_matrix(con, today: str | None = None) -> dict:
    """展示会ファネルを『縦=区分→内訳(理由)のカスケード × 横=展示会』のマトリクスで返す（Excel転記用）。
    戻り値: {"exhibitions": [展示会名...（＋未設定）], "rows": [{kubun, reason, by_exh:{展示会:件数}, total}...]}。"""
    today = today or date.today().isoformat()
    data = exhibition_deal_rows(con)
    names = sfa_db.list_exhibition_names(con)
    has_unset = any(not (r.get("exhibition_name") or "") for r in data)
    exh_cols = list(names) + (["（未設定）"] if has_unset else [])

    def _exh_of(r):
        return (r.get("exhibition_name") or "") or "（未設定）"

    def _counts(items):
        d: dict = {}
        for r in items:
            e = _exh_of(r)
            d[e] = d.get(e, 0) + 1
        return d

    by_bucket: dict = {k: [] for k, _ in EXH_BUCKETS}
    for r in data:
        by_bucket[classify_exhibition_deal(r, today)].append(r)
    out_rows: list = []
    for k, lbl in EXH_BUCKETS:
        grp = by_bucket.get(k, [])
        out_rows.append({"kubun": lbl, "reason": "", "by_exh": _counts(grp), "total": len(grp)})
        if k in EXH_BREAKDOWN_BUCKETS and grp:
            by_reason: dict = {}
            for r in grp:
                cr = (r.get("close_reason") or "（理由未設定）") if (r.get("status") or "open") == "closed" else "（未クローズ）"
                by_reason.setdefault(cr, []).append(r)
            for cr in sorted(by_reason, key=lambda x: (-len(by_reason[x]), x)):
                items = by_reason[cr]
                out_rows.append({"kubun": "", "reason": cr, "by_exh": _counts(items), "total": len(items)})
    out_rows.append({"kubun": "合計", "reason": "", "by_exh": _counts(data), "total": len(data)})
    return {"exhibitions": exh_cols, "rows": out_rows}


def funnel_cvs(rows: list[dict], today: str) -> dict:
    """ファネル各段の到達件数とCV（到達ベース）を返す。rows は exhibition_deal_rows 形式。
      total  … 母集団件数
      first  … 初回面談到達（面談1回以上）
      second … 2次到達（面談2回以上）
      won    … 受注（bucket='won'）
      cv_first  = first/total   （初回面談到達率）
      cv_second = second/first  （初回→2次到達率）
      cv_won    = won/second    （2次→受注率）"""
    total = len(rows)
    first = sum(1 for r in rows if (r.get("mtg") or 0) >= 1)
    second = sum(1 for r in rows if (r.get("mtg") or 0) >= 2)
    won = sum(1 for r in rows if classify_exhibition_deal(r, today) == "won")

    def _pct(n, d):
        return round(n / d * 100, 1) if d else 0.0

    return {"total": total, "first": first, "second": second, "won": won,
            "cv_first": _pct(first, total), "cv_second": _pct(second, first),
            "cv_won": _pct(won, second)}


def all_funnel_matrix(con, today: str | None = None) -> dict:
    """全案件ファネル（展示会以外も含む全商談 open+closed）を『縦=区分→内訳(理由)のカスケード ×
    横=経路(lead_pattern)』のマトリクスで返す。展示会ファネルと同じ11区分で分類し、経路別に集計する。
    戻り値: {"routes":[経路...（＋未設定）], "rows":[{kubun,reason,by_route,total}...],
             "cvs":{"__all__":{...}, 経路:{...}}}（cvs は funnel_cvs 形式）。"""
    today = today or date.today().isoformat()
    data = exhibition_deal_rows(con, all_deals=True)

    def _route_of(r):
        return (r.get("lead_pattern") or "") or "（未設定）"

    present = {_route_of(r) for r in data}
    routes = [p for p in sfa_db.LEAD_PATTERNS if p in present]
    if "（未設定）" in present:
        routes.append("（未設定）")
    for p in sorted(present):   # マスタ外(レガシー)の経路値も末尾に拾う
        if p not in routes:
            routes.append(p)

    def _counts(items):
        d: dict = {}
        for r in items:
            e = _route_of(r)
            d[e] = d.get(e, 0) + 1
        return d

    by_bucket: dict = {k: [] for k, _ in EXH_BUCKETS}
    for r in data:
        by_bucket[classify_exhibition_deal(r, today)].append(r)
    out_rows: list = []
    for k, lbl in EXH_BUCKETS:
        grp = by_bucket.get(k, [])
        out_rows.append({"kubun": lbl, "reason": "", "by_route": _counts(grp), "total": len(grp)})
        if k in EXH_BREAKDOWN_BUCKETS and grp:
            by_reason: dict = {}
            for r in grp:
                cr = (r.get("close_reason") or "（理由未設定）") if (r.get("status") or "open") == "closed" else "（未クローズ）"
                by_reason.setdefault(cr, []).append(r)
            for cr in sorted(by_reason, key=lambda x: (-len(by_reason[x]), x)):
                items = by_reason[cr]
                out_rows.append({"kubun": "", "reason": cr, "by_route": _counts(items), "total": len(items)})
    out_rows.append({"kubun": "合計", "reason": "", "by_route": _counts(data), "total": len(data)})
    cvs = {"__all__": funnel_cvs(data, today)}
    for rt in routes:
        cvs[rt] = funnel_cvs([r for r in data if _route_of(r) == rt], today)
    return {"routes": routes, "rows": out_rows, "cvs": cvs}


def _stock_wow(con, week_start: str, prev_start: str) -> dict:
    """ストック指標の前週比。先週スナップショットが無ければ available=False。"""
    cur = sfa_db.get_weekly_snapshot(con, week_start)
    prev = sfa_db.get_weekly_snapshot(con, prev_start)
    if not prev:
        return {"available": False}
    keys = ("open_deals", "pipeline_lump", "pipeline_recurring", "leads_active",
            # 経営KPI: A(件数/金額)・C(rate=pp差分)・G(件数/月額)
            "pos_pipe", "pos_won", "pos_lost", "pos_cost", "pipeline_annual",
            "cq_budget_rate", "cq_ms_rate", "cq_close30_rate", "cq_mtgcap_rate",
            "delivery_active_count", "delivery_recurring")
    delta = {k: (cur.get(k) or 0) - (prev.get(k) or 0) for k in keys if k in cur or k in prev}
    funnel_delta = {}
    for stage in sfa_db.DEAL_STAGES:
        k = f"stage_count:{stage}"
        if k in cur or k in prev:
            funnel_delta[stage] = (cur.get(k) or 0) - (prev.get(k) or 0)
    return {"available": True, "prev_week_start": prev_start, **delta, "funnel": funnel_delta}


# ---- 経営KPI（Hishoダッシュボード相当をSFA自身のDBから自前計算） ----
# Hishoへ読みに行かない。元データはSFAの商談（＝Hishoと同じデータ）なのでSFA単独で自前計算する。
# 共通定義（厳守）:
#   open   = status='open' OR status IS NULL ／ closed = status='closed'
#   sales  = business_type_l1 != 'コスト削減'（コスト削減案件は別勘定）
#   受注   = stage='受注' の全件（open/closedを問わない。勝ち案件は完了クローズしても受注として数える。Hisho側と一致）
#   失注   = closed AND close_reason='失注'（ステージ「失注」は#67で撤廃済み）
#   面談   = activities.type='面談' AND occurred_on が非空
#   firstMeeting=最小occurred_on / lastMeeting=最大occurred_on / meetingCount=面談件数
_CQ_BUDGET_STAGES = ("初回アポ実施", "要件詰め", "提案", "クロージング", "保留中")
_CQ_MTG_CAPS = {"初回アポ実施": 1, "要件詰め": 3, "提案": 4, "クロージング": 5, "保留中": 5}
_PROPOSAL_REACHED_STAGES = ("提案", "クロージング", "受注")


def _is_open(r) -> bool:
    s = r.get("status")
    return s is None or s == "open"


def _is_closed(r) -> bool:
    return r.get("status") == "closed"


def _is_cost(r) -> bool:
    return (r.get("business_type_l1") or "") == "コスト削減"


def _is_sales(r) -> bool:
    return not _is_cost(r)


def _is_won(r) -> bool:
    return (r.get("stage") or "") == "受注"


def _is_lost(r) -> bool:
    return _is_closed(r) and (r.get("close_reason") or "") == "失注"


def _rate(denom: int, miss: int) -> float:
    """達成率(%) = (母数−未達)/母数*100。母数0は0扱い。"""
    return 0.0 if denom <= 0 else round((denom - miss) / denom * 100, 1)


def _kpi_deal_rows(con) -> list[dict]:
    """全商談に面談集計(meeting_count / first_meeting / last_meeting)を付けて返す（A/C/F共通ソース）。
    面談＝activities.type='面談' かつ occurred_on が非空。meeting_count は面談“件数”。"""
    rows = con.execute(
        "SELECT d.id, d.deal_name, d.stage, d.status, d.close_reason, d.business_type_l1, "
        "d.business_type_l2, d.importance, d.cost_stage, "
        "d.client_budget, d.next_milestone_date, d.value_lumpsum, d.value_recurring, "
        "acc.name AS account, "
        "(SELECT COUNT(*) FROM activities a WHERE a.deal_id=d.id AND a.type='面談' "
        "  AND a.occurred_on IS NOT NULL AND a.occurred_on!='') AS meeting_count, "
        "(SELECT MIN(a.occurred_on) FROM activities a WHERE a.deal_id=d.id AND a.type='面談' "
        "  AND a.occurred_on IS NOT NULL AND a.occurred_on!='') AS first_meeting, "
        "(SELECT MAX(a.occurred_on) FROM activities a WHERE a.deal_id=d.id AND a.type='面談' "
        "  AND a.occurred_on IS NOT NULL AND a.occurred_on!='') AS last_meeting "
        "FROM deals d LEFT JOIN accounts acc ON acc.id=d.account_id").fetchall()
    return [dict(r) for r in rows]


def _kpi_position(rows: list[dict]) -> dict:
    """A. 商談ポジション内訳（ストック）。sales=L1≠'コスト削減'。"""
    a_rows = []
    n_pipe = n_won = n_lost = n_cost = 0
    pipe_annual = 0.0
    for r in rows:
        bucket = None
        if _is_sales(r):
            if _is_won(r):                     # 受注=stage='受注'（全status）
                bucket = "won"; n_won += 1
            elif _is_open(r):                  # sales & open & stage≠受注
                bucket = "pipe"; n_pipe += 1
                # パイプライン金額（万・年額換算）= Σ(単発 + 継続月額×12)
                pipe_annual += (r.get("value_lumpsum") or 0) + (r.get("value_recurring") or 0) * 12
            elif _is_lost(r):                  # closed & close_reason='失注'
                bucket = "lost"; n_lost += 1
        elif _is_open(r) or _is_won(r) or (r.get("cost_stage") or "") == "成果確定":
            # コスト削減: 進行中に加え、勝ち(stage=受注/成果確定)はクローズ後も残す
            # （SFAで受注完了クローズしても消えないように。Hisho側の集計と一致させる）
            bucket = "cost"; n_cost += 1
        if bucket:
            rr = dict(r); rr["_bucket"] = bucket; a_rows.append(rr)
    return {"nPipe": n_pipe, "nWon": n_won, "nLost": n_lost, "nCost": n_cost,
            "nTotal": n_pipe + n_won + n_lost + n_cost,
            "pipeline_annual": round(pipe_annual), "rows": a_rows}


def _kpi_process_quality(rows: list[dict], today: date) -> dict:
    """C. プロセス品質 達成率4本。各KPIは rate/denom/miss（②③は実績平均avg）と母数行rowsを返す。"""
    # ① 予算把握率
    c1 = []
    for r in rows:
        if _is_sales(r) and _is_open(r) and (r.get("stage") or "") in _CQ_BUDGET_STAGES:
            cb = (r.get("client_budget") or "").strip()
            miss = (cb == "" or cb == "未確認")
            rr = dict(r); rr["_miss"] = miss
            rr["_detail"] = "予算未把握" if miss else cb
            c1.append(rr)
    c1_miss = sum(1 for r in c1 if r["_miss"])
    budget = {"rate": _rate(len(c1), c1_miss), "denom": len(c1), "miss": c1_miss, "rows": c1}

    # ② 面談後→次回MS 1週以内
    c2 = []
    gap_sum = 0.0; gap_n = 0
    for r in rows:
        if not (_is_sales(r) and _is_open(r) and (r.get("stage") or "") != "受注"):
            continue
        lm = _pdate(r.get("last_meeting"))
        if lm is None:                          # 直近面談が無い商談は母数外
            continue
        nms = _pdate(r.get("next_milestone_date"))
        if nms is None:
            miss = True; detail = "次回MS未設定"
        else:
            gap = (nms - lm).days
            gap_sum += gap; gap_n += 1           # 実績平均は next_milestone のある母数で
            miss = (nms < today) or (gap > 7)
            detail = f"面談{r.get('last_meeting')}→MS{r.get('next_milestone_date')}（{gap}日{'・過去' if nms < today else ''}）"
        rr = dict(r); rr["_miss"] = miss; rr["_detail"] = detail
        c2.append(rr)
    c2_miss = sum(1 for r in c2 if r["_miss"])
    ms = {"rate": _rate(len(c2), c2_miss), "denom": len(c2), "miss": c2_miss,
          "avg": (round(gap_sum / gap_n, 1) if gap_n else None), "rows": c2}

    # ③ 初回面談→1ヶ月以内close（結果KPI）
    c3 = []
    el_sum = 0.0; el_n = 0
    for r in rows:
        if not (_is_sales(r) and (r.get("meeting_count") or 0) >= 1):
            continue
        fm = _pdate(r.get("first_meeting"))
        inflight = _is_open(r) and (r.get("stage") or "") != "受注"
        if inflight and fm is not None:
            elapsed = (today - fm).days
            el_sum += elapsed; el_n += 1         # 実績平均は open(進行中)母数で
            miss = elapsed > 30
            detail = f"初回面談{r.get('first_meeting')}から{elapsed}日・未クローズ"
        else:
            miss = False
            detail = "受注" if _is_won(r) else ("クローズ済" if _is_closed(r) else "進行中")
        rr = dict(r); rr["_miss"] = miss; rr["_detail"] = detail
        c3.append(rr)
    c3_miss = sum(1 for r in c3 if r["_miss"])
    close30 = {"rate": _rate(len(c3), c3_miss), "denom": len(c3), "miss": c3_miss,
               "avg": (round(el_sum / el_n, 1) if el_n else None), "rows": c3}

    # ④ ステージ別 面談回数上限
    c4 = []
    for r in rows:
        stg = r.get("stage") or ""
        if not (_is_sales(r) and _is_open(r) and stg in _CQ_MTG_CAPS):
            continue
        cap = _CQ_MTG_CAPS[stg]
        mc = r.get("meeting_count") or 0
        miss = mc > cap
        rr = dict(r); rr["_miss"] = miss; rr["_detail"] = f"面談{mc}回 / 上限{cap}回"
        c4.append(rr)
    c4_miss = sum(1 for r in c4 if r["_miss"])
    mtgcap = {"rate": _rate(len(c4), c4_miss), "denom": len(c4), "miss": c4_miss, "rows": c4}

    return {"budget": budget, "ms": ms, "close30": close30, "mtgcap": mtgcap}


def _kpi_outcome(rows: list[dict]) -> dict:
    """F. アウトカム転換率（到達ベース・母数小の参考値）。sales(全status)対象。"""
    f_rows = []
    reached_fm = reached_prop = won = 0
    for r in rows:
        if not _is_sales(r):
            continue
        fm = (r.get("meeting_count") or 0) >= 1
        prop = (r.get("stage") or "") in _PROPOSAL_REACHED_STAGES
        w = _is_won(r)
        if fm: reached_fm += 1
        if prop: reached_prop += 1
        if w: won += 1
        rr = dict(r); rr["_fm"] = fm; rr["_prop"] = prop; rr["_won"] = w
        f_rows.append(rr)
    cv1 = round(reached_prop / reached_fm * 100, 1) if reached_fm else 0.0
    cv2 = round(won / reached_prop * 100, 1) if reached_prop else 0.0
    return {"reachedFM": reached_fm, "reachedProp": reached_prop, "won": won,
            "cv1": cv1, "cv2": cv2, "rows": f_rows}


def _kpi_delivery(con) -> dict:
    """G. デリバリー（進行中・ストック）。
    active = deliveries.status='進行中'（未設定なら紐づくdealがopen）。
    金額（継続月額/単発総額）は商談(deal_id)単位で1回だけ計上する（1商談を複数deliveryに
    分割しても按分せず二重計上しない）。active_count は active deliveryの deal_id ユニーク数。"""
    out_rows = []
    seen = set()
    recurring = lumpsum = 0.0
    for dv in sfa_db.list_deliveries(con):
        st = (dv.get("status") or "").strip()
        if st == "進行中":
            active = True
        elif st == "":
            ds = dv.get("deal_status")
            active = (ds is None or ds == "open")
        else:
            active = False
        if not active:
            continue
        did = dv.get("deal_id")
        first = did not in seen
        rr = dict(dv); rr["_counts_amount"] = first
        if first:
            seen.add(did)
            deal = sfa_db.get_deal(con, did) or {}
            rr["_recurring"] = deal.get("value_recurring") or 0
            rr["_lumpsum"] = deal.get("value_lumpsum") or 0
            recurring += rr["_recurring"]; lumpsum += rr["_lumpsum"]
        else:
            rr["_recurring"] = 0; rr["_lumpsum"] = 0
        out_rows.append(rr)
    return {"active_count": len(seen), "recurring": round(recurring),
            "lumpsum": round(lumpsum), "rows": out_rows}


def compute_kpi_pack(con, today: date | None = None) -> dict:
    """経営KPI A/C/F/G を SFA自身のDBから自前計算して構造化dictで返す（HTML化は呼び出し側）。
    todayはJST基準の date を渡す（省略時 _today_jst）。監査ページ用に母数行/判定フラグも含める。"""
    today = today or _today_jst()
    rows = _kpi_deal_rows(con)
    return {
        "A": _kpi_position(rows),
        "C": _kpi_process_quality(rows, today),
        "F": _kpi_outcome(rows),
        "G": _kpi_delivery(con),
    }
