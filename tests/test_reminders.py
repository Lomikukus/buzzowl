"""reminders.py — recurrence advance, next-occurrence roll-forward, snooze parsing."""

from datetime import date, datetime, timedelta, timezone

import pytest

import reminders as rm


def test_validate_recurrence():
    assert rm.validate_recurrence(None) is None
    assert rm.validate_recurrence("") is None
    assert rm.validate_recurrence("Weekly") == "weekly"
    with pytest.raises(rm.ReminderError):
        rm.validate_recurrence("fortnightly")


def test_advance_daily_weekly_monthly():
    assert rm.advance(date(2026, 8, 17), "daily") == date(2026, 8, 18)
    assert rm.advance(date(2026, 8, 17), "weekly") == date(2026, 8, 24)
    assert rm.advance(date(2026, 8, 17), "monthly") == date(2026, 9, 17)
    # month-end clamps
    assert rm.advance(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert rm.advance(date(2026, 12, 15), "monthly") == date(2027, 1, 15)


def test_next_due_keeps_weekday_and_rolls_forward():
    today = date(2026, 8, 19)  # Wednesday
    # weekly Monday task, completed on time → next Monday
    assert rm.next_due(date(2026, 8, 17), "weekly", today) == date(2026, 8, 24)
    # weekly Monday task, 3 weeks overdue → first Monday after today, not 3 stale ones
    assert rm.next_due(date(2026, 7, 27), "weekly", today) == date(2026, 8, 24)
    # daily task with no due date → tomorrow
    assert rm.next_due(None, "daily", today) == date(2026, 8, 20)
    with pytest.raises(rm.ReminderError):
        rm.next_due(date(2026, 8, 17), None, today)


NOW = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)  # a Monday


@pytest.mark.parametrize("spec,expected", [
    ("1d", NOW + timedelta(days=1)),
    ("3d", NOW + timedelta(days=3)),
    ("2w", NOW + timedelta(weeks=2)),
    ("4h", NOW + timedelta(hours=4)),
    ("tomorrow", datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)),
    ("next_week", datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)),
    ("2026-09-01", datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)),
    ("2026-09-01T14:30:00Z", datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)),
    (None, None), ("", None),
])
def test_parse_snooze(spec, expected):
    assert rm.parse_snooze(spec, NOW) == expected


def test_parse_snooze_rejects_garbage():
    with pytest.raises(rm.ReminderError):
        rm.parse_snooze("later", NOW)
    with pytest.raises(rm.ReminderError):
        rm.parse_snooze("0d", NOW)


def test_is_snoozed():
    assert rm.is_snoozed(NOW + timedelta(hours=1), NOW) is True
    assert rm.is_snoozed(NOW - timedelta(hours=1), NOW) is False
    assert rm.is_snoozed(None, NOW) is False
