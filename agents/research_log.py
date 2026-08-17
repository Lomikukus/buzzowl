"""
agents/research_log.py — In-memory ring buffer for research pipeline events.

Stores the last MAX_ENTRIES structured log entries, queryable via
GET /api/research/log. Also emits to the WebSocket feed.
"""

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

MAX_ENTRIES = 500

_lock = threading.Lock()
_entries: deque = deque(maxlen=MAX_ENTRIES)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def push(level: str, component: str, subject: str, message: str, detail: Optional[str] = None) -> None:
    entry = {
        "ts": _now(),
        "level": level,           # info | warn | error | debug
        "component": component,   # delegator | orchestrator | worker | aggregator | runner
        "subject": subject,
        "message": message,
        "detail": detail,
    }
    with _lock:
        _entries.append(entry)


def get_recent(n: int = 100, subject: Optional[str] = None) -> list[dict]:
    with _lock:
        entries = list(_entries)
    if subject:
        entries = [e for e in entries if e["subject"].lower() == subject.lower()]
    return entries[-n:]


def info(component: str, subject: str, message: str, detail: Optional[str] = None) -> None:
    push("info", component, subject, message, detail)


def warn(component: str, subject: str, message: str, detail: Optional[str] = None) -> None:
    push("warn", component, subject, message, detail)


def error(component: str, subject: str, message: str, detail: Optional[str] = None) -> None:
    push("error", component, subject, message, detail)
