"""#64 §5.4: scripts/calendar_crosscheck_backfill_report.py の回帰テスト。

実際のGoogle Calendar APIは呼ばず、フェイクのカレンダークライアントを注入して
build_backfill_report / resolve_date_range / daterange の純粋なロジックだけを検証する。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def backfill_mod():
    spec = importlib.util.spec_from_file_location(
        "calendar_crosscheck_backfill_report", ROOT / "scripts" / "calendar_crosscheck_backfill_report.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakeCalendarClient:
    """(email, date) -> events のフェイク（バックフィルは複数日にまたがるため日付ごとに変える）。"""

    def __init__(self, events_by_key: dict):
        self._events = events_by_key

    def list_events_for_date(self, email: str, target_date: date):
        return self._events.get((email, target_date), [])


def _ev(wc_mod, summary, attendees, day, *, all_day=False):
    start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=JST)
    end = datetime(day.year, day.month, day.day, 11, 0, tzinfo=JST)
    return wc_mod.CalendarEvent(id="x", summary=summary, start=start, end=end,
                               all_day=all_day, attendees=attendees)


def test_daterange_is_inclusive(backfill_mod):
    days = list(backfill_mod.daterange(date(2026, 8, 1), date(2026, 8, 3)))
    assert days == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


def test_build_backfill_report_groups_by_owner_and_tags_events(backfill_mod):
    from cowork import workspace_calendar as wc
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    events = {
        ("hayase@inproc.org", d1): [_ev(wc, "A社商談", ["hayase@inproc.org", "tanaka@acme.co.jp"], d1)],
        ("hayase@inproc.org", d2): [_ev(wc, "社内定例", ["hayase@inproc.org", "iwasaki@inproc.org"], d2)],
        ("iwasaki@inproc.org", d1): [],
        ("iwasaki@inproc.org", d2): [],
    }
    client = _FakeCalendarClient(events)
    owner_map = {"早瀬": "hayase@inproc.org", "岩崎": "iwasaki@inproc.org"}
    backfill_mod.CALENDAR_CROSSCHECK_OWNERS = ["早瀬", "岩崎"]
    report = backfill_mod.build_backfill_report(client, owner_map, d1, d2)
    assert "▼ 早瀬" in report and "▼ 岩崎" in report
    assert "A社商談" in report and "社内定例" in report
    assert "🌐外部候補" in report
    assert "全2件中 外部候補1件" in report


def test_build_backfill_report_notes_missing_owner_map_entry(backfill_mod):
    d1 = date(2026, 8, 1)
    client = _FakeCalendarClient({})
    backfill_mod.CALENDAR_CROSSCHECK_OWNERS = ["土屋"]
    report = backfill_mod.build_backfill_report(client, {}, d1, d1)
    assert "owner_slack_map.json未登録" in report
