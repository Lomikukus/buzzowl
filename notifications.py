"""
notifications.py — Telegram notification dispatcher for Buzzowl.

Reads TELEGRAMBOT and TELEGRAM_CHAT_ID from environment (.env).
TELEGRAM_CHAT_ID may be comma-separated for multiple recipients / group chats.

All functions degrade gracefully — if Telegram is not configured or the
request fails, a warning is logged and execution continues normally.

Public API:
    notify(text)                  — send a plain text message
    notify_research_report(...)   — send full research report as .md file (ONE message per run)
    notify_stale_clients(clients) — stale client nudge
    notify_weekly_digest(stats)   — Monday morning summary
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
    return bool(_token() and _chat_ids())


# ---------------------------------------------------------------------------
# Core senders
# ---------------------------------------------------------------------------

def notify(text: str) -> bool:
    """Send a plain Telegram message to all recipients. Returns True if at least one succeeded."""
    if not _configured():
        logger.debug("Telegram not configured — skipping notification")
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


def _send_document(filename: str, content: bytes, caption: str) -> bool:
    """Send a file to all recipients. Returns True if at least one succeeded."""
    if not _configured():
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


def notify_research_report(
    subject: str,
    findings: list[dict],
    signals: list[dict],
    synthesized_report: str,
    today: str,
) -> None:
    """Send ONE consolidated research report as a .md file after a full research run."""
    if not _configured():
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

def notify_stale_clients(clients: list[dict]) -> None:
    """Push nudge for clients with no recent activity."""
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


def notify_weekly_digest(stats: dict) -> None:
    """Push Monday morning weekly digest."""
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
