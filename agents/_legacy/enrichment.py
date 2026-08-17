"""
agents/enrichment.py — Enrichment pipeline for new sessions (Phase 5, Step 1).

run_enrichment(session_id, entities, org_id, run_id) is called by
_trigger_enrichment in server.py after entity extraction completes.

For each company/person extracted from the session:
  1. Web search via DuckDuckGo
  2. Fetch top result page for richer content
  3. Synthesise with Ollama into a structured profile
  4. Write a type=research document linked to the client/contact

Graceful degradation:
- Ollama offline → write raw search snippets as content
- DuckDuckGo error → skip that entity, log, continue
- fetch_page error → skip page fetch, use search snippets only
"""

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Optional

import requests
import yaml

import db as _db
from agents.tools import _web_search, _fetch_page, _write_document

logger = logging.getLogger("whisper.agents.enrichment")

_cfg_path = Path(__file__).parent.parent / "config.yaml"


def _load_agent_config() -> tuple[str, int]:
    try:
        with open(_cfg_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("ollama_model", "llama3.2"), cfg.get("agent_num_ctx", 16384)
    except Exception:
        return "llama3.2", 16384


def _call_ollama(prompt: str, model: str, num_ctx: int) -> str:
    try:
        resp = requests.post(
            "http://localhost:11434/api/chat",
            json={
                "model": model,
                "think": False,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"num_ctx": num_ctx},
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()
    except Exception as exc:
        logger.warning("Ollama synthesis unavailable: %s", exc)
        return ""


async def _synthesize(prompt: str, model: str, num_ctx: int) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_call_ollama, prompt, model, num_ctx))


async def _enrich_company(
    company_name: str,
    org_id: int,
    run_id: Optional[int],
    model: str,
    num_ctx: int,
) -> Optional[dict]:
    logger.info("Enriching company: %s", company_name)

    search = await _web_search(f"{company_name} company overview", n_results=5)
    results = search.get("results") or []

    if not results:
        logger.info("No web results for company '%s' — skipping", company_name)
        return None

    snippets = "\n".join(
        f"- {r['title']}: {r['snippet']}" for r in results[:5] if r.get("snippet")
    )

    # Fetch the top result for deeper content
    fetch_text = ""
    top_url = results[0].get("url", "")
    if top_url:
        page = await _fetch_page(top_url)
        fetch_text = page.get("text", "")[:3000]

    raw = f"Search results:\n{snippets}"
    if fetch_text:
        raw += f"\n\nTop page content:\n{fetch_text}"

    prompt = f"""You are a sales research assistant. Based ONLY on the following web search data, write a company profile for {company_name}.

{raw[:4500]}

Respond in markdown with these sections:

## Overview
What the company does (1-2 sentences).

## Industry & Products
Their market and key offerings (2-4 bullets).

## Size & Signals
Headcount, revenue, funding, or growth signals if mentioned.

## Key Facts
3-5 bullets most relevant to a sales team.

Only include facts supported by the data above. Do not invent information."""

    content = await _synthesize(prompt, model, num_ctx)
    if not content:
        content = f"## Web Search Results\n\n{snippets}"

    doc = await _write_document(
        org_id=org_id,
        agent_run_id=run_id,
        type="research",
        title=f"Company Research: {company_name}",
        content=content,
        client_name=company_name,
        metadata={"enrichment_type": "company_web_search", "search_engine": "duckduckgo"},
    )
    logger.info("Wrote company research doc for '%s' (db_id=%s)", company_name, doc.get("db_id"))
    return doc


async def _enrich_person(
    person_name: str,
    company_names: list[str],
    org_id: int,
    run_id: Optional[int],
    model: str,
    num_ctx: int,
) -> Optional[dict]:
    logger.info("Enriching person: %s", person_name)

    company_hint = company_names[0] if company_names else ""
    query = f"{person_name} {company_hint}".strip()

    search = await _web_search(query, n_results=3)
    results = search.get("results") or []

    if not results:
        logger.info("No web results for person '%s' — skipping", person_name)
        return None

    snippets = "\n".join(
        f"- {r['title']}: {r['snippet']}" for r in results[:3] if r.get("snippet")
    )

    prompt = f"""You are a sales research assistant. Based ONLY on the following search results, write a brief profile for {person_name}.

{snippets}

Respond in markdown with these sections:

## Role & Background
Their current role and relevant experience.

## Key Facts
2-4 bullets relevant to a sales engagement.

If the results are unclear or not about this person, write a short note saying so. Do not invent information."""

    content = await _synthesize(prompt, model, num_ctx)
    if not content:
        content = f"## Web Search Results\n\n{snippets}"

    # Link the contact research doc to the first known company client
    client_name = company_names[0] if company_names else ""

    doc = await _write_document(
        org_id=org_id,
        agent_run_id=run_id,
        type="research",
        title=f"Contact Research: {person_name}",
        content=content,
        client_name=client_name,
        metadata={
            "enrichment_type": "person_web_search",
            "person_name": person_name,
            "search_engine": "duckduckgo",
        },
    )
    logger.info("Wrote contact research doc for '%s' (db_id=%s)", person_name, doc.get("db_id"))
    return doc


async def run_enrichment(
    session_id: str,
    entities: dict,
    org_id: int,
    run_id: Optional[int],
) -> dict:
    """
    Main entry point. Called by _trigger_enrichment in server.py.

    entities: {"companies": [...], "people": [...]}
    Returns: {"enriched": int, "docs": [...], "errors": [...]}
    """
    model, num_ctx = _load_agent_config()
    # companies may be [str] (legacy) or [{"name": str, "confidence": str}] (Phase 11+)
    companies: list[str] = []
    for c in (entities.get("companies") or []):
        if isinstance(c, dict) and c.get("name"):
            companies.append(str(c["name"]).strip())
        elif isinstance(c, str) and c.strip():
            companies.append(c.strip())
    # people may be strings or {"name": ..., "role": ...} dicts — normalise to names
    people: list[str] = []
    for p in (entities.get("people") or []):
        if isinstance(p, dict):
            name = p.get("name", "").strip()
            if name:
                people.append(name)
        elif isinstance(p, str) and p.strip():
            people.append(p.strip())

    logger.info(
        "Starting enrichment for session %s: %d companies, %d people",
        session_id, len(companies), len(people),
    )

    docs: list[dict] = []
    errors: list[dict] = []

    for company in companies:
        try:
            doc = await _enrich_company(company, org_id, run_id, model, num_ctx)
            if doc:
                docs.append(doc)
        except Exception as exc:
            logger.warning("Enrichment failed for company '%s': %s", company, exc)
            errors.append({"entity": company, "type": "company", "error": str(exc)})

    for person in people:
        try:
            doc = await _enrich_person(person, companies, org_id, run_id, model, num_ctx)
            if doc:
                docs.append(doc)
        except Exception as exc:
            logger.warning("Enrichment failed for person '%s': %s", person, exc)
            errors.append({"entity": person, "type": "person", "error": str(exc)})

    logger.info(
        "Enrichment complete for session %s: %d docs written, %d errors",
        session_id, len(docs), len(errors),
    )
    return {
        "session_id": session_id,
        "enriched": len(docs),
        "docs": docs,
        "errors": errors,
    }
