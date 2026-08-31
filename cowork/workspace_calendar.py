"""Google Calendar 突合（#64）。ドメイン全体の委任（domain-wide delegation）で
Google Workspace内の各メンバーになりすまし、予定のタイトル・参加者を読む薄いクライアント。

Hisho側 `google_calendar.py` は早瀬個人のOAuthリフレッシュトークン1本で動く「個人秘書」
実装であり、他メンバーの予定は読めない。本モジュールは別用途（複数メンバー横断の外部会議
検知）のため、独立したサービスアカウント認証を使う。セットアップ手順は
docs/calendar-crosscheck/02_Googleカレンダー委任セットアップ手順.md を参照。

2026-08-31追記（#101マイルストン2）: ドメイン全体の委任（Workspace管理者の設定が必要・#64のP2）
より前に、まず早瀬個人のカレンダーだけ「今日明日のタスク」画面に重ねたいという要望のため、
`list_events_for_date_shared`（委任を使わず、カレンダー所有者本人がこのサービスアカウントへ
自分のカレンダーを共有するだけで動く簡易版）を追加した。同じ`GOOGLE_CALENDAR_SA_JSON`の
サービスアカウントを流用できる（Calendar APIが有効なプロジェクトであれば新規作成不要）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# 休暇・不在表記など、外部会議候補から明確に除外したいタイトルの簡易フィルタ。
_EXCLUDE_TITLE_RE = re.compile(r"(休|有給|OOO|out\s*of\s*office)", re.IGNORECASE)


@dataclass
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    attendees: list[str]


def _parse_event(raw: dict, tz: ZoneInfo) -> CalendarEvent:
    start_obj = raw.get("start", {}) or {}
    end_obj = raw.get("end", {}) or {}
    all_day = "date" in start_obj
    if all_day:
        start = datetime.fromisoformat(start_obj["date"]).replace(tzinfo=tz)
        end = datetime.fromisoformat(end_obj["date"]).replace(tzinfo=tz)
    else:
        start = datetime.fromisoformat(start_obj["dateTime"]).astimezone(tz)
        end = datetime.fromisoformat(end_obj["dateTime"]).astimezone(tz)
    attendees = [a.get("email", "") for a in raw.get("attendees", []) if a.get("email")]
    return CalendarEvent(id=raw.get("id", ""), summary=raw.get("summary") or "(無題)",
                         start=start, end=end, all_day=all_day, attendees=attendees)


def is_external_meeting(event: CalendarEvent, *, self_email: str, own_domain: str = "inproc.org") -> bool:
    """#64: 「外部との商談」候補と推定できる予定か判定する（有無チェックのみ・v1）。

    終日予定・自分のみの予定・休暇系タイトルは除外。参加者に自社ドメイン以外の
    メールが1件でも含まれれば外部会議候補とみなす。
    """
    if event.all_day:
        return False
    if _EXCLUDE_TITLE_RE.search(event.summary or ""):
        return False
    others = [a for a in event.attendees if a.lower() != (self_email or "").lower()]
    if not others:
        return False
    domain_suffix = "@" + own_domain.lower().lstrip("@")
    return any(not a.lower().endswith(domain_suffix) for a in others)


def load_service_account_info(sa_json: str) -> dict:
    """`GOOGLE_CALENDAR_SA_JSON` はファイルパス or JSON文字列のどちらでも受け付ける
    （scripts/export_deals_to_sheets.pyのGOOGLE_SERVICE_ACCOUNT_JSONと同じ二方式対応）。"""
    p = Path(sa_json)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError:
        fixed = re.sub(
            r'("private_key"\s*:\s*")(.*?)(")',
            lambda m: m.group(1) + m.group(2).replace("\n", "\\n") + m.group(3),
            sa_json, flags=re.DOTALL)
        info = json.loads(fixed)
    info["private_key"] = (info.get("private_key") or "").replace("\\n", "\n")
    return info


class WorkspaceCalendarClient:
    """ドメイン全体の委任で各メンバーのカレンダーを読む薄いクライアント（#64）。"""

    def __init__(self, sa_info: dict, tz: ZoneInfo):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        self._base_creds = Credentials.from_service_account_info(sa_info, scopes=CALENDAR_SCOPES)
        self._build = build
        self.tz = tz

    def list_events_for_date(self, user_email: str, target_date: date_cls) -> list[CalendarEvent]:
        """指定メンバー(user_email)になりすまし、指定日の予定を取得する。
        ドメイン全体の委任(with_subject)が必要（#64のP2セットアップ済みが前提）。"""
        start = datetime.combine(target_date, time(0, 0), self.tz)
        end = start + timedelta(days=1)
        creds = self._base_creds.with_subject(user_email)
        service = self._build("calendar", "v3", credentials=creds, cache_discovery=False)
        result = service.events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime").execute()
        return [_parse_event(e, self.tz) for e in result.get("items", [])]

    def list_events_for_date_shared(self, calendar_id: str, target_date: date_cls) -> list[CalendarEvent]:
        """委任(with_subject)を使わない簡易版（#101マイルストン2、2026-08-31）。
        カレンダー所有者本人が、このサービスアカウントのメールアドレス（sa_info["client_email"]）
        へ自分のカレンダーを「予定の詳細をすべて表示」権限で共有するだけで動く——
        Google Workspace管理者によるドメイン全体の委任（#64のP2）は不要。
        calendar_idは通常、共有した本人のGoogleアカウントのメールアドレス。"""
        start = datetime.combine(target_date, time(0, 0), self.tz)
        end = start + timedelta(days=1)
        service = self._build("calendar", "v3", credentials=self._base_creds, cache_discovery=False)
        result = service.events().list(
            calendarId=calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime").execute()
        return [_parse_event(e, self.tz) for e in result.get("items", [])]
