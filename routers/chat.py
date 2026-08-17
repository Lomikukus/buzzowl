"""
Chat router — POST /api/chat

RAG-based Q&A over the knowledge base. Context is built in layers:
  1. Client + contact roster (always injected — free, covers "what clients do we have?")
  2. Entity spotlight — full profile injected for any client/contact explicitly named
     in the query or scoped via the UI chip
  3. Tool-calling loop (max 5 rounds) — AI calls search_kb, get_client, search_clients,
     get_contact, search_contacts, get_recent_findings, or trigger_research mid-reasoning.
     Fallback: one-shot RAG if Ollama tool calling fails.
"""

import asyncio
import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from routers.auth import _limit
from pydantic import BaseModel

import llm
from context import DB_AVAILABLE, config, db_module
from routers.auth import current_user

router = APIRouter()
logger = logging.getLogger("whisper.chat")

# ---------------------------------------------------------------------------
# Tool definitions (Ollama function-calling format)
# ---------------------------------------------------------------------------

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": (
                "Search the knowledge base for meetings, OSINT reports, research findings, and notes. "
                "Call this with different queries if the first attempt returns thin results — "
                "try synonyms, the company name alone, or the person's name alone. "
                "Always call this before saying you don't have information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "top_k": {"type": "integer", "description": "Number of results (default 8, max 20)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_client",
            "description": (
                "Get the full profile of a client including all linked documents. "
                "Use after search_kb when you need complete detail on a specific company."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact client name (use search_clients if unsure)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contact",
            "description": "Get the full profile of a contact person by exact name, including their role and linked client.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact contact name"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_clients",
            "description": (
                "Fuzzy-match client names — handles typos, partial names, abbreviations. "
                "Always try this before get_client if the name might not match exactly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "partial_name": {"type": "string", "description": "Partial or approximate client name"},
                },
                "required": ["partial_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": "Fuzzy-match contact persons by partial or approximate name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "partial_name": {"type": "string", "description": "Partial or approximate contact name"},
                },
                "required": ["partial_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_findings",
            "description": (
                "Get the latest research findings for a client, sorted by relevance score. "
                "Use this when asking about recent news, OSINT, or what we know about a company."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string", "description": "Exact client name"},
                    "n": {"type": "integer", "description": "Number of findings to return (default 8, max 20)"},
                },
                "required": ["client_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_research",
            "description": (
                "Queue a background research job to fill gaps in our knowledge about a client. "
                "Use this when the user asks about a client but the KB has insufficient data. "
                "The research agent will run autonomously and add findings to the KB. "
                "Always tell the user you've triggered research so they know to check back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string", "description": "Client name to research"},
                    "gaps": {"type": "string", "description": "Description of what information is missing (e.g. 'recent news, financials, key contacts')"},
                },
                "required": ["client_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_seller_products",
            "description": (
                "Get the seller's own product catalog. Use when the user asks about their products, "
                "offerings, or wants to see what Hermes researched about their company's solutions. "
                "Set focus_only=true to show only the products they selected as priorities."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus_only": {"type": "boolean", "description": "If true, return only focus/priority products"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_product_verification",
            "description": (
                "Update a product's details based on seller verification. "
                "Use when the seller corrects or supplements information about one of their products "
                "during the verification conversation. Only updates the specified fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string", "description": "Exact product name to update"},
                    "corrections": {
                        "type": "object",
                        "description": (
                            "Fields to update. Allowed: description, pricing_info, target_customer, "
                            "key_features (array of strings), category."
                        ),
                    },
                },
                "required": ["product_name", "corrections"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_people",
            "description": (
                "Start a people-search agent for a client to find named people (executives, "
                "managers, or a specific role/persona) and add them as contacts. "
                "Use whenever the user asks to find, research, or identify people / contacts / "
                "personas at a company — do not just report that the KB has none. "
                "Pass target_roles to steer toward a persona (e.g. 'CISO, IT-Architekt, DevOps')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string", "description": "Company to find people at"},
                    "target_roles": {"type": "string", "description": "Optional comma-separated roles/personas to prioritise (e.g. 'CISO, IT ops, CTO')"},
                },
                "required": ["client_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create a to-do / follow-up reminder for the rep. Use when the user says things "
                "like 'remind me to…', 'follow up with X on <date>', or 'add a task'. "
                "The task appears on their Home 'My Tasks' list and, if a client is set, feeds the "
                "daily 'who to contact today' queue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task description, e.g. 'Call about renewal'"},
                    "client_name": {"type": "string", "description": "Related client, if any"},
                    "due_date": {"type": "string", "description": "Due date in YYYY-MM-DD"},
                    "notes": {"type": "string", "description": "Optional extra detail"},
                },
                "required": ["title"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    client_name: Optional[str] = None   # explicit scope from UI scope chip
    model: Optional[str] = None          # model override from dropdown
    session_id: Optional[int] = None     # persist turn to this session
    backend: Optional[str] = None        # "pi" → route to Pi agent service; else Ollama RAG
    stream: Optional[bool] = False       # Pi only: return chat_id, poll /api/chat/progress/{id}


# ---------------------------------------------------------------------------
# Cloud chat helpers (llm.py "chat" role)
# ---------------------------------------------------------------------------

def _call_cloud_sync(model: str, system: str, user_msg: str, org_id: Optional[int] = None) -> Optional[str]:
    """One-shot cloud call (no tools). Returns answer string or None."""
    try:
        answer = llm.complete(org_id=org_id, surface="chat", 
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            role="chat", model=model, timeout=120,
        )
    except Exception as exc:
        logger.warning("Cloud chat failed: %s", exc)
        return None
    return answer or None


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _safe_meta(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw or {}


def _build_roster(clients: list[dict], contacts: list[dict]) -> str:
    lines = []
    if clients:
        lines.append("CLIENT ROSTER:")
        for c in clients:
            meta = _safe_meta(c.get("metadata"))
            parts = [c["name"]]
            if c.get("session_count"):
                parts.append(f"{c['session_count']} session{'s' if c['session_count'] != 1 else ''}")
            extras = [f"{f.replace('_',' ')}: {meta[f]}" for f in ("industry", "status", "deal_stage") if meta.get(f)]
            if extras:
                parts.append("(" + ", ".join(extras) + ")")
            if c.get("last_activity"):
                parts.append(f"last active: {str(c['last_activity'])[:10]}")
            lines.append("  - " + " | ".join(parts))
    else:
        lines.append("CLIENT ROSTER: (none)")
    lines.append("")
    if contacts:
        lines.append("CONTACT ROSTER:")
        for ct in contacts[:25]:
            meta = _safe_meta(ct.get("metadata"))
            detail = " · ".join(filter(None, [meta.get("role", ""), meta.get("company", "")]))
            lines.append(f"  - {ct['name']}" + (f" ({detail})" if detail else ""))
    else:
        lines.append("CONTACT ROSTER: (none)")
    return "\n".join(lines)


def _format_client_spotlight(client: dict) -> str:
    meta = _safe_meta(client.get("metadata"))
    lines = [f"FULL PROFILE — {client['name']}:"]
    for field in ("industry", "status", "website", "deal_stage", "deal_value", "notes"):
        if meta.get(field):
            lines.append(f"  {field.replace('_', ' ').title()}: {meta[field]}")
    lines.append(f"  Sessions: {client.get('session_count', 0)}")
    if client.get("last_activity"):
        lines.append(f"  Last active: {str(client.get('last_activity', ''))[:10]}")
    docs = client.get("documents") or []
    if docs:
        lines.append(f"  Linked documents ({len(docs)}):")
        for d in docs[:20]:
            doc_meta = _safe_meta(d.get("metadata"))
            date_str = (doc_meta.get("date") or doc_meta.get("osint_date") or "")[:10]
            date_part = f" [{date_str}]" if date_str else ""
            src = doc_meta.get("source_url", "")
            src_part = f" → {src}" if src else ""
            lines.append(f"    - [{d.get('type','')}]{date_part} {d.get('title','')}{src_part}")
    return "\n".join(lines)


def _detect_mentioned_entities(query: str, clients: list[dict], contacts: list[dict]) -> tuple[list[str], list[str]]:
    q = query.lower()
    matched_clients = [c["name"] for c in clients if c["name"].lower() in q]
    matched_contacts = [ct["name"] for ct in contacts if ct["name"].lower() in q]
    return matched_clients, matched_contacts


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def _run_tool(name: str, args: dict, org_id: int, user_id: Optional[int] = None) -> tuple[str, list[dict]]:
    """Execute one tool call. Returns (result_string, sources_list)."""
    sources: list[dict] = []

    if name == "search_kb":
        query = args.get("query", "").strip()
        if not query:
            return "No query provided.", []
        top_k = min(int(args.get("top_k", 8)), 20)
        results = await db_module.hybrid_search(org_id, query, top_k=top_k)
        if not results:
            return f"No results found for '{query}'. Try a different query or a broader term.", []
        parts = []
        for r in results:
            meta = _safe_meta(r.get("metadata"))
            source_url = meta.get("source_url", "")
            date_str = (
                meta.get("date") or meta.get("osint_date") or
                meta.get("published_date") or meta.get("fetched_at") or ""
            )[:10]
            doc_type = r.get("subtype") or r.get("result_type", "doc")
            sources.append({
                "title": r.get("display_title", ""),
                "type": doc_type,
                "snippet": r.get("snippet", ""),
                "result_type": r.get("result_type", "document"),
                "url": source_url,
            })
            label = f"[{doc_type.upper()}]"
            if date_str:
                label += f" [{date_str}]"
            line = f"{label} {r.get('display_title', '')}\n{r.get('snippet', '')}"
            if source_url:
                line += f"\nSource: {source_url}"
            parts.append(line)
        return "\n\n".join(parts), sources

    if name == "get_client":
        client_name = args.get("name", "").strip()
        if not client_name:
            return "No client name provided.", []
        client = await db_module.get_client(org_id, client_name)
        if not client:
            return f"No client found with name '{client_name}'. Try search_clients to find the correct name.", []
        return _format_client_spotlight(client), []

    if name == "get_contact":
        contact_name = args.get("name", "").strip()
        if not contact_name:
            return "No contact name provided.", []
        contact = await db_module.get_contact(org_id, contact_name)
        if not contact:
            return f"No contact found with name '{contact_name}'. Try search_contacts to find the correct name.", []
        meta = _safe_meta(contact.get("metadata"))
        lines = [f"CONTACT PROFILE — {contact['name']}:"]
        for field in ("role", "email", "phone", "linkedin", "company", "notes"):
            if meta.get(field):
                lines.append(f"  {field.title()}: {meta[field]}")
        if contact.get("last_activity"):
            lines.append(f"  Last active: {str(contact['last_activity'])[:10]}")
        return "\n".join(lines), []

    if name == "search_clients":
        partial = args.get("partial_name", "").strip()
        if not partial:
            return "No search term provided.", []
        results = await db_module.search_clients(org_id, partial)
        if not results:
            return f"No clients found matching '{partial}'.", []
        lines = [f"Clients matching '{partial}':"]
        for c in results:
            meta = _safe_meta(c.get("metadata"))
            detail = " · ".join(filter(None, [meta.get("industry", ""), meta.get("status", "")]))
            lines.append(
                f"  - {c['name']} (sessions: {c.get('session_count', 0)})"
                + (f" [{detail}]" if detail else "")
            )
        return "\n".join(lines), []

    if name == "search_contacts":
        partial = args.get("partial_name", "").strip()
        if not partial:
            return "No search term provided.", []
        results = await db_module.search_contacts(org_id, partial)
        if not results:
            return f"No contacts found matching '{partial}'.", []
        lines = [f"Contacts matching '{partial}':"]
        for ct in results:
            meta = _safe_meta(ct.get("metadata"))
            detail = " · ".join(filter(None, [meta.get("role", ""), meta.get("company", "")]))
            lines.append(f"  - {ct['name']}" + (f" ({detail})" if detail else ""))
        return "\n".join(lines), []

    if name == "get_recent_findings":
        client_name = args.get("client_name", "").strip()
        if not client_name:
            return "No client name provided.", []
        n = min(int(args.get("n", 8)), 20)
        findings = await db_module.get_client_findings(org_id, client_name, n)
        if not findings:
            return (
                f"No research findings found for '{client_name}'. "
                "Consider calling trigger_research to start gathering information."
            ), []
        parts = [f"Research findings for {client_name} ({len(findings)} results):"]
        for f_doc in findings:
            meta = _safe_meta(f_doc.get("metadata"))
            score = meta.get("relevance_score", "?")
            source_url = meta.get("source_url", "")
            date_str = (meta.get("fetched_at") or meta.get("date") or "")[:10]
            sources.append({
                "title": f_doc.get("title", ""),
                "type": "finding",
                "snippet": (f_doc.get("content") or "")[:150],
                "result_type": "document",
                "url": source_url,
            })
            line = f"  [{score}/5]"
            if date_str:
                line += f" [{date_str}]"
            line += f" {f_doc.get('title', '')}"
            if source_url:
                line += f"\n    Source: {source_url}"
            content = (f_doc.get("content") or "")[:300]
            if content:
                line += f"\n    {content}"
            parts.append(line)
        return "\n".join(parts), sources

    if name == "trigger_research":
        client_name = args.get("client_name", "").strip()
        gaps = args.get("gaps", "general research").strip()
        if not client_name:
            return "No client name provided.", []
        if not DB_AVAILABLE:
            return "Database unavailable — cannot queue research.", []

        backend = config.get("agent_service_backend", "python")
        if backend in ("pi", "hermes", "split"):
            try:
                from routers.agents import _fire_agent_service, _watch_agent_service_run
                import asyncio as _asyncio
                task = (
                    f"Research {client_name} in depth. Focus especially on: {gaps}. "
                    "Cover financials, leadership, strategic priorities, recent news, and sales intelligence signals. "
                    "Write individual findings as you go, then produce a comprehensive final report."
                )
                svc_url, svc_run_id = await _fire_agent_service(
                    client_name, org_id,
                    brain=config.get("agent_service_brain", "openrouter"),
                    model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
                    task=task, agent_type="research",
                )
                db_run_id = await db_module.create_agent_run(
                    org_id=org_id, agent_type="research",
                    task=f"Research: {client_name}", trigger_type="manual",
                )
                await db_module.update_agent_run(
                    db_run_id, "running",
                    output={"service_run_id": svc_run_id, "service_url": svc_url},
                )
                _asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name))
                return (
                    f"Research started for '{client_name}' (run #{db_run_id}). "
                    f"Focus: {gaps}. "
                    "The agent is running now — check the /agents dashboard for live progress, "
                    "or ask again in a few minutes once it completes."
                ), []
            except Exception as exc:
                logger.warning("trigger_research via agent service failed: %s", exc)
                return f"Failed to start research for '{client_name}': {exc}", []

        # Python embedded queue fallback
        research_brain = config.get("research_brain") or config.get("agent_brain", "openrouter")
        research_model = config.get("research_model") or config.get("agent_model", "deepseek/deepseek-v4-flash")
        payload: dict = {"source": "chat", "gaps": gaps, "_brain_override": research_brain, "_model_override": research_model}
        task_id = await db_module.enqueue_research_task(
            org_id=org_id,
            subject_type="company",
            subject=client_name,
            task_type="orchestrate",
            payload=payload,
            depth=0,
            priority=8,
        )
        if task_id < 0:
            return f"Failed to queue research for '{client_name}' — DB error.", []
        return (
            f"Research queued for '{client_name}' (task #{task_id}). "
            f"Focus: {gaps}. "
            "The research agent will run in the background. "
            "Tell the user to check the /agents dashboard or ask again in a few minutes."
        ), []

    if name == "find_people":
        client_name = (args.get("client_name") or "").strip()
        if not client_name:
            return "No client name provided for the people search.", []
        if not DB_AVAILABLE:
            return "Database unavailable — cannot start people search.", []
        target_roles = (args.get("target_roles") or "").strip()
        try:
            from routers.agents import _start_people_search
            res = await _start_people_search(
                org_id, client_name, target_roles=target_roles,
                user_id=user_id, trigger_type="chat",
            )
            roles_msg = f" targeting roles: {target_roles}" if target_roles else ""
            return (
                f"Started a people-search agent for '{client_name}'{roles_msg} (run #{res.get('run_id')}). "
                "It will find named people, save profile findings, and add contacts when done — "
                "tell the user to check the client's Contacts in a few minutes."
            ), []
        except Exception as exc:
            logger.warning("find_people tool failed: %s", exc)
            return f"Failed to start the people search for '{client_name}': {exc}", []

    if name == "create_task":
        title = (args.get("title") or "").strip()
        if not title:
            return "No task title provided.", []
        if not DB_AVAILABLE:
            return "Database unavailable — cannot create task.", []
        due_date = None
        if args.get("due_date"):
            try:
                from datetime import date as _date
                due_date = _date.fromisoformat(str(args["due_date"])[:10])
            except (ValueError, TypeError):
                due_date = None
        row = await db_module.create_task(
            org_id, user_id, title,
            client_name=(args.get("client_name") or "").strip() or None,
            notes=(args.get("notes") or "").strip() or None,
            due_date=due_date, source="chat",
        )
        if not row:
            return "Could not create the task.", []
        when = f" due {due_date.isoformat()}" if due_date else ""
        client_bit = f" for {row.get('client_name')}" if row.get("client_name") else ""
        return (
            f'Created task "{title}"{client_bit}{when}. '
            "It's on the rep's Home 'My Tasks' list"
            + (" and will factor into who to contact today." if row.get("client_name") else ".")
        ), []

    if name == "get_seller_products":
        if not DB_AVAILABLE:
            return "Database unavailable.", []
        focus_only = bool(args.get("focus_only", False))
        products = await db_module.list_products(org_id, focus_only=focus_only)
        if not products:
            label = "focus products" if focus_only else "products"
            return f"No {label} found in the product catalog yet.", []
        lines = [f"{'Focus products' if focus_only else 'Product catalog'} ({len(products)} items):"]
        for p in products:
            features = p.get("key_features") or []
            feature_str = ", ".join(features[:3]) if features else ""
            line = f"  - **{p['name']}** [{p.get('category', '?')}]"
            if p.get("description"):
                line += f": {p['description'][:120]}"
            if feature_str:
                line += f"\n    Key features: {feature_str}"
            if p.get("pricing_info"):
                line += f"\n    Pricing: {p['pricing_info']}"
            if p.get("target_customer"):
                line += f"\n    Target: {p['target_customer']}"
            lines.append(line)
        return "\n".join(lines), []

    if name == "update_product_verification":
        if not DB_AVAILABLE:
            return "Database unavailable.", []
        product_name = args.get("product_name", "").strip()
        corrections = args.get("corrections") or {}
        if not product_name:
            return "No product name provided.", []
        _ALLOWED_CORRECTIONS = {"description", "pricing_info", "target_customer", "key_features", "category"}
        patch = {k: v for k, v in corrections.items() if k in _ALLOWED_CORRECTIONS}
        if not patch:
            return f"No valid fields to update. Allowed: {', '.join(sorted(_ALLOWED_CORRECTIONS))}", []
        products = await db_module.list_products(org_id)
        match = next((p for p in products if p["name"].lower() == product_name.lower()), None)
        if not match:
            names = ", ".join(p["name"] for p in products[:10])
            return f"No product named '{product_name}' found. Available: {names}", []
        updated = await db_module.update_product(match["id"], org_id, patch)
        if not updated:
            return f"Failed to update '{product_name}'.", []
        return f"Updated '{product_name}' — fields changed: {', '.join(patch.keys())}.", []

    return f"Unknown tool: {name}", []


# ---------------------------------------------------------------------------
# Tool-calling loop
# ---------------------------------------------------------------------------

async def _run_tool_loop(
    system: str,
    user_msg: str,
    org_id: int,
    model: str,
    history: Optional[list[dict]] = None,
    max_rounds: int = 5,
    user_id: Optional[int] = None,
) -> tuple[Optional[str], list[dict]]:
    """
    Tool-calling loop. Calls the cloud brain with CHAT_TOOLS, executes returned tool calls,
    feeds results back, and repeats up to max_rounds. Returns (answer, sources).
    history: previous turns as [{"role":"user","content":"..."}, {"role":"assistant","content":"..."}, ...]
    Messages stay in llm.py's neutral format; tool_calls arrive normalised as
    {"id", "name", "arguments"(dict)} and tool results always carry tool_call_id.
    """
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    all_sources: list[dict] = []

    for round_num in range(max_rounds):
        data = await llm.achat(list(messages), CHAT_TOOLS, org_id=org_id, surface="chat",
                               role="chat", model=model, timeout=120)

        tool_calls = data.get("tool_calls") or []
        content = (data.get("content") or "").strip()

        if not tool_calls:
            # Direct answer or model doesn't support tool calling
            return content, all_sources

        # Append assistant's tool-call message (neutral format)
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })

        # Execute each tool call and append its result
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("arguments") or {}

            logger.debug("chat tool call: %s %s", tool_name, args)
            result_str, sources = await _run_tool(tool_name, args, org_id, user_id)
            all_sources.extend(sources)
            messages.append({
                "role": "tool",
                "content": result_str,
                "tool_call_id": tc.get("id", ""),
                "tool_name": tool_name,
            })

    # Max rounds exhausted — synthesise a final answer
    messages.append({
        "role": "user",
        "content": f"Based on all the search results above, please answer this question concisely: {user_msg}",
    })
    data = await llm.achat(list(messages), None, role="chat", model=model, timeout=120, org_id=org_id, surface="chat")
    return (data.get("content") or "").strip(), all_sources


# ---------------------------------------------------------------------------
# Pi chat helper
# ---------------------------------------------------------------------------

async def _call_pi_chat(
    message: str,
    org_id: int,
    client_name: Optional[str],
    org_name: str,
    history: list,
) -> tuple[str, list[dict]]:
    pi_url = config.get("agent_service_url_pi", "http://localhost:8001")
    pi_token = config.get("agent_service_token", "")
    headers: dict = {"Content-Type": "application/json"}
    if pi_token:
        headers["Authorization"] = f"Bearer {pi_token}"
    # Always pass brain/model from config — never use the frontend's Ollama model selector
    # (Ollama model names like "qwen3.5" are not valid OpenRouter model IDs)
    pi_brain = config.get("pi_chat_brain") or config.get("agent_service_brain", "openrouter")
    pi_model = config.get("pi_chat_model") or config.get("agent_service_model", "deepseek/deepseek-v4-flash")
    payload: dict = {
        "message": message,
        "org_id": org_id,
        "client_name": client_name,
        "org_name": org_name,
        "history": history,
        "provider": llm.provider_for_brain(pi_brain),
        "brain": pi_brain,
        "model": pi_model,
    }
    _PI_EMPTY = "_(Pi returned no text"  # fallback prefix Pi returns when model gives empty output
    async with httpx.AsyncClient(timeout=httpx.Timeout(95.0)) as client:
        for attempt in range(2):
            r = await client.post(f"{pi_url}/chat", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            answer = data.get("answer", "(no answer)")
            if not answer.startswith(_PI_EMPTY):
                return answer, data.get("sources", [])
            if attempt == 0:
                logger.warning("Pi returned empty answer (DeepSeek quirk) — retrying once")
        return answer, data.get("sources", [])


def _pi_headers() -> dict:
    headers: dict = {"Content-Type": "application/json"}
    pi_token = config.get("agent_service_token", "")
    if pi_token:
        headers["Authorization"] = f"Bearer {pi_token}"
    return headers


async def _start_pi_chat_async(
    message: str,
    org_id: int,
    client_name: Optional[str],
    org_name: str,
    history: list,
) -> str:
    """Enqueue an async (thinking-preview) chat run on Pi; returns the chat_id."""
    pi_url = config.get("agent_service_url_pi", "http://localhost:8001")
    payload = {
        "message": message,
        "org_id": org_id,
        "client_name": client_name,
        "org_name": org_name,
        "history": history,
        "provider": llm.provider_for_brain(
            config.get("pi_chat_brain") or config.get("agent_service_brain", "openrouter")),
        "brain": config.get("pi_chat_brain") or config.get("agent_service_brain", "openrouter"),
        "model": config.get("pi_chat_model") or config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
        "async_mode": True,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        r = await client.post(f"{pi_url}/chat", json=payload, headers=_pi_headers())
        r.raise_for_status()
        return r.json()["chat_id"]


# chat_id → turn info awaiting session persistence (populated by POST /api/chat
# stream mode, consumed exactly once when the progress poll sees status=done).
# Bounded: abandoned entries (user closed the tab) are dropped on overflow.
_pending_chat_turns: dict = {}
_PENDING_CHAT_MAX = 200


@router.get("/api/chat/progress/{chat_id}")
async def chat_progress(chat_id: str, user: dict = Depends(current_user)):
    """Poll an async Pi chat run — live progress events, then the final answer."""
    pending = _pending_chat_turns.get(chat_id)
    if pending and pending.get("org_id") != user["org_id"]:
        raise HTTPException(status_code=404, detail="Chat run not found or expired")
    pi_url = config.get("agent_service_url_pi", "http://localhost:8001")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        r = await client.get(f"{pi_url}/chat/runs/{chat_id}", headers=_pi_headers())
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Chat run not found or expired")
    r.raise_for_status()
    data = r.json()

    if data.get("status") == "failed" and not data.get("answer"):
        data["answer"] = f"_(Pi chat error — {data.get('error') or 'unknown'})_"

    # Persist the turn into the chat session exactly once, on completion
    if data.get("status") in ("done", "failed"):
        turn = _pending_chat_turns.pop(chat_id, None)
        if turn and turn["session_id"] and DB_AVAILABLE:
            try:
                await db_module.append_chat_turn(
                    turn["session_id"], turn["org_id"], turn["message"],
                    data.get("answer", ""), data.get("sources", []),
                )
                if turn["needs_title"]:
                    title = turn["message"][:60].strip()
                    if len(turn["message"]) > 60:
                        title += "…"
                    await db_module.update_chat_session_title(
                        turn["session_id"], turn["org_id"], title,
                    )
            except Exception as exc:
                logger.warning("chat turn persistence failed for %s: %s", chat_id, exc)

    return {
        "status": data.get("status"),
        "events": data.get("events", []),
        "answer": data.get("answer", ""),
        "sources": data.get("sources", []),
        "backend": "pi",
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/api/chat")
@_limit("20/minute")
async def chat_endpoint(request: Request, body: ChatRequest, user: dict = Depends(current_user)):
    org_id = user["org_id"]
    org_name = user.get("org_name", "your organisation")

    if DB_AVAILABLE:
        db_module.log_prompt(org_id, user["id"], "chat", body.message, {
            "client_name": body.client_name, "backend": body.backend,
            "session_id": body.session_id, "model": body.model,
        })

    # ── Pi backend path ───────────────────────────────────────────────────
    if body.backend == "pi":
        history: list[dict] = []
        session = None
        if body.session_id and DB_AVAILABLE:
            session = await db_module.get_chat_session(body.session_id, org_id)
            if session:
                for m in (session.get("messages") or [])[-10:]:
                    if m.get("role") in ("user", "ai"):
                        history.append({"role": m["role"], "content": m["content"]})

        if body.stream:
            # Thinking-preview mode: enqueue on Pi, return the chat_id; the UI
            # polls /api/chat/progress/{chat_id} for live tool-call events.
            chat_id = await _start_pi_chat_async(
                body.message, org_id, body.client_name, org_name, history,
            )
            if len(_pending_chat_turns) > _PENDING_CHAT_MAX:
                _pending_chat_turns.clear()
            _pending_chat_turns[chat_id] = {
                "org_id": org_id,
                "session_id": body.session_id,
                "message": body.message,
                "needs_title": bool(session and session.get("title") == "New conversation"),
            }
            return {"chat_id": chat_id, "backend": "pi"}

        try:
            answer, sources = await _call_pi_chat(
                body.message, org_id, body.client_name, org_name, history,
            )
        except Exception as exc:
            logger.warning("Pi chat failed: %s", exc)
            answer = f"_(Pi chat error — {exc})_"
            sources = []
        if body.session_id and DB_AVAILABLE:
            await db_module.append_chat_turn(body.session_id, org_id, body.message, answer, sources)
            if session and session.get("title") == "New conversation":
                title = body.message[:60].strip()
                if len(body.message) > 60:
                    title += "…"
                await db_module.update_chat_session_title(body.session_id, org_id, title)
        return {"answer": answer, "sources": sources, "backend": "pi"}

    model = body.model or config.get("pi_chat_model") or config.get("agent_service_model", "deepseek/deepseek-v4-flash")

    # ── Layer 1: roster (always injected, free) ───────────────────────────
    clients, contacts = [], []
    if DB_AVAILABLE:
        clients = await db_module.list_clients(org_id)
        contacts = await db_module.list_contacts(org_id)
    roster_str = _build_roster(clients, contacts)

    # ── Layer 2: entity spotlight (exact-name matches, free) ─────────────
    spotlight_parts: list[str] = []
    if body.client_name and DB_AVAILABLE:
        scoped = await db_module.get_client(org_id, body.client_name)
        if scoped:
            spotlight_parts.append(_format_client_spotlight(scoped))

    if DB_AVAILABLE:
        mentioned_clients, mentioned_contacts = _detect_mentioned_entities(
            body.message, clients, contacts
        )
        scoped_lower = (body.client_name or "").lower()
        for cname in mentioned_clients:
            if cname.lower() == scoped_lower:
                continue
            full = await db_module.get_client(org_id, cname)
            if full:
                spotlight_parts.append(_format_client_spotlight(full))
        for ctname in mentioned_contacts:
            ct_match = next((ct for ct in contacts if ct["name"].lower() == ctname.lower()), None)
            if ct_match:
                meta = _safe_meta(ct_match.get("metadata"))
                lines = [f"CONTACT PROFILE — {ct_match['name']}:"]
                for field in ("role", "email", "phone", "linkedin", "company", "notes"):
                    if meta.get(field):
                        lines.append(f"  {field.title()}: {meta[field]}")
                if ct_match.get("last_activity"):
                    lines.append(f"  Last active: {str(ct_match['last_activity'])[:10]}")
                spotlight_parts.append("\n".join(lines))

    scope_line = (
        f"ACTIVE SCOPE: The user is currently focused on '{body.client_name}'. "
        f"Answer questions in the context of this client unless explicitly told otherwise.\n\n"
        if body.client_name else ""
    )
    system_prompt = (
        f"You are a sales intelligence assistant for {org_name}. "
        "You have tools to search the knowledge base — always use them before answering.\n\n"
        f"{scope_line}"
        "RULES:\n"
        "1. Always call search_kb or get_client before saying you lack information. "
        "If the first search is thin, try again with a different query (e.g. company name alone, person name, topic keyword).\n"
        "2. Call search_clients or search_contacts for any name that might have a typo or partial match.\n"
        "3. When you find relevant documents, include their source URLs in your answer "
        "(e.g. 'According to [reuters.com article](url)...'). Never drop source URLs.\n"
        "4. If the KB genuinely lacks general information about a client after searching, call trigger_research "
        "to queue background research, then tell the user what you triggered and to check back shortly.\n"
        "5. To find PEOPLE at a client — executives, or a specific role/persona (e.g. 'IT ops', CISO, CTO, DevOps) — "
        "call find_people with client_name and target_roles. Do this whenever the user asks to find, research, or "
        "identify contacts/people/personas at a company; never just say the KB has none — start the search.\n"
        "6. When the user wants a reminder or follow-up ('remind me to…', 'follow up with X on <date>', 'add a task'), "
        "call create_task with a short title, the client_name if one applies, and a due_date in YYYY-MM-DD.\n"
        "7. Be concise and actionable. Cite the document or source behind every claim.\n\n"
        f"ROSTER (complete client and contact directory — no search needed for names):\n{roster_str}"
    )
    if spotlight_parts:
        system_prompt += "\n\nSPOTLIGHT (full profiles for entities mentioned in this query):\n"
        system_prompt += "\n\n---\n\n".join(spotlight_parts)

    # ── Session history (last 5 turns → multi-turn context) ──────────────
    history: list[dict] = []
    session = None
    if body.session_id and DB_AVAILABLE:
        session = await db_module.get_chat_session(body.session_id, org_id)
        if session:
            for m in (session.get("messages") or [])[-10:]:  # last 10 msgs = 5 turns
                if m.get("role") == "user":
                    history.append({"role": "user", "content": m["content"]})
                elif m.get("role") == "ai":
                    history.append({"role": "assistant", "content": m["content"]})

    # ── Layer 3: tool-calling loop ────────────────────────────────────────
    answer: Optional[str] = None
    sources: list[dict] = []
    try:
        answer, sources = await _run_tool_loop(system_prompt, body.message, org_id, model, history, user_id=user["id"])
    except Exception as exc:
        logger.warning("tool loop failed (%s), falling back to one-shot RAG", exc)
        loop = asyncio.get_event_loop()
        # Fallback: inject top-6 search results and do a single shot
        fallback_ctx = system_prompt
        if DB_AVAILABLE:
            results = await db_module.hybrid_search(org_id, body.message, top_k=6)
            for r in results:
                sources.append({
                    "title": r.get("display_title", ""),
                    "type": r.get("subtype") or r.get("result_type", ""),
                    "snippet": r.get("snippet", ""),
                    "result_type": r.get("result_type", "document"),
                })
            if results:
                docs_ctx_parts = []
                for r in results:
                    meta = _safe_meta(r.get("metadata"))
                    src = meta.get("source_url", "")
                    line = (
                        f"[{(r.get('subtype') or r.get('result_type','doc')).upper()}]"
                        f" {r.get('display_title','')}\n{r.get('snippet','')}"
                    )
                    if src:
                        line += f"\nSource: {src}"
                    docs_ctx_parts.append(line)
                docs_ctx = "\n\n".join(docs_ctx_parts)
                fallback_ctx += f"\n\nSEARCH RESULTS:\n{docs_ctx}"
        try:
            answer = await loop.run_in_executor(
                None, lambda: _call_cloud_sync(model, fallback_ctx, body.message, org_id=user["org_id"])
            )
        except Exception:
            answer = None

    if not answer:
        answer = "(AI temporarily unavailable. Search results are shown below.)"

    # ── Persist turn to session ───────────────────────────────────────────
    if body.session_id and DB_AVAILABLE:
        await db_module.append_chat_turn(body.session_id, org_id, body.message, answer, sources)
        # Auto-title from first message if session is still "New conversation"
        if session and session.get("title") == "New conversation":
            title = body.message[:60].strip()
            if len(body.message) > 60:
                title += "…"
            await db_module.update_chat_session_title(body.session_id, org_id, title)

    return {"answer": answer, "sources": sources}


# ---------------------------------------------------------------------------
# Session management endpoints
# ---------------------------------------------------------------------------

class SessionCreateRequest(BaseModel):
    title: Optional[str] = None
    client_name: Optional[str] = None


@router.post("/api/chat/sessions")
async def create_session(body: SessionCreateRequest, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")
    session = await db_module.create_chat_session(
        org_id=user["org_id"],
        user_id=user["id"],
        title=body.title or "New conversation",
        client_name=body.client_name,
    )
    return session


@router.get("/api/chat/sessions")
async def list_sessions(user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"sessions": []}
    sessions = await db_module.list_chat_sessions(user["org_id"], user["id"])
    return {"sessions": sessions}


@router.get("/api/chat/sessions/{session_id}")
async def get_session(session_id: int, user: dict = Depends(current_user)):
    from fastapi import HTTPException
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    session = await db_module.get_chat_session(session_id, user["org_id"])
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.patch("/api/chat/sessions/{session_id}")
async def rename_session(session_id: int, body: dict, user: dict = Depends(current_user)):
    from fastapi import HTTPException
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    await db_module.update_chat_session_title(session_id, user["org_id"], title)
    return {"ok": True}


@router.delete("/api/chat/sessions/{session_id}")
async def delete_session(session_id: int, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB unavailable")
    await db_module.delete_chat_session(session_id, user["org_id"])
    return {"ok": True}
