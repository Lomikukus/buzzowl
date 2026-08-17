"""
Agents router — agent runs, deep research queue, and the /ws/agents WebSocket.

Covers:
- Manual agent triggering (POST /api/agents/run)
- Agent run history and status queries
- Research queue management (enqueue, list, cancel, pause/resume)
- Finding feedback
- /ws/agents WebSocket: live event stream from agents.events
"""

import asyncio
import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

import llm
from routers.auth import _limit

from context import DB_AVAILABLE, config, console, db_module, cache_get, cache_set
from routers.auth import current_user

logger = logging.getLogger("wk.agents")

router = APIRouter()


def _ascii_name(name: str) -> str:
    """ü→ue, ö→oe, ä→ae, ß→ss + strip remaining diacritics.
    English-language sources (Reuters, Bloomberg, LinkedIn) almost always use
    the ASCII form, so searches for 'Müllerhütte' miss far more than
    searches for 'Muellerhuette'."""
    result = (
        name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
            .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
            .replace("ß", "ss")
    )
    return unicodedata.normalize("NFKD", result).encode("ascii", "ignore").decode()


# Placeholder used when an org has no products at all — keeps the match
# synthesis from silently aborting on a fresh org.
_NO_PRODUCTS_PLACEHOLDER = (
    "(No products configured — provide a general assessment of seller capabilities)"
)


def _format_match_product_catalog(products: list) -> str:
    """Format the seller's product catalog for the client-product MATCH prompt.

    Pure/synchronous so it can be unit-tested without a DB. Returns a
    placeholder string when there are no products so the synthesis still
    produces a best-effort report instead of aborting."""
    if not products:
        return _NO_PRODUCTS_PLACEHOLDER
    return "\n".join(
        f"- **{p['name']}** ({p.get('category') or 'general'}): "
        f"{(p.get('description') or '(no description)')[:300]}"
        + (f" | Features: {', '.join(p['key_features'][:4])}" if p.get("key_features") else "")
        + (f" | Target: {p['target_customer']}" if p.get("target_customer") else "")
        for p in products
    )


async def _fetch_match_products(org_id: int) -> list:
    """Product catalog that feeds the client-product MATCH: FOCUS products only,
    so deliberately non-focus products (e.g. WatsonX) are never matched/scored.
    Falls back to ALL products only when the org has zero focus products, so a
    fresh org that hasn't picked focus products yet isn't broken."""
    products = await db_module.list_products(org_id, focus_only=True)
    if not products:
        products = await db_module.list_products(org_id)
    return products


# ---------------------------------------------------------------------------
# Agent service (Pi / Hermes) helpers
# ---------------------------------------------------------------------------

_RESEARCH_TASK_TEMPLATE = (
    "Research {subject} in depth for a B2B sales context. Prioritise 2025 and 2026 information. Cover all of: "
    "(1) Industry classification — identify the company's primary industry using a standard category "
    "(e.g. Automotive, Software/SaaS, Healthcare, Financial Services, Manufacturing, Retail, "
    "Logistics, Consulting, Energy, Telecoms, Media). "
    "Call update_client with {{\"industry\": \"<category>\"}} to save this to the client record. "
    "(2) Financials — most recent revenue, operating profit, R&D spend, headcount, and growth trajectory. "
    "Prefer official press releases and earnings reports over secondary sources. "
    "(3) Leadership & decision-makers — CEO, CFO, CTO, CRO, VP Sales/Marketing with LinkedIn profiles. "
    "Note executive changes in the last 12 months. Who is the economic buyer for technology purchases? "
    "(4) Strategic priorities — product roadmap, recent M&A, key investments, digital transformation programmes. "
    "(5) Technology & vendors — known tech stack, current ERP/CRM/cloud vendors, outsourcing partners. "
    "Any public RFPs, tenders, or vendor consolidation signals. "
    "(6) Sales intelligence signals — stated pain points, budget signals, hiring surges in relevant departments, "
    "regulatory pressures, competitive threats, public complaints or analyst criticism. "
    "(7) Recent news from the last 12 months — prefer Reuters, Bloomberg, TechCrunch, or the company newsroom. "
    "Discard anything dated before 2024. "
    "Every claim must include the source URL. Discard pages with no verifiable source URL. "
    "Write individual findings as you go (type='finding'). "
    "End with a comprehensive final report (type='research') including a ## Sources section."
)


_PRODUCT_RESEARCH_TASK_TEMPLATE = (
    "Research {company_name} ({website_url}) in depth. Primary goal: map ALL products and SaaS offerings. "
    "{product_hints_line}"
    "For each product: name, category (SaaS/API/SDK/On-premise/Service/Hardware), key features, "
    "target customer, pricing model, differentiators, and — critically — the OFFICIAL product page URL "
    "(the canonical URL on the vendor's own site for that specific product, e.g. ibm.com/products/concert). "
    "Search YouTube for demo and presentation videos — call fetch_youtube_transcript on any found. "
    "Output sections: ## Product Portfolio (per-product breakdown — include Official URL: <url> for each), "
    "## Pricing Intelligence, ## Target Market, ## Competitive Differentiators, ## Recent Developments, "
    "## Sources (every URL consulted, one per line)."
)

_PRODUCT_DEEP_RESEARCH_TASK_TEMPLATE = (
    "Deep research on {company_name}'s specific products: {product_list}. "
    "Focus: technical capabilities, recent updates, competitive positioning, customer case studies, pricing details. "
    "For EACH product, find and include its official product page URL (the canonical vendor URL for that product). "
    "Search YouTube: '{company_name} {{product}} demo 2024 2025', '{company_name} {{product}} walkthrough'. "
    "Call fetch_youtube_transcript on any found demo videos. "
    "Fetch product pages, release notes, customer testimonials. "
    "Output detailed profiles for each product. Each profile must include 'Official URL: <url>'. "
    "End with ## Sources listing every URL consulted, one per line."
)

_PRODUCT_EXTRACTION_PROMPT = (
    "Given this research report about a company's products and SaaS offerings, extract a structured list.\n"
    'Return JSON only: {{"products": [{{"name": "...", "category": "SaaS|SDK|API|On-premise|Service|Hardware", '
    '"description": "1-2 sentence description", "key_features": ["feature1", "feature2"], '
    '"pricing_info": "pricing details or null", "target_customer": "target customer or null", '
    '"website_url": "official product page URL or null"}}]}}\n'
    "For website_url: look for lines like 'Official URL:', 'Product page:', or any canonical vendor URL "
    "specific to that product (e.g. ibm.com/products/concert). Use null if not found.\n"
    "{requested_line}"
    "Return ONLY valid JSON. No markdown, no explanation, no code fences.\n\nReport:\n{document_content}"
)


def _norm_prod(name: str) -> str:
    """Normalise a product name for matching: lowercase, drop a leading 'IBM',
    drop parentheticals, collapse whitespace. Lets 'IBM Verify Access' match
    'IBM Verify Access (IBM Verify Identity Access)' etc."""
    import re as _re
    n = (name or "").lower()
    n = _re.sub(r"\(.*?\)", "", n)
    n = _re.sub(r"^ibm\s+", "", n)
    n = _re.sub(r"\s+", " ", n).strip()
    return n

_PAIN_POINT_RESEARCH_TEMPLATE = (
    "Research {client_name} specifically for B2B sales intelligence. Run ALL of the following search angles — "
    "save each confirmed finding as write_document(type='finding') with source_url as you go:\n"
    "(1) Strategic initiatives — '{client_name} digital transformation cloud migration M&A 2024 2025 2026'. "
    "Look for new market entries, ESG commitments, major product launches.\n"
    "(2) Regulatory pressures — '{client_name} EU AI Act DORA NIS2 compliance deadline 2025 2026'. "
    "Find specific laws, mandates, audit requirements, certification deadlines.\n"
    "(3) Operational pain points — '{client_name} job posting site:linkedin.com cloud AI compliance engineer'. "
    "Also search '{client_name} vendor frustration legacy system modernisation'.\n"
    "(4) Budget and investment signals — '{client_name} CAPEX OPEX technology investment budget 2025 2026'. "
    "Search for RFPs, tenders, board-approved tech initiatives, cost-cutting programmes.\n"
    "(5) Executive statements — '{client_name} CEO CTO CRO CFO priorities 2025 2026 interview'. "
    "Fetch press releases, earnings calls, and conference talks.\n"
    "(6) YouTube/conference talks — '{client_name} CEO interview conference 2025 site:youtube.com', "
    "'{client_name} CTO keynote 2025'. Call fetch_youtube_transcript on any found video.\n"
    "(7) Earnings call transcripts — '{client_name} earnings call transcript Q4 2025 Q1 2026'. "
    "Extract CFO/CEO guidance, cost mentions, and technology priorities.\n"
    "(8) Analyst reports — '{client_name} Gartner Forrester IDC analyst 2025 2026'. "
    "Also search '{client_name} industry analyst report technology investment'.\n"
    "(9) LinkedIn executive posts — '{client_name} CTO CEO LinkedIn post 2025 2026 strategy'.\n"
    "(10) Recent news — '{client_name} news 2025 2026 site:reuters.com OR site:bloomberg.com OR "
    "site:techcrunch.com'. Discard anything before 2024.\n"
    "Every claim MUST cite a source URL. Discard any unverifiable claims. "
    "End with a structured ## Pain Points & Opportunities section ranking each signal "
    "high/medium/low by confidence with its source URL."
)

_MATCH_SYNTHESIS_TEMPLATE = (
    "You are a senior B2B sales strategist. You have been given confirmed research about {client_name}'s "
    "pain points, strategic initiatives, regulatory pressures, and buying signals. "
    "Each research finding below ends with 'Source: <URL>' — these are the real URLs you MUST use "
    "as clickable links in your report. Do NOT invent or change any URLs.\n\n"
    "You also have the seller's complete product catalog below.\n\n"
    "SELLER PRODUCTS:\n{product_list}\n\n"
    "PAIN POINT RESEARCH SUMMARY (each finding includes its source URL):\n{pain_point_summary}\n\n"
    "{hiring_signals}"
    "Produce a detailed product-client match report. For EACH seller product:\n"
    "(1) Assign a fit score 1–10 using this rubric:\n"
    "    10 = explicit budget signal + confirmed matching initiative with direct evidence\n"
    "    7-8 = strong indirect evidence or regulatory mandate that clearly applies\n"
    "    5-6 = plausible fit, circumstantial signals\n"
    "    3-4 = weak or speculative signal\n"
    "    1-2 = no supporting evidence found\n"
    "(2) Assess fit category: Strong Fit (score 7-10), Potential Fit (score 4-6), Not a Fit (score 1-3).\n"
    "(3) If STRONG or POTENTIAL FIT: cite the specific evidence with a clickable Markdown link "
    "[text](URL) using the exact source URL from the findings. Name the best contact person by "
    "name and role if known. Suggest a specific opening angle — one sentence the seller could send.\n"
    "(4) If NOT A FIT: say so in one sentence and briefly explain why. Do not invent reasons.\n\n"
    "Output format — use EXACTLY these headers (include the score):\n"
    "  '## ✓ Strong Fit [score/10]: [Product Name]' — evidence + contact + opening angle\n"
    "  '## ~ Potential Fit [score/10]: [Product Name]' — probable need, less certain evidence\n"
    "  '## ✗ Not a Fit [score/10]: [Product Name]' — one sentence why\n\n"
    "End with:\n"
    "## Recommended Actions\n"
    "Top 3 outreach opportunities ranked by score. For each: contact name + role, "
    "specific approach angle, and a suggested opening message the seller can send immediately.\n\n"
    "## Sources\n"
    "List every source URL as a Markdown link: [page title or domain](URL), one per line. "
    "Use ONLY URLs that appear in the research findings above — do not invent any.\n\n"
    "IMPORTANT: Call write_document(type='match_report', "
    "title='Match: {client_name} — {{today_date}}') with the complete report. "
    "Include client_name='{client_name}' in the write_document call."
)



def _get_service_url(agent_type: str) -> str:
    return config.get("agent_service_url_pi", config.get("agent_service_url", "http://localhost:8001"))


async def _fire_agent_service(
    subject: str,
    org_id: int,
    brain: str,
    model: str,
    task: Optional[str] = None,
    callback_url: Optional[str] = None,
    agent_type: str = "research",
) -> tuple[str, int]:
    """POST a run to the correct agent service. Returns (svc_url, run_id)."""
    svc_url = _get_service_url(agent_type)
    if callback_url is None:
        server_url = config.get("server_url", "http://host.docker.internal:8000")
        callback_url = f"{server_url}/api/agents/callback"
    payload = {
        "agent_type": agent_type,
        # Use ASCII form in the task string so agents find English-language sources
        # (Bloomberg, Reuters, LinkedIn omit umlauts). subject= keeps the canonical
        # name for DB client lookup and document linking.
        "task": task or _RESEARCH_TASK_TEMPLATE.format(subject=_ascii_name(subject)),
        "org_id": org_id,
        "subject": subject,
        # provider is the canonical field; brain stays one release for
        # not-yet-rebuilt Pi containers (Pi maps brain→provider itself).
        "provider": llm.provider_for_brain(brain or config.get("agent_service_brain", "openrouter")),
        "brain": brain or config.get("agent_service_brain", "openrouter"),
        "model": model or config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
        "callback_url": callback_url,
    }
    headers = {}
    token = config.get("agent_service_token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{svc_url}/runs", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    return svc_url, int(data["run_id"])


def _clean_tool_call(t: dict) -> dict:
    """Slim a raw tool call for DB storage — trim large content fields."""
    args = t.get("args") or {}
    if isinstance(args, dict):
        args = {k: (str(v)[:120] if k == "content" else v) for k, v in args.items()}
    return {
        "tool": t.get("tool", ""),
        "args": args if isinstance(args, dict) else {},
        "result": str(t.get("result", ""))[:200],
        "ts": t.get("ts", ""),
    }


async def _recover_orphaned_run(db_run_id: int, subject: Optional[str], retries_left: int = 1) -> bool:
    """A delegated run vanished because the agent service restarted (a deploy
    restarts agent-pi, dropping its in-memory runs). The work wasn't persisted,
    so re-fire it once on the now-running service rather than silently losing the
    cycle's research. Returns True when a replacement run was started.

    Guarded by output.refired so a permanently-flapping service can't loop, and
    requires `subject` so the re-fire can rebuild a valid request — without it we
    can't reconstruct the run, so the caller fails the row instead.
    """
    if retries_left <= 0 or not subject or not DB_AVAILABLE:
        return False
    run = await db_module.get_agent_run(db_run_id)
    if not run:
        return False
    out = run.get("output")
    if isinstance(out, dict) and out.get("refired"):
        return False  # already retried once
    try:
        new_url, new_svc = await _fire_agent_service(
            subject, run["org_id"],
            brain=config.get("agent_service_brain", "openrouter"),
            model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
            task=run.get("task"), agent_type=run.get("agent_type") or "osint",
        )
    except Exception as exc:
        logger.warning("Re-fire of orphaned run %d failed (service still down?): %s", db_run_id, exc)
        return False
    await db_module.update_agent_run(
        db_run_id, "running", output={"service_run_id": new_svc, "refired": True},
    )
    asyncio.create_task(
        _watch_agent_service_run(db_run_id, new_url, new_svc, subject=subject, retries_left=retries_left - 1)
    )
    logger.info("Re-fired orphaned run %d after agent service restart → svc_run %s", db_run_id, new_svc)
    return True


async def _watch_agent_service_run(
    db_run_id: int, svc_url: str, svc_run_id: int,
    subject: Optional[str] = None, retries_left: int = 1,
) -> None:
    """Background: poll agent service until terminal, then update agent_runs row.

    Stops early if the callback endpoint already marked the run terminal. If the
    service restarts mid-run (poll returns 404 — run gone from its memory), the
    run is recovered via _recover_orphaned_run (re-fired once) when `subject` is
    known, else the row is failed cleanly instead of polling a dead id forever.
    """
    missing_polls = 0
    while True:
        await asyncio.sleep(5)
        # Fast-exit: callback may have already updated the DB row
        if DB_AVAILABLE:
            try:
                existing = await db_module.get_agent_run(db_run_id)
                if existing and existing.get("status") in ("done", "failed"):
                    # Callback landed but didn't include tool_calls — do one final fetch
                    # to persist the complete tool_calls before stopping.
                    try:
                        _tok = config.get("agent_service_token", "")
                        _ph = {"Authorization": f"Bearer {_tok}"} if _tok else {}
                        async with httpx.AsyncClient(timeout=10.0) as _hc:
                            _r = await _hc.get(f"{svc_url}/runs/{svc_run_id}", headers=_ph)
                            _r.raise_for_status()
                            _data = _r.json()
                        final_tcs = [_clean_tool_call(t) for t in (_data.get("tool_calls") or [])]
                        if final_tcs:
                            await db_module.update_agent_run(
                                db_run_id, existing["status"], tool_calls=final_tcs
                            )
                    except Exception:
                        pass
                    logger.debug("Watcher stopping — run %d already terminal (callback landed)", db_run_id)
                    break
            except Exception:
                pass
        try:
            poll_headers = {}
            _tok = config.get("agent_service_token", "")
            if _tok:
                poll_headers["Authorization"] = f"Bearer {_tok}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{svc_url}/runs/{svc_run_id}", headers=poll_headers)
            # 404 = the service is up but has no record of this run → it restarted
            # and lost it (a down service raises ConnectionError, handled below, so
            # we keep waiting through a restart rather than reacting to a blip).
            if r.status_code == 404:
                missing_polls += 1
                if missing_polls >= 2:
                    if await _recover_orphaned_run(db_run_id, subject, retries_left):
                        return  # a fresh watcher took over the re-fired run
                    await db_module.update_agent_run(
                        db_run_id, "failed",
                        error="agent service restarted before the run finished",
                    )
                    logger.warning("Run %d lost to agent service restart (no retry context)", db_run_id)
                    return
                continue
            r.raise_for_status()
            missing_polls = 0
            data = r.json()
            status = data.get("status", "running")
            tool_calls = [_clean_tool_call(t) for t in (data.get("tool_calls") or [])]
            if status in ("done", "failed", "timeout", "cancelled"):
                await db_module.update_agent_run(
                    db_run_id,
                    "done" if status == "done" else "failed",
                    tool_calls=tool_calls,
                    output={"service_run_id": svc_run_id, **(data.get("output") or {})},
                    error=data.get("error"),
                )
                logger.info("Agent service run %s finished → db_run %d: %s", svc_run_id, db_run_id, status)
                # After enrichment or people_search, fire a contact_extraction second pass
                if status == "done" and DB_AVAILABLE:
                    try:
                        run_info = await db_module.get_agent_run(db_run_id)
                        if run_info and run_info.get("agent_type") in ("enrichment", "people_search"):
                            await _trigger_contact_extraction(run_info.get("task", ""), run_info["org_id"])
                    except Exception as cx_err:
                        logger.warning("Contact extraction trigger failed: %s", cx_err)
                break
            else:
                await db_module.update_agent_run(db_run_id, "running", tool_calls=tool_calls)
        except Exception as exc:
            logger.warning("Agent service poll error for run %s: %s", svc_run_id, exc)


async def reattach_orphaned_watchers() -> None:
    """On server startup, re-attach a watcher to every delegated run still marked
    running/queued. Their in-process watchers died with the previous server
    process; without this the rows hang forever (or, when the agent service also
    restarted in the same deploy, are never reconciled now that the service only
    cleans up its OWN rows). The re-attached watcher decides each run's fate:
    still executing on the service → keep tracking and let the callback land;
    gone (404) → fail cleanly. Subject is left unset so no run is re-fired blindly
    here — auto re-fire is reserved for the live watcher, which knows the subject.
    """
    if not DB_AVAILABLE or not getattr(db_module, "_pool", None):
        return
    try:
        async with db_module._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, agent_type, output FROM agent_runs
                   WHERE status IN ('running', 'queued')
                     AND trigger_type <> 'external_service'
                     AND output ? 'service_run_id'"""
            )
    except Exception as exc:
        logger.warning("orphaned-watcher reconcile query failed: %s", exc)
        return
    n = 0
    for row in rows:
        out = row["output"] if isinstance(row["output"], dict) else {}
        svc_run_id = out.get("service_run_id")
        if svc_run_id is None:
            continue
        svc_url = out.get("service_url") or _get_service_url(row["agent_type"] or "")
        try:
            asyncio.create_task(
                _watch_agent_service_run(int(row["id"]), svc_url, int(svc_run_id))
            )
            n += 1
        except (TypeError, ValueError):
            continue
    if n:
        logger.info("Re-attached watchers to %d in-flight delegated run(s) after restart", n)


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

async def _run_agent_background(
    run_id: int, org_id: int, agent_type: str, task: str,
    client_name: str, sample_size: Optional[int] = None,
) -> None:
    try:
        from agents.runner import run_agent
        await run_agent(run_id, org_id, agent_type, task,
                        client_name=client_name or None, sample_size=sample_size)
    except Exception as exc:
        console.print(f"[red]Agent run {run_id} failed: {exc}[/red]")
        return
    # After enrichment completes, fire a contact_extraction second pass via Pi
    if agent_type == "enrichment":
        await _trigger_contact_extraction(task, org_id)


async def _trigger_contact_extraction(enrichment_task: str, org_id: int, company: Optional[str] = None) -> None:
    """Parse the subject from a task string and fire a Pi contact_extraction run."""
    if not company:
        import re as _re
        m = _re.search(r"Companies:\s*([^,\n]+)", enrichment_task)
        if not m:
            # Fallback: look for "for <Company>" pattern used by people_search tasks
            m = _re.search(r"for\s+([A-Z][^\.\n,]{2,40})", enrichment_task)
        if not m:
            return
        company = m.group(1).strip()
    if not company:
        return
    try:
        cx_run_id = await db_module.create_agent_run(
            org_id=org_id, agent_type="contact_extraction",
            task=f"Extract contacts from recent research findings for {company}.",
            trigger_type="event_hook",
        )
        cx_svc_url, cx_svc_run_id = await _fire_agent_service(
            subject=company, org_id=org_id,
            brain=config.get("agent_service_brain", "openrouter"),
            model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
            task=f"Extract contacts from recent research findings for {company}.",
            agent_type="contact_extraction",
        )
        await db_module.update_agent_run(
            cx_run_id, "running",
            output={"service_run_id": cx_svc_run_id, "service_url": cx_svc_url},
        )
        asyncio.create_task(_watch_agent_service_run(cx_run_id, cx_svc_url, cx_svc_run_id, subject=company))
        logger.info("Contact extraction run %d started for %s (post-embedded-enrichment)", cx_run_id, company)
    except Exception as cx_err:
        logger.warning("Contact extraction trigger failed: %s", cx_err)


@router.post("/api/agents/run")
async def trigger_agent_run(body: dict, user: dict = Depends(current_user)):
    agent_type  = body.get("agent_type", "research").strip()
    task        = body.get("task", "").strip()
    client_name = body.get("client_name", "").strip()
    sample_size = body.get("sample_size")  # optional int, used by org agent for testing
    if sample_size is not None:
        sample_size = int(sample_size)

    if not task:
        raise HTTPException(status_code=400, detail="task is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type=agent_type,
        task=task, trigger_type="manual", triggered_by=user["id"],
    )
    asyncio.create_task(_run_agent_background(
        run_id, user["org_id"], agent_type, task, client_name, sample_size=sample_size,
    ))
    return {"run_id": run_id, "status": "pending"}


@router.get("/api/agents/tasks/{run_id}")
async def get_agent_task(run_id: int, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    run = await db_module.get_agent_run(run_id, user["org_id"])
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.get("/api/agents/tasks/{run_id}/docs")
async def get_agent_run_docs(run_id: int, user: dict = Depends(current_user)):
    """Return documents written by this agent run."""
    if not DB_AVAILABLE:
        return {"docs": []}
    docs = await db_module.list_documents_by_run_id(user["org_id"], run_id)
    return {"docs": docs}


@router.get("/api/agents/runs")
async def list_agent_run_history(user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"runs": [], "count": 0}
    runs = await db_module.list_agent_runs(user["org_id"])
    return {"runs": runs, "count": len(runs)}


@router.get("/api/agents/service-runs")
async def list_service_runs(user: dict = Depends(current_user)):
    """Return recent Pi/Hermes agent_runs with full tool_calls for the dashboard."""
    if not DB_AVAILABLE or not db_module._pool:
        return {"runs": []}
    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_type, status, task, trigger_type,
                   created_at, completed_at, error,
                   tool_calls, output
            FROM agent_runs
            WHERE org_id = $1
              AND output ? 'service_run_id'
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user["org_id"],
        )
    import json as _json
    runs = []
    for r in rows:
        d = dict(r)
        # asyncpg returns jsonb as string when not decoded — decode if needed
        for field in ("tool_calls", "output"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = _json.loads(d[field])
                except Exception:
                    d[field] = []
        d["created_at"] = str(d["created_at"]) if d.get("created_at") else None
        d["completed_at"] = str(d["completed_at"]) if d.get("completed_at") else None
        runs.append(d)
    return {"runs": runs}


# ---------------------------------------------------------------------------
# Agent service callback
# ---------------------------------------------------------------------------

@router.post("/api/agents/callback")
async def agent_service_callback(body: dict, request: Request):
    """Receives completion push from Pi / Hermes agent service containers.

    The agent service fires this on run completion so the main server doesn't
    need to poll. The watcher loop still runs as a fallback but stops early
    when it sees the run is already terminal.

    Auth is fail-closed: no configured agent_service_token means the endpoint
    is disabled (401), unless the ALLOW_INSECURE_INTERNAL=1 dev backdoor is set.
    """
    token = config.get("agent_service_token", "")
    if not token:
        if os.environ.get("ALLOW_INSECURE_INTERNAL", "") != "1":
            raise HTTPException(
                status_code=401,
                detail="Internal APIs disabled: agent_service_token is not configured",
            )
    else:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    svc_run_id  = body.get("run_id")
    status      = body.get("status", "done")
    agent_type  = body.get("agent_type", "research")
    subject     = body.get("subject", "")
    output      = body.get("output") or {}
    stale_clients = body.get("stale_clients") or output.get("stale_clients") or []
    error       = body.get("error")

    if not svc_run_id:
        raise HTTPException(status_code=400, detail="run_id required")
    if not DB_AVAILABLE:
        return {"ok": True, "message": "DB unavailable, callback noted"}

    # org_id from callback payload (sent by Hermes since this session)
    org_id: Optional[int] = body.get("org_id") or None
    if org_id:
        try:
            org_id = int(org_id)
        except (TypeError, ValueError):
            org_id = None

    # Map svc_run_id → db_run_id (the row the main server created)
    db_run_id = None
    prior_output: dict = {}
    if db_module._pool:
        async with db_module._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, org_id, output FROM agent_runs WHERE output->>'service_run_id' = $1 ORDER BY id DESC LIMIT 1",
                str(svc_run_id),
            )
        if row:
            db_run_id = row["id"]
            org_id = org_id or row["org_id"]  # prefer DB value if available
            try:
                import json as _pj
                prior_output = row["output"] if isinstance(row["output"], dict) else _pj.loads(row["output"] or "{}")
            except Exception:
                prior_output = {}

    final_status = "done" if status == "done" else "failed"
    if db_run_id:
        # Merge, don't replace: preserve fire-time fields the later callbacks need
        # (requested_products for product research, _pi_brain/_pi_model for match).
        await db_module.update_agent_run(
            db_run_id, final_status,
            output={**prior_output, "service_run_id": svc_run_id, **output},
            error=error,
        )

    # After enrichment or people_search: fire contact_extraction via Pi
    if final_status == "done" and db_run_id and org_id:
        try:
            run_info = await db_module.get_agent_run(db_run_id)
            if run_info and run_info.get("agent_type") in ("enrichment", "people_search"):
                asyncio.create_task(
                    _trigger_contact_extraction(run_info.get("task", ""), org_id, company=subject or None)
                )
                logger.info("Callback: queued contact_extraction for subject=%s (db_run=%d)", subject, db_run_id)
        except Exception as cx_err:
            logger.warning("Callback contact_extraction trigger failed: %s", cx_err)

    # Monitor callback: fire research for each stale client
    if final_status == "done" and agent_type == "monitor" and stale_clients and org_id:
        logger.info("Monitor callback: firing research for %d stale clients", len(stale_clients))
        # Autonomy (Phase 2): the monitor's stale list IS an agent judgement. At
        # level >= 2 each auto-fire is budgeted (daily cap + cooldown) and stamped
        # trigger_type='autonomous'; below that it keeps the legacy heartbeat
        # provenance so manual monitor runs behave as before.
        import autonomy
        auto_level = await autonomy.level(org_id)
        for client_name in stale_clients:
            child_trigger = "heartbeat"
            if auto_level >= autonomy.LEVEL_ACT:
                client_row = None
                try:
                    client_row = await db_module.get_client(org_id, client_name)
                except Exception:
                    pass
                budget = await autonomy.check_budget(org_id, client_row)
                if not budget.ok:
                    logger.info("Monitor: skipping '%s' — %s", client_name, budget.reason)
                    await autonomy.record_decision(org_id, autonomy.DecisionContext(
                        seam="monitor_stale", client_name=client_name,
                        signals=["monitor agent flagged as stale"], allowed_actions=("skip", "research")),
                        autonomy.Decision(action="skip", reason=budget.reason, source="budget"))
                    continue
                child_trigger = autonomy.TRIGGER
            try:
                svc_url, child_svc_run_id = await _fire_agent_service(
                    client_name, org_id,
                    brain=config.get("agent_service_brain", "openrouter"),
                    model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
                    agent_type="research",
                )
                child_run_id = await db_module.create_agent_run(
                    org_id=org_id, agent_type="research",
                    task=f"Research: {client_name}", trigger_type=child_trigger,
                )
                if child_trigger == autonomy.TRIGGER:
                    await autonomy.mark_client_acted(org_id, client_name)
                await db_module.update_agent_run(
                    child_run_id, "running",
                    output={"service_run_id": child_svc_run_id, "service_url": svc_url},
                )
                asyncio.create_task(_watch_agent_service_run(child_run_id, svc_url, child_svc_run_id, subject=client_name))
                logger.info("Monitor: queued research for '%s' (svc_run=%d)", client_name, child_svc_run_id)
            except Exception as exc:
                logger.warning("Monitor: research trigger failed for '%s': %s", client_name, exc)

    # Product research callback: extract product catalog from research report
    if final_status == "done" and agent_type == "product_research" and org_id:
        asyncio.create_task(_handle_product_research_callback(org_id, svc_run_id, subject))

    # Product deep research callback: create Pi verification chat session
    if final_status == "done" and agent_type == "product_deep_research" and org_id:
        asyncio.create_task(_handle_product_deep_research_callback(org_id, svc_run_id, subject))

    # Pain point research complete → fire match_synthesis via Pi.
    # Also attempt synthesis on "failed" if the run saved findings before dying (e.g. mid-run 403).
    if agent_type == "pain_point_research" and org_id and subject and (
        final_status == "done" or (final_status == "failed" and output.get("findings_saved", 0) > 0)
    ):
        # Read pi_brain/pi_model from the DB run record (stored before Hermes call — Pydantic drops extra fields)
        _stored_out: dict = {}
        if db_run_id:
            try:
                _run_row = await db_module.get_agent_run(db_run_id)
                _stored_out = (_run_row.get("output") or {}) if _run_row else {}
            except Exception:
                pass
        pi_brain = _stored_out.get("_pi_brain") or output.get("_pi_brain") or config.get("match_brain", "openrouter")
        pi_model = _stored_out.get("_pi_model") or output.get("_pi_model") or config.get("match_model", "deepseek/deepseek-v4-pro")
        asyncio.create_task(_handle_pain_point_callback(org_id, svc_run_id, subject, pi_brain, pi_model))

    # match_synthesis complete → update client metadata + notify
    if final_status == "done" and agent_type == "match_synthesis" and org_id and subject:
        asyncio.create_task(_handle_match_synthesis_callback(org_id, svc_run_id, subject))

    # research/osint complete → generate brief → then auto-queue pain_point_research (proactive matching)
    if final_status == "done" and agent_type in ("research", "osint") and org_id and subject:
        asyncio.create_task(_brief_then_match(org_id, subject))

    # Telegram push on success
    if final_status == "done" and subject:
        try:
            import datetime as _dt
            import notifications as _notify
            if agent_type == "monitor":
                _notify.notify(
                    f"🔍 *Monitor complete*\n"
                    f"🔄 Research queued for {len(stale_clients)} stale client(s)"
                    + (f": {', '.join(stale_clients[:3])}" if stale_clients else "")
                )
            elif agent_type in ("research", "osint", "product_research", "product_deep_research") and db_module._pool and org_id:
                # Fetch the report document written during this run and send as .md file
                async with db_module._pool.acquire() as _conn:
                    _rdoc = await _conn.fetchrow(
                        "SELECT content FROM documents WHERE org_id=$1 AND agent_run_id=$2 AND type IN ('research','osint') ORDER BY created_at DESC LIMIT 1",
                        org_id, int(svc_run_id),
                    )
                    _findings = await _conn.fetch(
                        "SELECT title, content, metadata FROM documents WHERE org_id=$1 AND agent_run_id=$2 AND type='finding'",
                        org_id, int(svc_run_id),
                    )
                    _signals = await _conn.fetch(
                        "SELECT title, content, metadata FROM documents WHERE org_id=$1 AND agent_run_id=$2 AND type='signal'",
                        org_id, int(svc_run_id),
                    )
                if _rdoc:
                    _notify.notify_research_report(
                        subject=subject,
                        findings=[dict(r) for r in _findings],
                        signals=[dict(r) for r in _signals],
                        synthesized_report=_rdoc["content"],
                        today=_dt.date.today().isoformat(),
                    )
                else:
                    findings_count = output.get("findings_saved", 0)
                    _notify.notify(f"✅ *{agent_type.title()} complete: {subject}*\n📄 {findings_count} document(s) written")
            else:
                findings_count = output.get("findings_saved", output.get("documents_written", 0))
                _notify.notify(
                    f"✅ *{agent_type.title()} complete: {subject}*\n"
                    f"📄 {findings_count} document(s) written"
                )
        except Exception as _exc:
            logger.warning("Telegram notification failed: %s", _exc)

    logger.info("Callback: svc_run=%s → db_run=%s agent_type=%s status=%s subject=%s",
                svc_run_id, db_run_id, agent_type, final_status, subject)
    return {"ok": True, "db_run_id": db_run_id}


@router.post("/api/agents/internal/run")
async def internal_trigger_run(body: dict, request: Request):
    """Internal endpoint: Pi agent requests a child Pi run mid-reasoning.

    Token-authenticated only — no user session required.
    Body: { agent_type, subject, task?, org_id?, brain?, model? }
    Returns: { run_id, svc_run_id, status }
    """
    token = config.get("agent_service_token", "")
    if token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    agent_type = (body.get("agent_type") or "research").strip()
    subject    = (body.get("subject") or "").strip()
    task       = (body.get("task") or "").strip()
    brain      = (body.get("brain") or "").strip()
    model      = (body.get("model") or "").strip()

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    # Resolve org_id: explicit → client lookup → first org
    org_id: Optional[int] = None
    try:
        org_id = int(body.get("org_id") or 0) or None
    except (TypeError, ValueError):
        pass
    if not org_id and db_module._pool:
        async with db_module._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT org_id FROM clients WHERE lower(name) = lower($1) ORDER BY id LIMIT 1",
                subject,
            )
        if row:
            org_id = row["org_id"]
    if not org_id:
        org = await db_module.get_first_org()
        if not org:
            raise HTTPException(status_code=503, detail="No org found")
        org_id = org["id"]

    if not task:
        task = _RESEARCH_TASK_TEMPLATE.format(subject=subject)

    db_run_id = await db_module.create_agent_run(
        org_id=org_id,
        agent_type=agent_type,
        task=task[:500],
        trigger_type="agent",
    )

    try:
        svc_url, svc_run_id = await _fire_agent_service(
            subject=subject,
            org_id=org_id,
            brain=brain,
            model=model,
            task=task,
            agent_type=agent_type,
        )
    except Exception as exc:
        await db_module.update_agent_run(db_run_id, "failed", error=str(exc))
        raise HTTPException(status_code=503, detail=f"Agent service unavailable: {exc}")

    await db_module.update_agent_run(
        db_run_id, "running",
        output={"service_run_id": svc_run_id, "service_url": svc_url},
    )
    asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=subject))

    logger.info("internal_trigger_run: agent_type=%s subject=%s db_run=%d svc_run=%d",
                agent_type, subject, db_run_id, svc_run_id)
    return {"run_id": db_run_id, "svc_run_id": svc_run_id, "status": "running"}


async def _handle_product_research_callback(org_id: int, svc_run_id, company_name: str) -> None:
    """Extract product catalog from Hermes research report, create draft products."""
    try:
        async with db_module._pool.acquire() as conn:
            doc = await conn.fetchrow(
                "SELECT id, content FROM documents WHERE org_id=$1 AND agent_run_id=$2 "
                "AND type='research' ORDER BY created_at DESC LIMIT 1",
                org_id, int(svc_run_id),
            )
            run_row = await conn.fetchrow(
                "SELECT output FROM agent_runs WHERE output->>'service_run_id'=$1 ORDER BY id DESC LIMIT 1",
                str(svc_run_id),
            )

        run_output = (run_row["output"] if run_row else None) or {}
        seller_company_id = run_output.get("seller_company_id")
        if not seller_company_id:
            sc = await db_module.get_seller_company(org_id)
            seller_company_id = sc["id"] if sc else None
        if not seller_company_id:
            logger.warning("product_research callback: no seller_company_id for org %d", org_id)
            await db_module.update_seller_company_status(org_id, "products_found")
            return

        products_created = 0
        if doc:
            import json as _json
            import re as _re
            from routers.pipeline import _call_pipeline_brain  # lazy import — avoid circular at module load
            prompt = _PRODUCT_EXTRACTION_PROMPT.format(document_content=doc["content"][:8000], requested_line="")
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, _call_pipeline_brain, prompt)
            raw = _re.sub(r'```\w*\n?', '', raw).strip()
            try:
                m = _re.search(r'\{.*\}', raw, flags=_re.DOTALL)
                if m:
                    data = _json.loads(m.group(0))
                    for p in data.get("products", []):
                        if p.get("name"):
                            pid = await db_module.create_product(
                                org_id=org_id,
                                seller_company_id=int(seller_company_id),
                                name=p["name"],
                                category=p.get("category"),
                                description=p.get("description"),
                                key_features=p.get("key_features") or [],
                                pricing_info=p.get("pricing_info"),
                                target_customer=p.get("target_customer"),
                                metadata={"source": "product_research", "agent_run_id": str(svc_run_id)},
                            )
                            extra = {}
                            if pid and doc:
                                extra["source_doc_id"] = doc["id"]
                            if pid and p.get("website_url"):
                                extra["website_url"] = p["website_url"]
                            if pid and extra:
                                await db_module.update_product(pid, org_id, extra)
                            products_created += 1
            except Exception as parse_err:
                logger.warning("Product extraction JSON parse error: %s — raw: %.200s", parse_err, raw)

        await db_module.update_seller_company_status(
            org_id, "products_found", research_doc_id=doc["id"] if doc else None
        )
        logger.info("product_research callback: %d products created for org %d", products_created, org_id)

        try:
            import notifications as _notify
            _notify.notify(
                f"🏭 *Product research complete: {company_name}*\n"
                f"📦 {products_created} products found — select your focus at /products"
            )
        except Exception:
            pass

    except Exception as exc:
        logger.error("_handle_product_research_callback failed: %s", exc, exc_info=True)
        try:
            await db_module.update_seller_company_status(org_id, "products_found")
        except Exception:
            pass


async def _handle_product_deep_research_callback(org_id: int, svc_run_id, company_name: str) -> None:
    """Extract enriched product data from deep research report, update stubs, create verification session."""
    try:
        # --- Step 1: Fetch research doc and update/create products ---
        async with db_module._pool.acquire() as conn:
            doc = await conn.fetchrow(
                "SELECT id, content FROM documents WHERE org_id=$1 AND agent_run_id=$2 "
                "AND type='research' ORDER BY created_at DESC LIMIT 1",
                org_id, int(svc_run_id),
            )

        sc = await db_module.get_seller_company(org_id)
        products_updated = 0

        # Which specific stubs did the seller ask to fill? Stored on the run at fire
        # time so we can write the researched data back onto exactly those products.
        requested_products: list = []
        try:
            async with db_module._pool.acquire() as conn:
                _rr = await conn.fetchrow(
                    "SELECT output FROM agent_runs WHERE output->>'service_run_id' = $1 ORDER BY id DESC LIMIT 1",
                    str(svc_run_id),
                )
            if _rr and _rr["output"]:
                import json as _j0
                _out = _rr["output"] if isinstance(_rr["output"], dict) else _j0.loads(_rr["output"])
                requested_products = _out.get("requested_products") or []
        except Exception:
            requested_products = []

        if doc:
            import json as _json
            import re as _re
            from routers.pipeline import _call_pipeline_brain
            requested_line = ""
            if requested_products:
                requested_line = (
                    "IMPORTANT: The seller specifically asked to fill in these products: "
                    + ", ".join(requested_products)
                    + ". Return an entry for EACH of them using EXACTLY that name, filled with the best "
                    "description / key_features / pricing / target_customer you can find in the report. "
                    "If one is a platform made of modules, summarise the platform for that entry (you may "
                    "also list the modules as extra entries).\n"
                )
            prompt = _PRODUCT_EXTRACTION_PROMPT.format(
                document_content=doc["content"][:8000], requested_line=requested_line
            )
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, _call_pipeline_brain, prompt)
            raw = _re.sub(r'```\w*\n?', '', raw).strip()
            try:
                m = _re.search(r'\{.*\}', raw, flags=_re.DOTALL)
                if m:
                    data = _json.loads(m.group(0))
                    existing = await db_module.list_products(org_id)
                    # Key by normalised name; on collision prefer the emptier product
                    # so a re-research fills the stub rather than an existing dup.
                    existing_by_name: dict = {}
                    for p in existing:
                        k = _norm_prod(p["name"])
                        cur = existing_by_name.get(k)
                        if cur is None or len(p.get("description") or "") < len(cur.get("description") or ""):
                            existing_by_name[k] = p
                    for ep in data.get("products", []):
                        pname = (ep.get("name") or "").strip()
                        if not pname:
                            continue
                        patch = {k: v for k, v in {
                            "description":     ep.get("description"),
                            "category":        ep.get("category"),
                            "key_features":    ep.get("key_features") or [],
                            "pricing_info":    ep.get("pricing_info"),
                            "target_customer": ep.get("target_customer"),
                            "website_url":     ep.get("website_url"),
                        }.items() if v}
                        match = existing_by_name.get(_norm_prod(pname))
                        if match:
                            if patch:
                                full_patch = dict(patch)
                                if doc:
                                    full_patch["source_doc_id"] = doc["id"]
                                await db_module.update_product(match["id"], org_id, full_patch)
                                products_updated += 1
                        elif sc and patch.get("description"):
                            pid = await db_module.create_product(
                                org_id=org_id,
                                seller_company_id=sc["id"],
                                name=pname,
                                category=ep.get("category"),
                                description=ep.get("description"),
                                key_features=ep.get("key_features") or [],
                                pricing_info=ep.get("pricing_info"),
                                target_customer=ep.get("target_customer"),
                                metadata={"source": "product_deep_research"},
                            )
                            extra = {}
                            if pid and doc:
                                extra["source_doc_id"] = doc["id"]
                            if pid and ep.get("website_url"):
                                extra["website_url"] = ep["website_url"]
                            if pid and extra:
                                await db_module.update_product(pid, org_id, extra)
                            products_updated += 1
            except Exception as parse_err:
                logger.warning("Deep research product extraction error: %s — raw: %.200s", parse_err, raw)

        logger.info("product_deep_research callback: %d products updated/created for org %d", products_updated, org_id)

        # --- Step 2: Build verification message from updated products ---
        products = await db_module.list_products(org_id, focus_only=True)
        if not products:
            products = await db_module.list_products(org_id)

        n = len(products)
        if products:
            first = products[0]
            rest = products[1:]
            first_name = first["name"]
            first_cat = first.get("category") or "?"
            first_desc = first.get("description") or "(no description found)"
            first_features = first.get("key_features") or []
            feature_lines = (
                "\n".join(f"  - {f}" for f in first_features[:5])
                if first_features else "  (none identified)"
            )
            pricing_str = first.get("pricing_info") or "(not found — do you know the pricing?)"
            target_str = first.get("target_customer") or "(unknown)"
            next_str = f"**{rest[0]['name']}**" if rest else "we'll wrap up the verification"

            verification_message = (
                f"I've completed deep research on your {n} focus product{'s' if n != 1 else ''} "
                f"for **{company_name}**. Let me walk through each one so you can verify and enrich my findings.\n\n"
                "---\n"
                f"### Starting with: {first_name} ({first_cat})\n\n"
                f"**What I found:** {first_desc}\n\n"
                f"**Key features identified:**\n{feature_lines}\n\n"
                f"**Pricing:** {pricing_str}\n"
                f"**Typical buyer:** {target_str}\n\n"
                "**Please verify:**\n"
                f"1. Does this summary accurately capture what **{first_name}** does?\n"
                "2. What pain points does it solve for customers?\n"
                "3. Who typically buys it — job title and company size?\n"
                "4. Any important capabilities I'm missing?\n\n"
                f"Once you've confirmed this one, I'll move on to {next_str}."
            )
        else:
            verification_message = (
                f"I've completed deep research for **{company_name}**, but didn't find enough product data to profile. "
                "Can you tell me about the main products or services your company offers? "
                "I'll update the catalog based on what you share."
            )

        session = await db_module.create_chat_session(
            org_id=org_id,
            user_id=None,
            title=f"Product Verification — {company_name}",
            client_name=None,
        )
        if session:
            from datetime import datetime, timezone as _tz
            opener = [{
                "role": "ai",
                "content": verification_message,
                "sources": [],
                "created_at": datetime.now(_tz.utc).isoformat(),
            }]
            async with db_module._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE chat_sessions SET messages=$2, updated_at=NOW() WHERE id=$1",
                    session["id"], opener,
                )
                await conn.execute(
                    "UPDATE seller_companies SET metadata=metadata||$2, updated_at=NOW() WHERE org_id=$1",
                    org_id, {"verification_session_id": session["id"]},
                )
            logger.info("product_deep_research callback: verification session %d created", session["id"])

        # Only advance status if still in deep_researching — not if triggered from "research more"
        company = await db_module.get_seller_company(org_id)
        if company and company.get("research_status") == "deep_researching":
            await db_module.update_seller_company_status(org_id, "deep_research_done")

        try:
            import notifications as _notify
            _notify.notify(
                f"🔬 *Deep product research done: {company_name}*\n"
                f"💬 Pi verification chat ready — open /knowledge to review"
            )
        except Exception:
            pass

    except Exception as exc:
        logger.error("_handle_product_deep_research_callback failed: %s", exc, exc_info=True)
        try:
            company = await db_module.get_seller_company(org_id)
            if company and company.get("research_status") == "deep_researching":
                await db_module.update_seller_company_status(org_id, "deep_research_done")
        except Exception:
            pass


async def _handle_pain_point_callback(
    org_id: int, svc_run_id, client_name: str, pi_brain: str, pi_model: str
) -> None:
    """After Hermes pain_point_research: read findings, build product catalog, fire Pi match_synthesis."""
    try:
        # Collect pain point findings for this client (recent, limited to keep context manageable)
        async with db_module._pool.acquire() as conn:
            findings = await conn.fetch(
                """
                SELECT d.content, d.metadata->>'source_url' AS source_url FROM documents d
                JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
                JOIN clients c ON c.id = dl.entity_id
                WHERE d.org_id = $1 AND c.name ILIKE $2
                  AND d.type IN ('finding', 'research', 'osint')
                ORDER BY d.created_at DESC
                LIMIT 40
                """,
                org_id, client_name,
            )

        def _fmt_finding(r) -> str:
            text = (r["content"] or "")[:1200]
            url = (r["source_url"] or "").strip()
            # Append source URL on its own line if it isn't already in the text
            if url and url not in text:
                text += f"\nSource: {url}"
            return text

        pain_point_summary = "\n\n---\n\n".join(
            _fmt_finding(r) for r in findings if r["content"]
        ) or f"No specific findings in the knowledge base for {client_name}. Use your general knowledge of this company's industry challenges."

        # Seller's product catalog for the MATCH — FOCUS products only (fall back
        # to all only when the org has no focus products). Non-focus products
        # (e.g. WatsonX) must never be matched/scored.
        products = await _fetch_match_products(org_id)
        if not products:
            # Don't silently abort — log and produce a best-effort report with no products
            logger.warning("_handle_pain_point_callback: no products for org %d — synthesising with placeholder", org_id)
        product_list = _format_match_product_catalog(products)

        # Open-positions / hiring signals (from the jobs scan) — relevant open
        # roles are strong evidence of an active initiative, so surface them
        # explicitly in the match input.
        hiring_signals = ""
        try:
            async with db_module._pool.acquire() as conn:
                jrow = await conn.fetchrow(
                    """SELECT d.metadata FROM documents d
                       JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
                       JOIN clients c ON c.id = dl.entity_id
                       WHERE d.org_id = $1 AND c.name ILIKE $2 AND d.type = 'jobs'
                       ORDER BY d.updated_at DESC LIMIT 1""",
                    org_id, client_name,
                )
            jmeta = (jrow["metadata"] if jrow else None) or {}
            if isinstance(jmeta, str):
                jmeta = json.loads(jmeta)
            needs = jmeta.get("inferred_needs") or []
            positions = jmeta.get("positions") or []
            if needs or positions:
                parts = [
                    "OPEN POSITIONS / HIRING SIGNALS (what " + client_name + " is actively hiring "
                    "for — a relevant open IT/management role is strong evidence of an active "
                    "initiative; weigh these in your fit scores and cite them where they support a fit):"
                ]
                if needs:
                    parts.append("Likely needs: " + "; ".join(str(n) for n in needs[:6]))
                if positions:
                    roles = ", ".join(
                        (p.get("title", "") + (f" ({p.get('team')})" if p.get("team") else ""))
                        for p in positions[:12] if p.get("title")
                    )
                    parts.append("Open roles: " + roles)
                if jmeta.get("careers_url"):
                    parts.append("Source: " + jmeta["careers_url"])
                hiring_signals = "\n".join(parts) + "\n\n"
        except Exception as exc:
            logger.warning("match synthesis: hiring-signals block failed for '%s': %s", client_name, exc)

        from datetime import date as _date
        today_date = _date.today().isoformat()
        task = _MATCH_SYNTHESIS_TEMPLATE.format(
            client_name=client_name,
            product_list=product_list,
            pain_point_summary=pain_point_summary,
            hiring_signals=hiring_signals,
            today_date=today_date,
        )

        # Get Pi URL for synthesis
        pi_url = config.get("agent_service_url_pi", config.get("agent_service_url", "http://localhost:8001"))
        server_url = config.get("server_url", "http://host.docker.internal:8000")

        run_id = await db_module.create_agent_run(
            org_id=org_id,
            agent_type="match_synthesis",
            task=task[:500],
            trigger_type="auto",
            triggered_by=None,
        )

        payload = {
            "task": task,
            "agent_type": "match_synthesis",
            "org_id": org_id,
            "provider": llm.provider_for_brain(pi_brain),
            "brain": pi_brain,
            "model": pi_model,
            "subject": client_name,
            "callback_url": f"{server_url}/api/agents/callback",
        }

        svc_token = config.get("agent_service_token", "")
        svc_headers = {"Authorization": f"Bearer {svc_token}"} if svc_token else {}

        async with httpx.AsyncClient(timeout=30) as client_http:
            resp = await client_http.post(f"{pi_url}/runs", json=payload, headers=svc_headers)
            resp.raise_for_status()
            data = resp.json()
            svc_id = data.get("run_id") or data.get("id")

        await db_module.update_agent_run(
            run_id, "running",
            output={"service_run_id": svc_id, "service_url": pi_url},
        )
        asyncio.create_task(_watch_agent_service_run(run_id, pi_url, svc_id, subject=client_name))
        logger.info("_handle_pain_point_callback: fired match_synthesis run %s for client=%s", svc_id, client_name)

    except Exception as exc:
        logger.error("_handle_pain_point_callback failed: %s", exc, exc_info=True)
        # Surface failure in the UI — otherwise client stays stuck at 'researching' forever
        try:
            async with db_module._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE clients SET metadata = metadata || $2 "
                    "WHERE org_id = $1 AND name ILIKE $3",
                    org_id,
                    {"match_status": "failed", "match_error": f"Synthesis setup failed: {exc}"},
                    client_name,
                )
        except Exception:
            pass


async def _handle_match_synthesis_callback(org_id: int, svc_run_id, client_name: str) -> None:
    """After Pi match_synthesis: update client metadata with match_status=done, send notification."""
    try:
        # Pi should have written a match_report document — find it by svc_run_id
        async with db_module._pool.acquire() as conn:
            doc = await conn.fetchrow(
                "SELECT id FROM documents WHERE org_id=$1 AND agent_run_id=$2 AND type='match_report' "
                "ORDER BY created_at DESC LIMIT 1",
                org_id, int(svc_run_id),
            )
            # If Pi didn't write it (older Pi version), create it from the agent run output
            if not doc:
                doc = await conn.fetchrow(
                    "SELECT id FROM documents WHERE org_id=$1 AND type='match_report' "
                    "AND content ILIKE $2 ORDER BY created_at DESC LIMIT 1",
                    org_id, f"%{client_name}%",
                )

        from datetime import datetime, timezone as _tz
        now_iso = datetime.now(_tz.utc).isoformat()

        # Update client metadata
        async with db_module._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE clients
                SET metadata = metadata || $2
                WHERE org_id = $1 AND name ILIKE $3
                """,
                org_id,
                {
                    "match_status": "done",
                    "match_updated_at": now_iso,
                    "match_run_id": str(svc_run_id),
                },
                client_name,
            )

        logger.info("_handle_match_synthesis_callback: match_status=done for client=%s org=%d", client_name, org_id)

        try:
            import notifications as _notify
            _notify.notify(
                f"🎯 *Match report ready: {client_name}*\n"
                f"Product-client fit analysis complete — view at /match"
            )
        except Exception:
            pass

    except Exception as exc:
        logger.error("_handle_match_synthesis_callback failed: %s", exc, exc_info=True)


async def _brief_then_match(org_id: int, client_name: str) -> None:
    """After research/osint: brief, then scan open positions (so hiring-derived
    needs are on file), then fire pain_point_research → match."""
    try:
        from routers.knowledge import _auto_generate_brief
        await _auto_generate_brief(org_id, client_name)
    except Exception as exc:
        logger.warning("_brief_then_match: brief step failed for '%s': %s", client_name, exc)
    # Open-positions scan runs after the rest of the research but BEFORE the match,
    # so the inferred needs (written as findings) feed the product-fit synthesis.
    try:
        from routers.pipeline import _scan_client_jobs
        client = await db_module.get_client(org_id, client_name)
        if client:
            await _scan_client_jobs(org_id, client)
    except Exception as exc:
        logger.warning("_brief_then_match: jobs scan failed for '%s': %s", client_name, exc)
    await _maybe_trigger_pain_point_research(org_id, client_name)


async def _maybe_trigger_pain_point_research(org_id: int, client_name: str) -> None:
    """Auto-mode: after research/osint completes, queue pain_point_research if products exist and no recent match."""
    try:
        # Check org has products
        products = await db_module.list_products(org_id)
        if not products:
            return

        # Check for recent match report (within 7 days)
        from datetime import datetime, timezone as _tz, timedelta
        cutoff = datetime.now(_tz.utc) - timedelta(days=7)

        async with db_module._pool.acquire() as conn:
            recent = await conn.fetchrow(
                """
                SELECT d.id FROM documents d
                JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
                JOIN clients c ON c.id = dl.entity_id
                WHERE d.org_id = $1 AND c.name ILIKE $2
                  AND d.type = 'match_report' AND d.created_at > $3
                LIMIT 1
                """,
                org_id, client_name, cutoff,
            )

        if recent:
            logger.debug("_maybe_trigger_pain_point_research: skipping %s — match report < 7 days old", client_name)
            return

        # Also check for an in-progress pain_point_research run
        async with db_module._pool.acquire() as conn:
            in_progress = await conn.fetchrow(
                """
                SELECT id FROM agent_runs
                WHERE org_id = $1 AND agent_type = 'pain_point_research'
                  AND status IN ('pending', 'running')
                  AND task ILIKE $2
                LIMIT 1
                """,
                org_id, f"%{client_name}%",
            )

        if in_progress:
            logger.debug("_maybe_trigger_pain_point_research: skipping %s — already running", client_name)
            return

        hermes_brain = config.get("match_brain", config.get("research_brain", "openrouter"))
        hermes_model = config.get("match_research_model", config.get("research_model", "deepseek/deepseek-v4-flash"))
        pi_url = config.get("agent_service_url_pi", "http://localhost:8001")

        task = _PAIN_POINT_RESEARCH_TEMPLATE.format(client_name=client_name)

        run_id = await db_module.create_agent_run(
            org_id=org_id,
            agent_type="pain_point_research",
            task=task[:500],
            trigger_type="auto",
            triggered_by=None,
        )

        payload = {
            "task": task,
            "agent_type": "pain_point_research",
            "org_id": org_id,
            "provider": llm.provider_for_brain(hermes_brain),
            "brain": hermes_brain,
            "model": hermes_model,
            "subject": client_name,
            "callback_url": f"{config.get('server_url', 'http://host.docker.internal:8000')}/api/agents/callback",
        }

        svc_token = config.get("agent_service_token", "")
        svc_headers = {"Authorization": f"Bearer {svc_token}"} if svc_token else {}

        async with httpx.AsyncClient(timeout=30) as client_http:
            resp = await client_http.post(f"{pi_url}/runs", json=payload, headers=svc_headers)
            resp.raise_for_status()
            data = resp.json()
            svc_id = data.get("run_id") or data.get("id")

        await db_module.update_agent_run(
            run_id, "running",
            output={"service_run_id": svc_id, "service_url": pi_url},
        )

        # Mirror what match.py does: mark the client as researching so the Match page shows progress
        try:
            async with db_module._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE clients SET metadata = metadata || $2 "
                    "WHERE org_id = $1 AND name ILIKE $3",
                    org_id,
                    {"match_status": "researching", "match_run_id": str(run_id), "match_error": None},
                    client_name,
                )
        except Exception as meta_exc:
            logger.warning("_maybe_trigger_pain_point_research: could not update match_status: %s", meta_exc)

        asyncio.create_task(_watch_agent_service_run(run_id, pi_url, svc_id, subject=client_name))
        logger.info("_maybe_trigger_pain_point_research: fired pain_point_research run %s for client=%s", svc_id, client_name)

    except Exception as exc:
        logger.error("_maybe_trigger_pain_point_research failed: %s", exc, exc_info=True)


@router.post("/api/agents/system/run")
async def trigger_system_agent(user: dict = Depends(current_user)):
    """Trigger the Pi monitor: surveys all clients, queues research for stale ones."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type="monitor",
        task="Monitor all clients for research staleness",
        trigger_type="manual", triggered_by=user["id"],
    )
    try:
        svc_url, svc_run_id = await _fire_agent_service(
            "org", user["org_id"],
            brain=config.get("agent_service_brain", "openrouter"),
            model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
            task="Monitor all clients for research staleness",
            agent_type="monitor",
        )
        await db_module.update_agent_run(
            run_id, "running",
            output={"service_run_id": svc_run_id, "service_url": svc_url},
        )
        asyncio.create_task(_watch_agent_service_run(run_id, svc_url, svc_run_id, subject="org"))
    except Exception as exc:
        await db_module.update_agent_run(run_id, "failed", error=str(exc))
        raise HTTPException(status_code=503, detail=f"Monitor service unavailable: {exc}")
    return {"run_id": run_id, "status": "started"}


@router.get("/api/agents/activity")
async def get_agent_activity(days: int = 7, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"summary": [], "runs": [], "days": days}
    cached = cache_get(("agent_activity", user["org_id"], days))
    if cached is not None:
        return cached
    activity = await db_module.get_agent_activity(user["org_id"], days=days)
    activity["days"] = days
    cache_set(("agent_activity", user["org_id"], days), activity)
    return activity


# ---------------------------------------------------------------------------
# Research queue (authenticated)
# ---------------------------------------------------------------------------

@router.post("/api/research/queue")
@_limit("10/minute")
async def enqueue_research(request: Request, body: dict, user: dict = Depends(current_user)):
    subject      = (body.get("subject") or "").strip()
    subject_type = (body.get("subject_type") or "company").strip()
    angles       = body.get("angles", "")

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    db_module.log_prompt(user["org_id"], user["id"], "research_trigger", subject,
                         {"subject_type": subject_type, "angles": angles, "source": "queue"})

    payload: dict = {"source": "api"}
    if angles:
        payload["angles"] = angles

    task_id = await db_module.enqueue_research_task(
        org_id=user["org_id"], subject_type=subject_type,
        subject=subject, task_type="orchestrate",
        payload=payload, depth=0, priority=7,
    )
    return {
        "task_id": task_id, "subject": subject,
        "subject_type": subject_type, "status": "pending",
    }


@router.get("/api/research/queue")
async def list_research_queue(
    status: Optional[str] = None,
    subject: Optional[str] = None,
    user: dict = Depends(current_user),
):
    if not DB_AVAILABLE:
        return {"tasks": [], "count": 0}
    tasks = await db_module.list_research_tasks(user["org_id"], status=status, subject=subject)
    return {"tasks": tasks, "count": len(tasks)}


@router.get("/api/research/tasks/{task_id}")
async def get_research_task_detail(task_id: int, user: dict = Depends(current_user)) -> dict:
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    task = await db_module.get_research_task(task_id, user["org_id"])
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ---------------------------------------------------------------------------
# Research worker control
# ---------------------------------------------------------------------------

@router.post("/api/research/pause")
async def pause_research_workers(user: dict = Depends(current_user)) -> dict:
    try:
        from agents.research_runner import pause_workers
        pause_workers()
    except Exception:
        pass
    return {"status": "paused"}


@router.post("/api/research/resume")
async def resume_research_workers(user: dict = Depends(current_user)) -> dict:
    try:
        from agents.research_runner import resume_workers
        resume_workers()
    except Exception:
        pass
    return {"status": "running"}


@router.post("/api/research/cancel")
async def cancel_research_subject_tasks(body: dict, user: dict = Depends(current_user)) -> dict:
    subject = (body.get("subject") or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    count = await db_module.cancel_research_subject(user["org_id"], subject)
    try:
        from agents.events import emit as _emit
        await _emit({
            "type": "subject_cancelled", "subject": subject,
            "count": count, "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return {"subject": subject, "cancelled": count}


@router.post("/api/agents/runs/cancel-all-running")
async def cancel_all_running_runs(user: dict = Depends(current_user)) -> dict:
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    count = await db_module.cancel_all_running_agent_runs(user["org_id"])
    return {"cancelled": count}


@router.post("/api/research/aggregate")
async def trigger_manual_aggregation(body: dict, user: dict = Depends(current_user)) -> dict:
    subject      = (body.get("subject") or "").strip()
    subject_type = (body.get("subject_type") or "company").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    task_id = await db_module.enqueue_research_task(
        org_id=user["org_id"], subject_type=subject_type,
        subject=subject, task_type="aggregate",
        payload={"triggered_by": "manual"}, depth=0, priority=9,
    )
    return {"task_id": task_id, "subject": subject, "status": "pending"}


@router.post("/api/agents/find-people")
async def find_people_for_client(body: dict, user: dict = Depends(current_user)) -> dict:
    """Trigger a people-search run for a client (optionally role-targeted), then auto-extract contacts."""
    client_name = (body.get("client_name") or "").strip()
    if not client_name:
        raise HTTPException(status_code=400, detail="client_name is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    try:
        return await _start_people_search(
            user["org_id"], client_name,
            target_roles=(body.get("target_roles") or "").strip(),
            user_id=user["id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Agent service unavailable: {exc}")


async def _start_people_search(org_id: int, client_name: str, target_roles: str = "",
                               user_id: Optional[int] = None, trigger_type: str = "manual") -> dict:
    """Fire a people-search (research) run + a people_search DB row so the callback
    auto-extracts contacts. Shared by the client-page endpoint and the chat find_people tool.
    `target_roles` (e.g. "CISO, IT-Architekt") steers the search toward a persona."""
    # ASCII form for web searches (English sources omit umlauts); canonical kept in subject=
    ascii_client = _ascii_name(client_name)
    role_line = ""
    if target_roles:
        role_line = (
            f"PRIORITISE people in these roles/functions: {target_roles}. "
            f"Search specifically for these roles at {ascii_client} "
            f"(e.g. '{ascii_client} {target_roles}', targeted LinkedIn title searches, the leadership/IT/security pages). "
            f"Still record other notable executives you find, but lead with these roles. "
        )
    task = (
        f"Find key executives, board members, managers, and notable employees at {ascii_client}. "
        f"{role_line}"
        f"For each person: search their name + {ascii_client} on LinkedIn and company news sources. "
        f"Save a profile finding for every named individual you discover. "
        f"Include their full name, title, and LinkedIn URL wherever available. "
        f"Also search for the company's business email format — try '{ascii_client} email format', "
        f"the company's contact or impressum page, and LinkedIn profiles. Common formats: "
        f"first.last@domain.com, f.last@domain.com, firstname@domain.com. "
        f"When you can confirm or confidently infer a person's business email, include it in their profile under 'email'."
    )
    brain = config.get("research_brain", config.get("agent_service_brain", "openrouter"))
    model = config.get("research_model", config.get("agent_service_model", "deepseek/deepseek-v4-flash"))
    svc_url, svc_run_id = await _fire_agent_service(
        subject=client_name, org_id=org_id,
        brain=brain, model=model, task=task, agent_type="research",
    )
    db_run_id = await db_module.create_agent_run(
        org_id=org_id, agent_type="people_search",
        task=task, trigger_type=trigger_type, triggered_by=user_id,
    )
    await db_module.update_agent_run(
        db_run_id, "running",
        output={"service_run_id": svc_run_id, "service_url": svc_url},
    )
    asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name))
    return {"run_id": db_run_id, "status": "running"}


@router.post("/api/contacts/{contact_name}/enrich")
async def enrich_contact_linkedin(contact_name: str, user: dict = Depends(current_user)) -> dict:
    """Fire a Pi contact_enrich run to find the LinkedIn URL for a single contact."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    contact = await db_module.get_contact(user["org_id"], contact_name)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    meta = contact.get("metadata") or {}
    role = meta.get("role", "")
    client_name = ""
    if contact.get("client_id"):
        client = await db_module.get_client_by_id(user["org_id"], contact["client_id"])
        client_name = client["name"] if client else ""

    task = (
        f"Find the LinkedIn profile URL for {contact_name}"
        + (f", {role}" if role else "")
        + (f" at {client_name}" if client_name else "")
        + f". Search LinkedIn and save their contact record with the linkedin_url."
    )
    brain = config.get("agent_service_brain", "openrouter")
    model = config.get("agent_service_model", "deepseek/deepseek-v4-flash")
    try:
        svc_url, svc_run_id = await _fire_agent_service(
            subject=client_name or contact_name, org_id=user["org_id"],
            brain=brain, model=model, task=task, agent_type="contact_enrich",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Agent service unavailable: {exc}")

    db_run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type="contact_enrich",
        task=task, trigger_type="manual", triggered_by=user["id"],
    )
    await db_module.update_agent_run(
        db_run_id, "running",
        output={"service_run_id": svc_run_id, "service_url": svc_url},
    )
    asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name or contact_name))
    return {"run_id": db_run_id, "status": "running"}


@router.post("/api/research/trigger")
@_limit("10/minute")
async def trigger_research_no_auth(body: dict, request: Request) -> dict:
    """Dashboard/agent convenience endpoint.

    Auth: a logged-in user's Bearer token OR the agent-service token.
    Org scoping:
      - User token    → org_id ALWAYS derived from the authenticated user.
                        A body org_id that differs from the caller's org is a 400.
      - Service token → org_id must be supplied in the body (Pi passes the org
                        of the originating run; there is no user context).
    Supports brain/model overrides and max_tasks cap for the delegator.
    """
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    svc_token = config.get("agent_service_token", "")
    is_service = bool(svc_token) and token == svc_token
    caller: Optional[dict] = None
    if not is_service and DB_AVAILABLE and db_module is not None and token:
        caller = await db_module.get_user_by_token(token)
    if not is_service and caller is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    subject      = (body.get("subject") or "").strip()
    subject_type = (body.get("subject_type") or "company").strip()
    angles       = body.get("angles", "")
    brain        = body.get("brain", "").strip()
    model        = body.get("model", "").strip()
    org_id_override = body.get("org_id")
    max_tasks    = body.get("max_tasks", 20)

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    # Resolve org — never from an unverified body value for user callers, and
    # never via a cross-org client-name lookup or a get_first_org() fallback.
    override_id: Optional[int] = None
    if org_id_override is not None:
        try:
            override_id = int(org_id_override)
        except (TypeError, ValueError):
            override_id = None

    if is_service:
        # Service-token path (Pi's trigger_research tool): the body carries the
        # org of the originating agent run — required, no global fallback.
        if not override_id:
            raise HTTPException(status_code=400, detail="org_id is required for service-token calls")
        org_id: int = override_id
    else:
        org_id = caller["org_id"]
        if override_id is not None and override_id != org_id:
            raise HTTPException(status_code=400, detail="org_id does not match your organisation")

    try:
        max_tasks = max(5, min(10000, int(max_tasks)))
    except (TypeError, ValueError):
        max_tasks = 20

    # Route to agent service when configured
    backend = config.get("agent_service_backend", "python")
    if backend in ("pi", "split"):
        try:
            task_str = _RESEARCH_TASK_TEMPLATE.format(subject=subject)
            if angles:
                task_str += f"\n\nFocus especially on: {angles}"
            svc_url, svc_run_id = await _fire_agent_service(
                subject, org_id, brain, model, task_str, agent_type="research",
            )
            db_run_id = await db_module.create_agent_run(
                org_id=org_id, agent_type="research",
                task=f"Research: {subject}", trigger_type="dashboard",
            )
            await db_module.update_agent_run(
                db_run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=subject))
            return {
                "run_id": db_run_id, "service_run_id": svc_run_id,
                "subject": subject, "org_id": org_id,
                "status": "running", "backend": backend, "agent_type": "research",
            }
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Agent service unavailable: {exc}")

    # Python embedded research queue (fallback)
    payload: dict = {"source": "dashboard", "_max_tasks": max_tasks}
    if angles:
        payload["angles"] = angles
    if brain:
        payload["_brain_override"] = brain
    if model:
        payload["_model_override"] = model

    task_id = await db_module.enqueue_research_task(
        org_id=org_id, subject_type=subject_type,
        subject=subject, task_type="orchestrate",
        payload=payload, depth=0, priority=7,
    )
    return {
        "task_id": task_id, "subject": subject,
        "subject_type": subject_type, "org_id": org_id,
        "status": "pending", "brain": brain or "default", "model": model or "default",
    }


# ---------------------------------------------------------------------------
# Finding feedback
# ---------------------------------------------------------------------------

@router.post("/api/research/findings/{doc_id}/feedback")
async def post_finding_feedback(doc_id: str, body: dict, user: dict = Depends(current_user)):
    """Store human feedback on a finding. Rating ≤ 2 marks it overridden so aggregators skip it."""
    rating  = body.get("rating")
    comment = (body.get("comment") or "").strip()

    if not isinstance(rating, int) or not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="rating must be an integer 1–5")
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    metadata_patch: dict = {
        "feedback": {
            "rating": rating, "comment": comment,
            "rated_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    if rating <= 2:
        metadata_patch["relevance_override"] = 1

    updated = await db_module.update_document(user["org_id"], doc_id, {"metadata": metadata_patch})
    if not updated:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"doc_id": doc_id, "rating": rating, "overridden": rating <= 2}


# ---------------------------------------------------------------------------
# /ws/agents WebSocket — live agent event stream
# ---------------------------------------------------------------------------

@router.websocket("/ws/agents")
async def agents_ws(ws: WebSocket) -> None:
    # Token auth via ?token= — the event stream exposes the org's research queue
    if DB_AVAILABLE and db_module is not None:
        token = ws.query_params.get("token", "")
        ws_user = await db_module.get_user_by_token(token) if token else None
        if ws_user is None:
            await ws.close(code=4401)
            return
    await ws.accept()
    from agents.events import subscribe, unsubscribe
    subscribe(ws)
    try:
        # Send initial queue snapshot so the dashboard renders immediately
        if DB_AVAILABLE:
            org = await db_module.get_first_org()
            if org:
                tasks = await db_module.list_research_tasks(org["id"])
                await ws.send_text(json.dumps({"type": "queue_snapshot", "tasks": tasks}, default=str))

        async def _send_snapshot() -> None:
            if DB_AVAILABLE:
                org2 = await db_module.get_first_org()
                if org2:
                    snap = await db_module.list_research_tasks(org2["id"])
                    await ws.send_text(json.dumps({"type": "queue_snapshot", "tasks": snap}, default=str))

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "refresh":
                        await _send_snapshot()
                except Exception:
                    pass
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        from agents.events import unsubscribe as _unsub
        _unsub(ws)



# ---------------------------------------------------------------------------
# Research log endpoint
# ---------------------------------------------------------------------------

@router.get("/api/research/log")
async def get_research_log(
    subject: Optional[str] = None,
    n: int = 100,
    user: dict = Depends(current_user),
):
    from agents.research_log import get_recent
    entries = get_recent(n=min(n, 500), subject=subject)
    return {"entries": entries, "count": len(entries)}
