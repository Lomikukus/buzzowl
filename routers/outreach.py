"""
routers/outreach.py — supervised outreach: approval queue API + send worker.

  POST /api/outreach                       create a draft (human)
  GET  /api/outreach?state=&mine=1         list (queue view)
  GET  /api/outreach/{id}                  one item
  PATCH /api/outreach/{id}                 edit subject/body/to (draft|pending|approved only)
  POST /api/outreach/{id}/transition       {"to": "<state>", "note": ""} — human actor
  GET  /api/outreach/guardrails            current guardrail status for the org
  POST /api/outreach/worker/tick           run one worker pass now (admin; tests/ops)

Send worker (APScheduler job, every minute): claims ONE approved item at a
time (row-locked), checks the guardrails, sends via mailer with the rep's
identity, moves it to `sent` and writes the contact_log row. Any guardrail
refusal or SMTP failure moves the item back to `approved` with last_error and
logs the reason (visible in the queue) — nothing is silently dropped.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

import autonomy
import mailer
import outreach as o
from context import DB_AVAILABLE, config, console, db_module
from routers.auth import current_user

logger = logging.getLogger("buzzowl.outreach")
router = APIRouter(prefix="/api/outreach")

_EDITABLE_STATES = (o.DRAFT, o.PENDING, o.APPROVED)


def _require_db():
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")


def _view(d: dict) -> dict:
    m = d.get("metadata") or {}
    return {
        "id": d["id"], "doc_id": d.get("doc_id"), "title": d.get("title"),
        "content": d.get("content"), "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"), "created_by": d.get("created_by"),
        "agent_run_id": d.get("agent_run_id"), **o.summarize(m),
        "history": m.get("history") or [], "allowed": o.allowed_targets(m.get("state") or o.DRAFT, o.HUMAN),
    }


# ---------------------------------------------------------------------------
# CRUD + transitions
# ---------------------------------------------------------------------------

@router.post("")
async def create_outreach(body: dict, user: dict = Depends(current_user)):
    """Create a draft. Humans only here — the agent uses the internal path."""
    _require_db()
    client = (body.get("client") or "").strip()
    subject = (body.get("subject") or "").strip()
    content = (body.get("body") or body.get("content") or "").strip()
    to_email = (body.get("to_email") or "").strip()
    if not client or not subject or not content:
        raise HTTPException(status_code=400, detail="client, subject and body are required")
    return await _create_draft(user["org_id"], client=client, subject=subject, content=content,
                               to_email=to_email, to_contact=(body.get("to_contact") or "").strip(),
                               sender_user_id=user["id"], created_by=user["id"],
                               source="human", purpose=(body.get("purpose") or "").strip())


async def _create_draft(org_id: int, *, client: str, subject: str, content: str,
                        to_email: str = "", to_contact: str = "",
                        sender_user_id: Optional[int] = None, created_by: Optional[int] = None,
                        source: str = "human", purpose: str = "",
                        agent_run_id: Optional[int] = None) -> dict:
    """Shared by the human endpoint and the agent tool (level-3 gated upstream)."""
    from datetime import datetime as _dt
    ts = _dt.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    meta = o.new_draft_metadata(client_name=client, to_email=to_email, to_contact=to_contact,
                                subject=subject, sender_user_id=sender_user_id, source=source,
                                agent_run_id=agent_run_id, purpose=purpose)
    int_id = await db_module.index_document(
        org_id=org_id, doc_id=f"outreach-{ts}", doc_type=o.DOC_TYPE,
        title=f"Outreach: {client} — {subject}"[:200], content=content, metadata=meta,
        embedding=[], source="agent" if source == "agent" else "human",
        created_by=created_by, agent_run_id=agent_run_id,
    )
    try:
        client_row = await db_module.get_client(org_id, client)
        if client_row and int_id > 0:
            await db_module.link_document(int_id, "client", client_row["id"])
    except Exception:
        pass
    doc = await db_module.get_document_by_int_id(org_id, int_id)
    return _view(doc) if doc else {"id": int_id}


@router.get("")
async def list_outreach(state: Optional[str] = None, mine: int = 0, client: Optional[str] = None,
                        limit: int = 100, user: dict = Depends(current_user)):
    _require_db()
    if state and state not in o.STATES:
        raise HTTPException(status_code=400, detail=f"unknown state {state!r}")
    rows = await db_module.list_outreach(user["org_id"], state=state,
                                         sender_user_id=user["id"] if mine else None,
                                         client_name=client, limit=min(max(limit, 1), 500))
    return {"items": [_view(d) for d in rows], "states": list(o.STATES)}


@router.get("/guardrails")
async def guardrail_status(user: dict = Depends(current_user)):
    return await _guardrails(user["org_id"])


@router.get("/{item_id}")
async def get_outreach(item_id: int, user: dict = Depends(current_user)):
    _require_db()
    d = await db_module.get_document_by_int_id(user["org_id"], item_id)
    if not d or d.get("type") != o.DOC_TYPE:
        raise HTTPException(status_code=404, detail="outreach item not found")
    d["metadata"] = db_module._doc_meta(d)
    return _view(d)


@router.patch("/{item_id}")
async def edit_outreach(item_id: int, body: dict, user: dict = Depends(current_user)):
    """Edit-before-approve: subject / body / recipient while not yet queued."""
    _require_db()
    d = await db_module.get_document_by_int_id(user["org_id"], item_id)
    if not d or d.get("type") != o.DOC_TYPE:
        raise HTTPException(status_code=404, detail="outreach item not found")
    meta = db_module._doc_meta(d)
    if (meta.get("state") or o.DRAFT) not in _EDITABLE_STATES:
        raise HTTPException(status_code=409, detail=f"cannot edit in state {meta.get('state')}")
    if user.get("role") != "admin" and meta.get("sender_user_id") not in (None, user["id"]):
        raise HTTPException(status_code=403, detail="not your draft")
    content = body.get("body") if "body" in body else body.get("content")
    title = None
    for k in ("subject", "to_email", "to_contact"):
        if k in body:
            meta[k] = (body.get(k) or "").strip()
    if "subject" in body:
        title = f"Outreach: {meta.get('client')} — {meta['subject']}"[:200]
    meta.setdefault("history", []).append({"from": meta.get("state"), "to": meta.get("state"),
                                           "actor": o.HUMAN, "actor_id": user["id"],
                                           "ts": datetime.now(timezone.utc).isoformat(), "note": "edited"})
    await db_module.update_document_metadata(user["org_id"], item_id, meta,
                                             content=content.strip() if isinstance(content, str) else None,
                                             title=title)
    d = await db_module.get_document_by_int_id(user["org_id"], item_id)
    d["metadata"] = db_module._doc_meta(d)
    return _view(d)


@router.post("/{item_id}/transition")
async def transition_outreach(item_id: int, body: dict, user: dict = Depends(current_user)):
    """Human transitions. Approving requires admin or being the sender."""
    _require_db()
    target = (body.get("to") or "").strip()
    note = (body.get("note") or "").strip()
    d = await db_module.get_document_by_int_id(user["org_id"], item_id)
    if not d or d.get("type") != o.DOC_TYPE:
        raise HTTPException(status_code=404, detail="outreach item not found")
    meta = db_module._doc_meta(d)
    if target == o.APPROVED and user.get("role") != "admin" and meta.get("sender_user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="only the sender or an admin may approve")
    try:
        new_meta = o.transition(meta, target, actor=o.HUMAN, actor_id=user["id"], note=note)
    except o.TransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db_module.update_document_metadata(user["org_id"], item_id, new_meta)
    d["metadata"] = new_meta
    return _view(d)


# ---------------------------------------------------------------------------
# Guardrails + send worker
# ---------------------------------------------------------------------------

async def _guardrails(org_id: int, to_email: str = "") -> dict:
    """Evaluate every guardrail; returns {ok, reason, ...status}."""
    s = await autonomy.settings(org_id)
    now = datetime.now(timezone.utc)
    status = {
        "enabled": bool(s.get("outreach_enabled")),
        "kill_switch": bool(s.get("outreach_kill_switch")),
        "smtp_configured": mailer.smtp_configured(),
        "max_per_day": int(s.get("outreach_max_per_day", 25)),
        "contact_floor_days": int(s.get("outreach_contact_floor_days", 7)),
        "quiet_hours": list(s.get("outreach_quiet_hours") or [20, 7]),
        "utc_hour": now.hour,
    }
    try:
        status["sent_today"] = await db_module.count_outreach_sent_today(org_id)
    except Exception:
        status["sent_today"] = 0
    ok, reason = True, ""
    if status["kill_switch"]:
        ok, reason = False, "outreach kill switch is on"
    elif not status["enabled"]:
        ok, reason = False, "outreach sending is disabled (outreach_enabled=false)"
    elif not status["smtp_configured"]:
        ok, reason = False, "SMTP not configured"
    elif status["sent_today"] >= status["max_per_day"]:
        ok, reason = False, f"daily send cap reached ({status['sent_today']}/{status['max_per_day']})"
    else:
        qs, qe = status["quiet_hours"]
        in_quiet = (qs <= now.hour < qe) if qs <= qe else (now.hour >= qs or now.hour < qe)
        if in_quiet:
            ok, reason = False, f"quiet hours ({qs:02d}:00–{qe:02d}:00 UTC)"
    if ok and to_email:
        try:
            last = await db_module.last_outreach_sent_to(org_id, to_email)
        except Exception:
            last = None
        if last and last > now - timedelta(days=status["contact_floor_days"]):
            ok, reason = False, (f"contact floor: {to_email} was mailed on {last.date().isoformat()} "
                                 f"(min {status['contact_floor_days']} days)")
    status.update({"ok": ok, "reason": reason})
    return status


async def send_one(org_id: Optional[int] = None) -> Optional[dict]:
    """One worker pass: claim → guardrails → send → sent|back-to-approved.
    Returns a summary dict or None when nothing was approved."""
    if not DB_AVAILABLE or db_module is None:
        return None
    item = await db_module.claim_next_approved_outreach(org_id)
    if not item:
        return None
    oid, org = item["id"], item["org_id"]
    meta = item["metadata"]
    to_email = (meta.get("to_email") or "").strip()

    def _fail(reason: str) -> dict:
        nonlocal meta
        meta = o.transition(meta, o.APPROVED, actor=o.WORKER, note=f"not sent: {reason}",
                            extra={"last_error": reason})
        return {"id": oid, "sent": False, "reason": reason}

    if not to_email:
        result = _fail("no recipient email")
    else:
        g = await _guardrails(org, to_email)
        if not g["ok"]:
            result = _fail(g["reason"])
        else:
            identity = {}
            if meta.get("sender_user_id"):
                try:
                    identity = await db_module.get_user_identity(org, int(meta["sender_user_id"]))
                except Exception:
                    identity = {}
            message_id = o.new_message_id(mailer.sender_domain())
            html = item.get("content") or ""
            if "<" not in html:                       # plain text body → simple html
                html = "<p>" + html.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
            if identity.get("signature"):
                html += "<p>" + identity["signature"].replace("\n", "<br>") + "</p>"
            ok, msg = await asyncio.get_running_loop().run_in_executor(
                None, lambda: mailer.send_email(
                    to_email, meta.get("subject") or item.get("title") or "", html,
                    from_name=identity.get("display_name") or None,
                    reply_to=identity.get("reply_to") or None,
                    message_id=message_id))
            if ok:
                meta = o.transition(meta, o.SENT, actor=o.WORKER, note="sent via SMTP",
                                    extra={"message_id": message_id, "last_error": None,
                                           "from_identity": identity.get("display_name") or ""})
                try:
                    await db_module.log_contact(org, meta.get("sender_user_id"), meta.get("client") or "",
                                                contact_name=meta.get("to_contact") or "",
                                                contact_email=to_email, subject=meta.get("subject") or "",
                                                body=item.get("content") or "", source_doc_id=oid)
                except Exception as exc:
                    logger.warning("contact_log write failed for outreach %s: %s", oid, exc)
                result = {"id": oid, "sent": True, "message_id": message_id, "to": to_email}
            else:
                result = _fail(f"smtp: {msg}")
    await db_module.update_document_metadata(org, oid, meta)
    console.print(f"[dim]outreach worker: #{oid} → {'sent' if result.get('sent') else 'held: ' + result.get('reason', '')}[/dim]")
    return result


async def worker_tick(max_items: int = 5) -> list[dict]:
    """APScheduler entry: send up to max_items approved mails."""
    results = []
    for _ in range(max_items):
        r = await send_one()
        if r is None:
            break
        results.append(r)
        if not r.get("sent"):
            break          # a guardrail refusal usually applies org-wide — stop this tick
    return results


@router.post("/worker/tick")
async def worker_tick_now(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return {"results": await worker_tick()}
