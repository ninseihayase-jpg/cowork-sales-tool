"""#64: カレンダー↔SFA突合の判定ロジック（is_external_meeting）の回帰テスト。

実際のGoogle Calendar API呼び出しは行わず、CalendarEventを直接組み立てて
判定ロジックのみを検証する。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from cowork.workspace_calendar import CalendarEvent, is_external_meeting

JST = ZoneInfo("Asia/Tokyo")


def _ev(summary, attendees, *, all_day=False):
    start = datetime(2026, 8, 17, 10, 0, tzinfo=JST)
    end = datetime(2026, 8, 17, 11, 0, tzinfo=JST)
    return CalendarEvent(id="x", summary=summary, start=start, end=end,
                         all_day=all_day, attendees=attendees)


def test_external_attendee_is_detected():
    ev = _ev("A社定例", ["hayase@inproc.org", "tanaka@acme.co.jp"])
    assert is_external_meeting(ev, self_email="hayase@inproc.org")


def test_internal_only_meeting_is_not_external():
    ev = _ev("社内定例", ["hayase@inproc.org", "iwasaki@inproc.org"])
    assert not is_external_meeting(ev, self_email="hayase@inproc.org")


def test_solo_event_is_excluded():
    ev = _ev("作業ブロック", ["hayase@inproc.org"])
    assert not is_external_meeting(ev, self_email="hayase@inproc.org")


def test_no_attendees_is_excluded():
    ev = _ev("リマインダー", [])
    assert not is_external_meeting(ev, self_email="hayase@inproc.org")


def test_all_day_event_is_excluded():
    ev = _ev("A社定例", ["hayase@inproc.org", "tanaka@acme.co.jp"], all_day=True)
    assert not is_external_meeting(ev, self_email="hayase@inproc.org")


def test_vacation_title_is_excluded_even_with_external_attendee():
    ev = _ev("有給休暇", ["hayase@inproc.org", "tanaka@acme.co.jp"])
    assert not is_external_meeting(ev, self_email="hayase@inproc.org")


def test_custom_domain_param():
    ev = _ev("パートナー定例", ["hayase@example.com", "partner@example.com"])
    assert not is_external_meeting(ev, self_email="hayase@example.com", own_domain="example.com")
    ev2 = _ev("パートナー定例", ["hayase@example.com", "partner@other.co.jp"])
    assert is_external_meeting(ev2, self_email="hayase@example.com", own_domain="example.com")
