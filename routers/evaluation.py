"""
routers/evaluation.py — thesis evaluation instrumentation (Session 88).

- Manual research timer (research_sessions): the denominator for the
  agent-vs-manual efficiency comparison (DSR evaluation, exposé §4.4)
- Match report feedback: per-product 👍/👎 + perceived-traceability rating
- Prompt log access + full evaluation data export (admin)
"""

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response

from context import DB_AVAILABLE, db_module
from routers.auth import current_user

logger = logging.getLogger("wk.evaluation")

router = APIRouter()


def _require_db() -> None:
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")


def _require_admin(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


# ---------------------------------------------------------------------------
# Manual research timer
# ---------------------------------------------------------------------------

@router.post("/api/eval/research-sessions/start")
async def start_research_session(body: dict, user: dict = Depends(current_user)):
    _require_db()
    client_name = (body.get("client_name") or "").strip()
    if not client_name:
        raise HTTPException(status_code=400, detail="client_name is required")
    row = await db_module.start_research_session(
        user["org_id"], user["id"], client_name, method=body.get("method", "manual"),
    )
    if not row:
        raise HTTPException(status_code=503, detail="DB unavailable")
    return {"id": row["id"], "started_at": row["started_at"].isoformat()}


@router.post("/api/eval/research-sessions/{session_id}/stop")
async def stop_research_session(session_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    sources = body.get("sources_checked")
    row = await db_module.stop_research_session(
        session_id, user["org_id"],
        sources_checked=int(sources) if sources is not None else None,
        notes=(body.get("notes") or "").strip() or None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found or already stopped")
    return {
        "id": row["id"],
        "client_name": row["client_name"],
        "duration_secs": row["duration_secs"],
    }


@router.get("/api/eval/research-sessions")
async def get_research_sessions(user: dict = Depends(current_user)):
    _require_db()
    sessions = await db_module.list_research_sessions(user["org_id"])
    return {"sessions": [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in s.items()}
        for s in sessions
    ]}


# ---------------------------------------------------------------------------
# Match report feedback (per-product vote + traceability rating)
# ---------------------------------------------------------------------------

@router.post("/api/match/reports/{doc_id}/feedback")
async def match_report_feedback(doc_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    doc = await db_module.get_document_by_int_id(user["org_id"], doc_id)
    if not doc or doc.get("type") != "match_report":
        raise HTTPException(status_code=404, detail="Match report not found")

    section = (body.get("section") or "").strip()
    vote = (body.get("vote") or "").strip()
    traceability = body.get("traceability")
    if vote and vote not in ("up", "down"):
        raise HTTPException(status_code=400, detail="vote must be 'up' or 'down'")
    if traceability is not None and not (1 <= int(traceability) <= 5):
        raise HTTPException(status_code=400, detail="traceability must be 1-5")
    if not (section and vote) and traceability is None:
        raise HTTPException(status_code=400, detail="Provide section+vote and/or traceability")

    meta = doc.get("metadata") or {}
    now = datetime.now(timezone.utc).isoformat()
    if section and vote:
        fb = meta.get("match_feedback") or {}
        fb[section] = {"vote": vote, "user_id": user["id"], "rated_at": now}
        meta["match_feedback"] = fb
    if traceability is not None:
        ratings = meta.get("traceability") or []
        ratings.append({"rating": int(traceability), "user_id": user["id"], "rated_at": now})
        meta["traceability"] = ratings

    async with db_module._pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET metadata = $3 WHERE id = $1 AND org_id = $2",
            doc_id, user["org_id"], meta,
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Outreach outcome tracking — generated mails get a status lifecycle so the
# evaluation can compare real outcomes (sent → replied → meeting) per client
# ---------------------------------------------------------------------------

_OUTREACH_STATUSES = ("generated", "sent", "replied", "meeting")


@router.post("/api/outreach/{doc_id}/status")
async def set_outreach_status(doc_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    status = (body.get("status") or "").strip()
    if status not in _OUTREACH_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(_OUTREACH_STATUSES)}")
    doc = await db_module.get_document_by_int_id(user["org_id"], doc_id)
    if not doc or (doc.get("metadata") or {}).get("brief_type") != "mail_template":
        raise HTTPException(status_code=404, detail="Mail document not found")

    meta = doc.get("metadata") or {}
    meta["outreach_status"] = status
    history = meta.get("outreach_history") or []
    history.append({"status": status, "user_id": user["id"],
                    "ts": datetime.now(timezone.utc).isoformat()})
    meta["outreach_history"] = history

    async with db_module._pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET metadata = $3 WHERE id = $1 AND org_id = $2",
            doc_id, user["org_id"], meta,
        )
    return {"ok": True, "status": status}


@router.get("/api/outreach/summary")
async def outreach_summary(user: dict = Depends(current_user)):
    """Counts by status + recent outreach rows — shown in settings."""
    _require_db()
    async with db_module._pool.acquire() as conn:
        counts = await conn.fetch(
            """SELECT COALESCE(metadata->>'outreach_status', 'generated') AS status,
                      count(*) AS n
               FROM documents
               WHERE org_id = $1 AND metadata->>'brief_type' = 'mail_template'
               GROUP BY 1""", user["org_id"])
        recent = await conn.fetch(
            """SELECT id, title, metadata->>'subject' AS client,
                      COALESCE(metadata->>'outreach_status', 'generated') AS status,
                      created_at
               FROM documents
               WHERE org_id = $1 AND metadata->>'brief_type' = 'mail_template'
               ORDER BY created_at DESC LIMIT 20""", user["org_id"])
    return {
        "counts": {r["status"]: r["n"] for r in counts},
        "recent": [{**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                   for r in recent],
    }


# ---------------------------------------------------------------------------
# Contact log — durable per-contact outreach record (Home "Last contacted")
# ---------------------------------------------------------------------------

@router.post("/api/contact-log")
async def add_contact_log(body: dict, user: dict = Depends(current_user)):
    """Record that the rep exported/sent an outreach mail to a contact."""
    _require_db()
    client_name = (body.get("client_name") or "").strip()
    if not client_name:
        raise HTTPException(status_code=400, detail="client_name required")
    log_id = await db_module.log_contact(
        user["org_id"], user["id"], client_name,
        contact_name=(body.get("contact_name") or "").strip(),
        contact_email=(body.get("contact_email") or "").strip(),
        subject=(body.get("subject") or "").strip(),
        body=body.get("body") or "",
        source_doc_id=body.get("source_doc_id"),
    )
    return {"ok": log_id > 0, "id": log_id}


@router.get("/api/contact-log")
async def get_contact_log(mine: bool = True, limit: int = 50, user: dict = Depends(current_user)):
    """Recent contacts for the Home 'Last contacted' panel (this rep by default)."""
    _require_db()
    rows = await db_module.list_contact_log(
        user["org_id"], user_id=user["id"] if mine else None, limit=min(limit, 200),
    )
    for r in rows:
        if r.get("sent_at"):
            r["sent_at"] = r["sent_at"].isoformat()
    return {"contacts": rows}


@router.patch("/api/contact-log/{log_id}")
async def patch_contact_log(log_id: int, body: dict, user: dict = Depends(current_user)):
    """Toggle replied / follow_up on a logged contact."""
    _require_db()
    ok = await db_module.update_contact_log(
        log_id, user["org_id"],
        replied=body.get("replied"),
        follow_up=body.get("follow_up"),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="contact log row not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Page-view beacon — fired by navbar.js on every page load so the evaluation
# can see which parts of the tool each tester actually used
# ---------------------------------------------------------------------------

@router.post("/api/eval/pageview")
async def log_pageview(body: dict, user: dict = Depends(current_user)):
    path = (body.get("path") or "")[:200]
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path must start with /")
    db_module.log_prompt(user["org_id"], user["id"], "page_view", path, {})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Prompt log + evaluation export (admin)
# ---------------------------------------------------------------------------

@router.get("/api/eval/prompts")
async def get_prompts(limit: int = 100, surface: str = None, user: dict = Depends(current_user)):
    _require_db()
    _require_admin(user)
    prompts = await db_module.list_prompts(user["org_id"], limit=min(limit, 500), surface=surface)
    return {"prompts": [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in p.items()}
        for p in prompts
    ]}


@router.get("/api/eval/export")
async def export_evaluation_data(user: dict = Depends(current_user)):
    """One-click JSON bundle of all evaluation data for thesis analysis."""
    _require_db()
    _require_admin(user)
    org_id = user["org_id"]

    sessions = await db_module.list_research_sessions(org_id, limit=10000)

    async with db_module._pool.acquire() as conn:
        finding_ratings = await conn.fetch(
            """SELECT d.id, d.title, d.type, d.created_at,
                      d.metadata->'feedback' AS feedback,
                      d.metadata->>'subject' AS subject
               FROM documents d
               WHERE d.org_id = $1 AND d.metadata ? 'feedback'
               ORDER BY d.created_at""", org_id)
        match_feedback = await conn.fetch(
            """SELECT d.id, d.title, d.created_at,
                      d.metadata->'match_feedback' AS match_feedback,
                      d.metadata->'traceability' AS traceability,
                      d.metadata->>'subject' AS subject
               FROM documents d
               WHERE d.org_id = $1 AND d.type = 'match_report'
                 AND (d.metadata ? 'match_feedback' OR d.metadata ? 'traceability')
               ORDER BY d.created_at""", org_id)
        agent_runs = await conn.fetch(
            """SELECT agent_type, count(*) AS runs,
                      avg(EXTRACT(EPOCH FROM (completed_at - created_at)))::int AS avg_secs
               FROM agent_runs
               WHERE org_id = $1 AND status = 'done' AND completed_at IS NOT NULL
               GROUP BY agent_type ORDER BY runs DESC""", org_id)
        outreach = await conn.fetch(
            """SELECT d.id, d.title, d.created_at,
                      d.metadata->>'subject' AS client,
                      d.metadata->>'template_type' AS template_type,
                      COALESCE(d.metadata->>'outreach_status', 'generated') AS status,
                      d.metadata->'outreach_history' AS history
               FROM documents d
               WHERE d.org_id = $1 AND d.metadata->>'brief_type' = 'mail_template'
               ORDER BY d.created_at""", org_id)

    prompts = await db_module.list_prompts(org_id, limit=10000)

    def _ser(rows):
        out = []
        for r in rows:
            d = dict(r)
            for k, v in d.items():
                if isinstance(v, datetime):
                    d[k] = v.isoformat()
                elif isinstance(v, str) and k in ("feedback", "match_feedback", "traceability", "history"):
                    try:
                        d[k] = json.loads(v)
                    except (ValueError, TypeError):
                        pass
            out.append(d)
        return out

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "research_sessions": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in s.items()}
            for s in sessions
        ],
        "finding_ratings": _ser(finding_ratings),
        "match_feedback": _ser(match_feedback),
        "agent_run_durations": _ser(agent_runs),
        "outreach": _ser(outreach),
        "prompt_log": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in p.items()}
            for p in prompts
        ],
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f"attachment; filename=evaluation-export-{datetime.now().strftime('%Y%m%d')}.json"},
    )


@router.get("/api/eval/research-timing.csv")
async def export_research_timing_csv(user: dict = Depends(current_user)):
    """Per-run agent-run timing as CSV — one row per run, duration_secs =
    completed_at - created_at. Powers the 'Research timing (CSV)' download on
    the Insights page. Admin-only, scoped to the caller's org."""
    _require_db()
    _require_admin(user)
    org_id = user["org_id"]

    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_type, trigger_type, status,
                      output->>'subject'    AS subject,
                      left(task, 300)       AS task_excerpt,
                      created_at, completed_at,
                      CASE WHEN completed_at IS NOT NULL
                           THEN EXTRACT(EPOCH FROM (completed_at - created_at))::int
                      END AS duration_secs
               FROM agent_runs
               WHERE org_id = $1
               ORDER BY created_at""",
            org_id,
        )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "agent_type", "trigger_type", "status", "subject",
                "task_excerpt", "created_at", "completed_at", "duration_secs"])
    for r in rows:
        w.writerow([
            r["id"], r["agent_type"], r["trigger_type"], r["status"],
            r["subject"] or "", r["task_excerpt"] or "",
            r["created_at"].isoformat() if r["created_at"] else "",
            r["completed_at"].isoformat() if r["completed_at"] else "",
            "" if r["duration_secs"] is None else r["duration_secs"],
        ])

    fname = f"research-timing-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ---------------------------------------------------------------------------
# Admin analytics dashboard — aggregated stats for the Insights tab
# ---------------------------------------------------------------------------

@router.get("/api/eval/stats")
async def eval_stats(days: int = 30, include_qa: bool = False, user: dict = Depends(current_user)):
    """Server-side aggregates for the admin Insights dashboard. Excludes the
    QA accounts (user ids 7, 8) by default so they don't skew adoption stats."""
    _require_db()
    _require_admin(user)
    org_id = user["org_id"]
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    qa = "" if include_qa else " AND COALESCE(pl.user_id, -1) NOT IN (7, 8)"
    qa_u = "" if include_qa else " AND u.id NOT IN (7, 8)"

    async with db_module._pool.acquire() as conn:
        logins_by_day = await conn.fetch(f"""
            SELECT to_char(date_trunc('day', pl.created_at), 'YYYY-MM-DD') AS day, count(*) AS n
            FROM prompt_log pl
            WHERE pl.org_id=$1 AND pl.surface='login' AND pl.created_at>=$2 {qa}
            GROUP BY 1 ORDER BY 1""", org_id, cutoff)
        active_by_day = await conn.fetch(f"""
            SELECT to_char(date_trunc('day', pl.created_at), 'YYYY-MM-DD') AS day,
                   count(DISTINCT pl.user_id) AS n
            FROM prompt_log pl WHERE pl.org_id=$1 AND pl.created_at>=$2 {qa}
            GROUP BY 1 ORDER BY 1""", org_id, cutoff)
        events_by_day = await conn.fetch(f"""
            SELECT to_char(date_trunc('day', pl.created_at), 'YYYY-MM-DD') AS day, count(*) AS n
            FROM prompt_log pl WHERE pl.org_id=$1 AND pl.created_at>=$2 {qa}
            GROUP BY 1 ORDER BY 1""", org_id, cutoff)
        usage_by_surface = await conn.fetch(f"""
            SELECT pl.surface, count(*) AS n FROM prompt_log pl
            WHERE pl.org_id=$1 AND pl.created_at>=$2 {qa}
            GROUP BY 1 ORDER BY 2 DESC""", org_id, cutoff)
        per_user = await conn.fetch(f"""
            SELECT u.id, COALESCE(u.display_name, u.username) AS name, u.role,
                   count(pl.id) AS events,
                   count(pl.id) FILTER (WHERE pl.surface='login') AS logins,
                   max(pl.created_at) AS last_active
            FROM users u
            LEFT JOIN prompt_log pl ON pl.user_id=u.id AND pl.created_at>=$2
            WHERE u.org_id=$1 {qa_u}
            GROUP BY u.id, name, u.role ORDER BY events DESC""", org_id, cutoff)
        agent_runs = await conn.fetch("""
            SELECT agent_type, count(*) AS runs,
                   avg(EXTRACT(EPOCH FROM (completed_at - created_at)))::int AS avg_secs,
                   count(*) FILTER (WHERE status='done')   AS done,
                   count(*) FILTER (WHERE status='failed') AS failed
            FROM agent_runs WHERE org_id=$1 AND created_at>=$2
            GROUP BY 1 ORDER BY 2 DESC""", org_id, cutoff)
        contact = await conn.fetchrow("""
            SELECT count(*) AS sent, count(*) FILTER (WHERE replied) AS replied,
                   count(*) FILTER (WHERE follow_up) AS follow_up
            FROM contact_log WHERE org_id=$1 AND sent_at>=$2""", org_id, cutoff)
        outreach = await conn.fetch("""
            SELECT COALESCE(metadata->>'outreach_status','generated') AS status, count(*) AS n
            FROM documents
            WHERE org_id=$1 AND metadata->>'brief_type'='mail_template' AND created_at>=$2
            GROUP BY 1""", org_id, cutoff)
        content = await conn.fetchrow("""
            SELECT (SELECT count(*) FROM clients   WHERE org_id=$1) AS clients_total,
                   (SELECT count(*) FROM documents WHERE org_id=$1 AND created_at>=$2) AS docs_window""",
            org_id, cutoff)

    def _rows(rs):
        return [dict(r) for r in rs]

    def _users(rs):
        out = []
        for r in rs:
            d = dict(r)
            d["last_active"] = d["last_active"].isoformat() if d.get("last_active") else None
            out.append(d)
        return out

    return {
        "days": days, "include_qa": include_qa,
        "logins_by_day": _rows(logins_by_day),
        "active_by_day": _rows(active_by_day),
        "events_by_day": _rows(events_by_day),
        "usage_by_surface": _rows(usage_by_surface),
        "per_user": _users(per_user),
        "agent_runs": _rows(agent_runs),
        "contact_log": dict(contact) if contact else {},
        "outreach": {r["status"]: r["n"] for r in outreach},
        "content": dict(content) if content else {},
    }


# ---------------------------------------------------------------------------
# Per-rep digests — admin review & send (Insights → Rep digests)
# ---------------------------------------------------------------------------

@router.get("/api/digests")
async def list_digests(status: str = "pending", user: dict = Depends(current_user)):
    """Generated per-rep digests for admin review. status: pending | sent | all."""
    _require_db()
    _require_admin(user)
    async with db_module._pool.acquire() as conn:
        # Only the newest digest per rep is reviewable/sendable — older runs pile up
        # as history and must not be shown or sent. DISTINCT ON keeps the latest per
        # rep, then the status filter is applied to that current version.
        rows = await conn.fetch("""
            SELECT * FROM (
                SELECT DISTINCT ON (metadata->>'rep_user_id')
                       id, content,
                       metadata->>'rep_name' AS rep_name,
                       metadata->>'rep_email' AS rep_email,
                       COALESCE(metadata->>'digest_status','pending') AS status,
                       metadata->>'generated_date' AS generated_date,
                       metadata->>'sent_at' AS sent_at,
                       metadata->>'client_count' AS client_count,
                       created_at
                FROM documents
                WHERE org_id=$1 AND metadata->>'brief_type'='rep_digest'
                ORDER BY metadata->>'rep_user_id',
                         metadata->>'generated_date' DESC, created_at DESC
            ) latest
            WHERE ($2='all' OR latest.status=$2)
            ORDER BY created_at DESC LIMIT 100""", user["org_id"], status)
    import mailer
    return {
        "smtp_configured": mailer.smtp_configured(),
        "digests": [{**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                    for r in rows],
    }


@router.get("/api/reps")
async def list_reps(user: dict = Depends(current_user)):
    """Org members (reps) for the admin broadcast tool. QA accounts excluded."""
    _require_db()
    _require_admin(user)
    users = await db_module.list_users(user["org_id"])
    reps = [
        {"id": u["id"], "name": u.get("display_name") or u.get("username"),
         "email": (u.get("email") or "").strip(), "role": u.get("role")}
        for u in users if u["id"] not in (7, 8)
    ]
    reps.sort(key=lambda r: (r["role"] == "admin", (r["name"] or "").lower()))
    import mailer
    return {"smtp_configured": mailer.smtp_configured(), "reps": reps}


@router.post("/api/reps/broadcast")
async def broadcast_to_reps(body: dict, user: dict = Depends(current_user)):
    """Email a general update / tip to selected reps. Admin-only.
    Body: { subject, message, user_ids?: [int] }  (user_ids omitted = all reps)."""
    _require_db()
    _require_admin(user)
    subject = (body.get("subject") or "").strip()
    message = (body.get("message") or "").strip()
    if not subject or not message:
        raise HTTPException(status_code=400, detail="subject and message are required")

    import html as _html
    import mailer
    users = await db_module.list_users(user["org_id"])
    targets = [u for u in users if u["id"] not in (7, 8)]
    ids = body.get("user_ids")
    if ids:
        idset = {int(x) for x in ids}
        targets = [u for u in targets if u["id"] in idset]

    def _render(rep_name: str) -> str:
        # Reps are German — greet in German. Body is whatever the admin/agent wrote.
        safe = _html.escape(message).replace("\n", "<br>")
        return (f"<p>Hallo {_html.escape(rep_name)},</p>"
                f"<div style=\"font-size:14px;line-height:1.6\">{safe}</div>"
                f"<p style=\"color:#888;font-size:12px\">— Buzzowl</p>")

    sent, skipped = 0, []
    for u in targets:
        email = (u.get("email") or "").strip()
        rep_name = u.get("display_name") or u.get("username") or "there"
        if not email:
            skipped.append(f"{rep_name} (no email)")
            continue
        try:
            ok, msg = mailer.send_email(email, subject, _render(rep_name))
            if ok:
                sent += 1
            else:
                skipped.append(f"{rep_name} ({msg})")
        except Exception as exc:
            skipped.append(f"{rep_name} (error: {exc})")
    try:
        db_module.log_prompt(user["org_id"], user["id"], "rep_broadcast", subject,
                             {"sent": sent, "skipped": len(skipped)})
    except Exception:
        pass
    return {"sent": sent, "skipped_count": len(skipped), "skipped": skipped}


@router.post("/api/reps/broadcast/draft")
async def draft_broadcast(body: dict, user: dict = Depends(current_user)):
    """Let the agent draft/polish a tip email for reps. Admin-only.
    Body: { topic?, message? } — a topic to write about and/or a rough draft to improve."""
    _require_db()
    _require_admin(user)
    topic = (body.get("topic") or "").strip()
    current = (body.get("message") or "").strip()
    if not topic and not current:
        raise HTTPException(status_code=400, detail="topic or message required")

    instruction = (
        "You are helping a sales manager write a short internal email to their sales reps "
        "for a sales-intelligence tool called Buzzowl. Write a friendly, concise, "
        "practical message (max ~160 words) — a tip or update they can act on. Plain, warm, "
        "no marketing fluff, no emoji spam. "
        f"Topic / intent: {topic or 'improve the draft below into a clear, actionable tip'}.\n"
        + (f"\nRough draft to improve:\n{current}\n" if current else "")
        + "\nThe message is the BODY ONLY: do NOT add a greeting line (no 'Hi team') "
        "or a sign-off/signature (no 'Thanks', no '[Your name]') — those are added "
        "automatically. Just the body paragraphs.\n"
        + "Return ONLY valid JSON of the form {\"subject\": \"...\", \"message\": \"...\"} "
        "with \\n for line breaks in message. Do not include anything outside the JSON."
    )

    import asyncio
    import re as _re
    from routers.knowledge import _call_brain_sync
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, lambda: _call_brain_sync(instruction))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI draft failed: {exc}")

    subject, message = "", ""
    try:
        m = _re.search(r"\{.*\}", text, _re.S)
        data = json.loads(m.group(0) if m else text)
        subject = (data.get("subject") or "").strip()
        message = (data.get("message") or "").strip()
    except Exception:
        message = (text or "").strip()  # fallback: raw text as the message body
    if not message:
        raise HTTPException(status_code=502, detail="AI returned an empty draft")
    return {"subject": subject, "message": message}


@router.get("/api/eval/user/{user_id}")
async def eval_user_detail(user_id: int, days: int = 90, limit: int = 1000, user: dict = Depends(current_user)):
    """Per-person drill-down: chronological action timeline + prompts + per-surface
    breakdown, for the admin Insights detail page."""
    _require_db()
    _require_admin(user)
    org_id = user["org_id"]
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with db_module._pool.acquire() as conn:
        u = await conn.fetchrow(
            "SELECT id, COALESCE(display_name, username) AS name, role, email "
            "FROM users WHERE id=$1 AND org_id=$2", user_id, org_id)
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        total = await conn.fetchval(
            "SELECT count(*) FROM prompt_log WHERE org_id=$1 AND user_id=$2 AND created_at>=$3",
            org_id, user_id, cutoff)
        by_surface = await conn.fetch(
            "SELECT surface, count(*) AS n FROM prompt_log "
            "WHERE org_id=$1 AND user_id=$2 AND created_at>=$3 GROUP BY 1 ORDER BY 2 DESC",
            org_id, user_id, cutoff)
        events = await conn.fetch(
            "SELECT surface, prompt, context, created_at FROM prompt_log "
            "WHERE org_id=$1 AND user_id=$2 AND created_at>=$3 ORDER BY created_at DESC LIMIT $4",
            org_id, user_id, cutoff, min(limit, 5000))
    return {
        "user": dict(u),
        "days": days,
        "total_events": total,
        "by_surface": [dict(r) for r in by_surface],
        "events": [{"surface": r["surface"], "prompt": r["prompt"],
                    "context": r["context"] if isinstance(r["context"], dict) else {},
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                   for r in events],
    }


@router.post("/api/digests/{doc_id}/send")
async def send_digest(doc_id: int, user: dict = Depends(current_user)):
    """Email one rep's digest via SMTP and mark it sent. Admin-triggered."""
    _require_db()
    _require_admin(user)
    doc = await db_module.get_document_by_int_id(user["org_id"], doc_id)
    if not doc or (doc.get("metadata") or {}).get("brief_type") != "rep_digest":
        raise HTTPException(status_code=404, detail="Digest not found")
    meta = doc.get("metadata") or {}
    to = (meta.get("rep_email") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="This rep has no email address on file")
    import mailer
    if not mailer.smtp_configured():
        raise HTTPException(status_code=503, detail="SMTP not configured (set smtp_host / smtp_from)")
    ok, msg = mailer.send_email(to, f"Your client update — {meta.get('generated_date','')}", doc.get("content") or "")
    if not ok:
        raise HTTPException(status_code=502, detail=f"Send failed: {msg}")
    meta["digest_status"] = "sent"
    meta["sent_at"] = datetime.now(timezone.utc).isoformat()
    async with db_module._pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET metadata = $3 WHERE id = $1 AND org_id = $2",
            doc_id, user["org_id"], meta,
        )
    return {"ok": True, "to": to}
