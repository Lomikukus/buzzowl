"""
Knowledge router — clients, contacts, documents, and search.

All entity-mutating routes trigger background research/OSINT tasks via pipeline.
Routes requiring authentication use the current_user dependency from auth.py.
"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

import llm
from context import DB_AVAILABLE, console, db_module
from context import _default_org_id, cache_get, cache_set, cache_clear
from routers.auth import current_user
from routers.pipeline import (
    _discover_client_sources,
    _monitor_client,
    _trigger_osint,
    _trigger_research,
)
from agents.tools import _http_fetch

logger = logging.getLogger("whisper.knowledge")

router = APIRouter()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@router.get("/api/search")
async def search(q: str = "", type: str = None, top_k: int = 10, user: dict = Depends(current_user)):
    if not DB_AVAILABLE or not q.strip():
        return {"results": [], "query": q}
    org_id = user["org_id"]
    db_module.log_prompt(org_id, user["id"], "kb_search", q.strip(), {"type": type})
    results = await db_module.hybrid_search(org_id, q.strip(), doc_type=type, top_k=top_k)
    return {"results": results, "query": q}


# ---------------------------------------------------------------------------
# Sessions (read-only listing)
# ---------------------------------------------------------------------------

@router.get("/api/sessions")
async def get_sessions(user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"sessions": []}
    return {"sessions": await db_module.list_documents(user["org_id"], doc_type="meeting")}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.post("/api/documents")
async def create_document(body: dict, user: dict = Depends(current_user)):
    doc_id     = body.get("doc_id", "").strip()
    doc_type   = body.get("type", "note").strip()
    title      = body.get("title", "").strip()
    content    = body.get("content", "")
    metadata   = body.get("metadata", {})
    visibility = body.get("visibility", "shared")
    client_links = body.get("client_links", [])

    if not all([doc_id, title]):
        raise HTTPException(status_code=400, detail="doc_id and title are required")

    embedding = await db_module.embed_text(f"{title}\n{content[:500]}")
    db_id = await db_module.index_document(
        org_id=user["org_id"], doc_id=doc_id, doc_type=doc_type,
        title=title, content=content, metadata=metadata,
        embedding=embedding, source="human",
        created_by=user["id"], visibility=visibility,
    )
    for client_name in client_links:
        client = await db_module.get_client(user["org_id"], client_name)
        if client:
            await db_module.link_document(db_id, "client", client["id"])
    return {"ok": True, "id": db_id}


@router.get("/api/documents/{doc_id}")
async def get_document(doc_id: str, user: dict = Depends(current_user)):
    doc = None
    if doc_id.isdigit():
        doc = await db_module.get_document_by_int_id(user["org_id"], int(doc_id))
    if not doc:
        doc = await db_module.get_document(user["org_id"], doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/api/documents/{doc_id}/download")
async def download_document(doc_id: str, user: dict = Depends(current_user)):
    import re
    from fastapi.responses import Response
    doc = None
    if doc_id.isdigit():
        doc = await db_module.get_document_by_int_id(user["org_id"], int(doc_id))
    if not doc:
        doc = await db_module.get_document(user["org_id"], doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    meta = doc.get("metadata") or {}
    source_url = meta.get("source_url", "")
    created = doc.get("created_at")
    created_str = created.isoformat() if hasattr(created, "isoformat") else str(created or "")

    frontmatter = [
        "---",
        f'title: "{doc["title"].replace(chr(34), chr(39))}"',
        f'type: {doc["type"]}',
        f'created_at: {created_str}',
    ]
    if source_url:
        frontmatter.append(f'source_url: "{source_url}"')
    frontmatter.append("---")
    body_md = "\n".join(frontmatter) + "\n\n" + doc.get("content", "")

    safe = re.sub(r'[^\w\s-]', '', doc["title"]).strip().replace(' ', '_')[:80] + ".md"
    return Response(
        content=body_md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@router.patch("/api/documents/{doc_id}")
async def patch_document(doc_id: str, body: dict, user: dict = Depends(current_user)):
    updated = await db_module.update_document(user["org_id"], doc_id, body)
    if not updated:
        raise HTTPException(status_code=404, detail="Document not found")
    return updated


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

@router.get("/api/clients")
async def get_clients(user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"clients": []}
    org_id = user["org_id"]
    cached = cache_get(("clients", org_id))
    if cached is not None:
        return cached
    result = {"clients": await db_module.list_clients(org_id)}
    cache_set(("clients", org_id), result)
    return result


@router.get("/api/org/members")
async def org_members(user: dict = Depends(current_user)):
    """Org member list (id, username, display_name) for owner-assignment pickers.
    Available to any member — unlike the admin-only /api/auth/users."""
    if not DB_AVAILABLE:
        return {"members": []}
    members = await db_module.list_users(user["org_id"])
    return {"members": [
        {"id": m["id"], "username": m["username"], "display_name": m.get("display_name") or m["username"]}
        for m in members
    ]}


def _valid_member_ids(body_ids, members_ids: set) -> list[int]:
    out = []
    for v in (body_ids or []):
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv in members_ids and iv not in out:
            out.append(iv)
    return out


def _client_owned_by(client: dict, uid: int) -> bool:
    """True if `uid` is the client's primary owner (created_by) or a co-owner
    (metadata.owner_ids). Mirrors the `isMine` rule on the clients page."""
    if client.get("created_by") == uid:
        return True
    meta = client.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    try:
        return uid in [int(x) for x in (meta.get("owner_ids") or [])]
    except (TypeError, ValueError):
        return False


@router.post("/api/clients/{name}/owner")
async def assign_client_owner(name: str, body: dict, user: dict = Depends(current_user)):
    """Assign client ownership. action ∈ set_primary | add_co_owner | remove_co_owner.
    set_primary changes created_by (as if that user added it); the co-owner actions
    edit metadata.owner_ids (shared ownership)."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    cache_clear()
    org_id = user["org_id"]
    action = (body.get("action") or "").strip()
    try:
        target = int(body.get("user_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="user_id is required")
    member_ids = {m["id"] for m in await db_module.list_users(org_id)}
    if target not in member_ids:
        raise HTTPException(status_code=400, detail="user_id is not a member of this org")

    client = await db_module.get_client(org_id, name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if action == "set_primary":
        updated = await db_module.set_client_created_by(org_id, name, target)
        if not updated:
            raise HTTPException(status_code=404, detail="Client not found")
        return {"ok": True, "created_by": target}

    if action in ("add_co_owner", "remove_co_owner"):
        meta = client.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        owners = [int(x) for x in (meta.get("owner_ids") or []) if str(x).isdigit()]
        if action == "add_co_owner" and target not in owners:
            owners.append(target)
        elif action == "remove_co_owner":
            owners = [o for o in owners if o != target]
        await db_module.update_client_metadata(org_id, name, {"owner_ids": owners})
        return {"ok": True, "owner_ids": owners}

    raise HTTPException(status_code=400, detail="action must be set_primary | add_co_owner | remove_co_owner")


@router.post("/api/clients")
async def create_client(body: dict, user: dict = Depends(current_user)):
    cache_clear()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    metadata  = body.get("metadata", {})
    # Owner assignment at creation: owner_id = primary owner (created_by, i.e. "as if
    # they added it"); co_owner_ids = shared owners. Both validated against the org.
    member_ids = {m["id"] for m in await db_module.list_users(user["org_id"])} if DB_AVAILABLE else set()
    try:
        owner_id = int(body["owner_id"]) if body.get("owner_id") else user["id"]
    except (TypeError, ValueError):
        owner_id = user["id"]
    if owner_id not in member_ids and member_ids:
        owner_id = user["id"]
    co_owner_ids = _valid_member_ids(body.get("co_owner_ids"), member_ids)
    if co_owner_ids:
        metadata = {**metadata, "owner_ids": co_owner_ids}
    embedding = await db_module.embed_text(f"{name} {metadata.get('industry', '')}")
    client_id = await db_module.upsert_client(
        org_id=user["org_id"], name=name,
        metadata=metadata, embedding=embedding,
        created_by=owner_id,
    )
    if body.get("skip_research"):
        # Caller will trigger research manually (e.g. CSV bulk import queue)
        return {"ok": True, "id": client_id, "osint_run_id": 0, "research_run_id": 0}

    # Pre-create run rows so IDs are available in the response (used by bulk CSV import to poll completion)
    osint_run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type="osint",
        task=f"OSINT: {name}", trigger_type="event_hook",
    ) if DB_AVAILABLE else 0
    research_run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type="research",
        task=f"Research: {name}", trigger_type="event_hook",
    ) if DB_AVAILABLE else 0
    asyncio.create_task(_trigger_osint(name, user["org_id"], run_id=osint_run_id or None))
    asyncio.create_task(_trigger_research(name, user["org_id"], run_id=research_run_id or None))
    asyncio.create_task(_discover_sources_for_new_client(user["org_id"], name))
    return {"ok": True, "id": client_id, "osint_run_id": osint_run_id, "research_run_id": research_run_id}


async def _discover_sources_for_new_client(org_id: int, name: str) -> None:
    """Background: seed monitored news/press sources right after client creation."""
    try:
        client = await db_module.get_client(org_id, name)
        if client:
            await _discover_client_sources(org_id, client)
    except Exception as exc:
        console.print(f"[yellow]source discovery for new client '{name}' failed: {exc}[/yellow]")


def _sniff_csv_delimiter(text: str) -> str:
    """Pick ',' or ';' from the header line. German Excel exports use ';' (the
    comma is the decimal separator here), so accept both instead of assuming."""
    header = next((ln for ln in text.splitlines() if ln.strip()), "")
    return ";" if header.count(";") > header.count(",") else ","


@router.post("/api/clients/bulk-import-csv")
async def bulk_import_clients_csv(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    """Bulk-import clients + contacts from a CSV. Pure pg_trgm fuzzy matching — no LLM."""
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text), delimiter=_sniff_csv_delimiter(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV has no headers")
    reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]

    _COMPANY_COLS = {"company", "company_name", "client", "organisation", "organization"}
    _CONTACT_COLS = {"contact_name", "contact", "name", "first_name"}
    _EMAIL_COLS   = {"email"}
    _ROLE_COLS    = {"role", "title", "job_title"}

    def _pick(row: dict, keys: set) -> str:
        for k in keys:
            v = (row.get(k) or "").strip()
            if v:
                return v
        return ""

    headers = set(reader.fieldnames)
    has_company_col = bool(headers & _COMPANY_COLS)

    # Tell the user how we interpreted their columns — the #1 confusion in import.
    def _detected(cols: set) -> Optional[str]:
        return next((h for h in reader.fieldnames if h in cols), None)
    detected_columns = {
        "company": _detected(_COMPANY_COLS) or ("(first column)" if not has_company_col else None),
        "contact": _detected(_CONTACT_COLS),
        "email":   _detected(_EMAIL_COLS),
        "role":    _detected(_ROLE_COLS),
    }

    clients_created: list[str] = []
    clients_matched: list[dict] = []
    contacts_created = 0
    contacts_updated = 0
    errors: list[str] = []
    rows_processed = 0

    # Cache resolved client names within this import to avoid repeated DB lookups
    _client_id_cache: dict[str, int] = {}

    for i, row in enumerate(reader, start=2):
        rows_processed += 1

        # --- resolve company ---
        if has_company_col:
            raw_company = _pick(row, _COMPANY_COLS)
        else:
            # fallback: use first column value
            raw_company = next(iter(row.values()), "").strip()

        if not raw_company:
            errors.append(f"Row {i}: missing company name")
            continue

        if raw_company in _client_id_cache:
            client_id = _client_id_cache[raw_company]
            canonical = raw_company
        else:
            canonical = await db_module.find_similar_client(user["org_id"], raw_company)
            if canonical:
                clients_matched.append({"raw": raw_company, "canonical": canonical})
                existing = await db_module.get_client(user["org_id"], canonical)
                client_id = existing["id"] if existing else -1
            else:
                canonical = raw_company
                client_id = await db_module.upsert_client(
                    org_id=user["org_id"], name=canonical,
                    metadata={}, embedding=None,
                    created_by=user["id"],
                )
                clients_created.append(canonical)
            _client_id_cache[raw_company] = client_id

        # --- resolve contact (only if contact columns present in CSV) ---
        if not (headers & (_CONTACT_COLS | _EMAIL_COLS)):
            continue  # no contact data in this CSV format

        first = (row.get("first_name") or "").strip()
        last  = (row.get("last_name")  or row.get("lastname") or "").strip()
        contact_name = _pick(row, _CONTACT_COLS)
        if first or last:
            contact_name = f"{first} {last}".strip()

        if not contact_name:
            continue  # company-only row is fine

        email = _pick(row, _EMAIL_COLS)
        role  = _pick(row, _ROLE_COLS)
        metadata = {}
        if email: metadata["email"] = email
        if role:  metadata["role"]  = role
        if first: metadata["first_name"] = first
        if last:  metadata["last_name"]  = last

        try:
            canonical_contact = await db_module.find_similar_contact(user["org_id"], contact_name)
            if canonical_contact:
                await db_module.upsert_contact(
                    org_id=user["org_id"], name=canonical_contact,
                    metadata=metadata, embedding=None,
                    client_id=client_id if client_id > 0 else None,
                    created_by=user["id"],
                )
                contacts_updated += 1
            else:
                await db_module.upsert_contact(
                    org_id=user["org_id"], name=contact_name,
                    metadata=metadata, embedding=None,
                    client_id=client_id if client_id > 0 else None,
                    created_by=user["id"],
                )
                contacts_created += 1
        except Exception as e:
            errors.append(f"Row {i} contact ({contact_name}): {e}")

    if clients_created:
        async def _research_queue(names: list, org_id: int) -> None:
            for name in names:
                await _trigger_osint(name, org_id, await_completion=True)
                await _trigger_research(name, org_id, await_completion=True)
        asyncio.create_task(_research_queue(list(clients_created), user["org_id"]))

    return {
        "clients_created": clients_created,
        "clients_matched": clients_matched,
        "contacts_created": contacts_created,
        "contacts_updated": contacts_updated,
        "errors": errors,
        "rows_processed": rows_processed,
        "detected_columns": detected_columns,
        "headers_seen": reader.fieldnames,
    }


@router.get("/api/clients/csv-template")
async def clients_csv_template(user: dict = Depends(current_user)):
    """Downloadable sample CSV showing the canonical headers + example rows."""
    from fastapi.responses import Response
    sample = (
        "company,contact_name,email,role\n"
        "ACME GmbH,Anna Schmidt,anna.schmidt@acme.de,Head of IT\n"
        "ACME GmbH,Markus Weber,markus.weber@acme.de,CFO\n"
        "Bosch,,,\n"
    )
    return Response(
        content=sample.encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=buzzowl-clients-template.csv"},
    )


def _personalize_mail(template: str, name: str, role: str, first_name: str, casual: bool) -> str:
    """Server-side mirror of WKMail.personalize (static/components/mailtools.js)."""
    fn = first_name or (name.split(" ")[0] if name else "")
    greet = (fn or "[First Name]") if casual else (name or "[Name]")
    return (template or "") \
        .replace("[Name]", greet) \
        .replace("[First Name]", fn or "[First Name]") \
        .replace("[Role]", role or "[Role]")


@router.post("/api/mail/eml-bundle")
async def mail_eml_bundle(body: dict, user: dict = Depends(current_user)):
    """Build a ZIP of RFC-822 .eml drafts — one per contact with an email — from
    already-generated emails. Each .eml carries `X-Unsent: 1` so Outlook opens it
    as an editable draft. The rep drags the folder into Outlook > Drafts.
    Phase 1 of the Outlook-drafts feature (Microsoft Graph one-click comes later).
    """
    import zipfile
    from email.message import EmailMessage
    from email.policy import SMTP as _EML_POLICY  # CRLF line endings (RFC 5322)
    from fastapi.responses import Response
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    org_id = user["org_id"]
    items = body.get("items") or []
    casual = bool(body.get("casual"))
    if not items:
        raise HTTPException(status_code=400, detail="No emails to bundle")

    def _safe(s: str) -> str:
        return (re.sub(r"[^\w.-]+", "_", s or "").strip("_") or "contact")[:60]

    buf = io.BytesIO()
    used: set = set()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            client_name = (item.get("client_name") or "").strip()
            subject = (item.get("subject") or client_name or "Outreach").strip()
            tmpl = item.get("body") or ""
            if not client_name or not tmpl:
                continue
            client = await db_module.get_client(org_id, client_name)
            contacts = await db_module.list_contacts(org_id, client_id=client["id"]) if client else []
            for c in contacts:
                m = c.get("metadata") or {}
                if isinstance(m, str):
                    try:
                        m = json.loads(m)
                    except Exception:
                        m = {}
                email_addr = (m.get("email") or "").strip()
                if not email_addr:
                    continue
                personalized = _personalize_mail(tmpl, c.get("name", ""), m.get("role", ""),
                                                  m.get("first_name", ""), casual)
                # The subject lives in its own header — strip any leading Betreff/Subject line.
                body_text = re.sub(r"^\s*(Betreff|Subject):[^\n]*\n+", "", personalized, flags=re.I)
                msg = EmailMessage()
                msg["To"] = email_addr
                msg["Subject"] = subject
                msg["X-Unsent"] = "1"   # Outlook opens this as an editable draft
                # quoted-printable + utf-8 so umlauts/accents (any language) survive
                # Outlook's import instead of being read as the system codepage.
                msg.set_content(body_text, subtype="plain", cte="quoted-printable")
                fname = f"{_safe(client_name)}__{_safe(c.get('name', ''))}.eml"
                base, n = fname, 2
                while fname in used:
                    fname = f"{base[:-4]}_{n}.eml"
                    n += 1
                used.add(fname)
                zf.writestr(fname, msg.as_bytes(policy=_EML_POLICY))
                written += 1

    if not written:
        raise HTTPException(status_code=400,
                            detail="No contacts with email addresses found for the selected clients")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename=outlook-drafts-{datetime.now().strftime("%Y%m%d")}.zip'},
    )


@router.post("/api/clients/{name}/trigger-research")
async def trigger_client_research(name: str, user: dict = Depends(current_user)):
    """Trigger OSINT + research for a client and return run IDs for polling.
    Used by the CSV import queue to fire research one client at a time.
    """
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    db_module.log_prompt(user["org_id"], user["id"], "research_trigger", name, {"source": "client_page"})
    osint_run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type="osint",
        task=f"OSINT: {name}", trigger_type="event_hook",
    )
    research_run_id = await db_module.create_agent_run(
        org_id=user["org_id"], agent_type="research",
        task=f"Research: {name}", trigger_type="event_hook",
    )
    asyncio.create_task(_trigger_osint(name, user["org_id"], run_id=osint_run_id or None))
    asyncio.create_task(_trigger_research(name, user["org_id"], run_id=research_run_id or None))
    return {"ok": True, "osint_run_id": osint_run_id, "research_run_id": research_run_id}


# ---------------------------------------------------------------------------
# Bulk research — server-side, tab-independent
# ---------------------------------------------------------------------------

# Max clients researched at once — protects the single agent container from a
# 50-client import firing 100+ runs simultaneously.
_BULK_RESEARCH_CONCURRENCY = 3


async def _client_ids_with_research(org_id: int) -> set:
    """Client ids that already have a research/osint/finding/brief document linked."""
    if not (DB_AVAILABLE and db_module._pool):
        return set()
    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT dl.entity_id AS client_id
               FROM document_links dl
               JOIN documents d ON d.id = dl.document_id
               WHERE d.org_id = $1 AND dl.entity_type = 'client'
                 AND d.type IN ('research','osint','finding','brief','client_brief')""",
            org_id,
        )
    return {r["client_id"] for r in rows}


async def resolve_bulk_research_targets(
    org_id: int, names: Optional[list] = None, only_missing: bool = True
) -> list[str]:
    """Resolve the set of client names to (re)research for an org.

    names        explicit client names; when omitted, every client in the org.
    only_missing  skip clients that already have a research/osint/finding doc.
    """
    all_clients = await db_module.list_clients(org_id)
    by_name = {c["name"]: c for c in all_clients}
    if names:
        targets = [by_name[n] for n in names if n in by_name]
    else:
        targets = list(all_clients)
    if only_missing:
        researched = await _client_ids_with_research(org_id)
        targets = [c for c in targets if c["id"] not in researched]
    return [c["name"] for c in targets]


async def _bulk_research_queue(names: list, org_id: int) -> None:
    """Research + OSINT a list of clients from the *server* process, with bounded
    concurrency. Unlike the old per-tab import loop, this survives the browser
    closing — it lives in the server's event loop until every client is done.
    """
    sem = asyncio.Semaphore(_BULK_RESEARCH_CONCURRENCY)
    total = len(names)
    console.print(f"[cyan]Bulk research: queued {total} client(s) for org {org_id}[/cyan]")

    async def _one(name: str, idx: int) -> None:
        async with sem:
            try:
                osint_run_id = await db_module.create_agent_run(
                    org_id=org_id, agent_type="osint",
                    task=f"OSINT: {name}", trigger_type="event_hook",
                )
                research_run_id = await db_module.create_agent_run(
                    org_id=org_id, agent_type="research",
                    task=f"Research: {name}", trigger_type="event_hook",
                )
                # await_completion paces the queue: at most _BULK_RESEARCH_CONCURRENCY
                # clients hold the semaphore (→ agent container) at any moment.
                await _trigger_osint(name, org_id, run_id=osint_run_id or None, await_completion=True)
                await _trigger_research(name, org_id, run_id=research_run_id or None, await_completion=True)
                console.print(f"[dim]Bulk research {idx}/{total}: done '{name}'[/dim]")
            except Exception as exc:
                console.print(f"[yellow]Bulk research failed for '{name}': {exc}[/yellow]")

    await asyncio.gather(*[_one(n, i + 1) for i, n in enumerate(names)])
    console.print(f"[cyan]Bulk research: finished {total} client(s) for org {org_id}[/cyan]")


@router.post("/api/clients/bulk-research")
async def bulk_research_clients(body: dict, user: dict = Depends(current_user)):
    """Fire research + OSINT for many clients server-side, so the work survives the
    browser tab closing. Used by CSV import (Phase 2) and to backfill clients that
    were created without research.

    Body:
      names?: [str]        explicit client names; default = all clients in the org
      only_missing?: bool  (default true) skip clients that already have research
    Returns immediately; the queue runs in the background on the server.
    """
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    names = await resolve_bulk_research_targets(
        user["org_id"], body.get("names"), body.get("only_missing", True)
    )
    if names:
        asyncio.create_task(_bulk_research_queue(list(names), user["org_id"]))
    return {"ok": True, "queued": len(names), "names": names}


# ---------------------------------------------------------------------------
# Monitored sources — list managed per client in metadata.monitored_sources
# ---------------------------------------------------------------------------

_MAX_SOURCES = 6


@router.put("/api/clients/{name}/sources")
async def put_client_sources(name: str, body: dict, user: dict = Depends(current_user)):
    """Replace the client's monitored-sources list (UI add/remove sends the full list).
    Per-source fingerprint state is preserved by URL so edits don't reset baselines."""
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    incoming = body.get("sources", [])
    if not isinstance(incoming, list) or len(incoming) > _MAX_SOURCES:
        raise HTTPException(status_code=400, detail=f"sources must be a list of at most {_MAX_SOURCES}")

    old_by_url = {
        (s.get("url") or "").strip().rstrip("/").lower(): s
        for s in ((client.get("metadata") or {}).get("monitored_sources") or [])
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    cleaned = []
    for s in incoming:
        url = (s.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"Invalid URL: {url[:80]}")
        prev = old_by_url.get(url.rstrip("/").lower(), {})
        cleaned.append({
            "url": url,
            "label": (s.get("label") or "").strip() or prev.get("label") or url.split("/")[2],
            "added": prev.get("added") or now_iso,
            "last_fp": prev.get("last_fp"),
            "last_changed_at": prev.get("last_changed_at"),
            "last_checked_at": prev.get("last_checked_at"),
        })
    await db_module.update_client_metadata(user["org_id"], name, {"monitored_sources": cleaned})
    return {"ok": True, "sources": cleaned}


@router.post("/api/clients/{name}/sources/discover")
async def discover_client_sources_endpoint(name: str, user: dict = Depends(current_user)):
    """Find newsroom/press pages for this client via SearXNG heuristics (no LLM)."""
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    sources = await _discover_client_sources(user["org_id"], client)
    return {"ok": True, "sources": sources}


@router.post("/api/clients/{name}/sources/check")
async def check_client_sources_endpoint(name: str, user: dict = Depends(current_user)):
    """Run the source-monitor check for this client right now. Same behavior as
    the daily sweep (focus clients with changes get researched) minus Telegram."""
    cache_clear()
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    summary = await _monitor_client(user["org_id"], client)
    return {"ok": True, **summary}


@router.get("/api/clients/{name}")
async def get_client(name: str, user: dict = Depends(current_user)):
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    docs = await db_module.list_documents(user["org_id"], client_id=client["id"])
    # Focus is per-rep: is_focus_mine = whether *this* user has starred it.
    # is_focus stays the org-wide union (any rep focuses → researched).
    meta = client.get("metadata") or {}
    focus_ids = meta.get("focus_user_ids") or []
    meta = {**meta, "is_focus_mine": user["id"] in focus_ids}
    return {**client, "metadata": meta, "documents": docs}


@router.patch("/api/clients/{name}")
async def patch_client(name: str, body: dict, user: dict = Depends(current_user)):
    cache_clear()
    patch = dict(body.get("metadata", body))
    # Focus is per-rep — translate is_focus into a focus_user_ids mutation
    # scoped to the current user (keeps the union flag derived server-side).
    updated = None
    if "is_focus" in patch:
        focus = bool(patch.pop("is_focus"))
        updated = await db_module.set_client_focus(
            user["org_id"], name, user["id"], focus
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Client not found")
    if patch:
        updated = await db_module.update_client_metadata(user["org_id"], name, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Client not found")
    return updated


@router.delete("/api/clients")
async def delete_client(name: str = Query(...), user: dict = Depends(current_user)):
    cache_clear()
    deleted = await db_module.delete_client(user["org_id"], name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True, "deleted": name}


@router.get("/api/clients/{name}/docs")
async def get_client_docs(name: str, user: dict = Depends(current_user)):
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    docs = await db_module.list_documents(user["org_id"], client_id=client["id"])
    return {"documents": docs}


@router.get("/api/clients/{name}/findings")
async def get_client_findings(name: str, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if not db_module._pool:
        return {"client": name, "findings": [], "count": 0}

    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.doc_id, d.title, d.content, d.metadata,
                   d.source, d.agent_run_id, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1
              AND d.type = 'finding'
              AND dl.entity_type = 'client'
              AND dl.entity_id = $2
            ORDER BY (d.metadata->>'relevance_score')::int DESC NULLS LAST,
                     d.created_at DESC
            """,
            user["org_id"], client["id"],
        )
    return {"client": name, "findings": [dict(r) for r in rows], "count": len(rows)}


@router.get("/api/clients/{name}/research-docs")
async def get_client_research_docs(name: str, user: dict = Depends(current_user)):
    """Return research and osint documents linked to this client, with full content."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if not db_module._pool:
        return {"client": name, "docs": [], "count": 0}

    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.doc_id, d.type, d.title, d.content, d.metadata,
                   d.source, d.agent_run_id, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1
              AND d.type = ANY(ARRAY['research', 'osint'])
              AND dl.entity_type = 'client'
              AND dl.entity_id = $2
            ORDER BY d.created_at DESC
            """,
            user["org_id"], client["id"],
        )
    return {"client": name, "docs": [dict(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------------------
# Cross-client research / document listing
# ---------------------------------------------------------------------------

@router.get("/api/research/docs")
async def list_research_docs(
    types: str = "research,finding,osint",
    limit: int = 200,
    user: dict = Depends(current_user),
):
    """Return research/finding/osint documents with linked client name, org-scoped."""
    if not DB_AVAILABLE or not db_module._pool:
        return {"documents": [], "count": 0}

    type_list = [t.strip() for t in types.split(",") if t.strip()]
    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.doc_id, d.type, d.title, d.content, d.metadata,
                   d.source, d.created_at,
                   c.name AS client_name
            FROM documents d
            LEFT JOIN document_links dl
              ON dl.document_id = d.id AND dl.entity_type = 'client'
            LEFT JOIN clients c ON c.id = dl.entity_id AND c.org_id = $1
            WHERE d.org_id = $1
              AND d.type = ANY($2)
            ORDER BY d.created_at DESC
            LIMIT $3
            """,
            user["org_id"], type_list, limit,
        )
    docs = [dict(r) for r in rows]
    return {"documents": docs, "count": len(docs)}


# ---------------------------------------------------------------------------
# Client brief generation
# ---------------------------------------------------------------------------

_BRIEF_PROMPT = """\
You are a senior sales intelligence analyst. Using ONLY the data provided below, produce a comprehensive Account Intelligence Brief for the client.

CITATION RULES — follow exactly:
- Every factual claim must carry a citation number in brackets, e.g. "revenue grew 12% [3]".
- Assign each source a unique number starting at [1]. The same source always reuses its number.
- At the end of the brief, include a ## References section listing every cited source as:
  [1] Title — URL  (or "internal document" if no URL)
- Never drop a citation. If you are unsure of the source, write "(unconfirmed)".

Structure the brief with these exact sections (use ## headings):

## Executive Summary
2–3 sentences: who is this client, where is the relationship, what is the single most important thing to know right now.

## Company Overview
Industry, size/scale, key products or services, strategic direction, market position.

## Industry Context
Key trends, regulations, and news affecting this client's industry. Use industry_research documents if available.

## Key Contacts
For each contact: name, role, relevance to the deal. Note influence level if known.

## Relationship & Deal Status
Meeting history, deal stage, deal value if known, last activity date.

## Recent Meeting Highlights
Most important points from the last 1–3 meetings: what was agreed, pain points, open action items.

## Strategic Intelligence
Key research findings, recent news, competitive pressures, market signals.

## Active Signals
Pain points, opportunities, and risks identified by agents. For each: type, headline, evidence.

## Recommended Next Steps
3–5 specific, actionable recommendations for the sales team.

## References
[numbered list of every cited source — title and URL]

---

Rules:
- Use ONLY the data provided below. Do not invent facts.
- If a section has no data, write "(no data yet)".
- Be specific and actionable. Avoid generic sales platitudes.
- Today's date: {today}

---

CLIENT DATA:
{context}
"""


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_MEETING_PREP_PROMPT = """\
You are a senior sales strategist preparing a detailed pre-meeting brief for {client_name} (meeting: {today}).
Using ONLY the data provided below, write a thorough Meeting Preparation Brief.

Use these exact ## headings:

## Context
Company overview, industry, deal stage, deal value if known. 2–4 sentences on where the relationship stands and what this meeting should accomplish.

## Last Discussed
Bullet list of every significant topic, decision, and concern raised in recent meetings. Be specific — include dates, names, and exact commitments where present.

## Open Actions
List all unresolved commitments and follow-ups. For each: what it is, who owns it, and urgency. Use "–" bullets. If none, write "(none recorded)".

## Recent News & Signals
Group by type (News / Opportunity / Risk / Pain Point). For each item include the date if available and a 1-sentence implication for the meeting.

## Talking Points
5–7 specific conversation starters, each grounded in actual data from the brief. Format each as:
**[Topic]** — opener sentence. *Why relevant:* one-line rationale. *Prepare for:* likely response or objection.

## Contacts
For each contact: name, role, email if known. Note their decision-making influence and what they personally care about based on past meetings.

---
Rules:
- Use ONLY the data provided. Do not invent facts.
- If a section has no data, write "(no data available)".
- Be detailed and specific — this brief is read 10 minutes before the meeting.
Today: {today}

---
CLIENT DATA:
{context}
"""


def _load_config_brief() -> dict:
    """Config for non-LLM keys (agent service URL/token, chat model names).
    LLM dispatch itself goes through llm.py — no API keys needed here."""
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    # Apply env-var overrides (same mapping as context.py)
    _env_map = {
        "agent_service_url_pi": "AGENT_PI_URL",
        "agent_service_url":    "AGENT_SERVICE_URL",
        "agent_service_token":  "AGENT_SERVICE_TOKEN",
    }
    for cfg_key, env_key in _env_map.items():
        val = os.environ.get(env_key, "")
        if val:
            cfg[cfg_key] = val
    return cfg


def _call_brain_sync(prompt: str) -> str:
    """Call the configured research brain (cloud) synchronously, with retry.

    The retry/backoff (now inside llm.py) is what makes bulk mail survive
    >10 clients: without it, the first OpenRouter rate-limit (429) on a later
    client failed that email outright.
    """
    return llm.complete(prompt, role="research", timeout=180)


async def _fetch_event_context(event_link: str) -> str:
    """Fetch and extract text from an event URL. Returns empty string on any failure."""
    if not event_link:
        return ""
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, _http_fetch, event_link)
    except Exception as exc:
        logger.warning("_fetch_event_context failed for %r: %s", event_link, exc)
        return ""
    if not text:
        return ""
    # Guard against binary/PDF content: require at least 200 printable ASCII chars
    printable = sum(1 for c in text if 32 <= ord(c) < 127)
    if printable < 200 or (len(text) > 0 and printable / len(text) < 0.75):
        logger.warning("_fetch_event_context: binary/garbled content for %r, skipping", event_link)
        return ""
    return text[:4000]


async def _build_event_block(body: dict) -> str:
    """Build the event context block for mail generation prompts.
    Fetches event_link content if provided. Falls back to name/date/link only on failure.
    """
    template_type = body.get("template_type", "")
    if template_type != "event_invitation":
        return ""

    event_name = (body.get("event_name") or "").strip()
    event_date = (body.get("event_date") or "").strip()
    event_link = (body.get("event_link") or "").strip()

    header_parts = []
    if event_name:
        header_parts.append(f"EVENT NAME: {event_name}")
    if event_date:
        header_parts.append(f"EVENT DATE: {event_date}")
    if event_link:
        header_parts.append(f"EVENT REGISTRATION LINK: {event_link}")

    if not header_parts:
        return ""

    block = "\n".join(header_parts) + "\n"

    if event_link:
        content = await _fetch_event_context(event_link)
        if content:
            block += f"EVENT CONTENT:\n{content}\n"

    return block


async def _fetch_event_via_pi(event_link: str, event_name: str, org_id: int) -> str:
    """Fire one Pi research run to fetch and summarise an event URL.
    Returns the event summary text, or empty string on any failure.
    Called once before the per-client mail loop so Pi is only invoked once.
    """
    if not event_link:
        return ""
    cfg = _load_config_brief()
    pi_url = cfg.get("agent_service_url_pi", cfg.get("agent_service_url", "http://localhost:8001"))
    token  = cfg.get("agent_service_token", "")
    brain  = cfg.get("agent_service_brain", "openrouter")
    model  = cfg.get("agent_service_model", "deepseek/deepseek-v4-flash")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    task = (
        f"Fetch this event page and extract a structured summary for our sales team.\n"
        f"URL: {event_link}\n\n"
        f"Extract: event name, date, location, full agenda with session times and titles, "
        f"speaker names and their roles, key topics and themes, registration link.\n\n"
        f"IMPORTANT: Use only fetch_page to retrieve the URL above. Do NOT perform web searches. "
        f"Write a document (type='event_info', title='Event: {event_name or event_link}') "
        f"with all extracted details."
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{pi_url}/runs",
                json={"agent_type": "research", "task": task, "subject": event_name or "event",
                      "org_id": org_id, "provider": llm.provider_for_brain(brain),
                      "brain": brain, "model": model},
                headers=headers,
            )
            r.raise_for_status()
            svc_run_id = r.json().get("run_id") or r.json().get("id")
    except Exception as exc:
        logger.warning("_fetch_event_via_pi: failed to fire Pi run: %s", exc)
        return ""

    # Poll Pi until done (max 30s — keep total request time under Cloudflare's 100s limit)
    data: dict = {}
    for _ in range(10):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{pi_url}/runs/{svc_run_id}", headers=headers)
                r.raise_for_status()
                data = r.json()
            if data.get("status") in ("done", "failed", "timeout", "cancelled"):
                break
        except Exception as exc:
            logger.warning("_fetch_event_via_pi: poll error: %s", exc)

    if data.get("status") != "done":
        logger.warning("_fetch_event_via_pi: Pi run %s ended with status %s", svc_run_id, data.get("status"))
        return ""

    # Extract content from the write_document tool call
    for tc in reversed(data.get("tool_calls") or []):
        if tc.get("tool") == "write_document":
            args = tc.get("args") or {}
            content = args.get("content", "")
            if content and len(content) > 100:
                return content[:6000]

    # Fallback: use the last fetch_page result if no write_document found
    for tc in reversed(data.get("tool_calls") or []):
        if tc.get("tool") in ("fetch_page", "fetch_page_browser"):
            result = tc.get("result", "")
            if result and len(result) > 100:
                return str(result)[:4000]

    return ""


async def _fetch_event_for_mail(event_link: str, event_name: str) -> str:
    """Fetch event page content for mail context.

    Falls through three methods in order:
      1. SearXNG — full-text search snippets (fast, no JS needed)
      2. browser-service — Playwright/Chrome headless (JS-rendered, most pages)
      3. Camofox — Firefox + fingerprint spoofing (LinkedIn, bot-protected pages)

    Returns extracted text (max ~5 000 chars) or empty string on total failure.
    """
    if not event_link:
        return ""

    searxng_url = os.environ.get("SEARXNG_URL", "http://localhost:8080")
    browser_url = os.environ.get("BROWSER_SERVICE_URL", "http://localhost:3000")
    camofox_url = os.environ.get("CAMOFOX_URL", "http://localhost:9377")

    # 1 — SearXNG: search for event name (or URL) and collect indexed snippets
    try:
        query = event_name or event_link
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{searxng_url}/search",
                params={"q": query, "format": "json", "language": "de-de", "categories": "general"},
                headers={"Accept": "application/json"},
            )
        if r.status_code == 200:
            results = r.json().get("results", [])
            event_domain = urlparse(event_link).netloc
            # Prefer results from the event's own domain
            matching = [res for res in results if event_domain and event_domain in res.get("url", "")]
            candidates = matching[:3] or results[:3]
            snippets = []
            for res in candidates:
                title = res.get("title", "")
                content = res.get("content", "")
                url = res.get("url", "")
                if title or content:
                    snippets.append(f"[{title}] {content} ({url})")
            combined = "\n".join(snippets).strip()
            if len(combined) > 200:
                logger.info("_fetch_event_for_mail: SearXNG returned %d chars for %r", len(combined), event_link)
                return combined[:4000]
        elif r.status_code == 429:
            logger.warning("_fetch_event_for_mail: SearXNG rate-limited (429)")
        else:
            logger.warning("_fetch_event_for_mail: SearXNG returned %s", r.status_code)
    except Exception as exc:
        logger.warning("_fetch_event_for_mail: SearXNG error: %s", exc)

    # Detect login/paywall pages — content from these is useless for mail context
    _LOGIN_WALL = re.compile(
        r'(new to linkedin|sign in to view|please (log|sign) in|'
        r'create.*account.*to view|you must be signed in|'
        r'join now\s+sign in|login required|'
        r'register to (view|access)|members only)',
        re.IGNORECASE,
    )

    # 2 — browser-service: Playwright/Chrome direct fetch (JS-rendered)
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                f"{browser_url}/fetch",
                json={"url": event_link, "max_chars": 5000, "wait_ms": 1500},
            )
        if r.status_code == 200:
            text = (r.json().get("text") or "").strip()
            if len(text) > 200 and text != "(no readable content)" and not _LOGIN_WALL.search(text[:500]):
                logger.info("_fetch_event_for_mail: browser-service returned %d chars for %r", len(text), event_link)
                return text[:5000]
            elif _LOGIN_WALL.search(text[:500]):
                logger.info("_fetch_event_for_mail: browser-service hit login wall for %r, trying Camofox", event_link)
    except Exception as exc:
        logger.warning("_fetch_event_for_mail: browser-service error: %s", exc)

    # 3 — Camofox: Firefox + fingerprint spoofing (anti-bot, LinkedIn, etc.)
    tab_id = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{camofox_url}/tabs",
                json={"userId": "server", "sessionKey": str(uuid.uuid4()), "url": event_link},
            )
        if r.status_code not in (200, 201):
            raise ValueError(f"Camofox /tabs returned {r.status_code}")
        body = r.json()
        tab_id = body.get("tabId") or body.get("id")
        if not tab_id:
            raise ValueError("no tabId in Camofox response")

        await asyncio.sleep(3)  # allow JS to finish rendering

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{camofox_url}/tabs/{tab_id}/snapshot",
                params={"userId": "server"},
            )
        if r.status_code == 200:
            snapshot = r.json().get("snapshot", "")
            text = re.sub(r'\n{3,}', '\n\n', snapshot).replace('\0', '').strip()[:6000]
            if len(text) > 200 and not _LOGIN_WALL.search(text[:500]):
                logger.info("_fetch_event_for_mail: Camofox returned %d chars for %r", len(text), event_link)
                return text
            elif _LOGIN_WALL.search(text[:500]):
                logger.info("_fetch_event_for_mail: Camofox also hit login wall for %r", event_link)
    except Exception as exc:
        logger.warning("_fetch_event_for_mail: Camofox error: %s", exc)
    finally:
        if tab_id and camofox_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.delete(f"{camofox_url}/tabs/{tab_id}", params={"userId": "server"})
            except Exception:
                pass

    logger.warning("_fetch_event_for_mail: all methods failed for %r", event_link)
    return ""


async def _get_source_refs(org_id: int, client_name: str) -> list:
    """Return lightweight source references (top findings + signals) for a client."""
    refs = []
    try:
        findings = await db_module.get_client_findings(org_id, client_name, n=10)
        for f in findings:
            meta = f.get("metadata") or {}
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except Exception: meta = {}
            refs.append({
                "type": "finding",
                "title": f.get("title", ""),
                "doc_id": f.get("doc_id", ""),
                "source_url": meta.get("source_url"),
            })
        signals = await db_module.list_signals(org_id, client_name=client_name, days=365, limit=5)
        for s in signals:
            meta = s.get("metadata") or {}
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except Exception: meta = {}
            refs.append({
                "type": meta.get("signal_type", "signal"),
                "title": s.get("title", ""),
                "doc_id": s.get("doc_id", ""),
                "source_url": meta.get("source_url"),
            })
    except Exception as exc:
        logger.warning("_get_source_refs failed for %s: %s", client_name, exc)
    return refs


async def _build_brief_context(org_id: int, client: dict) -> str:
    """Assemble all KB data for a client into a single context string."""
    client_id = client["id"]
    client_name = client["name"]
    parts: list[str] = []

    # 1. Client profile
    meta = client.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    profile_lines = [f"CLIENT: {client_name}"]
    for field in ("industry", "status", "website", "deal_stage", "deal_value", "employees", "hq", "notes"):
        if meta.get(field):
            profile_lines.append(f"  {field.replace('_',' ').title()}: {meta[field]}")
    profile_lines.append(f"  Sessions: {client.get('session_count', 0)}")
    if client.get("last_activity"):
        profile_lines.append(f"  Last active: {str(client['last_activity'])[:10]}")
    parts.append("\n".join(profile_lines))

    if not db_module._pool:
        return "\n\n---\n\n".join(parts)

    async with db_module._pool.acquire() as conn:
        # 2. Contacts
        contact_rows = await conn.fetch(
            "SELECT name, metadata FROM contacts WHERE org_id=$1 AND client_id=$2",
            org_id, client_id,
        )
        if contact_rows:
            contact_lines = ["CONTACTS:"]
            for r in contact_rows:
                cmeta = r["metadata"] or {}
                if isinstance(cmeta, str):
                    try:
                        cmeta = json.loads(cmeta)
                    except Exception:
                        cmeta = {}
                detail = " · ".join(filter(None, [cmeta.get("role", ""), cmeta.get("email", "")]))
                contact_lines.append(f"  - {r['name']}" + (f" ({detail})" if detail else ""))
            parts.append("\n".join(contact_lines))

        # 3. All linked documents (meetings, research, osint, findings, signals, notes)
        # Exclude previously generated mail templates — they bias the LLM to repeat old content
        doc_rows = await conn.fetch(
            """
            SELECT d.type, d.title, d.content, d.metadata, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1 AND dl.entity_type = 'client' AND dl.entity_id = $2
              AND NOT (d.type = 'note' AND d.metadata->>'brief_type' = 'mail_template')
            ORDER BY d.created_at DESC
            """,
            org_id, client_id,
        )
        for row in doc_rows:
            dmeta = row["metadata"] or {}
            if isinstance(dmeta, str):
                try:
                    dmeta = json.loads(dmeta)
                except Exception:
                    dmeta = {}
            date_str = str(row["created_at"])[:10]
            src = dmeta.get("source_url", "")
            header = f"[{row['type'].upper()}] {row['title']} ({date_str})"
            if src:
                header += f"\nSource: {src}"
            content = (row["content"] or "")[:800]
            parts.append(f"{header}\n{content}")

        # 4. Industry research docs (linked via subject industry match, not client_id)
        industry = meta.get("industry", "")
        if industry:
            industry_rows = await conn.fetch(
                """
                SELECT d.title, d.content, d.created_at
                FROM documents d
                WHERE d.org_id = $1
                  AND d.type = 'industry_research'
                  AND d.metadata->>'industry' ILIKE $2
                ORDER BY d.created_at DESC LIMIT 2
                """,
                org_id, f"%{industry}%",
            )
            for row in industry_rows:
                date_str = str(row["created_at"])[:10]
                content = (row["content"] or "")[:1200]
                parts.append(f"[INDUSTRY_RESEARCH] {row['title']} ({date_str})\n{content}")

    return "\n\n---\n\n".join(parts)


@router.get("/api/clients/{name}/brief")
async def get_client_brief(name: str, user: dict = Depends(current_user)):
    """Return the latest generated brief for a client."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    async with db_module._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id, d.content, d.metadata, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1 AND d.type = 'client_brief'
              AND dl.entity_type = 'client' AND dl.entity_id = $2
            ORDER BY d.created_at DESC LIMIT 1
            """,
            user["org_id"], client["id"],
        )
    if not row:
        return {"brief": None, "generated_at": None}
    return {
        "brief": row["content"],
        "generated_at": str(row["created_at"])[:19],
        "doc_id": row["id"],
    }


async def _auto_generate_brief(org_id: int, client_name: str) -> bool:
    """Generate a brief from an internal call (no HTTP context). Returns True on success."""
    try:
        client = await db_module.get_client(org_id, client_name)
        if not client:
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        context = await _build_brief_context(org_id, client)
        loop = asyncio.get_event_loop()
        prompt = _BRIEF_PROMPT.format(today=today, context=context)
        brief_content = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
        if not brief_content:
            return False
        doc_id_str = f"brief-{hashlib.sha256(client_name.encode()).hexdigest()[:12]}-{today}"
        embedding = await db_module.embed_text(brief_content[:512])
        doc_id = await db_module.index_document(
            org_id=org_id,
            doc_id=doc_id_str,
            doc_type="client_brief",
            title=f"{client_name} — Account Brief {today}",
            content=brief_content,
            metadata={"subject": client_name, "generated_date": today},
            embedding=embedding or [],
            source="agent",
        )
        if doc_id > 0:
            await db_module.link_document(doc_id, "client", client["id"])
        logger.info("_auto_generate_brief: brief generated for '%s'", client_name)
        return True
    except Exception as exc:
        logger.warning("_auto_generate_brief: failed for '%s': %s", client_name, exc)
        return False


@router.post("/api/clients/{name}/brief")
async def generate_client_brief(name: str, user: dict = Depends(current_user)):
    """Generate (or regenerate) the intelligence brief for a client. Runs the cloud model."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    db_module.log_prompt(user["org_id"], user["id"], "brief", name, {"client": name})

    org_id = user["org_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context = await _build_brief_context(org_id, client)

    loop = asyncio.get_event_loop()
    try:
        prompt = _BRIEF_PROMPT.format(today=today, context=context)
        brief_content = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
    except Exception as exc:
        logger.error("Brief generation failed for %s: %s", name, exc)
        raise HTTPException(status_code=502, detail=f"AI call failed: {exc}")

    # Save as type=client_brief document, linked to client
    doc_id_str = f"brief-{hashlib.sha256(name.encode()).hexdigest()[:12]}-{today}"
    embedding = await db_module.embed_text(brief_content[:512])
    doc_id = await db_module.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="client_brief",
        title=f"{name} — Account Brief {today}",
        content=brief_content,
        metadata={"subject": name, "generated_date": today},
        embedding=embedding or [],
        source="agent",
        created_by=user["id"],
    )
    if doc_id > 0:
        await db_module.link_document(doc_id, "client", client["id"])

    return {
        "brief": brief_content,
        "generated_at": today,
        "doc_id": doc_id,
    }


# ---------------------------------------------------------------------------
# Meeting Prep Brief
# ---------------------------------------------------------------------------

async def _build_meeting_prep_context(org_id: int, client: dict) -> str:
    """Rich context for meeting prep: full profile, last 5 meetings, top findings, research, signals, contacts."""
    client_id = client["id"]
    client_name = client["name"]
    parts: list[str] = []

    meta = client.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    profile_lines = [f"CLIENT: {client_name}"]
    for field in ("industry", "status", "deal_stage", "deal_value", "website", "hq", "employees", "assigned_to", "notes"):
        if meta.get(field):
            profile_lines.append(f"  {field.replace('_', ' ').title()}: {meta[field]}")
    if client.get("last_activity"):
        profile_lines.append(f"  Last Activity: {client['last_activity']}")
    if client.get("session_count"):
        profile_lines.append(f"  Total Meetings: {client['session_count']}")
    parts.append("\n".join(profile_lines))

    if not db_module._pool:
        return "\n\n---\n\n".join(parts)

    async with db_module._pool.acquire() as conn:
        meeting_rows = await conn.fetch(
            """
            SELECT d.title, d.content, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1 AND d.type = 'meeting'
              AND dl.entity_type = 'client' AND dl.entity_id = $2
            ORDER BY d.created_at DESC LIMIT 5
            """,
            org_id, client_id,
        )
        for r in meeting_rows:
            date_str = str(r["created_at"])[:10]
            content = (r["content"] or "")[:2000]
            parts.append(f"[MEETING {date_str}] {r['title']}\n{content}")

        findings = await db_module.get_client_findings(org_id, client_name, n=10)
        if findings:
            finding_lines = ["TOP RESEARCH FINDINGS:"]
            for f in findings:
                fmeta = f.get("metadata") or {}
                if isinstance(fmeta, str):
                    try:
                        fmeta = json.loads(fmeta)
                    except Exception:
                        fmeta = {}
                score = fmeta.get("relevance_score", "?")
                src = fmeta.get("source_url", "")
                date_f = str(f.get("created_at", ""))[:10]
                snippet = (f["content"] or "")[:400]
                finding_lines.append(f"  [{score}/5] {f['title']} ({date_f}){' — ' + src if src else ''}\n    {snippet}")
            parts.append("\n".join(finding_lines))

        # Recent research and OSINT documents (summaries, not raw findings)
        research_rows = await conn.fetch(
            """
            SELECT d.title, d.content, d.created_at, d.type
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1 AND d.type IN ('research', 'osint')
              AND dl.entity_type = 'client' AND dl.entity_id = $2
            ORDER BY d.created_at DESC LIMIT 3
            """,
            org_id, client_id,
        )
        for r in research_rows:
            date_str = str(r["created_at"])[:10]
            snippet = (r["content"] or "")[:800]
            parts.append(f"[{r['type'].upper()} REPORT {date_str}] {r['title']}\n{snippet}")

        signals = await db_module.list_signals(org_id, client_name=client_name, days=60, limit=15)
        if signals:
            sig_lines = ["RECENT SIGNALS (last 60 days):"]
            for s in signals:
                smeta = s.get("metadata") or {}
                if isinstance(smeta, str):
                    try:
                        smeta = json.loads(smeta)
                    except Exception:
                        smeta = {}
                stype = smeta.get("signal_type", "news").upper()
                date_s = str(s.get("created_at", ""))[:10]
                evidence = smeta.get("evidence", "")[:200]
                sig_lines.append(f"  [{stype}] {s['title']} ({date_s}): {evidence}")
            parts.append("\n".join(sig_lines))

        contact_rows = await conn.fetch(
            "SELECT name, metadata FROM contacts WHERE org_id=$1 AND client_id=$2 ORDER BY session_count DESC",
            org_id, client_id,
        )
        if contact_rows:
            contact_lines = ["CONTACTS:"]
            for r in contact_rows:
                cmeta = r["metadata"] or {}
                if isinstance(cmeta, str):
                    try:
                        cmeta = json.loads(cmeta)
                    except Exception:
                        cmeta = {}
                detail_parts = []
                for f in ("role", "email", "phone", "linkedin", "influence"):
                    if cmeta.get(f):
                        detail_parts.append(f"{f}: {cmeta[f]}")
                if cmeta.get("notes"):
                    detail_parts.append(f"notes: {cmeta['notes'][:150]}")
                detail = " | ".join(detail_parts)
                contact_lines.append(f"  - {r['name']}" + (f" ({detail})" if detail else ""))
            parts.append("\n".join(contact_lines))

    return "\n\n---\n\n".join(parts)


@router.get("/api/clients/{name}/meeting-prep")
async def get_client_meeting_prep(name: str, user: dict = Depends(current_user)):
    """Return the latest meeting prep brief for a client."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    async with db_module._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id, d.content, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1 AND d.type = 'brief'
              AND d.doc_id LIKE 'meeting-prep-%'
              AND dl.entity_type = 'client' AND dl.entity_id = $2
            ORDER BY d.created_at DESC LIMIT 1
            """,
            user["org_id"], client["id"],
        )
    if not row:
        return {"brief": None, "generated_at": None}
    return {
        "brief": row["content"],
        "generated_at": str(row["created_at"])[:19],
        "doc_id": row["id"],
    }


@router.post("/api/clients/{name}/meeting-prep")
async def generate_client_meeting_prep(name: str, user: dict = Depends(current_user)):
    """Generate a meeting prep brief from last meetings, findings, signals, and contacts."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    db_module.log_prompt(user["org_id"], user["id"], "mp_brief", name, {"client": name})

    org_id = user["org_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx = await _build_meeting_prep_context(org_id, client)

    loop = asyncio.get_event_loop()
    try:
        prompt = _MEETING_PREP_PROMPT.format(client_name=name, today=today, context=ctx)
        brief_content = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
    except Exception as exc:
        logger.error("Meeting prep generation failed for %s: %s", name, exc)
        raise HTTPException(status_code=502, detail=f"AI call failed: {exc}")

    slug = _slugify(name)
    doc_id_str = f"meeting-prep-{slug}-{today}"
    embedding = await db_module.embed_text(brief_content[:512])
    doc_id = await db_module.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="brief",
        title=f"{name} — Meeting Prep {today}",
        content=brief_content,
        metadata={"subject": name, "generated_date": today, "brief_type": "meeting_prep"},
        embedding=embedding or [],
        source="agent",
        created_by=user["id"],
    )
    if doc_id > 0:
        await db_module.link_document(doc_id, "client", client["id"])

    return {
        "brief": brief_content,
        "generated_at": today,
        "doc_id": doc_id,
    }


# ---------------------------------------------------------------------------
# Presentation Prompt Generator (Phase 22)
# ---------------------------------------------------------------------------

_PRES_FIT_SYNTHESIS_PROMPT = """\
Given the following client and product data, write 2–3 specific sentences explaining exactly why this product is a strong fit for this client. Reference specific pain points, signals, or research findings. Be concrete — avoid generic sales language.

CLIENT: {client_name}
Industry: {industry}
Deal Stage: {deal_stage}

PRODUCT: {product_name}
Description: {description}
Target Customer: {target_customer}

PAIN POINTS / RISKS:
{pain_points}

TOP RESEARCH FINDING:
{top_finding}

Write only the 2–3 sentence fit rationale. No preamble, no headings.
"""

_MAIL_TYPE_LABELS = {
    "event_invitation": "event invitation",
    "follow_up": "follow-up",
    "introduction": "introduction / cold outreach",
    "check_in": "relationship check-in",
}

def _clean_mail_output(text: str, event_name: str = "", event_link: str = "") -> str:
    """Strip LLM-generated headers/signatures and append Python-controlled subject/registration lines."""
    lines = text.splitlines()
    # Strip any leading subject/header line the LLM may have added despite instructions
    while lines and re.match(r'(?i)^(subject|betreff|to|an|von|from)\s*:', lines[0].strip()):
        lines.pop(0)
    text = "\n".join(lines).strip()
    # Strip trailing signature block [Your name] / [Your title] / [Company]
    text = re.sub(r'\n+\[Your name\].*', '', text, flags=re.DOTALL | re.IGNORECASE).strip()
    # Strip trailing closing phrase (safety net — LLM sometimes ignores the instruction)
    text = re.sub(
        r'\n+[ \t]*(?:mit\s+(?:freundlichen|besten)\s+gr[üu][ßs]en|beste[nm]?\s+gr[üu][ßs]en?'
        r'|viele\s+gr[üu][ßs]en?|freundliche\s+gr[üu][ßs]en?|herzliche\s+gr[üu][ßs]en?'
        r'|kind\s+regards|best\s+regards|warm\s+regards|yours\s+sincerely|sincerely|regards|cheers)'
        r'[ \t]*\.?\s*$',
        '', text, flags=re.IGNORECASE
    ).strip()
    # Append registration line if link provided (extra blank line after for auto-signature spacing)
    if event_link:
        text = text + f"\n\nAnmeldung: {event_link}\n\n"
    return text


_MAIL_TEMPLATE_PROMPT = """\
You are a senior B2B sales professional writing a personalised outreach email.
Write a {type_label} email from a sales rep to a key contact at {client_name}.

RULES:
- Reference specific facts from CLIENT DATA (pain points, signals, research findings, contacts). No hollow phrases like "I hope this email finds you well".
- Length: 150–250 words for event_invitation/introduction; 80–150 for follow_up/check_in.
- Include one clear call to action at the end.
- Do NOT invent facts. Only assert specific facts (numbers, names, dates, metrics) that appear in a CLIENT DATA document. For each such fact you use, you must be able to name the document it came from.
- When citing a fact from the client's own published source (press release, newsroom article, analyst report), you may note it briefly inline in the email: e.g. "laut Reuters-Bericht vom Mai 2026" or "as reported in Q1 earnings".
- If EVENT CONTENT is provided, reference specific agenda items, speaker names, or session topics that are relevant to this client's situation.
- Do NOT write a subject line, Betreff, or any email header (no "Subject:", "Betreff:", "To:", "An:"). The subject is inserted automatically. Start directly with the greeting/salutation.
- Do NOT add a signature block, sender name, title, or company at the end. The recipient's Outlook auto-signature handles this.
- Do NOT add a closing phrase or sign-off (e.g. "Mit freundlichen Grüßen", "Beste Grüße", "Freundliche Grüße", "Kind regards", "Best regards"). End directly with the call to action.
- Do NOT invent or use the sender's personal name anywhere in the email body. If writing a self-introduction sentence, reference only the company (e.g. "Ich bin von [Company] und betreue…" or "I represent [Company] and…"). Never write a specific person's name.
- Do NOT include any registration link, sign-up URL, or invite URL in the email body. The link will be appended automatically.
{event_block}{product_block}{instructions_block}{mode_block}
OUTPUT:
Write the email body only. For the salutation use the placeholder the MODE specifies:
- Formal / no mode instruction: use `[Name]` (e.g. "Sehr geehrter [Name]," or "Dear [Name],")
- Casual mode: use `[First Name]` (e.g. "Hi [First Name]," or "Hallo [First Name],")
Where it fits naturally you may also use `[Role]` to reference the recipient's title.
Do NOT invent or assume a real person's name — placeholders are replaced before sending.

Then output exactly this separator on its own line:
---SOURCES---

Then output a bulleted list of the key facts you drew on to write this email. For each fact:
- [source_type] "exact quote of the specific fact as used in the email" — Document: <exact title from CLIENT DATA> | URL: <source_url from CLIENT DATA, or "internal" if no URL>

Rules for SOURCES:
- source_type must be one of: pain_point, risk, opportunity, news, finding, research, meeting, contact
- The document title must match a [FINDING], [RESEARCH], [OSINT], [SIGNAL], or [MEETING] entry in CLIENT DATA
- Only list facts actually referenced in the email. Maximum 5 bullets.
- If a fact has no traceable document in CLIENT DATA, do not include it in SOURCES (and do not assert it in the email).

---
CLIENT DATA:
{context}

---
FINAL REMINDER — these instructions override anything above and must be followed exactly for THIS email:
{instructions_block}{mode_block}Write only the email body (no subject, no sign-off, no sender name), then the ---SOURCES--- block.
"""


async def _build_presentation_prompt_context(org_id: int, client: dict, product: dict) -> str:
    """Assemble a Copilot-ready presentation prompt from KB data + LLM-synthesized fit paragraph."""
    client_name = client["name"]
    client_id = client["id"]
    meta = client.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    product_name = product.get("name", "")
    key_features_raw = product.get("key_features") or []
    if isinstance(key_features_raw, str):
        try:
            key_features_raw = json.loads(key_features_raw)
        except Exception:
            key_features_raw = [key_features_raw]
    key_features_bullets = "\n".join(f"- {f}" for f in key_features_raw) if key_features_raw else "(not specified)"

    client_lines = []
    for field in ("industry", "status", "website", "hq", "employees"):
        if meta.get(field):
            client_lines.append(f"{field.replace('_', ' ').title()}: {meta[field]}")

    # Fetch all findings once; split into intel bullets (for pain points section) and
    # sourced findings (for research section). Signals (type='signal') are the future
    # destination once the agent pipeline writes them; for now findings serve both roles.
    all_findings = await db_module.get_client_findings(org_id, client_name, n=25)

    # Also pull any type='signal' docs that do exist (from future runs or manual entry)
    signals = await db_module.list_signals(org_id, client_name=client_name, days=365, limit=20)
    pain_bullets: list[str] = []
    opportunity_bullets: list[str] = []
    for s in signals:
        smeta = s.get("metadata") or {}
        if isinstance(smeta, str):
            try:
                smeta = json.loads(smeta)
            except Exception:
                smeta = {}
        stype = smeta.get("signal_type", "news")
        headline = s.get("title", "")
        evidence = smeta.get("evidence", "")[:150]
        date_s = str(s.get("created_at", ""))[:10]
        bullet = f"- [{stype.replace('_', ' ').upper()}] {headline} ({date_s})" + (f": {evidence}" if evidence else "")
        if stype in ("pain_point", "risk"):
            pain_bullets.append(bullet)
        elif stype == "opportunity":
            opportunity_bullets.append(bullet)

    # If no explicit signals, derive intel bullets from high-score findings
    intel_bullets: list[str] = []
    if not pain_bullets:
        for f in all_findings:
            if len(intel_bullets) >= 6:
                break
            fmeta = f.get("metadata") or {}
            if isinstance(fmeta, str):
                try:
                    fmeta = json.loads(fmeta)
                except Exception:
                    fmeta = {}
            score_raw = fmeta.get("relevance_score")
            try:
                score = int(score_raw)
            except (TypeError, ValueError):
                continue
            if score < 3:
                continue
            snippet = (f.get("content") or "")[:120].replace("\n", " ").strip()
            date_f = str(f.get("created_at", ""))[:10]
            bullet = f"- {f['title']} ({date_f})" + (f": {snippet}…" if snippet else "")
            intel_bullets.append(bullet)

    finding_lines: list[str] = []
    top_finding_text = ""
    finding_idx = 1
    for f in all_findings:
        if finding_idx > 3:
            break
        fmeta = f.get("metadata") or {}
        if isinstance(fmeta, str):
            try:
                fmeta = json.loads(fmeta)
            except Exception:
                fmeta = {}
        score_raw = fmeta.get("relevance_score")
        try:
            score = int(score_raw)
        except (TypeError, ValueError):
            continue
        if score < 3:
            continue
        src = fmeta.get("source_url", "")
        date_f = str(f.get("created_at", ""))[:10]
        snippet = (f.get("content") or "")[:400]
        line = f"{finding_idx}. {f['title']} (relevance: {score}/5, {date_f})"
        if src:
            line += f" — Source: {src}"
        if snippet:
            line += f"\n   {snippet}"
        finding_lines.append(line)
        if finding_idx == 1:
            top_finding_text = f"{f['title']}: {snippet[:200]}"
        finding_idx += 1

    contact_lines: list[str] = []
    match_intel = ""
    if db_module._pool:
        async with db_module._pool.acquire() as conn:
            contact_rows = await conn.fetch(
                "SELECT name, metadata FROM contacts WHERE org_id=$1 AND client_id=$2 ORDER BY session_count DESC",
                org_id, client_id,
            )
            _CONTACT_HIGH = {"cio", "cdo", "cto", "architect", "engineer", "developer", "data",
                             "ai", "digital", "it", "technology", "innovation", "devops",
                             "platform", "cloud", "security", "infrastructure"}
            _CONTACT_LOW = {"supervisory", "board", "chairman", "vorstand", "aufsichtsrat",
                            "investor", "shareholder", "employee representative", "labor",
                            "human resources"}
            scored_contacts = []
            for r in contact_rows:
                cmeta = r["metadata"] or {}
                if isinstance(cmeta, str):
                    try:
                        cmeta = json.loads(cmeta)
                    except Exception:
                        cmeta = {}
                role_lower = (cmeta.get("role") or "").lower()
                if any(k in role_lower for k in _CONTACT_LOW):
                    score_c = 0
                elif any(k in role_lower for k in _CONTACT_HIGH):
                    score_c = 2
                else:
                    score_c = 1
                scored_contacts.append((score_c, r["name"], cmeta))
            scored_contacts.sort(key=lambda x: x[0], reverse=True)
            for _, cname, cmeta in scored_contacts[:6]:
                label = cname
                if cmeta.get("role"):
                    label += f" — {cmeta['role']}"
                detail = []
                if cmeta.get("linkedin"):
                    detail.append(f"LinkedIn: {cmeta['linkedin']}")
                if cmeta.get("email"):
                    detail.append(f"email: {cmeta['email']}")
                contact_lines.append("- " + label + (f" ({', '.join(detail)})" if detail else ""))

            match_rows = await conn.fetch(
                """
                SELECT d.content FROM documents d
                JOIN document_links dl ON dl.document_id = d.id
                WHERE d.org_id = $1 AND d.type = 'match_report'
                  AND dl.entity_type = 'client' AND dl.entity_id = $2
                ORDER BY d.created_at DESC LIMIT 1
                """,
                org_id, client_id,
            )
            if match_rows:
                full_content = match_rows[0]["content"] or ""
                pattern = re.compile(
                    r'##\s+' + re.escape(product_name) + r'.*?(?=\n##\s|\Z)',
                    re.IGNORECASE | re.DOTALL,
                )
                m = pattern.search(full_content)
                if m:
                    match_intel = m.group(0)[:800].strip()

    # LLM call to synthesize the fit rationale
    fit_paragraph = match_intel
    loop = asyncio.get_event_loop()
    effective_pain = pain_bullets or intel_bullets
    pain_for_llm = "\n".join(effective_pain[:5]) if effective_pain else "(no pain points recorded)"
    synthesis_prompt = _PRES_FIT_SYNTHESIS_PROMPT.format(
        client_name=client_name,
        industry=meta.get("industry", "unknown"),
        deal_stage=meta.get("deal_stage", "unknown"),
        product_name=product_name,
        description=(product.get("description") or "")[:300],
        target_customer=product.get("target_customer") or "not specified",
        pain_points=pain_for_llm,
        top_finding=top_finding_text or "(no research findings)",
    )
    try:
        fit_paragraph = await loop.run_in_executor(None, lambda: _call_brain_sync(synthesis_prompt))
    except Exception as exc:
        logger.warning("Presentation prompt fit synthesis failed: %s", exc)

    sections: list[str] = [
        f"You are preparing a B2B sales presentation for **{client_name}**.\n"
        f"Use the data below to build a specific, fact-based presentation. Do not invent facts.\n",
    ]

    client_block = f"## About the Client: {client_name}\n"
    client_block += "\n".join(client_lines) if client_lines else "(no profile data)"
    sections.append(client_block)

    if contact_lines:
        sections.append("## Key Contacts\n" + "\n".join(contact_lines))

    if pain_bullets:
        sections.append("## Client Pain Points & Risks\n" + "\n".join(pain_bullets))
    elif intel_bullets:
        sections.append("## Client Intelligence\n" + "\n".join(intel_bullets))
    if opportunity_bullets:
        sections.append("## Opportunities Identified\n" + "\n".join(opportunity_bullets))

    prod_fields = []
    if product.get("category"):
        prod_fields.append(f"Category: {product['category']}")
    if product.get("description"):
        prod_fields.append(f"Description: {product['description']}")
    prod_fields.append(f"Key Features:\n{key_features_bullets}")
    if product.get("pricing_info"):
        prod_fields.append(f"Pricing: {product['pricing_info']}")
    if product.get("target_customer"):
        prod_fields.append(f"Target Customer: {product['target_customer']}")
    if product.get("website_url"):
        prod_fields.append(f"Website: {product['website_url']}")
    sections.append(f"## Our Product: {product_name}\n" + "\n".join(prod_fields))

    sections.append(f"## Why {product_name} Fits {client_name}\n{fit_paragraph or '(see match report)'}")

    if finding_lines:
        sections.append("## Recent Research Findings\n" + "\n".join(finding_lines))

    sections.append(
        f"## Suggested Slides\n"
        f"Generate a 10-slide presentation deck covering:\n"
        f"1. Executive Summary — who {client_name} is and why this meeting matters\n"
        f"2. {client_name}'s Key Challenges — drawn from the Pain Points section above\n"
        f"3. Our Solution: {product_name} — overview and key features\n"
        f"4. How {product_name} Solves {client_name}'s Problems — specific mapping\n"
        f"5. Proof Points — relevant case studies or analyst quotes\n"
        f"6. Pricing & Commercial Model — {product.get('pricing_info') or 'see product details'}\n"
        f"7. Implementation & Timeline — steps to get started\n"
        f"8. ROI & Business Case — expected impact for {client_name}\n"
        f"9. Next Steps — proposed actions from this meeting\n"
        f"10. Q&A\n\n"
        f"Use the data above to make every slide specific to {client_name}. Do not invent facts."
    )

    return "\n\n---\n\n".join(sections)


@router.get("/api/clients/{name}/presentation-prompts")
async def list_client_presentation_prompts(name: str, user: dict = Depends(current_user)):
    """List all saved presentation prompts for a client (across all products)."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    slug = _slugify(name)
    async with db_module._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.content, d.metadata, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1 AND d.type = 'note'
              AND d.doc_id LIKE $2
              AND dl.entity_type = 'client' AND dl.entity_id = $3
            ORDER BY d.created_at DESC
            """,
            user["org_id"], f"presentation-prompt-{slug}-%", client["id"],
        )

    prompts = []
    for r in rows:
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        content = r["content"] or ""
        prompts.append({
            "doc_id": r["id"],
            "product_id": meta.get("product_id"),
            "product_name": meta.get("product_name", "Unknown product"),
            "generated_at": str(r["created_at"])[:19],
            "content": content,
            "preview": content[:120],
        })

    return {"prompts": prompts}


@router.post("/api/clients/{name}/presentation-prompt")
async def generate_presentation_prompt(
    name: str,
    body: dict,
    user: dict = Depends(current_user),
):
    """Generate a Copilot-ready presentation prompt for a client × product pair."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    product_id = body.get("product_id")
    if not product_id:
        raise HTTPException(status_code=400, detail="product_id is required")

    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    product = await db_module.get_product(int(product_id), user["org_id"])
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    db_module.log_prompt(user["org_id"], user["id"], "presentation",
                         f"{name} × {product.get('name', '')}",
                         {"client": name, "product_id": int(product_id)})

    org_id = user["org_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    product_name = product.get("name", "Unknown")

    prompt_content = await _build_presentation_prompt_context(org_id, client, product)

    slug = _slugify(name)
    doc_id_str = f"presentation-prompt-{slug}-{product_id}-{today}"
    embedding = await db_module.embed_text(prompt_content[:512])
    doc_id = await db_module.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="note",
        title=f"{name} × {product_name} — Presentation Prompt {today}",
        content=prompt_content,
        metadata={
            "subject": name,
            "brief_type": "presentation_prompt",
            "product_id": int(product_id),
            "product_name": product_name,
            "generated_date": today,
        },
        embedding=embedding or [],
        source="agent",
        created_by=user["id"],
    )
    if doc_id > 0:
        await db_module.link_document(doc_id, "client", client["id"])

    return {
        "prompt": prompt_content,
        "generated_at": today,
        "doc_id": doc_id,
        "product_name": product_name,
    }


@router.post("/api/clients/{name}/mail-template")
async def generate_mail_template(
    name: str,
    body: dict,
    user: dict = Depends(current_user),
):
    """Generate a personalised outreach email for a client using KB context."""
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")

    template_type = body.get("template_type", "").strip()
    if template_type not in _MAIL_TYPE_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"template_type must be one of: {', '.join(_MAIL_TYPE_LABELS)}",
        )

    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    db_module.log_prompt(user["org_id"], user["id"], "mail", f"{template_type}: {name}", {
        "client": name,
        "template_type": template_type,
        "custom_instructions": (body.get("custom_instructions") or "")[:500],
        "product_id": body.get("product_id"),
        "source": "mail_template",
    })

    org_id = user["org_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build client context — use product context if product_id supplied
    product_id = body.get("product_id")
    product = None
    if product_id:
        product = await db_module.get_product(int(product_id), org_id)

    loop = asyncio.get_event_loop()
    if product:
        context = await _build_presentation_prompt_context(org_id, client, product)
    else:
        context = await _build_brief_context(org_id, client)

    # Build event block — fetch event URL via Pi once (not per client)
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
        if header_parts:
            event_block = "\n".join(header_parts) + "\n"
        if event_link:
            event_content = await _fetch_event_for_mail(event_link, event_name)
            if event_content:
                event_block += f"EVENT CONTENT:\n{event_content}\n"

    # Build product block
    product_block = ""
    if product:
        pname = product.get("name", "")
        pdesc = product.get("description", "")
        if pname:
            product_block = f"PRODUCT TO PROMOTE: {pname}\n"
            if pdesc:
                product_block += f"Product description: {pdesc[:300]}\n"

    # Build custom instructions block
    instructions_block = ""
    custom = body.get("custom_instructions", "").strip()
    if custom:
        instructions_block = f"ADDITIONAL INSTRUCTIONS: {custom}\n"

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

    prompt = _MAIL_TEMPLATE_PROMPT.format(
        type_label=_MAIL_TYPE_LABELS[template_type],
        client_name=name,
        event_block=event_block,
        product_block=product_block,
        instructions_block=instructions_block,
        mode_block=mode_block,
        context=context,
    )

    try:
        generated = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    if '---SOURCES---' in generated:
        parts = generated.split('---SOURCES---', 1)
        email_body = _clean_mail_output(parts[0].strip(), event_name, event_link)
        sources_reasoning = parts[1].strip() or None
    else:
        email_body = _clean_mail_output(generated.strip(), event_name, event_link)
        sources_reasoning = None

    slug = _slugify(name)
    doc_id_str = f"mail-template-{slug}-{template_type}-{today}"
    if product_id:
        doc_id_str = f"mail-template-{slug}-{template_type}-prod{product_id}-{today}"

    embedding = await db_module.embed_text(email_body[:512])
    doc_id = await db_module.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="note",
        title=f"{name} — {_MAIL_TYPE_LABELS[template_type].title()} Email {today}",
        content=email_body,
        metadata={
            "subject": name,
            "brief_type": "mail_template",
            "template_type": template_type,
            "product_id": int(product_id) if product_id else None,
            "generated_date": today,
            "sources_reasoning": sources_reasoning,
        },
        embedding=embedding or [],
        source="agent",
        created_by=user["id"],
    )
    if doc_id > 0:
        await db_module.link_document(doc_id, "client", client["id"])

    sources_list = await _get_source_refs(org_id, name)
    return {
        "email": email_body,
        "generated_at": today,
        "doc_id": doc_id,
        "template_type": template_type,
        "event_name": event_name,
        "event_link": event_link,
        "sources_reasoning": sources_reasoning,
        "sources_list": sources_list,
        "client_name": name,
    }


async def _build_knowledge_inventory(org_id: int, client_id: int, client_name: str) -> str:
    """Build a compact summary of what KB data exists for this client."""
    lines = [f"## Knowledge Inventory for {client_name}"]

    # Count documents by type
    all_docs = await db_module.list_documents(org_id, client_id=client_id)
    type_counts = {}
    type_latest = {}
    for doc in all_docs:
        t = doc.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
        d = str(doc.get("updated_at") or doc.get("created_at") or "")[:10]
        if d and (t not in type_latest or d > type_latest[t]):
            type_latest[t] = d

    type_labels = {
        "meeting": "Meeting transcripts", "research": "Research reports",
        "osint": "OSINT reports", "finding": "Research findings",
        "signal": "Intelligence signals", "note": "Notes",
        "industry_research": "Industry research", "contact_research": "Contact profiles",
        "match_report": "Product match reports",
    }
    for t, label in type_labels.items():
        if t in type_counts:
            latest = f" (latest: {type_latest[t]})" if t in type_latest else ""
            lines.append(f"- {label}: {type_counts[t]}{latest}")

    # Findings avg relevance
    findings = await db_module.get_client_findings(org_id, client_name, n=50)
    if findings:
        scores = [f.get("metadata", {}).get("relevance_score", 0) for f in findings if f.get("metadata", {}).get("relevance_score")]
        avg = round(sum(scores) / len(scores), 1) if scores else 0
        lines.append(f"  (findings avg relevance: {avg}/5)")

    # Signals breakdown
    signals = await db_module.list_signals(org_id, client_name=client_name, days=60, limit=100)
    if signals:
        sig_types = {}
        for s in signals:
            st = s.get("metadata", {}).get("signal_type", "other")
            sig_types[st] = sig_types.get(st, 0) + 1
        breakdown = ", ".join(f"{st}: {n}" for st, n in sig_types.items())
        lines.append(f"  (signals breakdown: {breakdown})")

    # Contacts
    try:
        contacts = await db_module.list_contacts(org_id, client_id=client_id)
        if contacts:
            lines.append(f"- Contacts on file: {len(contacts)}")
    except Exception:
        pass

    return "\n".join(lines)


@router.post("/api/clients/{name}/meeting-prep/chat")
async def meeting_prep_chat(name: str, body: dict, user: dict = Depends(current_user)):
    """Answer a question using full KB RAG with active tool-calling — not just the brief."""
    from routers.chat import _run_tool_loop, CHAT_TOOLS

    org_id = user["org_id"]
    question = (body.get("question") or "").strip()
    brief    = (body.get("brief") or "").strip()
    history  = body.get("history", [])   # list of {role, content} dicts

    if question and DB_AVAILABLE:
        db_module.log_prompt(org_id, user["id"], "mp_chat", question, {"client_name": name})

    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    # Fetch client record for client_id
    client = await db_module.get_client(org_id, name)
    client_id = client["id"] if client else None

    # If no brief passed, try to fetch from DB
    if not brief and client_id:
        async with db_module._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT d.content FROM documents d
                   JOIN document_links dl ON dl.document_id = d.id
                   WHERE d.org_id=$1 AND d.type='brief' AND d.doc_id LIKE 'meeting-prep-%'
                     AND dl.entity_type='client' AND dl.entity_id=$2
                   ORDER BY d.created_at DESC LIMIT 1""",
                org_id, client_id,
            )
        if row:
            brief = row["content"] or ""

    # Build knowledge inventory to orient the AI
    inventory = ""
    if client_id:
        try:
            inventory = await _build_knowledge_inventory(org_id, client_id, name)
        except Exception:
            pass

    # Select model from config
    cfg = _load_config_brief()
    model = (
        cfg.get("pi_chat_model")
        or cfg.get("research_model")
        or cfg.get("agent_model")
        or "deepseek/deepseek-v4-flash"
    )

    # Format conversation history for the tool loop
    valid_turns = [t for t in (history or [])[-6:] if t.get("role") in ("user", "assistant") and t.get("content")]
    history_msgs = [{"role": t["role"], "content": t["content"]} for t in valid_turns]

    brief_section = f"\n\n## Meeting Prep Brief\n{brief}" if brief else ""
    inventory_section = f"\n\n{inventory}" if inventory else ""

    system = f"""You are a sales advisor helping a rep prepare for a meeting with {name}.

CRITICAL INSTRUCTIONS — follow these before answering:
1. You MUST call search_kb at least once before answering. Include "{name}" in your queries.
   Example queries: "{name} strategic priorities", "{name} financial situation", "{name} recent news", "{name} key contacts"
2. Call get_recent_findings with client_name="{name}" to retrieve research findings.
3. Call search_kb with different angles if the first call returns thin results.
4. After gathering KB data, synthesise it with the brief below into a direct, actionable answer.
5. Cite document titles and types in your answer. Be specific — mention actual facts from the KB.
6. Never answer from the brief alone. The KB contains research, OSINT, findings, and signals about this client.{inventory_section}{brief_section}"""

    try:
        answer, sources = await _run_tool_loop(
            system=system,
            user_msg=question,
            org_id=org_id,
            model=model,
            history=history_msgs,
            max_rounds=4,
        )
        if not answer:
            raise ValueError("empty answer")
    except Exception as exc:
        logger.warning("Meeting prep tool loop failed for %s: %s — falling back", name, exc)
        # Fallback: one-shot with pre-loaded context
        context_block = (inventory_section + brief_section).strip() or "(no context)"
        prompt = f"You are a sales advisor preparing for a meeting with {name}.\n\n{context_block}\n\nQuestion: {question}\n\nAnswer directly using all available context."
        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
        except Exception as exc2:
            logger.error("Meeting prep fallback also failed for %s: %s", name, exc2)
            raise HTTPException(status_code=502, detail=f"AI call failed: {exc2}")
        sources = []

    return {"answer": answer or "(no answer generated)", "sources": sources}


# ---------------------------------------------------------------------------
# Contacts / People
# ---------------------------------------------------------------------------

@router.get("/api/people")
async def get_people(client_name: Optional[str] = None, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"contacts": []}
    org_id = user["org_id"]
    cached = cache_get(("people", org_id, client_name))
    if cached is not None:
        return cached
    client_id = None
    if client_name:
        client = await db_module.get_client(org_id, client_name)
        client_id = client["id"] if client else None
    result = {"contacts": await db_module.list_contacts(org_id, client_id=client_id)}
    cache_set(("people", org_id, client_name), result)
    return result


@router.post("/api/contacts")
async def create_contact(body: dict, user: dict = Depends(current_user)):
    cache_clear()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    metadata    = body.get("metadata", {})
    # Store first/last separately in metadata if provided; fall back to splitting the combined name
    first_name = body.get("first_name", "").strip()
    last_name  = body.get("last_name", "").strip()
    if not first_name and not last_name:
        parts = name.split(None, 1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else ""
    if first_name: metadata["first_name"] = first_name
    if last_name:  metadata["last_name"]  = last_name
    client_name = body.get("client", "").strip() or None
    client_id   = None
    if client_name:
        c = await db_module.get_client(user["org_id"], client_name)
        client_id = c["id"] if c else None
    embedding = await db_module.embed_text(
        f"{name} {metadata.get('role', '')} {client_name or ''}"
    )
    contact_id = await db_module.upsert_contact(
        org_id=user["org_id"], name=name,
        metadata=metadata, embedding=embedding,
        client_id=client_id, created_by=user["id"],
    )
    return {"ok": True, "id": contact_id}


@router.post("/api/clients/{client_name}/contacts/import")
async def import_contacts_csv(
    client_name: str,
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    """Import contacts from a CSV file. Columns: name (or first_name+last_name), email, role."""
    cache_clear()
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")  # strips BOM if present
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    client = await db_module.get_client(user["org_id"], client_name)
    client_id = client["id"] if client else None

    reader = csv.DictReader(io.StringIO(text), delimiter=_sniff_csv_delimiter(text))
    # Normalise header names to lowercase, strip whitespace
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV has no headers")
    reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]

    imported, errors = 0, []
    for i, row in enumerate(reader, start=2):  # row 1 = header
        # Resolve name: prefer combined first+last, fallback to "name"
        first = (row.get("first_name") or row.get("firstname") or "").strip()
        last  = (row.get("last_name")  or row.get("lastname")  or "").strip()
        name  = (row.get("name") or "").strip()
        if first or last:
            name = f"{first} {last}".strip()
        if not name:
            errors.append(f"Row {i}: missing name")
            continue

        email = (row.get("email") or "").strip()
        role  = (row.get("role") or row.get("title") or row.get("job_title") or "").strip()
        metadata = {}
        if email: metadata["email"] = email
        if role:  metadata["role"]  = role
        if first: metadata["first_name"] = first
        if last:  metadata["last_name"]  = last

        try:
            embedding = await db_module.embed_text(
                f"{name} {role} {client_name}"
            )
            await db_module.upsert_contact(
                org_id=user["org_id"], name=name,
                metadata=metadata, embedding=embedding,
                client_id=client_id, created_by=user["id"],
            )
            imported += 1
        except Exception as e:
            errors.append(f"Row {i} ({name}): {e}")

    return {"imported": imported, "errors": errors}


@router.get("/api/contacts/{name}")
async def get_contact(name: str, user: dict = Depends(current_user)):
    contact = await db_module.get_contact(user["org_id"], name)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    # Docs directly linked to contact
    docs = await db_module.list_documents(user["org_id"], contact_id=contact["id"])
    # Also pull profile findings for this person from the client's docs
    if not docs and contact.get("client_id"):
        all_client_docs = await db_module.list_documents(user["org_id"], client_id=contact["client_id"])
        first = name.split()[0].lower()
        last  = name.split()[-1].lower()
        docs = [
            d for d in all_client_docs
            if (first in d["title"].lower() or last in d["title"].lower())
            and d.get("metadata", {}).get("source_type") in ("profile", None)
        ]
    # Resolve client name
    client_name = None
    if contact.get("client_id"):
        client = await db_module.get_client_by_id(user["org_id"], contact["client_id"])
        client_name = client["name"] if client else None
    return {**contact, "client_name": client_name, "documents": docs}


@router.patch("/api/contacts/{name}")
async def patch_contact(name: str, body: dict, user: dict = Depends(current_user)):
    patch   = body.get("metadata", body)
    updated = await db_module.update_contact_metadata(user["org_id"], name, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Contact not found")
    return updated


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@router.get("/api/signals")
async def get_signals(
    client_name: Optional[str] = None,
    signal_type: Optional[str] = None,
    days: int = 30,
    page: int = 0,
    limit: int = 20,
    min_relevance: Optional[int] = None,
    mine: bool = False,
    scope: Optional[str] = None,
    user: dict = Depends(current_user),
):
    if not DB_AVAILABLE:
        return {"signals": [], "page": page, "limit": limit}
    subjects = None
    if mine:
        # Home/recorder news panel: only this rep's own clients, not the org's.
        clients = await db_module.list_clients(user["org_id"])
        uid = user["id"]
        subjects = [c["name"] for c in clients if _client_owned_by(c, uid)]
        if not subjects:
            return {"signals": [], "page": page, "limit": limit}
    signals = await db_module.list_signals(
        org_id=user["org_id"],
        client_name=client_name,
        signal_type=signal_type,
        days=days,
        limit=limit,
        offset=page * limit,
        min_relevance=min_relevance,
        subjects=subjects,
        scope=scope,
    )
    return {"signals": signals, "page": page, "limit": limit}


@router.patch("/api/signals/{doc_id}/read")
async def mark_signal_read(doc_id: str, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"ok": False}
    await db_module.update_document(user["org_id"], doc_id, {"metadata": {"read": True}})
    return {"ok": True}
