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
# Deals + timeline for the agent (Phase 4)
# ---------------------------------------------------------------------------

async def _resolve_client_id(org_id: int, client_name: str) -> Optional[dict]:
    """exact → trigram-similar → unique substring match ("acme" → "Acme Corp")."""
    c = await db_module.get_client(org_id, client_name)
    if not c:
        canon = await db_module.find_similar_client(org_id, client_name)
        c = await db_module.get_client(org_id, canon) if canon else None
    if not c:
        hits = await db_module.search_clients(org_id, client_name, limit=2)
        if len(hits) == 1:
            c = await db_module.get_client(org_id, hits[0]["name"])
    return c


@router.get("/deals")
async def internal_get_deals(org_id: int, request: Request, client_name: str = "",
                             status: str = "open", limit: int = 50):
    """Pi `get_deals` tool: pipeline read. Query: ?org_id=&client_name=&status=open|won|lost|all"""
    _check_token(request)
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    import deals as dl
    client_id = None
    if client_name.strip():
        c = await _resolve_client_id(org_id, client_name.strip())
        if not c:
            return {"deals": [], "note": f"no client matching {client_name!r}"}
        client_id = c["id"]
    st = None if status in ("", "all") else status
    rows = await db_module.list_deals(org_id, status=st, client_id=client_id, limit=min(max(limit, 1), 200))
    out = []
    for d in rows:
        out.append({
            "id": d["id"], "client": d.get("client_name"), "name": d["name"], "stage": d["stage"],
            "stage_label": (dl.stage_info(d["stage"]) or {}).get("label", d["stage"]),
            "status": d["status"], "value": d.get("value"), "currency": d.get("currency"),
            "probability": d.get("probability") if d.get("probability") is not None else dl.default_probability(d["stage"]),
            "weighted_value": round(dl.weighted_value(d.get("value"), d.get("probability"), d["stage"]), 2),
            "expected_close": d["expected_close"].isoformat() if d.get("expected_close") else None,
            "owner": d.get("owner_name"),
            "updated_at": d["updated_at"].isoformat() if d.get("updated_at") else None,
        })
    return {"deals": out, "stages": dl.stage_ids()}


@router.post("/deals/stage")
async def internal_update_deal_stage(body: dict, request: Request):
    """Pi `update_deal_stage` tool. Body: {org_id, deal_id | (client_name [+ deal_name]),
    stage, note?, agent_run_id?}.

    Gated server-side on autonomy level >= 2 ('act'). Agents may only move a deal
    between OPEN stages — closing (won/lost) and reopening stay human decisions.
    Every move is written to deal_events with actor_agent_run_id for the audit trail."""
    _check_token(request)
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    org_id: Optional[int] = body.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id required")

    import autonomy
    import deals as dl
    lvl = await autonomy.level(org_id)
    if lvl < autonomy.LEVEL_ACT:
        raise HTTPException(
            status_code=403,
            detail=f"autonomy level {lvl} does not permit agents to change deal stages (needs level 2)")

    try:
        stage = dl.validate_stage(body.get("stage") or "")
    except dl.DealError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if dl.status_for_stage(stage) != dl.STATUS_OPEN:
        raise HTTPException(status_code=403,
                            detail=f"agents may not close deals — moving to {stage!r} is a human decision")

    deal = None
    if body.get("deal_id"):
        deal = await db_module.get_deal(org_id, int(body["deal_id"]))
    elif (body.get("client_name") or "").strip():
        c = await _resolve_client_id(org_id, body["client_name"].strip())
        if not c:
            raise HTTPException(status_code=404, detail=f"no client matching {body['client_name']!r}")
        open_deals = await db_module.list_deals(org_id, status=dl.STATUS_OPEN, client_id=c["id"], limit=50)
        want = (body.get("deal_name") or "").strip().lower()
        if want:
            open_deals = [d for d in open_deals if want in d["name"].lower()]
        if len(open_deals) == 1:
            deal = open_deals[0]
        elif len(open_deals) > 1:
            raise HTTPException(status_code=409, detail="several open deals for this client — pass deal_id or deal_name: "
                                + ", ".join(f"#{d['id']} {d['name']}" for d in open_deals))
    if not deal:
        raise HTTPException(status_code=404, detail="deal not found")
    if deal["status"] != dl.STATUS_OPEN:
        raise HTTPException(status_code=403, detail=f"deal #{deal['id']} is {deal['status']} — reopening is a human decision")
    if deal["stage"] == stage:
        return {"ok": True, "id": deal["id"], "stage": stage, "unchanged": True}

    patch = {"stage": stage, "status": dl.STATUS_OPEN}
    if deal.get("probability") is None or deal.get("probability") == dl.default_probability(deal["stage"]):
        patch["probability"] = dl.default_probability(stage)
    run_id = body.get("agent_run_id")
    row = await db_module.update_deal(org_id, deal["id"], patch, actor_agent_run_id=int(run_id) if run_id else None,
                                      note=(body.get("note") or "").strip() or "moved by agent")
    logger.info("agent moved deal #%s %s → %s (org %s, run %s)", deal["id"], deal["stage"], stage, org_id, run_id)
    return {"ok": True, "id": row["id"], "name": row["name"], "from": deal["stage"], "stage": row["stage"],
            "probability": row.get("probability")}


@router.get("/timeline")
async def internal_client_timeline(org_id: int, client_name: str, request: Request, limit: int = 30):
    """Pi `get_client_timeline` tool: unified activity feed for one client."""
    _check_token(request)
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    c = await _resolve_client_id(org_id, client_name.strip())
    if not c:
        return {"items": [], "note": f"no client matching {client_name!r}"}
    rows = await db_module.client_timeline(org_id, c["id"], limit=min(max(limit, 1), 200))
    items = []
    for r in rows:
        ts = r.get("ts")
        items.append({"ts": ts.isoformat() if hasattr(ts, "isoformat") else ts, "kind": r.get("kind"),
                      "actor": r.get("actor"), "title": r.get("title"), "ref": r.get("ref")})
    return {"client": c["name"], "items": items}


# ---------------------------------------------------------------------------
# POST /api/internal/federation/inbound — Phase 5 SPIKE: card received over Matrix
# ---------------------------------------------------------------------------

@router.post("/federation/inbound")
async def internal_federation_inbound(body: dict, request: Request):
    """Store a client card received from a partner install as a read-only, badged
    `shared_external` document. NEVER merges into local clients — a human links or
    copies explicitly from the review queue. Remote content is untrusted: rendered
    as escaped markdown text, never HTML; agents get it only with provenance.
    Body: {org_id, card: {schema, kind, card_id, sender_org, client{...}, contacts[], ...},
           provenance: {room_id, event_id, sender, verified_device, ...}}"""
    _check_token(request)
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    org_id = body.get("org_id")
    card = body.get("card") or {}
    prov = body.get("provenance") or {}
    if not org_id or not isinstance(card, dict):
        raise HTTPException(status_code=400, detail="org_id and card required")
    if card.get("kind") != "client_card" or not isinstance(card.get("client"), dict):
        raise HTTPException(status_code=400, detail="unsupported card")
    client = card["client"]
    name = str(client.get("name") or "").strip()[:200]
    if not name:
        raise HTTPException(status_code=400, detail="card.client.name required")

    def _t(v, n=500):  # remote strings: plain text only, length-capped
        return str(v or "").replace("\r", " ").replace("\n", " ").strip()[:n]

    sender = _t(card.get("sender_org"), 120) or _t(prov.get("sender"), 120) or "partner"
    lines = [f"# {name} — shared by {sender}", "",
             f"_Received {_t(prov.get('received_at'), 40)} via Matrix room `{_t(prov.get('room_id'), 80)}`; "
             f"event `{_t(prov.get('event_id'), 80)}`; sender device verified: {prov.get('verified_device')}._", "",
             "## Profile"]
    for k in ("industry", "website", "location", "summary"):
        if client.get(k):
            lines.append(f"- **{k}**: {_t(client.get(k))}")
    contacts = [c for c in (card.get("contacts") or []) if isinstance(c, dict)][:50]
    if contacts:
        lines += ["", "## Contacts (shared)"]
        for c in contacts:
            lines.append("- " + " · ".join(_t(c.get(k), 120) for k in ("name", "role", "email") if c.get(k)))
    finds = [f for f in (card.get("findings_summary") or []) if isinstance(f, dict)][:50]
    if finds:
        lines += ["", "## Findings summary (as shared)"]
        for f in finds:
            lines.append(f"- {_t(f.get('date'), 20)} [{_t(f.get('type'), 30)}] {_t(f.get('title'), 200)}"
                         + (f" — source: {_t(f.get('source_url'), 300)}" if f.get("source_url") else ""))
    lines += ["", "## Sources", f"- Shared over Matrix by {sender} (card_id {_t(card.get('card_id'), 80)}); "
              "not verified locally — treat as partner-provided (unconfirmed)."]
    content = "\n".join(lines)
    doc_id = f"shared-{_t(card.get('card_id'), 60) or _t(prov.get('event_id'), 60)}"
    metadata = {
        "subject": name, "shared_by": sender, "share_scope": card.get("share_scope"),
        "card_id": card.get("card_id"), "schema": card.get("schema"),
        "federation": {k: prov.get(k) for k in ("room_id", "event_id", "sender", "server_ts",
                                                    "verified_device", "sender_key", "received_at")},
        "review_status": "pending",     # review queue: pending | linked | dismissed
        "untrusted_remote": True,
    }
    try:
        embedding = await db_module.embed_text(f"{name}\n{content[:2000]}")
    except Exception:
        embedding = []
    did = await db_module.index_document(
        org_id=int(org_id), doc_id=doc_id, doc_type="shared_external", title=f"Shared: {name} (from {sender})",
        content=content, metadata=metadata, embedding=embedding or [], source="federation")
    logger.info("federation inbound: card for %r from %s → doc %s (org %s)", name, sender, did, org_id)
    return {"ok": True, "document_id": did, "doc_id": doc_id, "review_status": "pending"}


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
