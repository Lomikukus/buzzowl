"""
mcp_server.py — Buzzowl MCP server (stdio transport)

Exposes the knowledge base as MCP tools for Claude Code and other agents.
Run standalone: python mcp_server.py
Add to Claude Code MCP config: { "command": "python", "args": ["/abs/path/to/mcp_server.py"] }
"""

import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import yaml
from mcp.server.fastmcp import FastMCP

import db as _db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_cfg_path = Path(__file__).parent / "config.yaml"
with open(_cfg_path) as f:
    _cfg = yaml.safe_load(f)

_DB_URL: str = os.environ.get("DATABASE_URL") or _cfg.get("db_url", "")

# Embedding config — MUST mirror the main server (context.py: config.yaml with
# EMBED_* env overrides). Passing only model+dim to init_db would silently fall
# back to the "ollama" backend and write vectors in a different embedding space
# than the main server's.
_EMBED_BACKEND: str = os.environ.get("EMBED_BACKEND") or _cfg.get("embed_backend", "")
_EMBED_URL: str = os.environ.get("EMBED_URL") or _cfg.get("embed_url", "")
_EMBED_API_KEY: str = os.environ.get("EMBED_API_KEY") or _cfg.get("embed_api_key", "")
_EMBED_MODEL: str = os.environ.get("EMBED_MODEL") or _cfg.get("embed_model", "nomic-embed-text")
_EMBED_DIM: int = int(os.environ.get("EMBED_DIM") or _cfg.get("embed_dim", 768))

# ---------------------------------------------------------------------------
# Lifespan — DB setup/teardown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(server):
    await _db.init_db(
        _DB_URL,
        _EMBED_MODEL,
        _EMBED_DIM,
        embed_backend=_EMBED_BACKEND,
        embed_url=_EMBED_URL,
        embed_api_key=_EMBED_API_KEY,
    )
    yield
    await _db.close_db()

mcp = FastMCP("Buzzowl", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _org_id() -> Optional[int]:
    org = await _db.get_first_org()
    return org["id"] if org else None

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_kb(query: str, type: str = "", client: str = "", top_k: int = 10) -> dict:
    """Search the knowledge base. Returns documents, clients, and contacts matching the query.

    Args:
        query: Search terms (natural language or keywords).
        type: Optional filter — meeting | research | note | osint | summary.
        client: Optional client name to scope the search.
        top_k: Max results to return (default 10).
    """
    org_id = await _org_id()
    if not org_id:
        return {"error": "no org found"}

    client_id: Optional[int] = None
    if client:
        row = await _db.get_client(org_id, client)
        if row:
            client_id = row["id"]

    results = await _db.hybrid_search(
        org_id=org_id,
        query=query,
        doc_type=type or None,
        client_id=client_id,
        top_k=top_k,
    )
    return {"results": results, "count": len(results)}


@mcp.tool()
async def list_clients() -> dict:
    """List all clients in the knowledge base, ordered by meeting activity."""
    org_id = await _org_id()
    if not org_id:
        return {"error": "no org found"}
    clients = await _db.list_clients(org_id)
    return {"clients": clients, "count": len(clients)}


@mcp.tool()
async def get_client(name: str) -> dict:
    """Get full profile and linked documents for a client.

    Args:
        name: Client name (case-insensitive).
    """
    org_id = await _org_id()
    if not org_id:
        return {"error": "no org found"}
    client = await _db.get_client(org_id, name)
    if not client:
        return {"error": f"client '{name}' not found"}
    docs = await _db.list_documents(org_id, client_id=client["id"])
    return {"client": client, "documents": docs, "document_count": len(docs)}


@mcp.tool()
async def write_document(
    type: str,
    title: str,
    content: str,
    client_name: str = "",
    metadata: dict = {},
) -> dict:
    """Write a document to the knowledge base and optionally link it to a client.

    Args:
        type: Document type — research | note | summary | osint.
        title: Document title.
        content: Full markdown content.
        client_name: Optional client to link this document to.
        metadata: Optional extra metadata (JSON object).
    """
    org_id = await _org_id()
    if not org_id:
        return {"error": "no org found"}

    doc_id = hashlib.sha256(f"{title}{time.time()}".encode()).hexdigest()[:16]
    embedding = await _db.embed_text(content[:4000])

    doc_db_id = await _db.index_document(
        org_id=org_id,
        doc_id=doc_id,
        doc_type=type,
        title=title,
        content=content,
        metadata=metadata,
        embedding=embedding,
        source="agent",
    )

    if doc_db_id and doc_db_id != -1 and client_name:
        client = await _db.get_client(org_id, client_name)
        if client:
            await _db.link_document(doc_db_id, "client", client["id"])

    return {"doc_id": doc_id, "db_id": doc_db_id, "title": title, "type": type}


@mcp.tool()
async def update_client_metadata(name: str, patch: dict) -> dict:
    """Merge new fields into a client's metadata.

    Args:
        name: Client name (case-insensitive).
        patch: JSON object with fields to add or update (e.g. {"website": "acme.com"}).
    """
    org_id = await _org_id()
    if not org_id:
        return {"error": "no org found"}
    updated = await _db.update_client_metadata(org_id, name, patch)
    if not updated:
        return {"error": f"client '{name}' not found"}
    return {"client": updated}


@mcp.tool()
async def trigger_agent(agent_type: str, task: str, client_name: str = "") -> dict:
    """Trigger an agent task and return immediately with a run_id to poll for status.

    Args:
        agent_type: research | osint | enrichment | org | meeting-prep.
        task: Natural language task description.
        client_name: Optional client context for the agent.
    """
    import asyncio
    from agents.runner import run_agent

    org_id = await _org_id()
    if not org_id:
        return {"error": "no org found"}

    run_id = await _db.create_agent_run(
        org_id=org_id,
        agent_type=agent_type,
        task=task,
        trigger_type="manual",
    )

    async def _run():
        try:
            await run_agent(run_id, org_id, agent_type, task, client_name=client_name or None)
        except Exception as exc:
            pass  # runner already updates status to failed

    asyncio.create_task(_run())

    return {
        "run_id": run_id,
        "status": "pending",
        "message": f"Agent '{agent_type}' started. Poll GET /api/agents/tasks/{run_id} for status.",
    }


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).parent


@mcp.tool()
async def list_staged_sessions() -> dict:
    """List all sessions in the pipeline staging area with their current status.

    Returns sessions waiting for enrichment, currently being processed by agents,
    or ready for promotion to the knowledge base.
    """
    staged_dir = _BASE_DIR / "data" / "staged"
    if not staged_dir.exists():
        return {"sessions": []}
    sessions = []
    for session_dir in sorted(staged_dir.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        meta_path = session_dir / "metadata.json"
        if meta_path.exists():
            try:
                sessions.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                sessions.append({"session_id": session_dir.name, "status": "staged"})
        else:
            sessions.append({"session_id": session_dir.name, "status": "staged"})
    return {"sessions": sessions, "count": len(sessions)}


@mcp.tool()
async def get_staged_session(session_id: str) -> dict:
    """Get full details for a staged session including its transcript.

    Args:
        session_id: Session ID in YYYYMMDD-HHMMSS format.
    """
    meta_path = _BASE_DIR / "data" / "staged" / session_id / "metadata.json"
    transcript_path = _BASE_DIR / "data" / "raw" / session_id / "transcript.txt"

    if not meta_path.exists():
        return {"error": f"Session {session_id} not found"}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta = {"session_id": session_id, "status": "staged"}

    transcript = ""
    if transcript_path.exists():
        transcript = transcript_path.read_text(encoding="utf-8")

    return {**meta, "transcript": transcript}


@mcp.tool()
async def promote_session(session_id: str) -> dict:
    """Promote a staged session to the knowledge base (vault + DB).

    Call this after enriching a session's entities or when you want to
    manually approve a session for storage.

    Args:
        session_id: Session ID in YYYYMMDD-HHMMSS format.
    """
    import asyncio
    import requests as _req

    try:
        resp = _req.post(
            f"http://localhost:8000/api/pipeline/staged/{session_id}/promote",
            timeout=120,
        )
        return resp.json()
    except Exception as e:
        return {"error": f"Promote failed: {e}"}


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("clients://{name}")
async def resource_client(name: str) -> str:
    """Full client profile as markdown."""
    org_id = await _org_id()
    if not org_id:
        return "# Error\nNo org found."
    client = await _db.get_client(org_id, name)
    if not client:
        return f"# Not Found\nClient '{name}' does not exist."
    docs = await _db.list_documents(org_id, client_id=client["id"])
    lines = [f"# {client['name']}", ""]
    meta = client.get("metadata") or {}
    if meta:
        lines.append("## Profile")
        for k, v in meta.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")
    if docs:
        lines.append(f"## Documents ({len(docs)})")
        for d in docs:
            lines.append(f"- [{d['title']}] ({d['type']}) — {d['created_at']}")
    return "\n".join(lines)


@mcp.resource("documents://{doc_id}")
async def resource_document(doc_id: str) -> str:
    """Document content by doc_id."""
    org_id = await _org_id()
    if not org_id:
        return "# Error\nNo org found."
    doc = await _db.get_document(org_id, doc_id)
    if not doc:
        return f"# Not Found\nDocument '{doc_id}' does not exist."
    lines = [f"# {doc['title']}", f"*Type: {doc['type']} | Created: {doc['created_at']}*", ""]
    lines.append(doc.get("content", ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def research_client(client_name: str) -> str:
    """Pull all documents for a client and ask for a structured summary."""
    return (
        f"You have access to the Buzzowl knowledge base. "
        f"Use the get_client tool to retrieve all information about '{client_name}', "
        f"then use search_kb to find any related documents. "
        f"Produce a structured research summary covering: company overview, key contacts, "
        f"recent meetings and outcomes, open action items, and recommended next steps."
    )


@mcp.prompt()
def meeting_prep(client_name: str) -> str:
    """Generate a pre-meeting brief for a client."""
    return (
        f"You have access to the Buzzowl knowledge base. "
        f"Use get_client and search_kb to gather everything known about '{client_name}'. "
        f"Generate a concise pre-meeting brief covering: who you're meeting (contacts and roles), "
        f"what was discussed last time, open action items from previous meetings, "
        f"key facts about the company, and 3 suggested talking points for this meeting."
    )


@mcp.prompt()
def weekly_brief() -> str:
    """Generate a weekly account health summary across all active clients."""
    return (
        "You have access to the Buzzowl knowledge base. "
        "Use list_clients to get all clients, then for each active client use get_client "
        "to review recent activity. Generate a weekly brief covering: "
        "clients with meetings this week, clients with no activity in 14+ days (flag for follow-up), "
        "open action items across all accounts, and any notable developments or risks."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
