"""
routers/notifications.py — Notification management endpoints.

  POST /api/notifications/test    — send a test message to Telegram
  GET  /api/notifications/status  — check if Telegram is configured
  POST /api/notifications/digest  — manually trigger weekly digest
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

import notifications as _notify
from context import DB_AVAILABLE, config, db_module
from routers.auth import current_user

router = APIRouter()
logger = logging.getLogger("whisper.notifications_router")


@router.post("/api/notifications/test")
async def send_test(user: dict = Depends(current_user)):
    """Send a test message to the CURRENT USER's linked Telegram chat."""
    ok = await _notify.notify_user(user["id"], f"✅ *Buzzowl connected*\nNotifications reach you here, "
                                              f"{user.get('display_name') or user.get('username')}.", "admin")
    return {"ok": ok, "configured": _notify._configured(), "linked": ok or bool(await _linked(user["id"]))}


async def _linked(user_id: int):
    u = await db_module.get_user_with_settings(user_id) if DB_AVAILABLE else None
    return (u or {}).get("settings", {}).get("telegram") if u else None


@router.get("/api/notifications/status")
async def get_status(user: dict = Depends(current_user)):
    """Bot configured? Is THIS user linked? Their preferences."""
    u = await db_module.get_user_with_settings(user["id"]) if DB_AVAILABLE else None
    settings = (u or {}).get("settings") or {}
    tg = settings.get("telegram") or {}
    return {
        "configured": _notify._configured(),
        "bot_username": _notify.bot_username() if _notify._configured() else None,
        "channel": "telegram" if _notify._configured() else None,
        "linked": bool(tg.get("chat_id")),
        "telegram": {k: tg.get(k) for k in ("username", "first_name", "linked_at")} if tg else None,
        "prefs": _notify.prefs_of(settings),
        "kinds": list(_notify.KINDS),
        "polling": _notify.polling_enabled(),
        "admin_chat_configured": _notify._admin_chat_configured(),
    }


@router.post("/api/notifications/telegram/link")
async def start_telegram_link(user: dict = Depends(current_user)):
    """One-time code + t.me deep link; the bot completes the link on /start <code>."""
    if not _notify._configured():
        return {"ok": False, "error": "Telegram bot not configured on this install (TELEGRAMBOT)"}
    return {"ok": True, **(await _notify.start_link(user["id"]))}


@router.delete("/api/notifications/telegram/link")
async def unlink_telegram(user: dict = Depends(current_user)):
    await _notify.unlink(user["id"])
    return {"ok": True}


@router.post("/api/notifications/prefs")
async def set_prefs(body: dict, user: dict = Depends(current_user)):
    patch = {k: bool(v) for k, v in (body or {}).items() if k in _notify.KINDS}
    if not patch:
        return {"ok": False, "error": "no known keys"}
    current = _notify.prefs_of((await db_module.get_user_with_settings(user["id"]) or {}).get("settings") or {})
    current.update(patch)
    await db_module.patch_user_settings_by_id(user["id"], {"telegram_prefs": current})
    return {"ok": True, "prefs": current}


@router.post("/api/notifications/digest")
async def send_digest(user: dict = Depends(current_user)):
    """Manually trigger the weekly digest."""
    org_id = user["org_id"]
    stats = await _build_digest_stats(org_id)
    n = await _notify.notify_org(org_id, _notify.weekly_digest_text(stats), "digest")
    return {"ok": True, "stats": stats, "recipients": n}


# PHASE20: internal-only — triggered by heartbeat scheduler, not frontend; verify before removing
# Added: stale-client nudge endpoint for heartbeat scheduler; no direct frontend call found
@router.post("/api/notifications/stale")
async def send_stale_clients(user: dict = Depends(current_user)):
    """Push nudge for clients with no activity in the last 30 days."""
    org_id = user["org_id"]
    if not DB_AVAILABLE:
        return {"ok": False, "error": "DB unavailable"}
    stale = await _get_stale_clients(org_id, days=30)
    n = 0
    if stale:
        n = await _notify.notify_org(org_id, _notify.stale_clients_text(stale), "digest")
    return {"ok": True, "stale_count": len(stale), "recipients": n}


# ---------------------------------------------------------------------------
# Two-way Telegram bot
# ---------------------------------------------------------------------------

@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Telegram → Buzzowl. /start <code> links a chat to a user, /stop unlinks,
    other messages are answered from the linked user's org knowledge base.
    Unlinked chats only get a hint. Register with setWebhook when not polling
    (config notifications.telegram_webhook_url)."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}
    asyncio.create_task(_notify.handle_update(body))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_digest_stats(org_id: int) -> dict:
    if not DB_AVAILABLE:
        return {}
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    week_label = f"Week of {week_start}"

    clients = await db_module.list_clients(org_id) or []
    active_clients = [c for c in clients if c.get("last_activity") and
                      str(c["last_activity"])[:10] >= week_start]

    try:
        async with db_module._pool.acquire() as conn:
            sessions_row = await conn.fetchrow(
                "SELECT COUNT(*) FROM documents WHERE org_id=$1 AND type='meeting' AND created_at >= NOW() - INTERVAL '7 days'",
                org_id,
            )
            findings_row = await conn.fetchrow(
                "SELECT COUNT(*) FROM documents WHERE org_id=$1 AND type='finding' AND created_at >= NOW() - INTERVAL '7 days'",
                org_id,
            )
            signals_row = await conn.fetchrow(
                "SELECT COUNT(*) FROM documents WHERE org_id=$1 AND type='signal' AND created_at >= NOW() - INTERVAL '7 days'",
                org_id,
            )
            runs_row = await conn.fetchrow(
                "SELECT COUNT(*) FROM agent_runs WHERE org_id=$1 AND started_at >= NOW() - INTERVAL '7 days'",
                org_id,
            )
    except Exception:
        return {"week": week_label}

    stale = await _get_stale_clients(org_id, days=30)

    return {
        "week": week_label,
        "sessions": sessions_row["count"] if sessions_row else 0,
        "active_clients": len(active_clients),
        "research_runs": runs_row["count"] if runs_row else 0,
        "new_findings": findings_row["count"] if findings_row else 0,
        "signals": signals_row["count"] if signals_row else 0,
        "stale_clients": [c["name"] for c in stale],
    }


async def _get_stale_clients(org_id: int, days: int = 30) -> list[dict]:
    # last_activity is a TEXT column — comparing it to a timestamptz in SQL
    # throws, so filter in Python instead.
    if not DB_AVAILABLE or not db_module._pool:
        return []
    try:
        async with db_module._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, last_activity FROM clients WHERE org_id = $1", org_id,
            )
        from routers.today import _parse_dt
        now = datetime.now(timezone.utc)
        stale = []
        for r in rows:
            last = _parse_dt(r["last_activity"])
            if last is None or (now - last).days >= days:
                stale.append(dict(r))
        stale.sort(key=lambda c: c["last_activity"] or "")
        return stale[:10]
    except Exception as exc:
        logger.warning("Stale clients query failed: %s", exc)
        return []
