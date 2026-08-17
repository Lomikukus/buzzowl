"""Phase 28/29 benchmark — Pi agent benchmark on any agent type.

POST /api/test/benchmark/osint?client=Acme&task_type=osint fires the task
against the Pi agent service and polls to completion.

Supported task_type values: osint (default), product_research, pain_point_research

Use: curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/test/benchmark/osint?client=Acme&task_type=osint&max_wait=900"
"""
import asyncio
import unicodedata

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from context import DB_AVAILABLE, config, db_module

router = APIRouter()


def _ascii_name(name: str) -> str:
    """Convert German umlauts to ASCII digraphs for broader search coverage.
    Müllerhütte → Muellerhuette, Müller → Mueller, Bärenfänger → Baerenfaenger.
    English-language sources (Bloomberg, Reuters, LinkedIn) almost always use the ASCII form."""
    result = (
        name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
            .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
            .replace("ß", "ss")
    )
    # Catch any remaining diacritics (accented chars from other languages)
    return unicodedata.normalize("NFKD", result).encode("ascii", "ignore").decode()


def _services() -> dict[str, str]:
    return {
        "pi": config.get("agent_service_url_pi", "http://agent-pi:8001"),
    }


_TASK_TEMPLATES: dict[str, tuple[str, str]] = {
    # (agent_type, task_template)
    "osint": (
        "osint",
        "Produce a structured 7-section OSINT report on {client}: company overview, financial "
        "performance (exact figures), leadership, strategic direction, sales intelligence, recent "
        "news (last 6 months), and a numbered ## Sources list. Run several web_search angles, "
        "fetch_page at least 2 promising pages, save key facts as findings with source_url, then "
        "write_document(type='osint').",
    ),
    "product_research": (
        "product_research",
        "Map the full product portfolio of {client}: all products, pricing tiers, target market, "
        "competitive differentiators, and recent developments. Fetch the homepage and products "
        "page, run web_search for pricing and features, then write_document(type='research') "
        "with sections: ## Product Portfolio, ## Pricing Intelligence, ## Target Market, "
        "## Competitive Differentiators, ## Recent Developments, ## Sources.",
    ),
    "pain_point_research": (
        "pain_point_research",
        "Research pain points, strategic initiatives, and buying signals for {client}. Cover all "
        "10 required angles: strategic initiatives, regulatory pressures, operational pain points, "
        "budget signals, hiring signals, executive statements, conference talks, LinkedIn signals, "
        "earnings calls, analyst reports. Save each confirmed finding as write_document(type='finding') "
        "with source_url, and write_document(type='signal') for each confirmed pain point or opportunity.",
    ),
}


async def _resolve_org_id(client: str) -> int:
    if DB_AVAILABLE and db_module._pool:
        try:
            async with db_module._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT org_id FROM clients WHERE name ILIKE $1 OR similarity(name, $1) > 0.6 ORDER BY similarity(name, $1) DESC LIMIT 1", client,
                )
            if row:
                return int(row["org_id"])
        except Exception:
            pass
    return 1


async def _fire(variant: str, url: str, client: str, org_id: int, task_type: str = "osint") -> dict:
    agent_type, task_template = _TASK_TEMPLATES.get(task_type, _TASK_TEMPLATES["osint"])
    payload = {
        "agent_type": agent_type,
        "subject": client,
        # Use ASCII name in the task so web searches find English-language sources.
        # Agents store/link with the original name via subject= above.
        "task": task_template.format(client=_ascii_name(client)),
        "org_id": org_id,
    }
    token = config.get("agent_service_token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as c:
            r = await c.post(f"{url}/runs", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            return {"variant": variant, "run_id": data.get("run_id"),
                    "status": data.get("status", "queued")}
    except Exception as e:
        return {"variant": variant, "run_id": None, "status": "error", "error": str(e)}


async def _poll_until_done(variant: str, url: str, run_id: int,
                            poll_interval: float = 10.0, max_wait: float = 900.0) -> dict:
    """Poll GET /runs/{run_id} until done/failed or max_wait seconds exceeded."""
    token = config.get("agent_service_token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
                r = await c.get(f"{url}/runs/{run_id}", headers=headers)
                r.raise_for_status()
                data = r.json()
                status = data.get("status", "running")
                if status in ("done", "failed"):
                    return {
                        "final_status": status,
                        "tool_calls": len(data.get("tool_calls") or []),
                        "output": data.get("output", {}),
                    }
        except Exception:
            pass
    return {"final_status": "timeout"}


@router.post("/api/test/benchmark/osint")
async def benchmark_osint(
    request: Request,
    client: str = Query(..., description="Client/company name, e.g. Acme"),
    task_type: str = Query(default="osint", description="Agent task type: osint, product_research, pain_point_research"),
    delay: int = Query(default=30, description="Seconds to wait between variants (Camofox cooldown)"),
    max_wait: int = Query(default=900, description="Max seconds to poll each variant for completion"),
):
    token = config.get("agent_service_token", "")
    if token and request.headers.get("Authorization", "") != f"Bearer {token}":
        raise HTTPException(401, "unauthorized")
    if task_type not in _TASK_TEMPLATES:
        raise HTTPException(400, f"unknown task_type '{task_type}'; valid: {list(_TASK_TEMPLATES)}")

    org_id = await _resolve_org_id(client)
    services = _services()

    # Fire variants sequentially: each must finish before the next starts.
    results = []
    variants_list = list(services.items())
    for i, (variant, url) in enumerate(variants_list):
        fired = await _fire(variant, url, client, org_id, task_type)
        if fired["run_id"] is not None:
            poll_result = await _poll_until_done(
                variant, url, fired["run_id"],
                max_wait=float(max_wait),
            )
            fired.update(poll_result)
        results.append(fired)
        if i < len(variants_list) - 1:
            await asyncio.sleep(delay)

    if all(r.get("run_id") is None for r in results):
        raise HTTPException(502, {"error": "all variants failed to start", "results": results})

    by_variant = {r["variant"]: r for r in results}
    run_ids = {f"{v}_run_id": by_variant.get(v, {}).get("run_id") for v in services}
    ids = [str(r["run_id"]) for r in results if r.get("run_id") is not None]
    compare_sql = (
        "SELECT r.id AS run_id, r.agent_type, d.type, LENGTH(d.content) AS chars, "
        "jsonb_array_length(r.tool_calls) AS tool_calls, r.status, "
        "r.completed_at - r.created_at AS duration "
        "FROM agent_runs r LEFT JOIN documents d ON d.agent_run_id = r.id "
        f"WHERE r.id IN ({', '.join(ids) or 'NULL'}) ORDER BY chars DESC NULLS LAST;"
    )
    findings_sql = (
        "SELECT d.agent_run_id, d.type, COUNT(*) AS doc_count, SUM(LENGTH(d.content)) AS total_chars "
        "FROM documents d "
        f"WHERE d.agent_run_id IN ({', '.join(ids) or 'NULL'}) "
        "GROUP BY d.agent_run_id, d.type ORDER BY d.agent_run_id, d.type;"
    )
    return {
        "client": client,
        "task_type": task_type,
        "org_id": org_id,
        **run_ids,
        "results": results,
        "compare_sql": compare_sql,
        "findings_sql": findings_sql,
    }
