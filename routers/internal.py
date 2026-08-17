"""
routers/internal.py — Internal action endpoints for the Pi agent service.

Called exclusively by agent services (Pi) — never by the browser UI.
Auth: Bearer {agent_service_token}, fail-closed. If no token is configured the
internal APIs are DISABLED (401) unless the explicit dev backdoor
ALLOW_INSECURE_INTERNAL=1 is set in the environment.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from context import DB_AVAILABLE, cache_clear, config, db_module
from routers.pipeline import _trigger_osint, _trigger_research

logger = logging.getLogger("wk.internal")

router = APIRouter(prefix="/api/internal")


def insecure_internal_allowed() -> bool:
    """Explicit dev backdoor: ALLOW_INSECURE_INTERNAL=1 disables internal auth.

    Checked at request time so tests (and operators) can toggle it via env.
    """
    return os.environ.get("ALLOW_INSECURE_INTERNAL", "") == "1"


def _check_token(request: Request) -> None:
    """Fail-closed service-token check for all /api/internal/* endpoints.

    - Token configured  → require exactly `Authorization: Bearer {token}`.
    - Token empty       → 401 ALWAYS, unless ALLOW_INSECURE_INTERNAL=1 (dev).
    """
    token = config.get("agent_service_token", "")
    if not token:
        if insecure_internal_allowed():
            return
        raise HTTPException(
            status_code=401,
            detail="Internal APIs disabled: agent_service_token is not configured",
        )
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != token:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# POST /api/internal/clients
# ---------------------------------------------------------------------------

@router.post("/clients")
async def internal_create_client(body: dict, request: Request):
    """Create or upsert a client. Fires OSINT + research side effects.

    Body: { org_id: int, name: str, metadata?: dict }
    """
    _check_token(request)
    cache_clear()

    org_id: Optional[int] = body.get("org_id")
    name: str = (body.get("name") or "").strip()
    metadata: dict = body.get("metadata") or {}

    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    embedding = await db_module.embed_text(f"{name} {metadata.get('industry', '')}")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    client_id = await db_module.upsert_client(
        org_id=org_id,
        name=name,
        metadata=metadata,
        embedding=embedding,
        date_str=date_str,
        created_by=None,
    )

    asyncio.create_task(_trigger_osint(name, org_id))
    asyncio.create_task(_trigger_research(name, org_id))

    logger.info("internal: created client '%s' (id=%d) for org=%d", name, client_id, org_id)
    return {"ok": True, "id": client_id, "name": name}


# ---------------------------------------------------------------------------
# POST /api/internal/bulk-research
# ---------------------------------------------------------------------------

@router.post("/bulk-research")
async def internal_bulk_research(body: dict, request: Request):
    """Kick server-side research+OSINT for many clients in an org (admin/backfill).

    Body: { org_id: int, names?: [str], only_missing?: bool (default true) }
    Returns the list of clients queued; work runs in the background on the server.
    """
    _check_token(request)

    org_id: Optional[int] = body.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    from routers.knowledge import resolve_bulk_research_targets, _bulk_research_queue

    names = await resolve_bulk_research_targets(
        org_id, body.get("names"), body.get("only_missing", True)
    )
    if names:
        asyncio.create_task(_bulk_research_queue(list(names), org_id))
    logger.info("internal: bulk-research queued %d client(s) for org=%d", len(names), org_id)
    return {"ok": True, "queued": len(names), "names": names}


# ---------------------------------------------------------------------------
# PATCH /api/internal/clients/{name}
# ---------------------------------------------------------------------------

@router.patch("/clients/{name}")
async def internal_update_client(name: str, body: dict, request: Request):
    """Merge-update client metadata.

    Body: { org_id: int, patch: dict }
    """
    _check_token(request)

    org_id: Optional[int] = body.get("org_id")
    patch: dict = body.get("patch") or body.get("metadata") or {}

    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    updated = await db_module.update_client_metadata(org_id, name, patch)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Client '{name}' not found")

    logger.info("internal: updated client '%s' for org=%d", name, org_id)
    return updated


# ---------------------------------------------------------------------------
# POST /api/internal/contacts
# ---------------------------------------------------------------------------

@router.post("/contacts")
async def internal_create_contact(body: dict, request: Request):
    """Create or upsert a contact, optionally linked to a client.

    Body: { org_id: int, name: str, client?: str, role?: str, email?: str, metadata?: dict }
    """
    _check_token(request)
    cache_clear()

    org_id: Optional[int] = body.get("org_id")
    name: str = (body.get("name") or "").strip()
    client_name: Optional[str] = (body.get("client") or "").strip() or None
    metadata: dict = body.get("metadata") or {}

    if body.get("role"):
        metadata["role"] = body["role"]
    if body.get("email"):
        metadata["email"] = body["email"]
    if body.get("linkedin_url"):
        metadata["linkedin_url"] = body["linkedin_url"]

    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    client_id: Optional[int] = None
    if client_name:
        client_row = await db_module.get_client(org_id, client_name)
        client_id = client_row["id"] if client_row else None

    embedding = await db_module.embed_text(
        f"{name} {metadata.get('role', '')} {client_name or ''}"
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    contact_id = await db_module.upsert_contact(
        org_id=org_id,
        name=name,
        metadata=metadata,
        embedding=embedding,
        client_id=client_id,
        date_str=date_str,
        created_by=None,
    )

    logger.info("internal: created contact '%s' (id=%d) for org=%d", name, contact_id, org_id)
    return {"ok": True, "id": contact_id, "name": name}


# ---------------------------------------------------------------------------
# POST /api/internal/find-people
# ---------------------------------------------------------------------------

@router.post("/find-people")
async def internal_find_people(body: dict, request: Request):
    """Start a role-targeted people-search run for a client.

    Body: { org_id: int, client_name: str, target_roles?: str, user_id?: int }
    Returns the created agent-run id. Mirrors POST /api/agents/find-people but
    token-authed for the Pi agent service (no user JWT).
    """
    _check_token(request)

    org_id: Optional[int] = body.get("org_id")
    client_name: str = (body.get("client_name") or "").strip()
    target_roles: str = (body.get("target_roles") or "").strip()

    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    if not client_name:
        raise HTTPException(status_code=400, detail="client_name is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    from routers.agents import _start_people_search

    try:
        result = await _start_people_search(
            org_id, client_name,
            target_roles=target_roles,
            user_id=body.get("user_id"),
            trigger_type="chat",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Agent service unavailable: {exc}")

    logger.info("internal: people-search for '%s' (run=%s) org=%d", client_name, result.get("run_id"), org_id)
    return result


# ---------------------------------------------------------------------------
# POST /api/internal/tasks
# ---------------------------------------------------------------------------

@router.post("/tasks")
async def internal_create_task(body: dict, request: Request):
    """Create a follow-up / to-do for a rep.

    Body: { org_id: int, title: str, user_id?: int, client_name?: str,
            due_date?: "YYYY-MM-DD", notes?: str }
    Returns the created task row.
    """
    _check_token(request)

    org_id: Optional[int] = body.get("org_id")
    title: str = (body.get("title") or "").strip()

    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    due_date = None
    if body.get("due_date"):
        from datetime import date as _date
        try:
            due_date = _date.fromisoformat(str(body["due_date"])[:10])
        except (ValueError, TypeError):
            due_date = None

    row = await db_module.create_task(
        org_id, body.get("user_id"), title,
        client_name=(body.get("client_name") or "").strip() or None,
        notes=(body.get("notes") or "").strip() or None,
        due_date=due_date, source="chat",
    )
    if not row:
        raise HTTPException(status_code=503, detail="Could not create the task")

    logger.info("internal: created task '%s' (id=%s) org=%d", title, row.get("id"), org_id)
    return row


# ---------------------------------------------------------------------------
# POST /api/internal/outreach/draft — agent-created outreach DRAFT
# ---------------------------------------------------------------------------

@router.post("/outreach/draft")
async def internal_outreach_draft(body: dict, request: Request):
    """Pi `draft_outreach` tool. Creates a DRAFT only — the state machine
    forbids the agent every further transition; a human must submit + approve.

    Gated server-side on the org's autonomy level (>= 3, 'outreach') so the
    tool cannot be smuggled in at lower levels even if a prompt asks for it.
    Body: {org_id, client_name, subject, body, to_email?, to_contact?,
           purpose?, agent_run_id?, sender_user_id?}
    """
    _check_token(request)
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    org_id: Optional[int] = body.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id required")

    import autonomy
    lvl = await autonomy.level(org_id)
    if lvl < autonomy.LEVEL_OUTREACH:
        raise HTTPException(
            status_code=403,
            detail=f"autonomy level {lvl} does not permit agent-drafted outreach (needs level 3)")

    client = (body.get("client_name") or body.get("client") or "").strip()
    subject = (body.get("subject") or "").strip()
    content = (body.get("body") or "").strip()
    if not client or not subject or not content:
        raise HTTPException(status_code=400, detail="client_name, subject and body are required")

    from routers.outreach import _create_draft
    view = await _create_draft(
        org_id, client=client, subject=subject, content=content,
        to_email=(body.get("to_email") or "").strip(),
        to_contact=(body.get("to_contact") or "").strip(),
        sender_user_id=body.get("sender_user_id"),
        created_by=None, source="agent",
        purpose=(body.get("purpose") or "").strip(),
        agent_run_id=body.get("agent_run_id"),
    )
    logger.info("agent outreach draft #%s for %r (org %s)", view.get("id"), client, org_id)
    return {"ok": True, "id": view.get("id"), "state": view.get("state"),
            "review_url": "/outreach"}


# ---------------------------------------------------------------------------
# GET /api/internal/system-status
# ---------------------------------------------------------------------------

@router.get("/system-status")
async def internal_system_status(org_id: int, request: Request):
    """Kanban view of recent agent_runs and research_tasks for the org.

    Query: ?org_id=N
    """
    _check_token(request)

    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    runs = await db_module.list_agent_runs(org_id, limit=20)
    tasks = await db_module.list_research_tasks(org_id)

    todo        = [r for r in runs if r["status"] == "pending"]
    in_progress = [r for r in runs if r["status"] == "running"]
    done        = [r for r in runs if r["status"] in ("done", "failed")][:10]

    # Only show root orchestrate tasks to keep the kanban clean
    rtasks_pending = [t for t in tasks if t["status"] == "pending"  and t.get("task_type") == "orchestrate"]
    rtasks_running = [t for t in tasks if t["status"] == "running"  and t.get("task_type") == "orchestrate"]
    rtasks_done    = [t for t in tasks if t["status"] == "done"     and t.get("task_type") == "orchestrate"][:5]

    def _slim_run(r: dict) -> dict:
        return {
            "id": r.get("id"),
            "agent_type": r.get("agent_type"),
            "task": (r.get("task") or "")[:80],
            "status": r.get("status"),
            "created_at": str(r.get("created_at") or "")[:19],
            "completed_at": str(r.get("completed_at") or "")[:19] or None,
            "error": r.get("error"),
        }

    def _slim_task(t: dict) -> dict:
        return {
            "id": t.get("id"),
            "subject": t.get("subject"),
            "status": t.get("status"),
            "created_at": str(t.get("created_at") or "")[:19],
            "completed_at": str(t.get("completed_at") or "")[:19] or None,
        }

    return {
        "agent_runs": {
            "todo":        [_slim_run(r) for r in todo],
            "in_progress": [_slim_run(r) for r in in_progress],
            "done":        [_slim_run(r) for r in done],
        },
        "research_queue": {
            "pending": [_slim_task(t) for t in rtasks_pending],
            "running": [_slim_task(t) for t in rtasks_running],
            "done":    [_slim_task(t) for t in rtasks_done],
        },
    }
