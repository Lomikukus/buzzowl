"""Match router — proactive product-client matching via Pi."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import llm
from routers.auth import current_user

logger = logging.getLogger("wk.match")
router = APIRouter()

# ── config — use shared context so .env overrides (AGENT_SERVICE_TOKEN) are applied ──

try:
    from context import config, db_module
    DB_AVAILABLE = db_module is not None
except Exception:
    config = {}  # type: ignore
    db_module = None  # type: ignore
    DB_AVAILABLE = False


# ── request models ───────────────────────────────────────────────────────────

class MatchRunRequest(BaseModel):
    client_name: str
    research_brain: Optional[str] = None
    research_model: Optional[str] = None
    # Field names shipped before the rename — still accepted from older clients.
    hermes_brain: Optional[str] = None
    hermes_model: Optional[str] = None
    pi_brain: Optional[str] = None
    pi_model: Optional[str] = None


# ── helpers ──────────────────────────────────────────────────────────────────

async def _fire_pain_point_research(
    org_id: int,
    client_name: str,
    research_brain: str,
    research_model: str,
    pi_brain: str,
    pi_model: str,
) -> dict:
    """Fire a pain_point_research run on Pi for the given client."""
    pi_url = config.get("agent_service_url_pi", "http://localhost:8001")
    server_url = config.get("server_url", "http://host.docker.internal:8000")

    from routers.agents import _PAIN_POINT_RESEARCH_TEMPLATE, _watch_agent_service_run
    task = _PAIN_POINT_RESEARCH_TEMPLATE.format(client_name=client_name)

    run_id = await db_module.create_agent_run(
        org_id=org_id,
        agent_type="pain_point_research",
        task=task[:500],
        trigger_type="manual",
        triggered_by=None,
    )

    # Store pi_brain/pi_model in the DB now so the callback still has them later
    await db_module.update_agent_run(
        run_id, "pending",
        output={"_pi_brain": pi_brain, "_pi_model": pi_model},
    )

    payload = {
        "task": task,
        "agent_type": "pain_point_research",
        "org_id": org_id,
        "provider": llm.provider_for_brain(research_brain),
        "brain": research_brain,
        "model": research_model,
        "subject": client_name,
        "callback_url": f"{server_url}/api/agents/callback",
    }

    svc_token = config.get("agent_service_token", "")
    svc_headers = {"Authorization": f"Bearer {svc_token}"} if svc_token else {}

    async with httpx.AsyncClient(timeout=30) as hc:
        resp = await hc.post(f"{pi_url}/runs", json=payload, headers=svc_headers)
        resp.raise_for_status()
        data = resp.json()
        svc_id = data.get("run_id") or data.get("id")

    await db_module.update_agent_run(
        run_id, "running",
        output={"service_run_id": svc_id, "service_url": pi_url,
                "_pi_brain": pi_brain, "_pi_model": pi_model},
    )

    asyncio.create_task(_watch_agent_service_run(run_id, pi_url, svc_id, subject=client_name))

    # Update client metadata to reflect in-progress state
    async with db_module._pool.acquire() as conn:
        await conn.execute(
            "UPDATE clients SET metadata = metadata || $2 "
            "WHERE org_id = $1 AND name ILIKE $3",
            org_id, {"match_status": "researching", "match_run_id": str(run_id),
                     "match_error": None}, client_name,
        )

    logger.info("Fired pain_point_research run_id=%s svc_run=%s for client=%s org=%d",
                run_id, svc_id, client_name, org_id)
    return {"run_id": run_id, "svc_run_id": svc_id}


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post("/api/match/run")
async def run_match(body: MatchRunRequest, user: dict = Depends(current_user)):
    """Trigger a full match cycle for a client (Pi pain-point research → Pi synthesis)."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    research_brain = (body.research_brain or body.hermes_brain
                      or config.get("match_brain", config.get("research_brain", "openrouter")))
    research_model = (body.research_model or body.hermes_model
                      or config.get("match_research_model", config.get("research_model", "deepseek/deepseek-v4-flash")))
    pi_brain = body.pi_brain or config.get("match_brain", "openrouter")
    pi_model = body.pi_model or config.get("match_model", "deepseek/deepseek-v4-pro")

    org_id = user["org_id"]

    # Verify client exists
    async with db_module._pool.acquire() as conn:
        client_row = await conn.fetchrow(
            "SELECT id, name FROM clients WHERE org_id = $1 AND name ILIKE $2 LIMIT 1",
            org_id, body.client_name,
        )
    if not client_row:
        raise HTTPException(status_code=404, detail=f"Client '{body.client_name}' not found")

    client_name = client_row["name"]

    try:
        result = await _fire_pain_point_research(
            org_id, client_name, research_brain, research_model, pi_brain, pi_model
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Agent service unavailable: {exc}")

    return {"run_id": result["run_id"], "status": "researching", "client_name": client_name}


@router.get("/api/match/status/{client_name:path}")
async def get_match_status(client_name: str, user: dict = Depends(current_user)):
    """Return the latest match run status for a client."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    org_id = user["org_id"]
    status_row = await db_module.get_client_match_status(org_id, client_name)

    if not status_row or not status_row.get("match_status"):
        return {"status": "none", "client_name": client_name}

    # If status is "done" check report exists
    report_doc_id = None
    if status_row.get("match_status") == "done":
        reports = await db_module.get_match_reports(org_id, client_name)
        if reports:
            report_doc_id = reports[0]["id"]

    return {
        "client_name": client_name,
        "status": status_row.get("match_status"),
        "match_updated_at": status_row.get("match_updated_at"),
        "match_run_id": status_row.get("match_run_id"),
        "report_doc_id": report_doc_id,
    }


@router.get("/api/match/reports")
async def list_match_reports(client_name: Optional[str] = None, user: dict = Depends(current_user)):
    """List all match_report documents for the org."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    reports = await db_module.get_match_reports(user["org_id"], client_name=client_name)
    return {"reports": [
        {
            "id": r["id"],
            "client_name": r.get("client_name"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "agent_run_id": r.get("agent_run_id"),
        }
        for r in reports
    ]}


@router.get("/api/match/reports/{client_name:path}")
async def get_match_report(client_name: str, user: dict = Depends(current_user)):
    """Return the latest match_report document for a client."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    reports = await db_module.get_match_reports(user["org_id"], client_name=client_name)
    if not reports:
        raise HTTPException(status_code=404, detail="No match report found for this client")

    r = reports[0]
    meta = r.get("metadata") or {}
    return {
        "client_name": r.get("client_name") or client_name,
        "content": r.get("content") or "",
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "run_id": r.get("agent_run_id"),
        "doc_id": r["id"],
        "match_feedback": meta.get("match_feedback") or {},
    }


# Match report H2 headings look like: "## ✓ Strong Fit [8/10]: IBM Verify"
_FIT_HEADING = re.compile(
    r"^#{1,3}\s*[^A-Za-z]*\s*(Strong Fit|Potential Fit|Not a Fit)\s*\[(\d+)\s*/\s*10\]\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)


@router.get("/api/clients/{client_name:path}/match-summary")
async def client_match_summary(client_name: str, user: dict = Depends(current_user)):
    """Per-product fit scores for a client, parsed from its latest match report —
    so the client page can show product fit inline without opening /match."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    reports = await db_module.get_match_reports(user["org_id"], client_name=client_name)
    if not reports:
        return {"client_name": client_name, "products": [], "report_doc_id": None, "created_at": None}
    r = reports[0]
    products = []
    for fit, score, product in _FIT_HEADING.findall(r.get("content") or ""):
        products.append({"product": product.strip(), "fit": fit, "score": int(score)})
    products.sort(key=lambda p: -p["score"])
    return {
        "client_name": r.get("client_name") or client_name,
        "report_doc_id": r["id"],
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "products": products,
    }


@router.post("/api/match/reset/{client_name:path}")
async def reset_match(client_name: str, user: dict = Depends(current_user)):
    """Clear a stuck match_status so a fresh run can be triggered."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    org_id = user["org_id"]
    async with db_module._pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE clients SET metadata = metadata - 'match_status' - 'match_error' - 'match_run_id' "
            "WHERE org_id = $1 AND name ILIKE $2",
            org_id, client_name,
        )
    updated = int(result.split()[-1]) if result else 0
    if not updated:
        raise HTTPException(status_code=404, detail=f"Client '{client_name}' not found")
    logger.info("Reset match_status for client=%s org=%d", client_name, org_id)
    return {"reset": True, "client_name": client_name}


@router.get("/api/match/debug/{client_name:path}")
async def debug_match(client_name: str, user: dict = Depends(current_user)):
    """Return full pipeline chain state for troubleshooting a match run."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    org_id = user["org_id"]
    pi_url = config.get("agent_service_url_pi", "http://localhost:8001")
    svc_token = config.get("agent_service_token", "")
    svc_headers = {"Authorization": f"Bearer {svc_token}"} if svc_token else {}

    # Client metadata
    async with db_module._pool.acquire() as conn:
        client_row = await conn.fetchrow(
            "SELECT metadata FROM clients WHERE org_id = $1 AND name ILIKE $2 LIMIT 1",
            org_id, client_name,
        )
    if not client_row:
        raise HTTPException(status_code=404, detail=f"Client '{client_name}' not found")

    metadata = dict(client_row["metadata"] or {})
    match_status = metadata.get("match_status")
    match_run_id = metadata.get("match_run_id")
    match_error = metadata.get("match_error")

    # Recent agent runs for this client
    async with db_module._pool.acquire() as conn:
        runs = await conn.fetch(
            "SELECT id, agent_type, status, output, error, created_at, completed_at "
            "FROM agent_runs WHERE org_id = $1 AND task ILIKE $2 "
            "ORDER BY created_at DESC LIMIT 6",
            org_id, f"%{client_name}%",
        )

    runs_out = []
    pi_live = None
    for r in runs:
        out = dict(r["output"] or {})
        svc_run_id = out.get("service_run_id")
        entry = {
            "run_id": r["id"],
            "agent_type": r["agent_type"],
            "status": r["status"],
            "tool_calls_count": len(out.get("tool_calls", [])) if isinstance(out.get("tool_calls"), list) else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r.get("completed_at") else None,
            "error": r["error"],
            "svc_run_id": svc_run_id,
        }
        # Live check Pi for the most recent pain_point_research run
        if r["agent_type"] == "pain_point_research" and svc_run_id and pi_live is None:
            try:
                async with httpx.AsyncClient(timeout=5.0) as hc:
                    hr = await hc.get(f"{pi_url}/runs/{svc_run_id}", headers=svc_headers)
                    hdata = hr.json()
                pi_live = {
                    "reachable": True,
                    "status": hdata.get("status"),
                    "tool_calls_count": len(hdata.get("tool_calls") or []),
                    "error": hdata.get("error"),
                }
            except Exception as exc:
                pi_live = {"reachable": False, "error": str(exc)}
        runs_out.append(entry)

    return {
        "client_name": client_name,
        "match_status": match_status,
        "match_run_id": match_run_id,
        "match_error": match_error,
        "pi_live": pi_live,
        "agent_runs": runs_out,
    }
