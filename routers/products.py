"""
Products router — seller company setup and product catalog management.

Covers:
- Seller company upsert + initial product research trigger
- Product CRUD (create, read, update, delete, share)
- Research status polling for the wizard UI
- Deep research trigger after seller selects focus products
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

import llm
from context import DB_AVAILABLE, config, db_module, cache_get, cache_set, cache_clear
from routers.auth import current_user
from routers.agents import (
    _PRODUCT_RESEARCH_TASK_TEMPLATE,
    _PRODUCT_DEEP_RESEARCH_TASK_TEMPLATE,
    _fire_agent_service,
    _watch_agent_service_run,
)
from routers.knowledge import (
    _build_presentation_prompt_context,
    _build_brief_context,
    _clean_mail_output,
    _MAIL_TEMPLATE_PROMPT,
    _MAIL_TYPE_LABELS,
    _call_brain_sync,
    _slugify,
    _get_source_refs,
    _fetch_event_via_pi,
    _fetch_event_for_mail,
)

logger = logging.getLogger("wk.products")

router = APIRouter()


async def _fire_product_deep_research(org_id: int, company: dict, product_names: list,
                                      triggered_by: Optional[int] = None) -> tuple:
    """Fire a product_deep_research run for specific products and store the requested
    names on the run so the callback writes the results back onto exactly those stubs.
    Shared by the 'Research More' endpoint and auto-research on product create.
    Returns (db_run_id, svc_run_id). Raises on fire failure."""
    product_list = ", ".join(product_names)
    task = _PRODUCT_DEEP_RESEARCH_TASK_TEMPLATE.format(
        company_name=company["name"], product_list=product_list,
    )
    svc_url, svc_run_id = await _fire_agent_service(
        subject=company["name"], org_id=org_id,
        brain=config.get("agent_service_brain", "openrouter"),
        model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
        task=task, agent_type="product_deep_research",
    )
    db_run_id = await db_module.create_agent_run(
        org_id=org_id, agent_type="product_deep_research",
        task=f"Research more products: {product_list[:120]}",
        trigger_type="manual", triggered_by=triggered_by,
    )
    await db_module.update_agent_run(
        db_run_id, "running",
        output={"service_run_id": svc_run_id, "service_url": svc_url,
                "requested_products": product_names},
    )
    asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id))
    logger.info("product_deep_research fired for %d product(s): %s", len(product_names), product_list[:80])
    return db_run_id, svc_run_id

# Fields callers are allowed to patch on a product
_PATCHABLE_PRODUCT_FIELDS = {
    "name", "description", "category", "key_features", "pricing_info",
    "target_customer", "is_focus", "priority", "is_favorite", "is_shared",
    "status", "metadata", "website_url",
}


# ---------------------------------------------------------------------------
# Seller company
# ---------------------------------------------------------------------------

@router.get("/api/seller/company")
async def get_seller_company_endpoint(user: dict = Depends(current_user)):
    """Return the seller's own company record + all products for this org."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    cached = cache_get(("seller_company", user["org_id"]))
    if cached is not None:
        return cached
    company, products = await asyncio.gather(
        db_module.get_seller_company(user["org_id"]),
        db_module.list_products(user["org_id"]),
    )
    result = {"company": company, "products": products if company else []}
    cache_set(("seller_company", user["org_id"]), result)
    return result


@router.post("/api/seller/company")
async def upsert_seller_company_endpoint(body: dict, user: dict = Depends(current_user)):
    """Upsert the seller company and trigger Hermes product research."""
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    website_url = (body.get("website_url") or "").strip() or None
    industry = (body.get("industry") or "").strip() or None

    company_id = await db_module.upsert_seller_company(
        org_id=user["org_id"],
        name=name,
        website_url=website_url,
        industry=industry,
    )
    await db_module.update_seller_company_status(user["org_id"], "researching")

    product_hints = (body.get("product_hints") or "").strip()
    hints_line = (
        f"The seller has flagged these specific products — ensure every one is found and profiled: {product_hints}. "
        if product_hints else ""
    )
    task = _PRODUCT_RESEARCH_TASK_TEMPLATE.format(
        company_name=name,
        website_url=website_url or name,
        product_hints_line=hints_line,
    )

    try:
        svc_url, svc_run_id = await _fire_agent_service(
            subject=name,
            org_id=user["org_id"],
            brain=config.get("agent_service_brain", "openrouter"),
            model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
            task=task,
            agent_type="product_research",
        )
        db_run_id = await db_module.create_agent_run(
            org_id=user["org_id"],
            agent_type="product_research",
            task=f"Product research: {name}",
            trigger_type="manual",
            triggered_by=user["id"],
        )
        await db_module.update_agent_run(
            db_run_id, "running",
            output={
                "service_run_id": svc_run_id,
                "service_url": svc_url,
                "seller_company_id": company_id,
            },
        )
        asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id))
        logger.info("Product research fired for '%s': svc_run=%d", name, svc_run_id)
    except Exception as exc:
        logger.error("Failed to fire product_research for '%s': %s", name, exc)
        await db_module.update_seller_company_status(user["org_id"], "pending")
        raise HTTPException(status_code=502, detail=f"Failed to start research: {exc}")

    return {"ok": True, "research_status": "researching", "company_id": company_id}


@router.get("/api/seller/research/status")
async def get_research_status(user: dict = Depends(current_user)):
    """Lightweight poll endpoint for the wizard UI."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    company = await db_module.get_seller_company(user["org_id"])
    if not company:
        return {"research_status": None, "product_count": 0, "verification_session_id": None}
    products = await db_module.list_products(user["org_id"])
    verification_session_id = (company.get("metadata") or {}).get("verification_session_id")
    updated_at = company.get("updated_at")
    return {
        "research_status": company["research_status"],
        "product_count": len(products),
        "verification_session_id": verification_session_id,
        "status_updated_at": updated_at.isoformat() if updated_at else None,
    }


# PHASE20: suspect — no frontend references found; verify before removing
# Added: dev/troubleshooting tool to re-run product extraction without re-firing Hermes
@router.post("/api/seller/company/extract-products")
async def extract_products_from_research(user: dict = Depends(current_user)):
    """Re-run product extraction from the most recent research document without re-running Hermes."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    company = await db_module.get_seller_company(user["org_id"])
    if not company:
        raise HTTPException(status_code=404, detail="No seller company set up")

    research_doc_id = company.get("research_doc_id")
    if not research_doc_id:
        raise HTTPException(status_code=404, detail="No research document found — run research first")

    doc = await db_module.get_document_by_int_id(user["org_id"], int(research_doc_id))
    if not doc:
        raise HTTPException(status_code=404, detail="Research document missing from DB")

    from routers.agents import _handle_product_research_callback
    asyncio.create_task(
        _handle_product_research_callback(user["org_id"], doc["agent_run_id"], company["name"])
    )
    logger.info("extract-products queued for org %d, doc %d", user["org_id"], research_doc_id)
    return {"ok": True, "doc_id": research_doc_id, "message": "Extraction queued — check /products in ~30s"}


@router.post("/api/seller/company/reset-status")
async def reset_research_status(user: dict = Depends(current_user)):
    """Unstick a hung research run — resets status to products_found (or pending if no products)."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    products = await db_module.list_products(user["org_id"])
    new_status = "products_found" if products else "pending"
    await db_module.update_seller_company_status(user["org_id"], new_status)
    logger.info("Research status reset to '%s' for org %d", new_status, user["org_id"])
    return {"ok": True, "research_status": new_status}


@router.post("/api/seller/company/research-more")
async def research_more_products(body: dict, user: dict = Depends(current_user)):
    """Create stub products for named products and fire a deep research run for them."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    company = await db_module.get_seller_company(user["org_id"])
    if not company:
        raise HTTPException(status_code=404, detail="No seller company configured")

    raw_names = body.get("product_names") or []
    if isinstance(raw_names, str):
        raw_names = [n.strip() for n in raw_names.split(",") if n.strip()]
    product_names = [n.strip() for n in raw_names if n.strip()]
    if not product_names:
        raise HTTPException(status_code=400, detail="product_names is required")

    stub_ids = []
    existing_all = await db_module.list_products(user["org_id"])
    existing_by_name = {p["name"].lower().strip(): p for p in existing_all}
    for pname in product_names:
        existing_match = existing_by_name.get(pname.lower().strip())
        if existing_match:
            stub_ids.append(existing_match["id"])
        else:
            pid = await db_module.create_product(
                org_id=user["org_id"],
                seller_company_id=company["id"],
                name=pname,
                metadata={"source": "manual_hint"},
            )
            stub_ids.append(pid)

    try:
        await _fire_product_deep_research(user["org_id"], company, product_names, triggered_by=user["id"])
    except Exception as exc:
        logger.error("Failed to fire research-more: %s", exc)
        raise HTTPException(status_code=502, detail=f"Failed to start research: {exc}")

    return {
        "ok": True,
        "stub_ids": stub_ids,
        "message": f"Research started for {len(product_names)} product(s) — they will update when done",
    }


@router.post("/api/seller/company/research")
async def trigger_deep_research(body: dict, user: dict = Depends(current_user)):
    """Mark selected products as focus and trigger deep research on them."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    company = await db_module.get_seller_company(user["org_id"])
    if not company:
        raise HTTPException(status_code=404, detail="No seller company configured")

    product_ids = body.get("product_ids") or []
    if not isinstance(product_ids, list):
        raise HTTPException(status_code=400, detail="product_ids must be a list")

    # Mark selected products as focus (others remain as-is)
    for pid in product_ids:
        await db_module.update_product(int(pid), user["org_id"], {"is_focus": True})

    focus_products = await db_module.list_products(user["org_id"], focus_only=True)
    if not focus_products:
        raise HTTPException(status_code=400, detail="No focus products selected")

    product_list = ", ".join(p["name"] for p in focus_products)
    task = _PRODUCT_DEEP_RESEARCH_TASK_TEMPLATE.format(
        company_name=company["name"],
        product_list=product_list,
    )

    await db_module.update_seller_company_status(user["org_id"], "deep_researching")

    try:
        svc_url, svc_run_id = await _fire_agent_service(
            subject=company["name"],
            org_id=user["org_id"],
            brain=config.get("agent_service_brain", "openrouter"),
            model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
            task=task,
            agent_type="product_deep_research",
        )
        db_run_id = await db_module.create_agent_run(
            org_id=user["org_id"],
            agent_type="product_deep_research",
            task=f"Deep product research: {product_list[:120]}",
            trigger_type="manual",
            triggered_by=user["id"],
        )
        await db_module.update_agent_run(
            db_run_id, "running",
            output={"service_run_id": svc_run_id, "service_url": svc_url},
        )
        asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id))
        logger.info("Deep product research fired for '%s' products: %s", company["name"], product_list[:80])
    except Exception as exc:
        logger.error("Failed to fire product_deep_research: %s", exc)
        await db_module.update_seller_company_status(user["org_id"], "products_found")
        raise HTTPException(status_code=502, detail=f"Failed to start deep research: {exc}")

    return {"ok": True, "research_status": "deep_researching", "product_list": product_list}


# ---------------------------------------------------------------------------
# Products CRUD
# ---------------------------------------------------------------------------

@router.get("/api/products")
async def list_products_endpoint(
    focus_only: bool = False,
    shared: bool = False,
    user: dict = Depends(current_user),
):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    products = await db_module.list_products(
        user["org_id"], focus_only=focus_only, shared_only=shared
    )
    return {"products": products}


@router.post("/api/products")
async def create_product_endpoint(body: dict, user: dict = Depends(current_user)):
    """Manually create a product."""
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    company = await db_module.get_seller_company(user["org_id"])
    if not company:
        raise HTTPException(status_code=400, detail="Set up your seller company first")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    product_id = await db_module.create_product(
        org_id=user["org_id"],
        seller_company_id=company["id"],
        name=name,
        category=body.get("category"),
        description=body.get("description"),
        key_features=body.get("key_features") or [],
        pricing_info=body.get("pricing_info"),
        target_customer=body.get("target_customer"),
        metadata=body.get("metadata") or {},
    )
    # Auto-research a new product that was added without details, so it fills
    # itself in the background (the callback writes results back onto this stub).
    auto_research = product_id and not (body.get("description") or "").strip()
    if auto_research:
        async def _bg_research():
            try:
                await _fire_product_deep_research(user["org_id"], company, [name], triggered_by=user["id"])
            except Exception as exc:
                logger.warning("auto product research on add failed for '%s': %s", name, exc)
        asyncio.create_task(_bg_research())
    return {"ok": True, "id": product_id, "research_started": bool(auto_research)}


@router.get("/api/products/{product_id}")
async def get_product_endpoint(product_id: int, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"product": product}


@router.patch("/api/products/{product_id}")
async def update_product_endpoint(product_id: int, body: dict, user: dict = Depends(current_user)):
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    patch = {k: v for k, v in body.items() if k in _PATCHABLE_PRODUCT_FIELDS}
    if not patch:
        raise HTTPException(status_code=400, detail="No patchable fields provided")
    updated = await db_module.update_product(product_id, user["org_id"], patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"ok": True, "product": updated}


@router.delete("/api/products/{product_id}")
async def delete_product_endpoint(product_id: int, user: dict = Depends(current_user)):
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    deleted = await db_module.delete_product(product_id, user["org_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"ok": True}


@router.post("/api/products/{product_id}/share")
async def toggle_product_share(product_id: int, user: dict = Depends(current_user)):
    """Toggle org-sharing for a product."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    updated = await db_module.update_product(
        product_id, user["org_id"], {"is_shared": not product["is_shared"]}
    )
    return {"ok": True, "is_shared": updated["is_shared"] if updated else None}


# ---------------------------------------------------------------------------
# Per-product source document
# ---------------------------------------------------------------------------

@router.get("/api/products/{product_id}/sources")
async def get_product_sources(product_id: int, user: dict = Depends(current_user)):
    """Return the content of the source research document for this product."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    src_id = product.get("source_doc_id")
    if not src_id:
        return {"content": None}
    doc = await db_module.get_document_by_int_id(user["org_id"], int(src_id))
    if not doc:
        return {"content": None, "title": None, "doc_id": src_id}
    return {
        "content": doc.get("content"),
        "title": doc.get("title"),
        "doc_id": src_id,
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }


# ---------------------------------------------------------------------------
# Per-product match sections
# ---------------------------------------------------------------------------

def _parse_fit(header: str) -> tuple[str, int]:
    """Return (fit_category, score) from a match report section header."""
    import re
    if "✓" in header or "Strong Fit" in header:
        cat = "strong"
    elif "~" in header or "Potential Fit" in header:
        cat = "potential"
    elif "✗" in header or "Not a Fit" in header:
        cat = "not_a_fit"
    else:
        cat = "unknown"
    m = re.search(r"\[(\d+)/10\]", header)
    score = int(m.group(1)) if m else 0
    return cat, score


def _extract_product_section(content: str, product_name: str) -> tuple[str, int, str] | None:
    """Scan a match report for the section covering product_name. Returns (fit_category, score, section_text) or None."""
    import re
    # Split on ## headings (keep the ## with each chunk)
    parts = re.split(r"(?=\n## )", "\n" + content)
    name_lower = product_name.lower()
    for part in parts:
        lines = part.lstrip("\n").split("\n")
        header = lines[0].strip()
        if not header.startswith("## "):
            continue
        if name_lower not in header.lower():
            continue
        cat, score = _parse_fit(header)
        # Drop the header line — caller renders it as the badge
        body = "\n".join(lines[1:]).strip()
        return cat, score, body
    return None


@router.get("/api/products/{product_id}/match_sections")
async def get_product_match_sections(product_id: int, user: dict = Depends(current_user)):
    """Return per-client match sections extracted from match_report documents for this product."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    reports = await db_module.get_match_reports(user["org_id"])
    sections = []
    for report in reports:
        result = _extract_product_section(report.get("content") or "", product["name"])
        if result is None:
            continue
        cat, score, body = result
        sections.append({
            "client_name": report.get("client_name"),
            "created_at": report["created_at"].isoformat() if report.get("created_at") else None,
            "fit_category": cat,
            "score": score,
            "content": body,
        })
    # Sort strong fits first, then by score desc
    _order = {"strong": 0, "potential": 1, "not_a_fit": 2, "unknown": 3}
    sections.sort(key=lambda s: (_order.get(s["fit_category"], 3), -s["score"]))
    return {"sections": sections}


# ---------------------------------------------------------------------------
# Opportunities pivot — products × match-fit bands
# ---------------------------------------------------------------------------

def _aggregate_opportunities(products: list, reports: list, min_score: int) -> tuple[list, int]:
    """Pivot match_report fit scores into per-product score bands.

    Returns (product_rows, matched_client_count). Pure function (no DB/IO) so it's
    unit-testable. `reports` must be newest-first (as db.get_match_reports returns).
    Band score = the match_report [N/10] fit score; scores < min_score are dropped.
    """
    from routers.match import _FIT_HEADING

    # Parse every report once, newest-first; dedup (client, product-as-written) keeping
    # the newest occurrence. parsed stays in newest-first order.
    parsed: list[tuple] = []          # (client, product_lower, score)
    seen: set[tuple] = set()
    matched_clients: set[str] = set()
    for rep in reports:
        client = (rep.get("client_name") or "").strip()
        if not client:
            continue
        for _fit, score, product in _FIT_HEADING.findall(rep.get("content") or ""):
            plow = product.strip().lower()
            key = (client, plow)
            if key in seen:
                continue
            seen.add(key)
            parsed.append((client, plow, int(score)))
            matched_clients.add(client)

    rows = []
    for p in products:
        cname_low = (p.get("name") or "").strip().lower()
        if not cname_low:
            continue
        # Clients whose report names this product (substring match, mirroring
        # _extract_product_section). parsed is newest-first, so first per client wins.
        per_client: dict[str, int] = {}
        for client, plow, score in parsed:
            if cname_low in plow and client not in per_client:
                per_client[client] = score
        bands: dict[str, list] = {}
        total = 0
        for client, score in per_client.items():
            if score < min_score:
                continue
            bands.setdefault(str(score), []).append(client)
            total += 1
        for k in bands:
            bands[k].sort(key=str.lower)
        rows.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "is_focus": bool(p.get("is_focus")),
            "category": p.get("category"),
            "total": total,
            "bands": bands,
        })
    # Focus products first, then most opportunities, then name.
    rows.sort(key=lambda r: (not r["is_focus"], -r["total"], (r["name"] or "").lower()))
    return rows, len(matched_clients)


@router.get("/api/opportunities")
async def get_opportunities(
    focus_only: bool = False,
    min_score: int = 5,
    user: dict = Depends(current_user),
):
    """Product-centric pivot of sales opportunities: for each product, how many
    companies fall in each match-fit band (10 → min_score) and which they are.

    Band score = the per-(product, client) match_report `[N/10]` fit score.
    """
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    min_score = max(1, min(10, min_score))
    org_id = user["org_id"]

    products = await db_module.list_products(org_id, focus_only=focus_only)
    reports = await db_module.get_match_reports(org_id)          # newest-first
    all_clients = await db_module.list_clients(org_id)

    rows, matched = _aggregate_opportunities(products, reports, min_score)
    return {
        "products": rows,
        "min_score": min_score,
        "matched_clients": matched,
        "total_clients": len(all_clients or []),
    }


# ---------------------------------------------------------------------------
# Per-product chat
# ---------------------------------------------------------------------------

async def _get_or_create_product_session(product: dict, user: dict) -> Optional[int]:
    """Return the chat session ID for this product, creating one if needed."""
    meta = product.get("metadata") or {}
    session_id = meta.get("chat_session_id")
    if not session_id:
        session = await db_module.create_chat_session(
            org_id=user["org_id"],
            user_id=user["id"],
            title=f"Product: {product['name']}",
        )
        if session:
            session_id = session["id"]
            await db_module.update_product(
                product["id"], user["org_id"],
                {"metadata": {**meta, "chat_session_id": session_id}},
            )
    return session_id


@router.get("/api/products/{product_id}/chat")
async def get_product_chat(product_id: int, user: dict = Depends(current_user)):
    """Return (or lazily create) the chat session for this product."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    session_id = await _get_or_create_product_session(product, user)
    if not session_id:
        return {"session_id": None, "messages": []}

    session = await db_module.get_chat_session(session_id, user["org_id"])
    return {"session_id": session_id, "messages": (session or {}).get("messages", [])}


@router.post("/api/products/{product_id}/chat")
async def product_chat_endpoint(product_id: int, body: dict, user: dict = Depends(current_user)):
    """Simple Ollama chat scoped to one product — no tool-calling loop needed."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    db_module.log_prompt(user["org_id"], user["id"], "product_chat", message,
                         {"product_id": product_id, "product_name": product["name"]})

    session_id = await _get_or_create_product_session(product, user)

    # Load last 5 turns for multi-turn context
    history: list[dict] = []
    if session_id:
        sess = await db_module.get_chat_session(session_id, user["org_id"])
        for m in ((sess or {}).get("messages") or [])[-10:]:
            role = "assistant" if m.get("role") == "ai" else m.get("role", "user")
            history.append({"role": role, "content": m["content"]})

    features_str = ", ".join((product.get("key_features") or [])[:6]) or "not specified"
    system = (
        f"You are a product expert helping verify and enrich research on {product['name']}.\n\n"
        f"Product profile:\n"
        f"  Category: {product.get('category') or 'not specified'}\n"
        f"  Description: {product.get('description') or 'not yet described'}\n"
        f"  Key Features: {features_str}\n"
        f"  Pricing: {product.get('pricing_info') or 'not specified'}\n"
        f"  Target Customer: {product.get('target_customer') or 'not specified'}\n\n"
        "Ask one focused question at a time to clarify or fill gaps. When the user "
        "provides corrections, confirm what you understood. Be concise and conversational."
    )
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": message}]

    answer = "_(AI temporarily unavailable)_"
    try:
        # Provider/model come from the llm.py "chat" role (legacy pi_chat_* /
        # agent_service_* keys); an explicit model from the request wins.
        answer = await llm.acomplete(
            messages=messages, role="chat", model=body.get("model") or None, timeout=90,
        )
    except Exception as exc:
        logger.error("Product chat error: %s", exc)

    if session_id:
        await db_module.append_chat_turn(session_id, user["org_id"], message, answer, [])

    return {"answer": answer, "session_id": session_id}


# ---------------------------------------------------------------------------
# Bulk mail generator — generate individualised emails for multiple clients
# ---------------------------------------------------------------------------
#
# Both mail endpoints support two modes:
#   default          — synchronous: generate everything, return results (kept for
#                      tests and small batches)
#   async_mode: true — enqueue a background job and return {job_id} immediately;
#                      poll GET /api/products/mail-jobs/{job_id}. Long batches
#                      previously exceeded Cloudflare's ~100s gateway timeout.

_mail_jobs: dict[str, dict] = {}
_MAIL_JOB_TTL_SECS = 3600


def _evict_mail_jobs() -> None:
    cutoff = time.time() - _MAIL_JOB_TTL_SECS
    for key in [k for k, j in _mail_jobs.items() if j["created_at"] < cutoff]:
        _mail_jobs.pop(key, None)


def _start_mail_job(org_id: int, total: int, generate_all) -> str:
    """Register a job and run `generate_all(results_sink)` in the background."""
    _evict_mail_jobs()
    job_id = uuid.uuid4().hex
    job = {
        "org_id": org_id, "status": "running", "total": total,
        "results": [], "error": None, "created_at": time.time(),
    }
    _mail_jobs[job_id] = job

    async def _runner() -> None:
        try:
            await generate_all(job["results"])
            job["status"] = "done"
        except Exception as exc:
            logger.error("mail job %s failed: %s", job_id, exc)
            job["status"] = "failed"
            job["error"] = str(exc)

    asyncio.create_task(_runner())
    return job_id


@router.get("/api/products/mail-jobs/{job_id}")
async def get_mail_job(job_id: str, user: dict = Depends(current_user)):
    """Poll a bulk/multi mail job — returns progress and the results so far."""
    _evict_mail_jobs()
    job = _mail_jobs.get(job_id)
    if not job or job["org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    results = list(job["results"])
    return {
        "status": job["status"],
        "total": job["total"],
        "done": len(results),
        "results": results,
        "error": job["error"],
        "generated_count": sum(1 for r in results if r.get("error") is None),
        "error_count": sum(1 for r in results if r.get("error") is not None),
    }


@router.post("/api/products/{product_id}/bulk-mail")
async def bulk_mail_for_product(
    product_id: int,
    body: dict,
    user: dict = Depends(current_user),
):
    """Generate personalised outreach emails for a list of clients for one product."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    client_names = body.get("client_names", [])
    if not client_names or not isinstance(client_names, list):
        raise HTTPException(status_code=400, detail="client_names must be a non-empty list")

    template_type = body.get("template_type", "").strip()
    if template_type not in _MAIL_TYPE_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"template_type must be one of: {', '.join(_MAIL_TYPE_LABELS)}",
        )

    product = await db_module.get_product(product_id, user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    org_id = user["org_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    loop = asyncio.get_event_loop()

    db_module.log_prompt(org_id, user["id"], "mail",
                         body.get("custom_instructions") or f"bulk {template_type}",
                         {"template_type": template_type, "clients": len(client_names),
                          "event_name": body.get("event_name"), "product_id": product_id})

    async def _generate_all(results_sink: list) -> None:
        # Build event block once — fetched event content is reused for every client
        event_name = ""
        event_link = ""
        event_block = ""
        if template_type == "event_invitation":
            event_name = (body.get("event_name") or "").strip()
            event_date = (body.get("event_date") or "").strip()
            event_link = (body.get("event_link") or "").strip()
            header_parts = []
            if event_name:
                header_parts.append(f"EVENT NAME: {event_name}")
            if event_date:
                header_parts.append(f"EVENT DATE: {event_date}")
            if event_link:
                header_parts.append(f"EVENT LINK: {event_link}")
            if header_parts:
                event_block = "\n".join(header_parts) + "\n"
            if event_link:
                event_content = await _fetch_event_for_mail(event_link, event_name)
                if event_content:
                    event_block += f"EVENT CONTENT:\n{event_content}\n"

        pname = product.get("name", "")
        pdesc = product.get("description", "")
        product_block = ""
        if pname:
            product_block = f"PRODUCT TO PROMOTE: {pname}\n"
            if pdesc:
                product_block += f"Product description: {pdesc[:300]}\n"

        custom = body.get("custom_instructions", "").strip()
        instructions_block = f"ADDITIONAL INSTRUCTIONS: {custom}\n" if custom else ""

        mail_mode = (body.get("mail_mode") or "general").strip()
        if mail_mode == "general":
            mode_block = (
                "MODE: General / cold outreach. Do NOT reference any prior meetings, calls, conversations, "
                "or existing relationship. Write as if contacting this company for the first time. "
                "Use `[Name]` (NOT `[First Name]`) as the salutation placeholder.\n"
            )
        elif mail_mode == "casual":
            mode_block = (
                "MODE: Casual / informal. Use the LITERAL placeholder `[First Name]` (exactly those characters, "
                "NOT `[Name]`, NOT an invented name) in the salutation — e.g. 'Hi [First Name],' or 'Hallo [First Name],'. "
                "Do NOT invent or guess the recipient's first name. The placeholder will be replaced automatically before sending. "
                "Write in a warm, direct, conversational tone. No 'Sehr geehrte/r', no formal titles, "
                "no full last name in the greeting.\n"
            )
        else:
            mode_block = "Use `[Name]` (NOT `[First Name]`) as the salutation placeholder.\n"

        _sem = asyncio.Semaphore(max(1, int(config.get("mail_concurrency", 4))))

        async def _process_bulk_client(client_name: str) -> dict:
            async with _sem:
                try:
                    client = await db_module.get_client(org_id, client_name)
                    if not client:
                        return {"client_name": client_name, "email": None, "doc_id": None, "error": "client not found"}

                    context = await _build_presentation_prompt_context(org_id, client, product)

                    prompt = _MAIL_TEMPLATE_PROMPT.format(
                        type_label=_MAIL_TYPE_LABELS[template_type],
                        client_name=client_name,
                        event_block=event_block,
                        product_block=product_block,
                        instructions_block=instructions_block,
                        mode_block=mode_block,
                        context=context,
                    )

                    generated = await loop.run_in_executor(None, lambda p=prompt: _call_brain_sync(p))

                    if '---SOURCES---' in generated:
                        _parts = generated.split('---SOURCES---', 1)
                        email_body = _clean_mail_output(_parts[0].strip(), event_name, event_link)
                        sources_reasoning = _parts[1].strip() or None
                    else:
                        email_body = _clean_mail_output(generated.strip(), event_name, event_link)
                        sources_reasoning = None

                    slug = _slugify(client_name)
                    doc_id_str = f"mail-template-{slug}-{template_type}-prod{product_id}-{today}"
                    embedding = await db_module.embed_text(email_body[:512])
                    doc_id = await db_module.index_document(
                        org_id=org_id,
                        doc_id=doc_id_str,
                        doc_type="note",
                        title=f"{client_name} — {_MAIL_TYPE_LABELS[template_type].title()} Email {today}",
                        content=email_body,
                        metadata={
                            "subject": client_name,
                            "brief_type": "mail_template",
                            "template_type": template_type,
                            "product_id": product_id,
                            "generated_date": today,
                            "sources_reasoning": sources_reasoning,
                        },
                        embedding=embedding or [],
                        source="agent",
                        created_by=user["id"],
                    )
                    if doc_id > 0:
                        await db_module.link_document(doc_id, "client", client["id"])

                    sources_list = await _get_source_refs(org_id, client_name)
                    return {"client_name": client_name, "email": email_body, "doc_id": doc_id, "error": None, "sources_reasoning": sources_reasoning, "sources_list": sources_list, "event_name": event_name, "event_link": event_link}

                except Exception as exc:
                    logger.error("bulk_mail error for %s: %s", client_name, exc)
                    return {"client_name": client_name, "email": None, "doc_id": None, "error": str(exc), "event_name": event_name, "event_link": event_link}

        async def _one(name: str) -> None:
            results_sink.append(await _process_bulk_client(name))

        await asyncio.gather(*[_one(n) for n in client_names])

    if body.get("async_mode"):
        job_id = _start_mail_job(org_id, len(client_names), _generate_all)
        return {"job_id": job_id, "total": len(client_names), "status": "running"}

    results: list = []
    await _generate_all(results)
    generated_count = sum(1 for r in results if r.get("error") is None)
    error_count = sum(1 for r in results if r.get("error") is not None)

    return {"results": results, "generated_count": generated_count, "error_count": error_count}


@router.post("/api/products/multi-mail")
async def multi_product_mail(body: dict, user: dict = Depends(current_user)):
    """Generate one email per client pitching multiple selected products."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    product_ids   = body.get("product_ids", [])
    client_names  = body.get("client_names", [])
    template_type = body.get("template_type", "").strip()

    if not isinstance(product_ids, list):
        raise HTTPException(status_code=400, detail="product_ids must be a list")
    if not client_names or not isinstance(client_names, list):
        raise HTTPException(status_code=400, detail="client_names must be a non-empty list")
    if template_type not in _MAIL_TYPE_LABELS:
        raise HTTPException(status_code=400, detail=f"template_type must be one of: {', '.join(_MAIL_TYPE_LABELS)}")

    org_id = user["org_id"]
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    loop   = asyncio.get_event_loop()

    products = []
    for pid in product_ids:
        p = await db_module.get_product(int(pid), org_id)
        if p:
            products.append(p)
    # products may be empty — a deliberate "general email, no product focus" send
    # (introduction, event invite, check-in) per sales-rep request.

    db_module.log_prompt(org_id, user["id"], "mail",
                         (body.get("custom_instructions") or f"multi {template_type}"),
                         {"template_type": template_type, "clients": len(client_names),
                          "event_name": body.get("event_name"),
                          "product_ids": [p["id"] for p in products]})

    async def _generate_all(results_sink: list) -> None:
        # Build event block once — fetched event content is reused for every client
        event_name = ""
        event_link = ""
        event_block = ""
        if template_type == "event_invitation":
            event_name = (body.get("event_name") or "").strip()
            event_date = (body.get("event_date") or "").strip()
            event_link = (body.get("event_link") or "").strip()
            header_parts = []
            if event_name:
                header_parts.append(f"EVENT NAME: {event_name}")
            if event_date:
                header_parts.append(f"EVENT DATE: {event_date}")
            if event_link:
                header_parts.append(f"EVENT LINK: {event_link}")
            if header_parts:
                event_block = "\n".join(header_parts) + "\n"
            if event_link:
                event_content = await _fetch_event_for_mail(event_link, event_name)
                if event_content:
                    event_block += f"EVENT CONTENT:\n{event_content}\n"

        if products:
            product_lines = ["PRODUCTS TO PROMOTE:"]
            for p in products:
                line = f"- {p['name']}"
                if p.get("category"):    line += f" [{p['category']}]"
                if p.get("description"): line += f": {p['description'][:200]}"
                product_lines.append(line)
            product_block = "\n".join(product_lines) + "\n"
        else:
            product_block = (
                "NO PRODUCT FOCUS: Do not pitch, name, or promote any specific product or solution. "
                f"Write a general {_MAIL_TYPE_LABELS[template_type]} email centred on the client and the reason "
                "for reaching out (e.g. the event, a genuine first introduction, or simply staying in touch).\n"
            )

        custom = (body.get("custom_instructions") or "").strip()
        instructions_block = f"ADDITIONAL INSTRUCTIONS: {custom}\n" if custom else ""

        mail_mode = (body.get("mail_mode") or "general").strip()
        if mail_mode == "general":
            mode_block = (
                "MODE: General / cold outreach. Do NOT reference any prior meetings, calls, conversations, "
                "or existing relationship. Write as if contacting this company for the first time. "
                "Use `[Name]` (NOT `[First Name]`) as the salutation placeholder.\n"
            )
        elif mail_mode == "casual":
            mode_block = (
                "MODE: Casual / informal. Use the LITERAL placeholder `[First Name]` (exactly those characters, "
                "NOT `[Name]`, NOT an invented name) in the salutation — e.g. 'Hi [First Name],' or 'Hallo [First Name],'. "
                "Do NOT invent or guess the recipient's first name. The placeholder will be replaced automatically before sending. "
                "Write in a warm, direct, conversational tone. No 'Sehr geehrte/r', no formal titles, "
                "no full last name in the greeting.\n"
            )
        else:
            mode_block = "Use `[Name]` (NOT `[First Name]`) as the salutation placeholder.\n"

        _sem = asyncio.Semaphore(max(1, int(config.get("mail_concurrency", 4))))

        async def _process_multi_client(client_name: str) -> dict:
            async with _sem:
                try:
                    client = await db_module.get_client(org_id, client_name)
                    if not client:
                        return {"client_name": client_name, "email": None, "doc_id": None, "error": "client not found"}

                    context = await _build_brief_context(org_id, client)
                    prompt  = _MAIL_TEMPLATE_PROMPT.format(
                        type_label=_MAIL_TYPE_LABELS[template_type],
                        client_name=client_name,
                        event_block=event_block,
                        product_block=product_block,
                        instructions_block=instructions_block,
                        mode_block=mode_block,
                        context=context,
                    )
                    generated = await loop.run_in_executor(None, lambda p=prompt: _call_brain_sync(p))

                    if '---SOURCES---' in generated:
                        _parts = generated.split('---SOURCES---', 1)
                        email_body = _clean_mail_output(_parts[0].strip(), event_name, event_link)
                        sources_reasoning = _parts[1].strip() or None
                    else:
                        email_body = _clean_mail_output(generated.strip(), event_name, event_link)
                        sources_reasoning = None

                    slug       = _slugify(client_name)
                    doc_id_str = f"mail-template-{slug}-{template_type}-multi-{today}"
                    embedding  = await db_module.embed_text(email_body[:512])
                    doc_id = await db_module.index_document(
                        org_id=org_id, doc_id=doc_id_str, doc_type="note",
                        title=f"{client_name} — {'Multi-Product ' if products else ''}{_MAIL_TYPE_LABELS[template_type].title()} {today}",
                        content=email_body,
                        metadata={
                            "subject": client_name, "brief_type": "mail_template",
                            "template_type": template_type,
                            "product_ids": [p["id"] for p in products],
                            "generated_date": today,
                            "sources_reasoning": sources_reasoning,
                        },
                        embedding=embedding or [], source="agent", created_by=user["id"],
                    )
                    if doc_id > 0:
                        await db_module.link_document(doc_id, "client", client["id"])

                    sources_list = await _get_source_refs(org_id, client_name)
                    return {"client_name": client_name, "email": email_body, "doc_id": doc_id, "error": None, "sources_reasoning": sources_reasoning, "sources_list": sources_list, "event_name": event_name, "event_link": event_link}

                except Exception as exc:
                    logger.error("multi_mail error for %s: %s", client_name, exc)
                    return {"client_name": client_name, "email": None, "doc_id": None, "error": str(exc), "event_name": event_name, "event_link": event_link}

        async def _one(name: str) -> None:
            results_sink.append(await _process_multi_client(name))

        await asyncio.gather(*[_one(n) for n in client_names])

    if body.get("async_mode"):
        job_id = _start_mail_job(org_id, len(client_names), _generate_all)
        return {"job_id": job_id, "total": len(client_names), "status": "running"}

    results: list = []
    await _generate_all(results)
    generated_count = sum(1 for r in results if r.get("error") is None)
    error_count = sum(1 for r in results if r.get("error") is not None)

    return {"results": results, "generated_count": generated_count, "error_count": error_count}
