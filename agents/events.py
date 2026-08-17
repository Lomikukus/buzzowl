"""
agents/events.py — Async broadcast event bus for agent activity.

Any component calls emit(event) to push a JSON event to connected /ws/agents
WebSocket clients. Multi-tenant (Phase 6a): every subscriber is registered with
its org_id; an event that carries "org_id" is delivered only to that org's
dashboards. Events without org_id are treated as system-wide and go to
everyone — emitters must set org_id whenever the payload names clients,
subjects, findings or queues.
"""
import json
import logging
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)
_subscribers: dict = {}   # WebSocket -> org_id (None = unknown/legacy)


def subscribe(ws: WebSocket, org_id: Optional[int] = None) -> None:
    _subscribers[ws] = org_id


def unsubscribe(ws: WebSocket) -> None:
    _subscribers.pop(ws, None)


async def emit(event: dict) -> None:
    """Broadcast event JSON to the dashboard sockets of event['org_id'] (or all
    sockets when the event has no org_id)."""
    if not _subscribers:
        return
    target_org = event.get("org_id")
    msg = json.dumps(event, default=str)
    dead = []
    for ws, org in list(_subscribers.items()):
        if target_org is not None and org is not None and org != target_org:
            continue
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _subscribers.pop(ws, None)
