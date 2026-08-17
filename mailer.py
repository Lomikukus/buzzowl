"""mailer.py — minimal SMTP sender for Buzzowl digest emails.

Admin-triggered only (Insights → Rep digests). Reads SMTP settings from config
(smtp_host / smtp_port / smtp_user / smtp_pass / smtp_from), which can be set in
config.yaml or via SMTP_* env vars. Graceful: never raises, returns (ok, message),
and no-ops with a clear message when unconfigured.
"""
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr

from context import config

logger = logging.getLogger("wk.mailer")


def smtp_configured() -> bool:
    return bool(config.get("smtp_host") and config.get("smtp_from"))


def send_email(to_addr: str, subject: str, html: str) -> tuple[bool, str]:
    """Send one HTML email. Returns (ok, message). Never raises."""
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
    from_name = config.get("smtp_from_name", "Buzzowl") or "Buzzowl"

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_addr

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
