"""#64: scripts/daily_appt_slack_notify.py のカレンダー突合ロジック（順方向/逆方向）の回帰テスト。

実際のGoogle Calendar API・Slack APIは呼ばず、フェイクのカレンダークライアントを注入して
純粋なロジック（check_owner_has_external_meeting / find_unmatched_calendar_meetings）だけを検証する。
scripts/ はパッケージ化されていないため importlib でモジュールをロードする。
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
def notify_mod():
    spec = importlib.util.spec_from_file_location(
        "daily_appt_slack_notify", ROOT / "scripts" / "daily_appt_slack_notify.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakeCalendarClient:
    def __init__(self, events_by_email: dict):
        self._events_by_email = events_by_email

    def list_events_for_date(self, email: str, target_date: date):
        return self._events_by_email.get(email, [])


def _ev(wc_mod, summary, attendees, *, all_day=False):
    start = datetime(2026, 8, 17, 10, 0, tzinfo=JST)
    end = datetime(2026, 8, 17, 11, 0, tzinfo=JST)
    return wc_mod.CalendarEvent(id="x", summary=summary, start=start, end=end,
                               all_day=all_day, attendees=attendees)


def test_check_owner_has_external_meeting_true(notify_mod):
    from cowork import workspace_calendar as wc
    events = {"hayase@inproc.org": [_ev(wc, "A社定例", ["hayase@inproc.org", "tanaka@acme.co.jp"])]}
    client = _FakeCalendarClient(events)
    found = notify_mod.check_owner_has_external_meeting(client, "hayase@inproc.org", date(2026, 8, 17))
    assert found is True


def test_check_owner_has_external_meeting_false_when_only_internal(notify_mod):
    from cowork import workspace_calendar as wc
    events = {"hayase@inproc.org": [_ev(wc, "社内定例", ["hayase@inproc.org", "iwasaki@inproc.org"])]}
    client = _FakeCalendarClient(events)
    found = notify_mod.check_owner_has_external_meeting(client, "hayase@inproc.org", date(2026, 8, 17))
    assert found is False


def test_find_unmatched_calendar_meetings_flags_meeting_without_sfa_deal(notify_mod):
    from cowork import workspace_calendar as wc
    events = {
        "hayase@inproc.org": [_ev(wc, "B社キックオフ", ["hayase@inproc.org", "sato@bcorp.co.jp"])],
        "iwasaki@inproc.org": [],
    }
    client = _FakeCalendarClient(events)
    owner_map = {"早瀬": "hayase@inproc.org", "岩崎": "iwasaki@inproc.org"}
    deals = []  # SFAには何も登録されていない
    unmatched = notify_mod.find_unmatched_calendar_meetings(
        client, owner_map, deals, date(2026, 8, 17), "2026-08-17")
    assert len(unmatched) == 1
    assert unmatched[0]["owner"] == "早瀬"
    assert "B社キックオフ" in unmatched[0]["title"]


def test_find_unmatched_calendar_meetings_none_when_sfa_deal_covers_it(notify_mod):
    from cowork import workspace_calendar as wc
    events = {"hayase@inproc.org": [_ev(wc, "B社キックオフ", ["hayase@inproc.org", "sato@bcorp.co.jp"])]}
    client = _FakeCalendarClient(events)
    owner_map = {"早瀬": "hayase@inproc.org"}
    deals = [{"owner": "早瀬", "next_milestone_date": "2026-08-17", "next_milestone_type": "アポ"}]
    unmatched = notify_mod.find_unmatched_calendar_meetings(
        client, owner_map, deals, date(2026, 8, 17), "2026-08-17")
    assert unmatched == []


def test_find_unmatched_calendar_meetings_ignores_internal_only_events(notify_mod):
    from cowork import workspace_calendar as wc
    events = {"hayase@inproc.org": [_ev(wc, "社内MTG", ["hayase@inproc.org", "iwasaki@inproc.org"])]}
    client = _FakeCalendarClient(events)
    owner_map = {"早瀬": "hayase@inproc.org"}
    deals = []
    unmatched = notify_mod.find_unmatched_calendar_meetings(
        client, owner_map, deals, date(2026, 8, 17), "2026-08-17")
    assert unmatched == []
