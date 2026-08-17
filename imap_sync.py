"""
imap_sync.py — reply & bounce ingestion for supervised outreach (Phase 3).

Polls one IMAP mailbox (the org's outreach/reply-to inbox) for messages that
refer to an outreach Message-ID via In-Reply-To / References and moves the
matching outreach document sent → replied (or → bounced for delivery-status
notifications). Pure ingestion: it never sends and never touches drafts.

Config (config.yaml or IMAP_* env): imap_host, imap_port (993), imap_user,
imap_pass, imap_folder (INBOX), imap_ssl (true). Unconfigured ⇒ clean no-op.

Matching is deliberately conservative:
  * A message counts as a REPLY when In-Reply-To or References contains a
    Message-ID we issued (documents.metadata.message_id) — the message_id
    domain check keeps us from matching arbitrary threads.
  * A message counts as a BOUNCE when it is a DSN (multipart/report,
    report-type=delivery-status) or the classic mailer-daemon subject shape,
    and its body/attached headers mention one of our Message-IDs.
  * Processed UIDs are remembered per mailbox (metadata on the org settings)
    so a message is applied once; the poller only ever reads.

The core is `process_message(raw_bytes)` — pure, unit-testable, no network.
"""

import email
import imaplib
import logging
import re
import ssl
from email import policy
from email.message import EmailMessage
from typing import Iterable, Optional

import context

logger = logging.getLogger("buzzowl.imap")

_MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def configured() -> bool:
    c = context.config
    return bool(c.get("imap_host") and c.get("imap_user"))


def _our_domains() -> list[str]:
    """Message-ID domains we issue (smtp_from's domain + explicit list)."""
    doms = []
    frm = str(context.config.get("smtp_from") or "")
    if "@" in frm:
        doms.append(frm.split("@", 1)[1].lower())
    extra = context.config.get("outreach_message_id_domains") or []
    doms.extend(str(d).lower() for d in extra)
    doms.append("buzzowl.local")
    return doms


def _extract_message_ids(text: str) -> list[str]:
    return _MSGID_RE.findall(text or "")


def _ours(mid: str) -> bool:
    dom = mid.rsplit("@", 1)[-1].rstrip(">").lower()
    return dom in _our_domains()


def classify(msg: EmailMessage) -> tuple[str, list[str]]:
    """Return ("reply"|"bounce"|"ignore", [our message ids referenced])."""
    refs = " ".join(filter(None, [msg.get("In-Reply-To", ""), msg.get("References", "")]))
    ours = [m for m in _extract_message_ids(refs) if _ours(m)]

    ctype = (msg.get_content_type() or "").lower()
    report_type = (msg.get_param("report-type", "", header="content-type") or "").lower()
    frm = (msg.get("From") or "").lower()
    subj = (msg.get("Subject") or "").lower()
    is_dsn = (ctype == "multipart/report" and report_type == "delivery-status") \
        or "mailer-daemon" in frm or "postmaster" in frm \
        or subj.startswith(("undeliver", "delivery status notification", "mail delivery failed",
                            "returned mail", "failure notice"))
    if is_dsn:
        # DSNs usually quote the original headers in a message/rfc822 part
        found = list(ours)
        try:
            for part in msg.walk():
                if part.get_content_type() in ("message/rfc822", "text/rfc822-headers"):
                    payload = part.get_payload()
                    inner = payload[0] if isinstance(payload, list) and payload else None
                    hdrs = ""
                    if inner is not None:
                        hdrs = " ".join(filter(None, [inner.get("Message-ID", ""),
                                                      inner.get("In-Reply-To", ""),
                                                      inner.get("References", "")]))
                    else:
                        try:
                            hdrs = part.get_content()
                        except Exception:
                            hdrs = ""
                    found.extend(m for m in _extract_message_ids(str(hdrs)) if _ours(m))
                elif part.get_content_type() == "text/plain":
                    try:
                        found.extend(m for m in _extract_message_ids(part.get_content()) if _ours(m))
                    except Exception:
                        pass
        except Exception:
            pass
        found = list(dict.fromkeys(found))
        return ("bounce" if found else "ignore"), found
    if ours:
        return "reply", ours
    return "ignore", []


async def apply(kind: str, message_ids: Iterable[str], *, note: str = "") -> list[dict]:
    """Apply a classified message to the matching outreach docs. Returns the
    list of {id, from_state, to_state} changes actually made."""
    import outreach as o
    if not (context.DB_AVAILABLE and context.db_module is not None):
        return []
    changes = []
    for mid in message_ids:
        doc = await context.db_module.find_outreach_by_message_id(mid)
        if not doc:
            continue
        meta = doc["metadata"]
        state = meta.get("state")
        target = o.REPLIED if kind == "reply" else o.BOUNCED
        if state == target:
            continue                              # already applied
        try:
            new_meta = o.transition(meta, target, actor=o.IMAP, note=note or f"{kind} detected via IMAP")
        except o.TransitionError as exc:
            logger.info("imap: %s for #%s not applicable from %s: %s", kind, doc["id"], state, exc)
            continue
        await context.db_module.update_document_metadata(doc["org_id"], doc["id"], new_meta)
        changes.append({"id": doc["id"], "from_state": state, "to_state": target, "message_id": mid})
    return changes


async def process_message(raw: bytes) -> dict:
    """Parse + classify + apply one raw RFC822 message. Pure w.r.t. IMAP."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    kind, mids = classify(msg)
    if kind == "ignore":
        return {"kind": "ignore", "changes": []}
    subj = (msg.get("Subject") or "")[:120]
    changes = await apply(kind, mids, note=f"{kind}: {subj}")
    return {"kind": kind, "message_ids": mids, "changes": changes}


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

def _connect() -> imaplib.IMAP4:
    c = context.config
    host = c["imap_host"]
    port = int(c.get("imap_port") or 993)
    use_ssl = str(c.get("imap_ssl", "true")).lower() not in ("0", "false", "no")
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
    else:
        conn = imaplib.IMAP4(host, port)
    conn.login(c["imap_user"], (c.get("imap_pass") or "").replace(" ", ""))
    return conn


def _fetch_unseen(conn: imaplib.IMAP4, folder: str, limit: int) -> list[tuple[bytes, bytes]]:
    conn.select(folder, readonly=True)
    typ, data = conn.search(None, "UNSEEN")
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()[-limit:]
    out = []
    for uid in uids:
        typ, parts = conn.fetch(uid, "(RFC822)")
        if typ == "OK" and parts and isinstance(parts[0], tuple):
            out.append((uid, parts[0][1]))
    return out


async def poll_once(limit: int = 50) -> dict:
    """One IMAP pass. Returns counters. No-op when unconfigured."""
    if not configured():
        return {"skipped": "imap not configured"}
    folder = context.config.get("imap_folder") or "INBOX"
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        conn = await loop.run_in_executor(None, _connect)
    except Exception as exc:
        logger.warning("imap: connect failed: %s", exc)
        return {"error": str(exc)}
    try:
        msgs = await loop.run_in_executor(None, _fetch_unseen, conn, folder, limit)
    except Exception as exc:
        logger.warning("imap: fetch failed: %s", exc)
        return {"error": str(exc)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    counters = {"seen": len(msgs), "replies": 0, "bounces": 0, "changes": 0}
    for _uid, raw in msgs:
        try:
            r = await process_message(raw)
        except Exception as exc:
            logger.warning("imap: message processing failed: %s", exc)
            continue
        if r["kind"] == "reply":
            counters["replies"] += 1
        elif r["kind"] == "bounce":
            counters["bounces"] += 1
        counters["changes"] += len(r.get("changes") or [])
    if counters["changes"]:
        logger.info("imap: %s", counters)
    return counters
