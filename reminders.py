"""
reminders.py — recurrence + snooze rules for user_tasks (Phase 4).

DB/HTTP-free so the router, the daily reminder heartbeat and the tests share
one definition of "when is the next occurrence" and "what does 'snooze 3d'
mean".
"""

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

RECURRENCES = ("daily", "weekly", "monthly")

_SNOOZE_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.I)


class ReminderError(ValueError):
    pass


def validate_recurrence(value) -> Optional[str]:
    """None/'' → None; otherwise one of RECURRENCES (case-insensitive)."""
    if value in (None, "", "none", "None"):
        return None
    v = str(value).strip().lower()
    if v not in RECURRENCES:
        raise ReminderError(f"recurrence must be one of {', '.join(RECURRENCES)} (or empty)")
    return v


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def advance(d: date, recurrence: str) -> date:
    if recurrence == "daily":
        return d + timedelta(days=1)
    if recurrence == "weekly":
        return d + timedelta(weeks=1)
    if recurrence == "monthly":
        return _add_months(d, 1)
    raise ReminderError(f"unknown recurrence {recurrence!r}")


def next_due(due: Optional[date], recurrence: str, today: Optional[date] = None) -> date:
    """Next occurrence after completing an instance.

    Advances from the instance's due date (not from today) so a weekly Monday
    task stays on Mondays, but rolls forward until the result is after today so
    a long-overdue instance doesn't spawn a chain of already-overdue ones.
    """
    recurrence = validate_recurrence(recurrence)
    if recurrence is None:
        raise ReminderError("task has no recurrence")
    today = today or date.today()
    nxt = advance(due or today, recurrence)
    guard = 0
    while nxt <= today and guard < 10_000:
        nxt = advance(nxt, recurrence)
        guard += 1
    return nxt


def parse_snooze(spec, now: Optional[datetime] = None) -> Optional[datetime]:
    """'1d' / '3d' / '2w' / '4h' / 'tomorrow' / 'next_week' / ISO datetime|date → aware UTC datetime.
    None/'' → None (clear snooze)."""
    if spec in (None, "", "none", False):
        return None
    now = now or datetime.now(timezone.utc)
    if isinstance(spec, datetime):
        return spec if spec.tzinfo else spec.replace(tzinfo=timezone.utc)
    if isinstance(spec, date):
        return datetime(spec.year, spec.month, spec.day, 8, 0, tzinfo=timezone.utc)
    s = str(spec).strip().lower()
    if s == "tomorrow":
        d = (now + timedelta(days=1)).date()
        return datetime(d.year, d.month, d.day, 8, 0, tzinfo=timezone.utc)
    if s in ("next_week", "next week", "nextweek"):
        d = now.date() + timedelta(days=(7 - now.weekday()) % 7 or 7)  # next Monday
        return datetime(d.year, d.month, d.day, 8, 0, tzinfo=timezone.utc)
    m = _SNOOZE_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            raise ReminderError("snooze amount must be positive")
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return now + delta
    # ISO datetime or date
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, 8, 0, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(str(spec).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ReminderError("snooze must be like '1d', '2w', 'tomorrow', 'next_week' or an ISO date/datetime")


def is_snoozed(snooze_until, now: Optional[datetime] = None) -> bool:
    if not snooze_until:
        return False
    now = now or datetime.now(timezone.utc)
    if snooze_until.tzinfo is None:
        snooze_until = snooze_until.replace(tzinfo=timezone.utc)
    return snooze_until > now
