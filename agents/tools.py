"""
agents/tools.py — Tool registry for the agent loop.

build_tools(org_id, agent_run_id) returns a list of Tool instances bound
to the given org. Each tool wraps a db.py function so agents can call them
by name without knowing about org_id or DB internals.

web_search tries SearXNG first (self-hosted Docker, http://localhost:8080),
then falls back to DuckDuckGo. fetch_page uses requests + stdlib html.parser.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Optional

import requests

import db as _db

logger = logging.getLogger("whisper.agents.tools")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema object
    fn: Callable


# ---------------------------------------------------------------------------
# Implementations (take explicit org_id so closures stay clean)
# ---------------------------------------------------------------------------

async def _search_kb(org_id: int, query: str, type: str = "", client: str = "", top_k: int = 10) -> dict:
    client_id: Optional[int] = None
    if client:
        c = await _db.get_client(org_id, client)
        if c:
            client_id = c["id"]
    results = await _db.hybrid_search(
        org_id=org_id, query=query, doc_type=type or None,
        client_id=client_id, top_k=top_k,
    )
    return {"results": results, "count": len(results)}


async def _get_client(org_id: int, name: str) -> dict:
    c = await _db.get_client(org_id, name)
    if not c:
        return {"error": f"client '{name}' not found"}
    docs = await _db.list_documents(org_id, client_id=c["id"])
    return {"client": c, "documents": docs, "document_count": len(docs)}


async def _list_clients(org_id: int) -> dict:
    clients = await _db.list_clients(org_id)
    return {"clients": clients, "count": len(clients)}


async def _write_document(
    org_id: int,
    agent_run_id: Optional[int],
    type: str,
    title: str,
    content: str,
    client_name: str = "",
    metadata: dict = {},
) -> dict:
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
        agent_run_id=agent_run_id,
    )
    if doc_db_id and doc_db_id != -1 and client_name:
        c = await _db.get_client(org_id, client_name)
        if c:
            await _db.link_document(doc_db_id, "client", c["id"])
    return {"doc_id": doc_id, "db_id": doc_db_id, "title": title, "type": type}


async def _update_client_metadata(org_id: int, name: str, patch: dict) -> dict:
    updated = await _db.update_client_metadata(org_id, name, patch)
    if not updated:
        return {"error": f"client '{name}' not found"}
    return {"client": updated}


class _TextExtractor(HTMLParser):
    """Strip tags, keep visible text. Skips script/style/nav/footer blocks."""

    _SKIP = {"script", "style", "nav", "footer", "header", "noscript", "aside"}

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return " ".join(self._chunks)


_SEARXNG_URL = "http://localhost:8080"


def _searxng_search(query: str, n_results: int, language: str = "en") -> list[dict]:
    """Synchronous SearXNG metasearch (self-hosted). Run in executor."""
    resp = requests.get(
        f"{_SEARXNG_URL}/search",
        params={"q": query, "format": "json", "language": language},
        timeout=10,
        headers={"User-Agent": "Buzzowl/1.0"},
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])[:n_results]
    ]


def _ddg_search(query: str, n_results: int) -> list[dict]:
    """Synchronous DuckDuckGo text search. Fallback when SearXNG is unavailable."""
    from ddgs import DDGS
    with DDGS() as ddgs:
        hits = list(ddgs.text(query, max_results=n_results))
    return [
        {"title": h.get("title", ""), "url": h.get("href", ""), "snippet": h.get("body", "")}
        for h in hits
    ]


def _http_fetch(url: str) -> str:
    """Synchronous HTTP GET + text extraction. Run in executor."""
    resp = requests.get(
        url,
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Buzzowl/1.0)"},
        allow_redirects=True,
    )
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "html" not in ct:
        return resp.text[:5000]
    parser = _TextExtractor()
    parser.feed(resp.text)
    return parser.get_text()


async def _web_search(query: str, n_results: int = 5) -> dict:
    loop = asyncio.get_running_loop()
    # Try SearXNG first (self-hosted, aggregates 70+ sources)
    try:
        results = await loop.run_in_executor(None, _searxng_search, query, n_results)
        if results:
            return {"results": results, "query": query, "count": len(results), "source": "searxng"}
        logger.debug("SearXNG returned 0 results for '%s', falling back to DuckDuckGo", query)
    except Exception as exc:
        logger.debug("SearXNG unavailable (%s), falling back to DuckDuckGo", exc)
    # Fallback: DuckDuckGo
    try:
        results = await loop.run_in_executor(None, _ddg_search, query, n_results)
        return {"results": results, "query": query, "count": len(results), "source": "duckduckgo"}
    except Exception as exc:
        logger.warning("web_search failed for '%s': %s", query, exc)
        return {"error": str(exc), "query": query, "results": []}


async def _fetch_page(url: str) -> dict:
    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, _http_fetch, url)
        return {"url": url, "text": text[:5000], "length": len(text)}
    except Exception as exc:
        logger.warning("fetch_page failed for '%s': %s", url, exc)
        return {"error": str(exc), "url": url}


# ---------------------------------------------------------------------------
# Public factory — returns tools bound to org_id
# ---------------------------------------------------------------------------

def build_tools(org_id: int, agent_run_id: Optional[int] = None) -> list[Tool]:
    """Return all tools bound to org_id (and optionally to agent_run_id for attribution)."""

    async def search_kb(query: str, type: str = "", client: str = "", top_k: int = 10) -> dict:
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = 10
        return await _search_kb(org_id, query, type, client, top_k)

    async def get_client(name: str) -> dict:
        return await _get_client(org_id, name)

    async def list_clients() -> dict:
        return await _list_clients(org_id)

    async def write_document(
        type: str, title: str, content: str, client_name: str = "", metadata: dict = {}
    ) -> dict:
        return await _write_document(org_id, agent_run_id, type, title, content, client_name, metadata)

    async def update_client_metadata(name: str, patch: dict) -> dict:
        return await _update_client_metadata(org_id, name, patch)

    async def web_search(query: str, n_results: int = 5) -> dict:
        try:
            n_results = int(n_results)
        except (TypeError, ValueError):
            n_results = 5
        return await _web_search(query, n_results)

    async def fetch_page(url: str) -> dict:
        return await _fetch_page(url)

    return [
        Tool(
            name="search_kb",
            description="Search the knowledge base for documents, clients, and contacts.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms (natural language or keywords)"},
                    "type":  {"type": "string", "description": "Filter: meeting|research|note|osint|summary"},
                    "client": {"type": "string", "description": "Scope to a specific client name"},
                    "top_k": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
                "required": ["query"],
            },
            fn=search_kb,
        ),
        Tool(
            name="get_client",
            description="Get full profile and all linked documents for a client.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Client name (case-insensitive)"}},
                "required": ["name"],
            },
            fn=get_client,
        ),
        Tool(
            name="list_clients",
            description="List all clients in the knowledge base, ordered by meeting activity.",
            parameters={"type": "object", "properties": {}},
            fn=list_clients,
        ),
        Tool(
            name="write_document",
            description=(
                "Write a document to the knowledge base, optionally linked to a client. "
                "Use type='research' for structured summaries, 'note' for freeform notes, "
                "'summary' for meeting summaries, 'osint' for web research findings."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type":        {"type": "string", "description": "research|note|summary|osint"},
                    "title":       {"type": "string", "description": "Document title"},
                    "content":     {"type": "string", "description": "Full markdown content"},
                    "client_name": {"type": "string", "description": "Client to link to (optional)"},
                    "metadata":    {"type": "object", "description": "Extra metadata (optional)"},
                },
                "required": ["type", "title", "content"],
            },
            fn=write_document,
        ),
        Tool(
            name="update_client_metadata",
            description="Merge new fields into a client's metadata profile.",
            parameters={
                "type": "object",
                "properties": {
                    "name":  {"type": "string", "description": "Client name"},
                    "patch": {"type": "object", "description": "Fields to add or update"},
                },
                "required": ["name", "patch"],
            },
            fn=update_client_metadata,
        ),
        Tool(
            name="web_search",
            description="Search the web for recent information about a topic or company.",
            parameters={
                "type": "object",
                "properties": {
                    "query":     {"type": "string", "description": "Search query"},
                    "n_results": {"type": "integer", "description": "Number of results", "default": 5},
                },
                "required": ["query"],
            },
            fn=web_search,
        ),
        Tool(
            name="fetch_page",
            description="Fetch and extract text content from a web page URL.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
            fn=fetch_page,
        ),
    ]
