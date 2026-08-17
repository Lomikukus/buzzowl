"""
agents/events.py — Async broadcast event bus for agent activity.

Any component calls emit(event) to push a JSON event to all connected
/ws/agents WebSocket clients.  subscribe/unsubscribe manage the connection set.
"""
import json
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)
_subscribers: set[WebSocket] = set()


def subscribe(ws: WebSocket) -> None:
    _subscribers.add(ws)


def unsubscribe(ws: WebSocket) -> None:
    _subscribers.discard(ws)


async def emit(event: dict) -> None:
    """Broadcast event JSON to all connected dashboard WebSocket clients."""
    if not _subscribers:
        return
    msg = json.dumps(event, default=str)
    dead: set[WebSocket] = set()
    for ws in _subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _subscribers.discard(ws)
