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
    """Send a test Telegram message to verify the integration is working."""
    ok = _notify.notify(
        f"✅ *Buzzowl connected*\n"
        f"Notifications are working. Logged in as: {user.get('display_name') or user.get('username')}"
    )
    return {"ok": ok, "configured": _notify._configured()}


@router.get("/api/notifications/status")
async def get_status(user: dict = Depends(current_user)):
    """Return whether Telegram is configured."""
    return {
        "configured": _notify._configured(),
        "channel": "telegram" if _notify._configured() else None,
    }


@router.post("/api/notifications/digest")
async def send_digest(user: dict = Depends(current_user)):
    """Manually trigger the weekly digest."""
    org_id = user["org_id"]
    stats = await _build_digest_stats(org_id)
    _notify.notify_weekly_digest(stats)
    return {"ok": True, "stats": stats}


# PHASE20: internal-only — triggered by heartbeat scheduler, not frontend; verify before removing
# Added: stale-client nudge endpoint for heartbeat scheduler; no direct frontend call found
@router.post("/api/notifications/stale")
async def send_stale_clients(user: dict = Depends(current_user)):
    """Push nudge for clients with no activity in the last 30 days."""
    org_id = user["org_id"]
    if not DB_AVAILABLE:
        return {"ok": False, "error": "DB unavailable"}
    stale = await _get_stale_clients(org_id, days=30)
    if stale:
        _notify.notify_stale_clients(stale)
    return {"ok": True, "stale_count": len(stale)}


# ---------------------------------------------------------------------------
# Two-way Telegram bot
# ---------------------------------------------------------------------------

@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram bot messages and reply with KB answers.

    Security: only responds to chat IDs listed in TELEGRAM_CHAT_ID env var.
    All other senders are silently ignored.
    Register this URL with Telegram via:
      curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://your-domain/api/telegram/webhook"
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    message = body.get("message") or body.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if not text or not chat_id:
        return {"ok": True}

    # Security gate — silently ignore unauthorised senders
    allowed = _notify._chat_ids()
    if chat_id not in allowed:
        logger.warning("Telegram webhook: ignored message from unauthorized chat_id=%s", chat_id)
        return {"ok": True}

    asyncio.create_task(_handle_telegram_message(chat_id, text))
    return {"ok": True}


async def _handle_telegram_message(chat_id: str, text: str) -> None:
    """Background: process a Telegram message and send the AI reply."""
    if not DB_AVAILABLE:
        _notify.send_to(chat_id, "Knowledge base is currently unavailable.")
        return

    org = await db_module.get_first_org()
    if not org:
        _notify.send_to(chat_id, "No organisation configured in the knowledge base.")
        return

    org_id = org["id"]
    org_name = org.get("name", "your organisation")
    backend = config.get("agent_service_backend", "python")

    try:
        if backend in ("pi", "hermes", "split"):
            from routers.chat import _call_pi_chat
            answer, _ = await _call_pi_chat(text, org_id, None, org_name, [])
        else:
            from routers.chat import _build_roster, _run_tool_loop
            clients = await db_module.list_clients(org_id) or []
            contacts = await db_module.list_contacts(org_id) or []
            roster_str = _build_roster(clients, contacts)
            model = config.get("ollama_model", "llama3.2")
            system = (
                f"You are a sales intelligence assistant for {org_name}. "
                "Use your tools to search the knowledge base before answering. "
                "Be concise and cite sources.\n\n"
                f"ROSTER:\n{roster_str}"
            )
            answer, _ = await _run_tool_loop(system, text, org_id, model)
    except Exception as exc:
        logger.warning("Telegram bot chat error: %s", exc)
        answer = f"Error: {exc}"

    _notify.send_to(chat_id, (answer or "(No answer returned)")[:4000])


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
