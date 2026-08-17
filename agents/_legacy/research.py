"""
agents/research.py — Research agent: synthesise all KB knowledge about a client.

run_research(client_name, org_id, run_id) gathers every document linked to a
client and produces a structured research brief written back to the KB as
type=research.

Triggered manually via POST /api/agents/run with agent_type=research,
or by a heartbeat cron job.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

import requests
import yaml

import db as _db
from agents.tools import _write_document

logger = logging.getLogger("whisper.agents.research")

_cfg_path = Path(__file__).parent.parent / "config.yaml"


def _load_ollama_model() -> str:
    try:
        with open(_cfg_path) as f:
            return yaml.safe_load(f).get("ollama_model", "llama3.2")
    except Exception:
        return "llama3.2"


def _call_ollama(prompt: str, model: str) -> str:
    try:
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as exc:
        logger.warning("Ollama call failed: %s", exc)
        return ""


async def run_research(
    client_name: str,
    org_id: int,
    run_id: Optional[int],
) -> dict:
    """
    Gather all KB documents for a client, synthesise with Ollama, write a
    type=research document linked to the client.

    Returns the written doc dict, or {"error": "..."} if client not found.
    """
    model = _load_ollama_model()

    client = await _db.get_client(org_id, client_name)
    if not client:
        return {"error": f"Client '{client_name}' not found"}

    docs = await _db.list_documents(org_id, client_id=client["id"])

    # Build context from all linked documents (cap at 10 to stay within prompt budget)
    meta = client.get("metadata") or {}
    context_parts = [
        f"# {client['name']}",
        f"Sessions: {client.get('session_count', 0)}  |  Last activity: {client.get('last_activity') or 'unknown'}",
        f"Profile: {json.dumps(meta)}",
    ]
    for doc in docs[:10]:
        snippet = (doc.get("content") or "")[:800]
        context_parts.append(f"\n## [{doc['type']}] {doc['title']}\n{snippet}")

    context = "\n".join(context_parts)

    prompt = f"""You are a sales research assistant. Based on the following knowledge base data, write a comprehensive research brief for {client_name}.

{context[:5000]}

Respond in markdown with these sections:

## Company Overview
What is known about this company.

## Key Contacts & Roles
Named people, their roles, and influence level.

## Recent Meeting Highlights
Key topics, decisions, and outcomes from recent sessions.

## Open Action Items
Any outstanding commitments or next steps mentioned.

## Recommended Next Steps
3 concrete actions for the sales team based on the data.

If a section has no data, write "No data available." Be specific and cite facts."""

    loop = asyncio.get_running_loop()
    content = await loop.run_in_executor(None, partial(_call_ollama, prompt, model))

    if not content:
        content = f"## Knowledge Base Data\n\n{context[:3000]}\n\n*(LLM synthesis unavailable — Ollama offline)*"

    doc = await _write_document(
        org_id=org_id,
        agent_run_id=run_id,
        type="research",
        title=f"Research: {client_name}",
        content=content,
        client_name=client_name,
        metadata={
            "research_date": datetime.now(timezone.utc).isoformat(),
            "docs_synthesized": len(docs),
        },
    )

    logger.info(
        "Research brief written for '%s': db_id=%s, %d docs synthesized",
        client_name, doc.get("db_id"), len(docs),
    )
    return {"doc": doc, "client": client_name, "docs_synthesized": len(docs)}
