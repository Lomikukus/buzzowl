"""mailer.py — SMTP sender for Buzzowl (digests, reminders, supervised outreach).

Reads SMTP settings from config (smtp_host / smtp_port / smtp_user / smtp_pass /
smtp_from), which can be set in config.yaml or via SMTP_* env vars. Graceful:
never raises, returns (ok, message), and no-ops with a clear message when
unconfigured.

Outreach identity (Phase 3): the org SMTP account stays the single transport —
no per-rep SMTP credentials. A rep is represented by From = "<Rep name> via
<org from-name>" <org address> and Reply-To = the rep's own address, so replies
land in the rep's inbox while SPF/DKIM keep matching the org domain. Threading
headers (Message-ID / In-Reply-To / References) let the IMAP poller match
replies back to the outreach document.
"""
import logging
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Optional

from context import config

logger = logging.getLogger("wk.mailer")


def smtp_configured() -> bool:
    return bool(config.get("smtp_host") and config.get("smtp_from"))


def sender_domain() -> str:
    """Domain part of the configured From address (for Message-IDs)."""
    from_addr = str(config.get("smtp_from") or "")
    return from_addr.split("@", 1)[1] if "@" in from_addr else ""


def html_to_text(html: str) -> str:
    """Cheap plain-text alternative: strip tags, keep line breaks."""
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", html or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def build_message(to_addr: str, subject: str, html: str, *,
                  text: Optional[str] = None,
                  from_name: Optional[str] = None,
                  reply_to: Optional[str] = None,
                  message_id: Optional[str] = None,
                  in_reply_to: Optional[str] = None,
                  references: Optional[list[str]] = None) -> MIMEMultipart:
    """Assemble a multipart/alternative message with threading headers.
    Pure function (no network) — unit-testable."""
    from_addr = config.get("smtp_from")
    org_name = config.get("smtp_from_name", "Buzzowl") or "Buzzowl"
    display = f"{from_name} via {org_name}" if from_name else org_name

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((display, from_addr))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=False)
    if reply_to:
        msg["Reply-To"] = reply_to
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = list(references or [])
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)
    if refs:
        msg["References"] = " ".join(refs)
    msg.attach(MIMEText(text if text is not None else html_to_text(html), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def send_email(to_addr: str, subject: str, html: str, **kwargs) -> tuple[bool, str]:
    """Send one email (multipart text+html). Returns (ok, message). Never raises.

    kwargs are forwarded to build_message: text, from_name, reply_to,
    message_id, in_reply_to, references.
    """
    if not smtp_configured():
        return False, "SMTP not configured (set smtp_host / smtp_from)"
    if not to_addr:
        return False, "no recipient email on file"

    host = config.get("smtp_host")
    port = int(config.get("smtp_port", 587) or 587)
    user = (config.get("smtp_user", "") or "").strip()
    # Google shows App Passwords as "xxxx xxxx xxxx xxxx" — strip the display
    # spaces so a pasted-with-spaces password still authenticates (it's 16 chars).
    pwd = (config.get("smtp_pass", "") or "").replace(" ", "")
    from_addr = config.get("smtp_from")

    msg = build_message(to_addr, subject, html, **kwargs)

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                if user:
                    s.login(user, pwd)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except Exception:
                    pass  # server without STARTTLS — send unencrypted (internal relay)
                if user:
                    s.login(user, pwd)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        return True, "sent"
    except Exception as exc:
        logger.warning("SMTP send to %s failed: %s", to_addr, exc)
        return False, str(exc)
