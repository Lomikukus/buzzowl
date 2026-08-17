"""
notifications.py — Telegram notifications, routed to PEOPLE (Phase 6b fix).

Every user links their own Telegram chat (Settings → Notifications → Connect:
a /start <code> deep link) and chooses what to receive. Nothing is broadcast
to "the org" any more:

  notify_user(user_id, text, kind)          one person, if linked and opted in
  notify_org(org_id, text, kind, roles)     every linked member (optionally admins only)
  notify_run(org_id, run, text, document)   the person who triggered a run (kind 'runs');
                                            automatic/heartbeat runs go only to people who
                                            opted into 'auto_runs' (default off)
  notify_admin_chat(text)                   the legacy operator chat (TELEGRAM_CHAT_ID) —
                                            feedback and system messages ONLY

Preferences (users.settings.telegram_prefs, all default true except auto_runs):
  runs, auto_runs, digest, reminders, signals, admin

Env: TELEGRAMBOT (bot token). TELEGRAM_CHAT_ID (comma list) is now the operator/
admin chat only. The bot receives messages via polling (default) or the webhook
(config notifications.telegram_webhook_url). Everything degrades gracefully.
"""

import json as _json
import logging
import os
import textwrap
from io import BytesIO
from typing import Optional

import requests

logger = logging.getLogger("whisper.notifications")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _token() -> str:
    return os.environ.get("TELEGRAMBOT", "")

def _chat_ids() -> list[str]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [cid.strip() for cid in raw.split(",") if cid.strip()]

def _configured() -> bool:
    """Bot token present (per-user routing works without any admin chat)."""
    return bool(_token())


def _admin_chat_configured() -> bool:
    return bool(_token() and _chat_ids())


# ---------------------------------------------------------------------------
# Core senders
# ---------------------------------------------------------------------------

def notify(text: str) -> bool:
    """Operator/admin chat (TELEGRAM_CHAT_ID) — for feedback and system messages only.
    Product events go to people via notify_user / notify_org / notify_run."""
    return notify_admin_chat(text)


def notify_admin_chat(text: str) -> bool:
    if not _admin_chat_configured():
        logger.debug("Telegram admin chat not configured — skipping")
        return False
    token = _token()
    any_ok = False
    for chat_id in _chat_ids():
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.ok:
                any_ok = True
            else:
                logger.warning("Telegram sendMessage failed for %s: %s", chat_id, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram notification error: %s", exc)
    return any_ok


def _send_document_to(chat_id: str, filename: str, content: bytes, caption: str) -> bool:
    if not _token():
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_token()}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "Markdown"},
            files={"document": (filename, BytesIO(content), "text/plain")},
            timeout=30,
        )
        if not resp.ok:
            logger.warning("Telegram sendDocument failed for %s: %s", chat_id, resp.text[:200])
        return resp.ok
    except Exception as exc:
        logger.warning("Telegram sendDocument error: %s", exc)
        return False


def _send_document(filename: str, content: bytes, caption: str) -> bool:
    """Admin chat only (legacy). Returns True if at least one succeeded."""
    if not _admin_chat_configured():
        return False
    token = _token()
    any_ok = False
    for chat_id in _chat_ids():
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "Markdown",
                },
                files={"document": (filename, BytesIO(content), "text/plain")},
                timeout=30,
            )
            if resp.ok:
                any_ok = True
            else:
                logger.warning("Telegram sendDocument failed for %s: %s", chat_id, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram sendDocument error: %s", exc)
    return any_ok


# ---------------------------------------------------------------------------
# Research report — the only notification fired after a full research run
# ---------------------------------------------------------------------------

def _safe_meta(raw) -> dict:
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except Exception:
            return {}
    return raw or {}


def _build_report_md(
    subject: str,
    findings: list[dict],
    signals: list[dict],
    synthesized_report: str,
    today: str,
) -> str:
    """Build the full markdown report sent as a file attachment.

    The synthesized_report already contains the full analyst output including
    ## Sources, so we prepend only a minimal metadata header.
    """
    type_icons = {"pain_point": "🔴", "opportunity": "🟢", "risk": "🟠", "news": "🔵"}

    header_lines = [
        f"*Generated: {today} · {len(findings)} findings · {len(signals)} signals*",
        "",
    ]

    if signals:
        header_lines.append("**Key Signals**")
        for s in signals[:5]:
            meta = _safe_meta(s.get("metadata"))
            stype = meta.get("signal_type", "news")
            icon = type_icons.get(stype, "•")
            headline = meta.get("headline") or s.get("title", "")
            evidence = meta.get("evidence", "")
            header_lines.append(f"{icon} **{headline}**" + (f" — {evidence}" if evidence else ""))
        header_lines.append("")
        header_lines.append("---")
        header_lines.append("")

    return "\n".join(header_lines) + synthesized_report


def build_research_report(subject: str, findings: list[dict], signals: list[dict],
                          synthesized_report: str, today: str) -> tuple[str, str, bytes]:
    """(caption, filename, md bytes) for a completed research run — sent to the person
    who triggered it via notify_run(document=...)."""
    md = _build_report_md(subject, findings, signals, synthesized_report, today)
    filename = f"research-{subject.lower().replace(' ', '-')}-{today}.md"
    type_icons = {"pain_point": "🔴", "opportunity": "🟢", "risk": "🟠", "news": "🔵"}
    top = []
    for sg in signals[:3]:
        meta = _safe_meta(sg.get("metadata"))
        top.append(f"{type_icons.get(meta.get('signal_type', 'news'), '•')} {textwrap.shorten(meta.get('headline') or sg.get('title', ''), 70)}")
    caption = "\n".join([f"🏢 *Research complete: {subject}*", f"📄 {len(findings)} findings · {len(signals)} signals"] + ([""] + top if top else []))
    return caption, filename, md.encode("utf-8")


def notify_research_report(
    subject: str,
    findings: list[dict],
    signals: list[dict],
    synthesized_report: str,
    today: str,
) -> None:
    """LEGACY (admin chat): send ONE consolidated research report as a .md file."""
    if not _admin_chat_configured():
        return

    md = _build_report_md(subject, findings, signals, synthesized_report, today)
    filename = f"research-{subject.lower().replace(' ', '-')}-{today}.md"

    # Short caption for the message
    signal_count = len(signals)
    finding_count = len(findings)
    top_signals = []
    type_icons = {"pain_point": "🔴", "opportunity": "🟢", "risk": "🟠", "news": "🔵"}
    for s in signals[:3]:
        meta = _safe_meta(s.get("metadata"))
        stype = meta.get("signal_type", "news")
        headline = meta.get("headline") or s.get("title", "")
        top_signals.append(f"{type_icons.get(stype, '•')} {textwrap.shorten(headline, 70)}")

    caption_lines = [f"🏢 *Research complete: {subject}*", f"📄 {finding_count} findings · {signal_count} signals"]
    if top_signals:
        caption_lines.append("")
        caption_lines.extend(top_signals)

    _send_document(
        filename=filename,
        content=md.encode("utf-8"),
        caption="\n".join(caption_lines),
    )


# ---------------------------------------------------------------------------
# Scheduled / manual notifications
# ---------------------------------------------------------------------------

def stale_clients_text(clients: list[dict]) -> str:
    lines = ["⏰ *Clients with no recent activity:*"]
    for c in clients[:5]:
        last = str(c.get("last_activity") or "never")[:10]
        lines.append(f"• {c['name']} — last active: {last}")
    if len(clients) > 5:
        lines.append(f"…and {len(clients) - 5} more")
    return "\n".join(lines)


def notify_stale_clients(clients: list[dict]) -> None:
    """LEGACY (admin chat): push nudge for clients with no recent activity."""
    if not clients:
        return
    lines = ["⏰ *Clients with no recent activity:*"]
    for c in clients[:5]:
        last = str(c.get("last_activity") or "never")[:10]
        lines.append(f"• {c['name']} — last active: {last}")
    if len(clients) > 5:
        lines.append(f"…and {len(clients) - 5} more")
    notify("\n".join(lines))


def send_to(chat_id: str, text: str) -> bool:
    """Send a message to one specific chat_id (used for bot replies)."""
    if not _token():
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_token()}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram send_to %s failed: %s", chat_id, resp.text[:200])
        return resp.ok
    except Exception as exc:
        logger.warning("Telegram send_to error: %s", exc)
        return False


def weekly_digest_text(stats: dict) -> str:
    lines = [
        "📊 *Buzzowl — Weekly Digest*",
        f"📅 {stats.get('week', '')}",
        "",
        f"🗣 Sessions this week: *{stats.get('sessions', 0)}*",
        f"🏢 Active clients: *{stats.get('active_clients', 0)}*",
        f"🔍 Research runs: *{stats.get('research_runs', 0)}*",
        f"📄 New findings: *{stats.get('new_findings', 0)}*",
        f"⚡ Signals extracted: *{stats.get('signals', 0)}*",
    ]
    if stats.get("stale_clients"):
        lines.append("")
        lines.append(f"⚠️ Needs attention: {', '.join(stats['stale_clients'][:3])}")
    return "\n".join(lines)


def notify_weekly_digest(stats: dict) -> None:
    """LEGACY (admin chat): push Monday morning weekly digest."""
    lines = [
        "📊 *Buzzowl — Weekly Digest*",
        f"📅 {stats.get('week', '')}",
        "",
        f"🗣 Sessions this week: *{stats.get('sessions', 0)}*",
        f"🏢 Active clients: *{stats.get('active_clients', 0)}*",
        f"🔍 Research runs: *{stats.get('research_runs', 0)}*",
        f"📄 New findings: *{stats.get('new_findings', 0)}*",
        f"⚡ Signals extracted: *{stats.get('signals', 0)}*",
    ]
    if stats.get("stale_clients"):
        lines.append("")
        lines.append(f"⚠️ Needs attention: {', '.join(stats['stale_clients'][:3])}")
    notify("\n".join(lines))


# ---------------------------------------------------------------------------
# Per-user routing (Phase 6b)
# ---------------------------------------------------------------------------

import asyncio as _asyncio
import secrets as _secrets
import time as _time
from datetime import datetime as _dt, timezone as _tz

DEFAULT_PREFS = {"runs": True, "auto_runs": False, "digest": True, "reminders": True, "signals": True, "admin": True}
KINDS = tuple(DEFAULT_PREFS)


def _db():
    import context
    return context.db_module if context.DB_AVAILABLE else None


def prefs_of(settings: dict) -> dict:
    p = dict(DEFAULT_PREFS)
    for k, v in ((settings or {}).get("telegram_prefs") or {}).items():
        if k in p:
            p[k] = bool(v)
    return p


def linked_chat(settings: dict) -> Optional[str]:
    tg = (settings or {}).get("telegram") or {}
    return str(tg.get("chat_id")) if tg.get("chat_id") else None


async def _send_async(chat_id: str, text: str, document: Optional[tuple] = None) -> bool:
    loop = _asyncio.get_event_loop()
    if document:
        caption, filename, content = document
        return await loop.run_in_executor(None, _send_document_to, chat_id, filename, content, caption)
    return await loop.run_in_executor(None, send_to, chat_id, text)


async def notify_user(user_id: int, text: str, kind: str = "runs", document: Optional[tuple] = None) -> bool:
    """One person — only if they linked Telegram and enabled this kind."""
    db = _db()
    if not db or not _token() or not user_id:
        return False
    u = await db.get_user_with_settings(user_id)
    if not u:
        return False
    chat = linked_chat(u["settings"])
    if not chat or not prefs_of(u["settings"]).get(kind, True):
        return False
    return await _send_async(chat, text, document)


async def notify_org(org_id: int, text: str, kind: str = "digest", roles: Optional[tuple] = None,
                     document: Optional[tuple] = None, exclude_user_id: Optional[int] = None) -> int:
    """Every linked member of the org who enabled this kind (optionally only some roles)."""
    db = _db()
    if not db or not _token() or not org_id:
        return 0
    n = 0
    for u in await db.list_users_with_settings(org_id):
        if roles and u.get("role") not in roles:
            continue
        if exclude_user_id and u["id"] == exclude_user_id:
            continue
        chat = linked_chat(u["settings"])
        if chat and prefs_of(u["settings"]).get(kind, True):
            if await _send_async(chat, text, document):
                n += 1
    return n


async def notify_run(org_id: int, run: Optional[dict], text: str, document: Optional[tuple] = None) -> int:
    """A completed agent run: the person who triggered it (kind 'runs'); automatic /
    heartbeat / autonomous runs go to members who opted into 'auto_runs'."""
    trig = (run or {}).get("triggered_by")
    if trig:
        return 1 if await notify_user(int(trig), text, "runs", document) else 0
    return await notify_org(org_id, text, "auto_runs", document=document)


# --- linking ------------------------------------------------------------------

_bot_info: dict = {}


def bot_username() -> Optional[str]:
    """Cached getMe → username for the t.me deep link."""
    if not _token():
        return None
    if _bot_info.get("expires", 0) > _time.time():
        return _bot_info.get("username")
    try:
        r = requests.get(f"https://api.telegram.org/bot{_token()}/getMe", timeout=8)
        name = (r.json().get("result") or {}).get("username") if r.ok else None
    except Exception:
        name = None
    _bot_info.update({"username": name, "expires": _time.time() + 3600})
    return name


async def start_link(user_id: int) -> dict:
    """Issue a one-time code; the user opens https://t.me/<bot>?start=<code>."""
    db = _db()
    code = _secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    expires = _dt.now(_tz.utc).timestamp() + 900
    await db.patch_user_settings_by_id(user_id, {"telegram_link": {"code": code, "expires": expires}})
    name = bot_username()
    return {"code": code, "bot_username": name,
            "deep_link": f"https://t.me/{name}?start={code}" if name else None, "expires_in_s": 900}


async def complete_link(code: str, chat_id: str, tg_user: dict) -> Optional[dict]:
    db = _db()
    u = await db.find_user_by_telegram_link_code(code)
    if not u:
        return None
    settings = u.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = _json.loads(settings)
        except Exception:
            settings = {}
    if float((settings.get("telegram_link") or {}).get("expires") or 0) < _time.time():
        return None
    await db.patch_user_settings_by_id(u["id"], {"telegram": {
        "chat_id": str(chat_id), "username": tg_user.get("username"), "first_name": tg_user.get("first_name"),
        "linked_at": _dt.now(_tz.utc).isoformat()}})
    await db.remove_user_setting_keys(u["id"], ["telegram_link"])
    return u


async def unlink(user_id: int) -> None:
    await _db().remove_user_setting_keys(user_id, ["telegram", "telegram_link"])


# --- inbound (webhook or polling) ---------------------------------------------

async def handle_update(update: dict) -> None:
    """One Telegram update: /start <code> links, /stop unlinks, /status, else chat
    with the knowledge base of the LINKED user's org. Unlinked chats get a hint —
    never a first-org guess."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = str((message.get("chat") or {}).get("id") or "")
    text = (message.get("text") or "").strip()
    tg_user = message.get("from") or {}
    if not chat_id or not text:
        return
    db = _db()
    if not db:
        send_to(chat_id, "Buzzowl's database is unavailable right now.")
        return
    low = text.lower()
    if low.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            send_to(chat_id, "Hi! To connect this chat, open Buzzowl → Settings → Notifications → Connect Telegram and tap the link there.")
            return
        u = await complete_link(code, chat_id, tg_user)
        if u:
            send_to(chat_id, f"✅ Linked to Buzzowl as {u.get('display_name') or u.get('username')} ({u.get('org_name')}). "
                             f"You'll get your notifications here. /stop to disconnect, /status to check.")
        else:
            send_to(chat_id, "That link code is unknown or expired — open Settings → Notifications in Buzzowl and connect again.")
        return
    u = await db.find_user_by_telegram_chat(chat_id)
    if low.startswith("/stop") or low.startswith("/unlink"):
        if u:
            await unlink(u["id"])
            send_to(chat_id, "Disconnected. You can reconnect any time from Settings → Notifications.")
        else:
            send_to(chat_id, "This chat is not connected to a Buzzowl account.")
        return
    if low.startswith("/status"):
        send_to(chat_id, f"Connected as {u.get('display_name') or u.get('username')} ({u.get('org_name')})." if u
                else "Not connected — use the Connect button in Buzzowl → Settings → Notifications.")
        return
    if not u:
        send_to(chat_id, "This chat isn't linked to a Buzzowl account yet. Open Buzzowl → Settings → Notifications → Connect Telegram.")
        return
    # linked → answer from THEIR org's knowledge base
    await _answer_from_kb(chat_id, text, u)


async def _answer_from_kb(chat_id: str, text: str, u: dict) -> None:
    import context
    db = _db()
    org_id, org_name = u["org_id"], u.get("org_name") or "your organisation"
    backend = context.config.get("agent_service_backend", "python")
    try:
        if backend in ("pi", "hermes", "split"):
            from routers.chat import _call_pi_chat
            answer, _ = await _call_pi_chat(text, org_id, None, org_name, [])
        else:
            from routers.chat import _build_roster, _run_tool_loop
            clients = await db.list_clients(org_id) or []
            contacts = await db.list_contacts(org_id) or []
            roster_str = _build_roster(clients, contacts)
            model = context.config.get("ollama_model", "llama3.2")
            system = (f"You are a sales intelligence assistant for {org_name}. Use your tools to search the knowledge base "
                      f"before answering. Be concise and cite sources.\n\nROSTER:\n{roster_str}")
            answer, _ = await _run_tool_loop(system, text, org_id, model)
    except Exception as exc:
        logger.warning("Telegram bot chat error: %s", exc)
        answer = f"Error: {exc}"
    send_to(chat_id, (answer or "(No answer returned)")[:4000])


# --- polling (default; no public webhook needed) -------------------------------

_poll_offset: dict = {"value": 0}


def polling_enabled() -> bool:
    import context
    cfg = (context.config or {}).get("notifications") or {}
    if not _token():
        return False
    if cfg.get("telegram_webhook_url"):
        return False                     # webhook mode set by the operator
    return bool(cfg.get("telegram_polling", True))


async def poll_updates() -> int:
    """Scheduler job: fetch new updates and handle them. Returns the count."""
    if not polling_enabled():
        return 0
    loop = _asyncio.get_event_loop()

    def _fetch():
        try:
            r = requests.get(f"https://api.telegram.org/bot{_token()}/getUpdates",
                             params={"offset": _poll_offset["value"], "timeout": 0, "allowed_updates": _json.dumps(["message", "edited_message"])},
                             timeout=15)
            if r.status_code == 409:
                logger.warning("Telegram getUpdates 409 — a webhook is set; disable it or set notifications.telegram_webhook_url")
                return []
            return (r.json().get("result") or []) if r.ok else []
        except Exception as exc:
            logger.debug("Telegram poll failed: %s", exc)
            return []

    updates = await loop.run_in_executor(None, _fetch)
    for upd in updates:
        _poll_offset["value"] = max(_poll_offset["value"], int(upd.get("update_id", 0)) + 1)
        try:
            await handle_update(upd)
        except Exception as exc:
            logger.warning("Telegram update handling failed: %s", exc)
    return len(updates)

