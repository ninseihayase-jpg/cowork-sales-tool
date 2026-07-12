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
    row = con.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(value_lumpsum),0) lump, "
        f"COALESCE(SUM(value_recurring),0) rec FROM deals d WHERE {_OPEN}").fetchone()
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
    meetings = con.execute(
        "SELECT COUNT(*) n FROM activities WHERE type='面談' AND occurred_on BETWEEN ? AND ?",
        (wk_start, wk_end)).fetchone()["n"]
    companies = con.execute(
        "SELECT COUNT(DISTINCT d.account_id) n FROM activities a JOIN deals d ON d.id=a.deal_id "
        "WHERE a.type='面談' AND a.occurred_on BETWEEN ? AND ?", (wk_start, wk_end)).fetchone()["n"]
    new_deals = con.execute(
        "SELECT COUNT(*) n FROM deals WHERE substr(created_at,1,10) BETWEEN ? AND ?",
        (wk_start, wk_end)).fetchone()["n"]
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
    stock_row = con.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(value_lumpsum),0) lump, "
        f"COALESCE(SUM(value_recurring),0) rec FROM deals d WHERE {_OPEN}").fetchone()
    closing = [dict(r) for r in con.execute(
        f"SELECT a.name AS account, d.deal_name, d.value_lumpsum AS lump, "
        f"d.next_milestone_date AS ms_date, d.owner FROM deals d "
        f"LEFT JOIN accounts a ON a.id=d.account_id "
        f"WHERE {_OPEN} AND d.stage='クロージング' ORDER BY d.next_milestone_date")]

    # コホート（展示会ファネル。lead_pattern='Exh.'の商談＝展示会由来を、面談回数で段階分け）
    exhibition = _exhibition_funnel(con)

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


def _exhibition_funnel(con) -> dict:
    """展示会由来(lead_pattern='Exh.')商談の面談回数ベースのファネル。
    - leads: 展示会由来の商談総数
    - first_meeting: 面談を1回以上実施
    - second_meeting: 面談を2回以上実施（＝次の商談に進んだ）
    - won: 受注ステージ
    キャンセル率・ニーズなしは手元集計/終了理由タグ(#19)とマージする前提（ここでは扱わない）。
    """
    total = con.execute(
        "SELECT COUNT(*) n FROM deals WHERE lead_pattern='Exh.'").fetchone()["n"]
    mtg_counts = {r["deal_id"]: r["c"] for r in con.execute(
        "SELECT a.deal_id, COUNT(*) c FROM activities a JOIN deals d ON d.id=a.deal_id "
        "WHERE d.lead_pattern='Exh.' AND a.type='面談' GROUP BY a.deal_id")}
    first = sum(1 for c in mtg_counts.values() if c >= 1)
    second = sum(1 for c in mtg_counts.values() if c >= 2)
    won = con.execute(
        "SELECT COUNT(*) n FROM deals WHERE lead_pattern='Exh.' AND stage='受注'").fetchone()["n"]
    return {"total": total, "first_meeting": first, "second_meeting": second, "won": won}


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
