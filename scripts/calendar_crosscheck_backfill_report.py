"""#64 §5.4: 直近2週間の打ち合わせを一括テスト判定し、「癖」を読み解くための一回限りの分析スクリプト。

日次のshadowモード（1日ずつ蓄積）を待たずに、過去分をまとめて`is_external_meeting`で
判定した結果を出力する。Slackには投稿せず、標準出力にレポートを出すだけ
（`python scripts/calendar_crosscheck_backfill_report.py > report.txt` で保存して読む想定）。

対象者ごとにまとめて出力するため、「この人はいつも参加者を招待しない」「この人は終日予定で
入れる」といった個人差のあるパターンを、その人の2週間分を通しで見て発見しやすくしてある。

実行:
  GOOGLE_CALENDAR_SA_JSON=path/to/calendar-sa.json python scripts/calendar_crosscheck_backfill_report.py
  BACKFILL_DAYS=7 python scripts/calendar_crosscheck_backfill_report.py       # 直近7日分に変更
  BACKFILL_START=2026-08-01 BACKFILL_END=2026-08-14 python scripts/calendar_crosscheck_backfill_report.py

環境変数:
  GOOGLE_CALENDAR_SA_JSON     - （必須）ドメイン全体委任サービスアカウントの鍵JSON（ファイルパス or JSON文字列）
  GOOGLE_WORKSPACE_DOMAIN     - 自社ドメイン（既定 inproc.org）
  CALENDAR_CROSSCHECK_OWNERS  - 対象メンバー（既定 吉江,中島,岩崎,早瀬,高橋,土屋）
  BACKFILL_DAYS               - 過去何日分を見るか（既定14。今日は未確定なので含めない＝昨日まで）
  BACKFILL_START / BACKFILL_END - 明示的な期間指定（YYYY-MM-DD、両方指定時はBACKFILL_DAYSより優先）
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date as _date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

GOOGLE_CALENDAR_SA_JSON = os.environ.get("GOOGLE_CALENDAR_SA_JSON", "").strip()
GOOGLE_WORKSPACE_DOMAIN = os.environ.get("GOOGLE_WORKSPACE_DOMAIN", "inproc.org").strip()
CALENDAR_CROSSCHECK_OWNERS = [s.strip() for s in
                             os.environ.get("CALENDAR_CROSSCHECK_OWNERS", "吉江,中島,岩崎,早瀬,高橋,土屋").split(",")
                             if s.strip()]
OWNER_MAP_PATH = ROOT / "config" / "owner_slack_map.json"


def load_owner_map() -> dict:
    if OWNER_MAP_PATH.exists():
        with open(OWNER_MAP_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def build_calendar_client():
    from cowork import workspace_calendar as wc
    sa_info = wc.load_service_account_info(GOOGLE_CALENDAR_SA_JSON)
    return wc.WorkspaceCalendarClient(sa_info, JST)


def resolve_date_range() -> tuple[_date_cls, _date_cls]:
    start_s = os.environ.get("BACKFILL_START", "").strip()
    end_s = os.environ.get("BACKFILL_END", "").strip()
    if start_s and end_s:
        return (datetime.strptime(start_s, "%Y-%m-%d").date(),
                datetime.strptime(end_s, "%Y-%m-%d").date())
    days = int(os.environ.get("BACKFILL_DAYS", "14") or 14)
    today = datetime.now(JST).date()
    end = today - timedelta(days=1)  # 今日は未確定なので昨日まで
    start = end - timedelta(days=days - 1)
    return (start, end)


def daterange(start: _date_cls, end: _date_cls):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


_EXCLUDE_TITLE_RE = re.compile(r"(休|有給|OOO|out\s*of\s*office)", re.IGNORECASE)


def build_backfill_report(calendar_client, owner_map: dict, start: _date_cls, end: _date_cls) -> str:
    """対象メンバーごとに、期間内の全予定を判定タグ付きで通しで列挙するレポートを作る。"""
    from cowork import workspace_calendar as wc
    lines = [f"=== カレンダー突合 バックフィル分析（{start.isoformat()} 〜 {end.isoformat()}）===",
             "※このレポートはSlackには投稿されません。標準出力のみです。\n"]

    for owner in CALENDAR_CROSSCHECK_OWNERS:
        email = owner_map.get(owner)
        lines.append(f"\n{'='*60}\n▼ {owner}（{email or '※owner_slack_map.json未登録'}）\n{'='*60}")
        if not email:
            continue
        total, external = 0, 0
        for d in daterange(start, end):
            try:
                events = calendar_client.list_events_for_date(email, d)
            except Exception as e:  # noqa: BLE001
                lines.append(f"[{d.isoformat()}] 取得失敗: {e}")
                continue
            if not events:
                continue
            lines.append(f"\n[{d.isoformat()}]")
            for e in events:
                total += 1
                is_ext = wc.is_external_meeting(e, self_email=email, own_domain=GOOGLE_WORKSPACE_DOMAIN)
                if is_ext:
                    external += 1
                tag = "🌐外部候補" if is_ext else "・対象外"
                when = "終日" if e.all_day else e.start.strftime("%H:%M")
                attendees = "、".join(e.attendees) or "(参加者なし)"
                lines.append(f"   [{tag}] {when} {e.summary}  参加者: {attendees}")
        lines.append(f"\n--- {owner} 集計: 全{total}件中 外部候補{external}件 ---")

    return "\n".join(lines)


def main():
    if not GOOGLE_CALENDAR_SA_JSON:
        print("[ERROR] GOOGLE_CALENDAR_SA_JSON が設定されていません。")
        sys.exit(1)

    start, end = resolve_date_range()
    print(f"[INFO] 対象期間: {start.isoformat()} 〜 {end.isoformat()}", file=sys.stderr)
    print(f"[INFO] 対象メンバー: {', '.join(CALENDAR_CROSSCHECK_OWNERS)}", file=sys.stderr)

    calendar_client = build_calendar_client()
    owner_map = {k: v for k, v in load_owner_map().items() if k in CALENDAR_CROSSCHECK_OWNERS}
    missing = [o for o in CALENDAR_CROSSCHECK_OWNERS if o not in owner_map]
    if missing:
        print(f"[WARN] owner_slack_map.jsonに未登録のメンバー: {', '.join(missing)}", file=sys.stderr)

    report = build_backfill_report(calendar_client, owner_map, start, end)
    print(report)


if __name__ == "__main__":
    main()
