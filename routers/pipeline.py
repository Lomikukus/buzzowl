"""
Pipeline router — session lifecycle from staging through promotion to the DB.

Covers:
- Session metadata helpers (read/write/update JSON sidecars)
- Entity extraction via Ollama
- Core promotion logic (DB index + sorted copy)
- Background task triggers (enrichment, research, OSINT, heartbeats)
- Pipeline sweep loop (auto-promote staged sessions)
- API routes: /api/pipeline/*, /api/export, /api/sessions/text
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import autonomy
import context
import llm
from context import (
    BASE_DIR,
    DB_AVAILABLE,
    SCHEDULER_AVAILABLE,
    _metadata_lock,
    config,
    console,
    db_module,
    executor,
)
from routers.auth import current_user
def extract_title_from_summary(summary_text: str) -> str:
    """Pull the **Title** line out of an LLM summary, or fall back to the first line."""
    match = re.search(
        r"\*\*Tit(?:le|el)[:\s]*\*\*\s*\n+(.+)",
        summary_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    for line in summary_text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith("**"):
            return line
    return "Untitled"

logger = logging.getLogger("wk.pipeline")

router = APIRouter()

# In-memory buffer for app-sourced transcript chunks, keyed by client session_id
_transcript_buffers: dict[str, list[str]] = {}

_NEWS_OSINT_TASK = (
    "News and signal scan for {subject}: find the most recent developments from the last 60 days. "
    "Search for latest news, press releases, leadership changes, strategic announcements, M&A activity, "
    "earnings results, and industry signals. Write individual findings (type='finding') as you go. "
    "End with a summary signal report (type='osint'). Only include events with verifiable source URLs."
)

# Seeded the first time the market monitor runs with an empty config. Editable
# per org via GET/PUT /api/market/sources. Front pages change daily, so they
# make good change-detection anchors (browser-service fallback handles JS pages).
# Generic words to ignore when matching a market signal's industry to a client's
# — they'd cause false matches across unrelated sectors.
_INDUSTRY_STOPWORDS = {
    "and", "the", "services", "service", "industry", "industries", "sector",
    "solutions", "group", "company", "products", "general", "other", "based",
    "international", "global", "technology", "technologies", "systems",
}

_DEFAULT_MARKET_SOURCES = [
    {"url": "https://www.reuters.com/business/", "label": "Reuters Business"},
    {"url": "https://www.handelsblatt.com/", "label": "Handelsblatt"},
    {"url": "https://www.manager-magazin.de/", "label": "manager magazin"},
    {"url": "https://www.heise.de/", "label": "heise online (tech)"},
]

_HB_NAMES: dict[str, str] = {
    "enrichment": "Daily Enrichment",
    "research": "Weekday Research",
    "osint": "Daily OSINT",
    "org": "Org Sweep",
    "quality_digest": "Quality Digest",
    "weekly_digest": "Weekly Digest",
    "stale_clients": "Stale Client Alert",
    "match_monitor": "Match Monitor",
    "focus_osint": "Focus Client OSINT",
    "source_monitor": "Source Monitor (all clients)",
    "nba_queue": "Daily Action Queue",
    "market_monitor": "Market News Monitor",
    "jobs_monitor": "Open Positions Monitor",
    "rep_digest": "Rep Client Digest",
    "task_reminder": "Task Reminder (email)",
    "research_qa": "Research QA Reviewer",
    "lessons_review": "Cross-Site Lessons Review",
}


# ---------------------------------------------------------------------------
# Data directory setup
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    """Create data/{raw,staged,sorted} under BASE_DIR if they don't exist."""
    for d in ("data/raw", "data/staged", "data/sorted"):
        (BASE_DIR / d).mkdir(parents=True, exist_ok=True)


def _migrate_legacy_dirs() -> None:
    """One-time migration: move old flat layout into the data/ hierarchy."""
    migrations = [
        (BASE_DIR / "raw_audio",       "data/raw",    "audio.wav"),
        (BASE_DIR / "raw_transcripts", "data/raw",    "transcript.txt"),
        (BASE_DIR / "summaries",       "data/staged", "summary.md"),
    ]
    for old_dir, new_parent, new_filename in migrations:
        if not old_dir.exists():
            continue
        for old_file in sorted(old_dir.iterdir()):
            if not old_file.is_file():
                continue
            session_id = old_file.stem
            dest_dir = BASE_DIR / new_parent / session_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / new_filename
            if not dest.exists():
                shutil.move(str(old_file), str(dest))


# ---------------------------------------------------------------------------
# Session metadata helpers
# ---------------------------------------------------------------------------

def _write_session_metadata(session_id: str, data: dict) -> None:
    path = BASE_DIR / "data" / "staged" / session_id / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _metadata_lock:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_session_metadata(session_id: str) -> Optional[dict]:
    path = BASE_DIR / "data" / "staged" / session_id / "metadata.json"
    if not path.exists():
        return None
    with _metadata_lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def _update_session_metadata(session_id: str, **fields) -> None:
    meta = _read_session_metadata(session_id)
    if meta is None:
        return
    meta.update(fields)
    _write_session_metadata(session_id, meta)


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

ENTITY_EXTRACTION_PROMPT = """\
You are an entity extractor for a sales knowledge base. Carefully analyze the following transcript and extract all structured information.

Return ONLY a valid JSON object with exactly these three keys:

"companies": array of objects, each with:
  - "name": the company/organization name as stated in the transcript
  - "confidence": "high" if the company is a main subject (client, prospect, partner mentioned by name multiple times or as the central topic); "medium" if mentioned once or as background context; "low" if mentioned very briefly or the name is uncertain

"people": array of objects, each with:
  - "name": full name if given, first name only if that is all that is stated
  - "role": their job title or department. Infer from context when possible — e.g. "our CFO Sandra" → role "CFO"; "Sandra from finance" → role "Finance"; "I'm the account executive" → role "Account Executive". Use "Unknown" only if no role context is available anywhere near their name.
  - "confidence": "high" if full name and explicit role are given; "medium" if first name only or role is inferred from context; "low" if mentioned once with no role context

"topics": array of 2–5 word topic phrases (e.g. "Q2 pricing review", "onboarding timeline")

Rules:
- INCLUDE every person with a name — even a first name mentioned once in passing ("Sandra from finance said...", "I'll follow up with Thomas", "our CTO Alex mentioned...")
- INCLUDE companies only when a specific name is stated — exclude vague terms like "the customer", "the vendor", "a partner" unless a name follows
- Infer roles aggressively from surrounding words: titles before names ("CEO John"), possessives ("their CFO"), job references near names ("Sandra handles procurement")
- Return 3–8 topics maximum
- Return valid JSON only — no explanation, no markdown, no code fences

Transcript:
{transcript}"""


def _normalise_confidence(raw: str) -> str:
    v = str(raw).lower().strip()
    return v if v in ("high", "medium", "low") else "medium"


def _call_pipeline_brain(prompt: str, org_id: Optional[int] = None) -> str:
    """Call the configured pipeline brain with a plain-text prompt. Returns the text response.

    Provider/model come from the llm.py "pipeline" role (config llm: block, or
    legacy pipeline_brain/pipeline_model keys).
    Returns empty string on failure — pipeline callers handle missing output gracefully.
    """
    try:
        return llm.complete(prompt, role="pipeline", timeout=120, org_id=org_id)
    except Exception as e:
        console.print(f"[yellow]Pipeline brain failed: {e}[/yellow]")
        return ""


def extract_entities(transcript: str, org_id: Optional[int] = None) -> dict:
    """Call Ollama to extract companies, people, and topics from a transcript.

    Companies are returned as [{"name": str, "confidence": str}].
    People are returned as [{"name": str, "role": str, "confidence": str}].
    Falls back to empty arrays on any failure — never raises.
    """
    prompt = ENTITY_EXTRACTION_PROMPT.format(transcript=transcript)
    try:
        raw = _call_pipeline_brain(prompt, org_id) or "{}"
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        raw = m.group(0) if m else "{}"
        data = json.loads(raw)

        companies = []
        for c in data.get("companies", []):
            if isinstance(c, dict) and c.get("name"):
                companies.append({"name": str(c["name"]).strip(), "confidence": _normalise_confidence(c.get("confidence", "medium"))})
            elif isinstance(c, str) and c.strip():
                companies.append({"name": c.strip(), "confidence": "medium"})

        people = []
        for p in data.get("people", []):
            if isinstance(p, dict) and p.get("name"):
                people.append({
                    "name":       str(p["name"]).strip(),
                    "role":       str(p.get("role", "Unknown")).strip() or "Unknown",
                    "confidence": _normalise_confidence(p.get("confidence", "medium")),
                })
            elif isinstance(p, str) and p.strip():
                people.append({"name": p.strip(), "role": "Unknown", "confidence": "low"})

        return {
            "companies": companies,
            "people":    people,
            "topics":    [str(t) for t in data.get("topics", [])],
        }
    except Exception as e:
        console.print(f"[yellow]Entity extraction failed: {e}[/yellow]")
        return {"companies": [], "people": [], "topics": []}


def _generate_summary(transcript: str, language: str, org_id: Optional[int] = None) -> str:
    """Generate a structured summary for a transcript via the configured pipeline brain."""
    prompt = (
        "You are a meeting and lecture summarizer. "
        f"The following transcript is in '{language}'. "
        "Produce a structured summary in the same language with these sections:\n"
        "**Title** (one line, auto-generated)\n"
        "**TL;DR** (3–5 sentences)\n"
        "**Key Takeaways** (bullet points)\n"
        "**Action Items** (bullet points, write 'None' if there are none)\n\n"
        f"Transcript:\n{transcript}"
    )
    result = _call_pipeline_brain(prompt, org_id)
    if result:
        return result
    return "**Title**\nUntitled\n\n**TL;DR**\nSummary unavailable.\n\n**Key Takeaways**\n- (none)\n\n**Action Items**\n- None"


# ---------------------------------------------------------------------------
# Core promotion
# ---------------------------------------------------------------------------

def _promote_session(session_id: str) -> dict:
    """Index session into the DB and copy to sorted/. Runs in executor thread.

    Returns {"ok": True, ...} on success or {"ok": False, "error": ...} on failure.
    Idempotent — already-promoted sessions return immediately.
    """
    transcript_path = BASE_DIR / "data" / "raw"    / session_id / "transcript.txt"
    summary_path    = BASE_DIR / "data" / "staged" / session_id / "summary.md"

    if not transcript_path.exists():
        return {"ok": False, "error": f"Transcript not found for session {session_id}"}
    if not summary_path.exists():
        return {"ok": False, "error": f"Summary not found for session {session_id}"}

    meta = _read_session_metadata(session_id)
    if meta and meta.get("status") == "promoted":
        return {"ok": True, "already_promoted": True, "title": meta.get("title", "")}

    transcript_text = transcript_path.read_text(encoding="utf-8")
    summary_text    = summary_path.read_text(encoding="utf-8")
    date_str        = f"{session_id[:4]}-{session_id[4:6]}-{session_id[6:8]}"

    created_by      = (meta or {}).get("created_by")
    created_by_name = (meta or {}).get("created_by_name")

    # Prefer pre-extracted values from metadata; fall back to inline extraction
    title       = (meta or {}).get("title") or extract_title_from_summary(summary_text)
    entities_meta = (meta or {}).get("entities", {})
    if entities_meta and (entities_meta.get("companies") or entities_meta.get("topics")):
        entities = entities_meta
    else:
        entities = extract_entities(transcript_text, (meta or {}).get("org_id"))

    # Duration: prefer metadata; fall back to parsing the last timestamp in the transcript
    duration_s = (meta or {}).get("duration_s") or 0
    if not duration_s:
        for line in reversed(transcript_text.splitlines()):
            m = re.search(r"→\s*(\d+):(\d+):(\d+)", line)
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                duration_s = h * 3600 + mi * 60 + s
                break

    speakers_count = (meta or {}).get("speakers") or 0
    if not speakers_count:
        speakers_found = set(re.findall(r"\[SPEAKER_\d+\]", transcript_text))
        speakers_count = len(speakers_found) if speakers_found else 1

    language       = (meta or {}).get("language") or config.get("language", "en")
    visibility     = (meta or {}).get("visibility", "shared")
    console.print(f"  [green]Promoting → {title}[/green]")

    # --- DB indexing (best-effort, non-fatal) ---
    if DB_AVAILABLE:
        try:
            # Org comes from the session metadata (set when the recording user's
            # /ws connected or the ingest token resolved); legacy sessions without
            # it fall back to the first org (single-tenant installs).
            meta_org = (meta or {}).get("org_id")
            org = {"id": int(meta_org)} if meta_org else db_module._run_coro_from_thread(db_module.get_first_org())
            if not org:
                console.print("[yellow]DB: no org found — run /api/auth/register first[/yellow]")
            else:
                org_id    = org["id"]
                embed_txt = f"{title}\n{summary_text}\n{' '.join(entities['topics'])}"
                embedding = db_module.get_embedding(embed_txt)
                doc_db_id = db_module._run_coro_from_thread(
                    db_module.index_document(
                        org_id=org_id,
                        doc_id=session_id,
                        doc_type="meeting",
                        title=title,
                        content=f"{transcript_text}\n\n{summary_text}",
                        metadata={
                            "date": date_str, "duration_s": duration_s,
                            "speakers": speakers_count, "language": language,
                            "topics": entities["topics"],
                            "transcript_path": str(transcript_path),
                            **({"recorded_by": created_by_name} if created_by_name else {}),
                        },
                        embedding=embedding,
                        visibility=visibility,
                        created_by=created_by,
                    )
                )
                client_name_by_idx: list[str] = []
                for co in entities["companies"]:
                    raw_cname  = co["name"] if isinstance(co, dict) else str(co)
                    confidence = co.get("confidence", "medium") if isinstance(co, dict) else "medium"
                    # Fuzzy dedup: use canonical name if a similar client already exists
                    canonical = db_module._run_coro_from_thread(
                        db_module.find_similar_client(org_id, raw_cname)
                    ) or raw_cname
                    if canonical != raw_cname:
                        console.print(f"  [dim]Dedup: '{raw_cname}' → '{canonical}'[/dim]")
                    client_name_by_idx.append(canonical)
                    c_emb = db_module.get_embedding(f"{canonical} {' '.join(entities['topics'])}")
                    client_db_id = db_module._run_coro_from_thread(
                        db_module.upsert_client(
                            org_id=org_id, name=canonical,
                            metadata={"last_activity": date_str, "confidence": confidence},
                            embedding=c_emb, date_str=date_str, created_by=created_by,
                        )
                    )
                    if doc_db_id and doc_db_id > 0 and client_db_id and client_db_id > 0:
                        db_module._run_coro_from_thread(
                            db_module.link_document(doc_db_id, "client", client_db_id)
                        )
                for person in entities["people"]:
                    pname      = person["name"] if isinstance(person, dict) else str(person)
                    prole      = person.get("role", "") if isinstance(person, dict) else ""
                    pconf      = person.get("confidence", "medium") if isinstance(person, dict) else "medium"
                    pcompany   = client_name_by_idx[0] if client_name_by_idx else None
                    c_id       = None
                    # Fuzzy dedup for contacts
                    canonical_p = db_module._run_coro_from_thread(
                        db_module.find_similar_contact(org_id, pname)
                    ) or pname
                    if canonical_p != pname:
                        console.print(f"  [dim]Dedup contact: '{pname}' → '{canonical_p}'[/dim]")
                    if pcompany:
                        c_row = db_module._run_coro_from_thread(db_module.get_client(org_id, pcompany))
                        c_id  = c_row["id"] if c_row else None
                    p_emb = db_module.get_embedding(f"{canonical_p} {prole} {pcompany or ''}")
                    contact_db_id = db_module._run_coro_from_thread(
                        db_module.upsert_contact(
                            org_id=org_id, name=canonical_p,
                            metadata={"role": prole, "company": pcompany or "", "confidence": pconf},
                            embedding=p_emb, client_id=c_id, date_str=date_str, created_by=created_by,
                        )
                    )
                    if doc_db_id and doc_db_id > 0 and contact_db_id and contact_db_id > 0:
                        db_module._run_coro_from_thread(
                            db_module.link_document(doc_db_id, "contact", contact_db_id)
                        )
                console.print(f"  [dim]DB indexed {session_id}[/dim]")
        except Exception as db_err:
            console.print(f"[yellow]DB indexing failed (non-fatal): {db_err}[/yellow]")

    # --- Copy to sorted/ ---
    sorted_dir = BASE_DIR / "data" / "sorted" / session_id
    sorted_dir.mkdir(parents=True, exist_ok=True)
    for src, fname in [(transcript_path, "transcript.txt"), (summary_path, "summary.md")]:
        dest = sorted_dir / fname
        if src.exists() and not dest.exists():
            shutil.copy2(str(src), str(dest))

    promoted_at   = datetime.now(timezone.utc).isoformat()
    existing_meta = _read_session_metadata(session_id)
    if existing_meta:
        _update_session_metadata(session_id, status="promoted", promoted_at=promoted_at)
    else:
        _write_session_metadata(session_id, {
            "session_id": session_id, "status": "promoted",
            "created_at": None, "duration_s": duration_s, "speakers": speakers_count,
            "language": language, "title": title, "entities": entities,
            "agent_run_id": None, "promoted_at": promoted_at, "error": None,
        })
    console.print(f"  [dim]Sorted → data/sorted/{session_id}/[/dim]")
    return {"ok": True, "path": session_id, "title": title, "entities": entities}


# ---------------------------------------------------------------------------
# Background task triggers
# ---------------------------------------------------------------------------

async def _trigger_enrichment(session_id: str, org_id: Optional[int]) -> None:
    """Background: extract entities → run enrichment agent → promote."""
    loop = asyncio.get_event_loop()

    def _prepare() -> None:
        meta = _read_session_metadata(session_id)
        if not meta:
            transcript_path = BASE_DIR / "data" / "raw" / session_id / "transcript.txt"
            if not transcript_path.exists():
                return
            _write_session_metadata(session_id, {
                "session_id": session_id, "status": "staged", "created_at": None,
                "duration_s": None, "speakers": None, "language": None,
                "title": None, "entities": None, "agent_run_id": None,
                "promoted_at": None, "error": None, "org_id": org_id,
            })
        elif org_id is not None and not meta.get("org_id"):
            # multi-tenant: remember which org recorded this session so promotion
            # and the sweep never fall back to a deployment-wide default
            _write_session_metadata(session_id, {**meta, "org_id": org_id})
        transcript_path = BASE_DIR / "data" / "raw"    / session_id / "transcript.txt"
        summary_path    = BASE_DIR / "data" / "staged" / session_id / "summary.md"
        if not transcript_path.exists():
            return
        transcript_text = transcript_path.read_text(encoding="utf-8")
        if not summary_path.exists():
            lang = (_read_session_metadata(session_id) or {}).get("language", "en") or "en"
            summary_text = _generate_summary(transcript_text, lang, org_id)
            summary_path.write_text(summary_text, encoding="utf-8")
        else:
            summary_text = summary_path.read_text(encoding="utf-8")
        title    = extract_title_from_summary(summary_text) if summary_text else "Untitled"
        entities = extract_entities(transcript_text, org_id)
        _update_session_metadata(session_id, title=title, entities=entities)

    try:
        await loop.run_in_executor(executor, _prepare)
    except Exception as e:
        console.print(f"[yellow]Entity prep failed for {session_id}: {e}[/yellow]")

    run_id: Optional[int] = None
    backend = config.get("agent_service_backend", "python")

    if DB_AVAILABLE and org_id and backend in ("pi", "split"):
        # Route enrichment to Pi agent service
        try:
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            meta     = _read_session_metadata(session_id)
            entities = (meta or {}).get("entities") or {}
            companies = [c["name"] if isinstance(c, dict) else str(c) for c in entities.get("companies", [])]
            raw_people = [p if isinstance(p, dict) else {"name": str(p), "role": "Unknown"} for p in entities.get("people", [])]
            people = [f"{p['name']} ({p.get('role', '?')})" for p in raw_people]

            # Deterministic contact creation — don't rely on the LLM for a simple DB write
            linked_company = companies[0] if companies else None
            client_id_for_contacts: Optional[int] = None
            if linked_company:
                try:
                    client_row = await db_module.get_client(org_id, linked_company)
                    client_id_for_contacts = client_row["id"] if client_row else None
                except Exception:
                    pass
            date_str = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
            for person in raw_people:
                pname = (person.get("name") or "").strip()
                role  = (person.get("role") or "Unknown").strip()
                if not pname or pname.lower() == "unknown":
                    continue
                try:
                    await db_module.upsert_contact(
                        org_id=org_id,
                        name=pname,
                        metadata={"role": role},
                        embedding=db_module.get_embedding(f"{pname} {role} {linked_company or ''}"),
                        client_id=client_id_for_contacts,
                        date_str=date_str,
                        created_by=None,
                    )
                    console.print(f"[dim]Contact upserted: {pname} ({role})[/dim]")
                except Exception as ce:
                    console.print(f"[yellow]Contact upsert failed for {pname}: {ce}[/yellow]")

            task = (
                f"Enrich entities extracted from sales meeting (session {session_id}).\n"
                + (f"Companies: {', '.join(companies)}\n" if companies else "")
                + (f"People: {', '.join(people)}\n" if people else "")
                + "For each company and person, do a quick web search and write one finding document."
            )
            run_id = await db_module.create_agent_run(
                org_id=org_id, agent_type="enrichment",
                task=task, trigger_type="event_hook",
            )
            _update_session_metadata(session_id, status="agent_working", agent_run_id=run_id)
            svc_url, svc_run_id = await _fire_agent_service(
                session_id, org_id,
                brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                task=task, agent_type="enrichment",
            )
            await db_module.update_agent_run(
                run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            asyncio.create_task(_watch_agent_service_run(run_id, svc_url, svc_run_id))
            # Treat as fire-and-forget: mark agent_done so the sweep promotes immediately
            _update_session_metadata(session_id, status="agent_done")
            console.print(f"[dim]Pi enrichment started for {session_id} (svc_run={svc_run_id})[/dim]")
        except Exception as e:
            console.print(f"[yellow]Pi enrichment trigger failed for {session_id}: {e}[/yellow]")
            if run_id:
                await db_module.update_agent_run(run_id, "failed", error=str(e))
            _update_session_metadata(session_id, status="failed", error=str(e))
            return

    elif DB_AVAILABLE and org_id:
        # Embedded Python enrichment (fallback for python backend)
        try:
            run_id = await db_module.create_agent_run(
                org_id=org_id, agent_type="enrichment",
                task=f"Enrich entities for session {session_id}",
                trigger_type="event_hook",
            )
            _update_session_metadata(session_id, status="agent_working", agent_run_id=run_id)
            await db_module.update_agent_run(run_id, "running")

            from agents._legacy.enrichment import run_enrichment
            meta     = _read_session_metadata(session_id)
            entities = (meta or {}).get("entities") or {}
            result   = await run_enrichment(session_id, entities, org_id, run_id)

            await db_module.update_agent_run(
                run_id, "done",
                output={"enriched": result.get("enriched"), "errors": result.get("errors")},
            )
            _update_session_metadata(session_id, status="agent_done")
        except Exception as e:
            console.print(f"[yellow]Enrichment agent failed for {session_id}: {e}[/yellow]")
            if run_id:
                await db_module.update_agent_run(run_id, "failed", error=str(e))
            _update_session_metadata(session_id, status="failed", error=str(e))
            return  # pipeline sweep auto-retries on next tick

    try:
        result = await loop.run_in_executor(executor, _promote_session, session_id)
        if not result.get("ok"):
            console.print(f"[yellow]Auto-promote failed for {session_id}: {result.get('error')}[/yellow]")
            _update_session_metadata(session_id, status="failed", error=result.get("error", "promote failed"))
    except Exception as e:
        console.print(f"[yellow]Promote error for {session_id}: {e}[/yellow]")
        _update_session_metadata(session_id, status="failed", error=str(e))


async def _clear_news_pending(org_id: int, client_name: str) -> None:
    """Research is being run for this client — clear the 'new info' badge.
    Best-effort; covers manual, sweep-fired, and heartbeat-fired triggers."""
    try:
        await db_module.update_client_metadata(
            org_id, client_name, {"news_pending": False, "news_pending_reason": []},
        )
    except Exception:
        pass


async def _trigger_research(client_name: str, org_id: int, run_id: Optional[int] = None, await_completion: bool = False) -> None:
    """Background: enqueue research for a newly seen client (agent service or Python queue).

    If run_id is provided (pre-created by the caller), it is used instead of creating a new
    agent_runs row — prevents duplicate rows when called from create_client bulk flow.
    """
    if not DB_AVAILABLE:
        return
    await _clear_news_pending(org_id, client_name)
    backend = config.get("agent_service_backend", "python")
    if backend in ("pi", "hermes", "split"):
        try:
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            svc_url, svc_run_id = await _fire_agent_service(
                client_name, org_id,
                brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                agent_type="research",
            )
            db_run_id = run_id if run_id else await db_module.create_agent_run(
                org_id=org_id, agent_type="research",
                task=f"Research: {client_name}", trigger_type="event_hook",
            )
            await db_module.update_agent_run(
                db_run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            if await_completion:
                await _watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name)
            else:
                asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name))
            console.print(f"[dim]Agent service research started for '{client_name}' (run={svc_run_id})[/dim]")
        except Exception as exc:
            console.print(f"[yellow]Research trigger failed for '{client_name}': {exc}[/yellow]")
            if run_id:
                try:
                    await db_module.update_agent_run(run_id, "failed", error=str(exc))
                except Exception:
                    pass
        return
    try:
        db_run_id = run_id if run_id else await db_module.create_agent_run(
            org_id=org_id, agent_type="research",
            task=f"Research: {client_name}", trigger_type="event_hook",
        )
        task_id = await db_module.enqueue_research_task(
            org_id=org_id, subject_type="company", subject=client_name,
            task_type="orchestrate", payload={"source": "new_client_hook"},
            depth=0, priority=7,
        )
        # Mark the run done immediately — python queue runs independently via research_runner
        await db_module.update_agent_run(db_run_id, "done", output={"research_task_id": task_id})
        console.print(f"[dim]Research task enqueued for '{client_name}' (task_id={task_id})[/dim]")
    except Exception as exc:
        console.print(f"[yellow]Research trigger failed for '{client_name}': {exc}[/yellow]")


async def _trigger_industry_research(industry: str, org_id: int) -> None:
    """Background: enqueue industry research if not done recently (< 7 days)."""
    if not DB_AVAILABLE or not industry.strip():
        return
    try:
        # Skip if fresh industry research exists (< 7 days old)
        if db_module._pool:
            async with db_module._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id FROM documents
                    WHERE org_id = $1 AND type = 'industry_research'
                      AND metadata->>'industry' ILIKE $2
                      AND created_at > NOW() - INTERVAL '7 days'
                    LIMIT 1
                    """,
                    org_id, f"%{industry}%",
                )
                if row:
                    console.print(f"[dim]Industry research for '{industry}' is fresh — skipping[/dim]")
                    return

        backend = context.config.get("agent_service_backend", "python")
        if backend in ("pi", "hermes", "split"):
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            task = (
                f"Research the {industry} industry in depth. Cover: "
                f"(1) current regulations and compliance requirements for {industry} in 2025/2026; "
                f"(2) market trends and outlook; "
                f"(3) recent news and disruptions; "
                f"(4) key challenges and opportunities for companies operating in this space. "
                "Write a comprehensive industry research report."
            )
            svc_url, svc_run_id = await _fire_agent_service(
                industry, org_id,
                brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                task=task, agent_type="research",
            )
            db_run_id = await db_module.create_agent_run(
                org_id=org_id, agent_type="research",
                task=f"Industry research: {industry}", trigger_type="event_hook",
            )
            await db_module.update_agent_run(
                db_run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=industry))
            console.print(f"[dim]Agent service industry research started for '{industry}' (run={svc_run_id})[/dim]")
        else:
            angles = [
                f'"{industry}" industry regulations 2025 2026',
                f'"{industry}" industry news 2026',
                f'"{industry}" market trends outlook 2026',
                f'"{industry}" new laws compliance requirements',
                f'"{industry}" industry disruption challenges opportunities',
            ]
            task_id = await db_module.enqueue_research_task(
                org_id=org_id,
                subject_type="industry",
                subject=industry,
                task_type="orchestrate",
                payload={"source": "industry_hook", "angles": angles},
                depth=0,
                priority=5,
            )
            console.print(f"[dim]Industry research enqueued for '{industry}' (task_id={task_id})[/dim]")
    except Exception as exc:
        console.print(f"[yellow]Industry research trigger failed for '{industry}': {exc}[/yellow]")


async def _trigger_osint(client_name: str, org_id: int, run_id: Optional[int] = None, await_completion: bool = False) -> None:
    """Background: run OSINT/research agent on a newly seen client.

    If run_id is provided (pre-created by the caller), it is used instead of creating a new
    agent_runs row — prevents duplicate rows when called from create_client bulk flow.
    """
    if not DB_AVAILABLE:
        return
    await _clear_news_pending(org_id, client_name)
    backend = config.get("agent_service_backend", "python")
    if backend in ("pi", "hermes", "split"):
        try:
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            svc_url, svc_run_id = await _fire_agent_service(
                client_name, org_id,
                brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                agent_type="osint",
            )
            db_run_id = run_id if run_id else await db_module.create_agent_run(
                org_id=org_id, agent_type="osint",
                task=f"OSINT: {client_name}", trigger_type="event_hook",
            )
            await db_module.update_agent_run(
                db_run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            if await_completion:
                await _watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name)
            else:
                asyncio.create_task(_watch_agent_service_run(db_run_id, svc_url, svc_run_id, subject=client_name))
            console.print(f"[dim]Agent service OSINT started for '{client_name}' (run={svc_run_id})[/dim]")
        except Exception as exc:
            console.print(f"[yellow]OSINT trigger failed for '{client_name}': {exc}[/yellow]")
            if run_id:
                try:
                    await db_module.update_agent_run(run_id, "failed", error=str(exc))
                except Exception:
                    pass
        return
    try:
        db_run_id = run_id if run_id else await db_module.create_agent_run(
            org_id=org_id, agent_type="osint",
            task=f"OSINT research: {client_name}", trigger_type="event_hook",
        )
        await db_module.update_agent_run(db_run_id, "running")
        from agents._legacy.osint import run_osint
        result = await run_osint(client_name, org_id, db_run_id)
        if result.get("error") and not result.get("doc"):
            await db_module.update_agent_run(db_run_id, "failed", error=result["error"])
        else:
            await db_module.update_agent_run(
                db_run_id, "done",
                output={"client_name": result.get("client_name"), "doc": result.get("doc")},
            )
    except Exception as exc:
        console.print(f"[yellow]OSINT trigger failed for '{client_name}': {exc}[/yellow]")


# ---------------------------------------------------------------------------
# Heartbeat scheduler
# ---------------------------------------------------------------------------

async def _searxng_query(
    query: str, limit: int = 10, *,
    categories: str | None = None, time_range: str | None = None, language: str | None = None,
) -> dict:
    """Raw SearXNG JSON query, degraded-backend-aware. Raises on a transport
    failure (timeout, DNS, non-2xx) — callers decide whether that's fatal.

    Returns {"results": [...] (capped to `limit`), "unresponsive": [[engine,
    reason], ...], "engines_ok": n}. SearXNG's own JSON carries
    `unresponsive_engines` as [engine, reason] pairs whenever an engine was
    suspended/rate-limited/CAPTCHA'd/timed out for this query — the WP7 field
    drive found brave/startpage/qwant/mojeek suspended while only bing news
    kept answering, with `error: null` on every scan. `engines_ok` is the
    count of distinct engines that actually contributed a result, so a caller
    can tell "every engine that ran was suspended" from "some engines
    worked, this particular query just had no hits". categories/time_range/
    language are only added to the request when the caller passes them, so
    every existing caller sees no behavior change."""
    searxng_url = context.config.get("searxng_url", "http://localhost:8080").rstrip("/")
    params = {"q": query, "format": "json", "safesearch": 0}
    if categories:
        params["categories"] = categories
    if time_range:
        params["time_range"] = time_range
    if language:
        params["language"] = language
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(f"{searxng_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    results = (data.get("results") or [])[:limit]
    unresponsive = [
        [str(item[0]), str(item[1])] for item in (data.get("unresponsive_engines") or [])
        if isinstance(item, (list, tuple)) and len(item) >= 2
    ]
    engines_ok = len({
        e for r in results
        for e in ([r["engine"]] if r.get("engine") else []) + list(r.get("engines") or [])
    })
    return {"results": results, "unresponsive": unresponsive, "engines_ok": engines_ok}


async def _searxng_results(
    query: str, limit: int = 10, *,
    categories: str | None = None, time_range: str | None = None, language: str | None = None,
) -> list[dict]:
    """Thin wrapper over _searxng_query for callers that only need the plain
    results list (source discovery, careers-URL discovery, the news change
    gate) — every existing caller/mock of this name keeps working unchanged.
    Each result dict keeps whatever SearXNG returns (url/title/content/engine/
    publishedDate)."""
    data = await _searxng_query(
        query, limit=limit, categories=categories, time_range=time_range, language=language,
    )
    return data["results"]


def _dedupe_unresponsive(pairs: list) -> list:
    """First-seen-wins dedupe of [engine, reason] pairs by engine name — the
    same engine is typically reported unresponsive on every query in a scan
    (brave/startpage/qwant stay suspended for the whole run), and callers
    want one line per engine, not N repeats of the same pair."""
    seen: set = set()
    out: list = []
    for pair in pairs or []:
        if not (isinstance(pair, (list, tuple)) and len(pair) >= 2):
            continue
        engine = str(pair[0])
        if engine in seen:
            continue
        seen.add(engine)
        out.append([engine, str(pair[1])])
    return out


async def _client_news_changed(org_id: int, client: dict, fail_open: bool = True) -> bool:
    """Cheap no-LLM change gate: fingerprint the top SearXNG news results for
    a client and compare against the fingerprint stored on the last run.

    Returns True (= run the research) when the news picture changed or there is
    no previous fingerprint. When SearXNG is unreachable, returns `fail_open` —
    True for the heartbeat gate (an outage must not silence monitoring), False
    for the all-clients sweep (an outage must not research every client).
    Stores the new fingerprint in client metadata as news_fp / news_fp_at.
    """
    try:
        results = await _searxng_results(f'"{client["name"]}" news', limit=5)
    except Exception as exc:
        console.print(f"[yellow]news gate: SearXNG unreachable ({exc}) — fail-{'open' if fail_open else 'closed'} for '{client['name']}'[/yellow]")
        return fail_open

    fp_input = "\n".join(f"{r.get('url', '')}|{r.get('title', '')}" for r in results)
    new_fp = hashlib.sha256(fp_input.encode()).hexdigest()
    old_fp = (client.get("metadata") or {}).get("news_fp")

    try:
        await db_module.update_client_metadata(
            org_id, client["name"],
            {"news_fp": new_fp, "news_fp_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception:
        pass  # fingerprint storage is best-effort

    return old_fp is None or old_fp != new_fp


# ---------------------------------------------------------------------------
# Monitored sources — per-client page watching (source_monitor heartbeat)
# ---------------------------------------------------------------------------

_MAX_MONITORED_SOURCES = 6
_SOURCE_KEYWORDS = ("news", "press", "presse", "media", "newsroom", "blog", "investor")
_SOURCE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_HTML_TAG_RE = re.compile(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>")


def _normalize_source_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _client_domain(client: dict) -> str:
    website = ((client.get("metadata") or {}).get("website") or "").strip()
    if not website:
        return ""
    if not website.startswith("http"):
        website = f"https://{website}"
    host = urlparse(website).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _site_base(website: str) -> str:
    """Normalize a client's metadata.website into a fetchable https:// base
    URL for the jobs-discovery block (D11).

    metadata.website is stored VERBATIM by routers/internal.py's
    internal_create_client — for the four clients created that way in the
    WP7b drive that meant a bare domain with no scheme ("trumpf.com",
    "datev.de", "vorwerk.de", "festo.com"). urlparse("trumpf.com") then
    yields an EMPTY netloc (the whole string lands in .path instead), so
    every consumer that trusted a schemeless website silently did nothing:
    _careers_probe_urls returned [] (netloc check), _fetch_page_raw handed
    "trumpf.com" straight to httpx (which raises, then the same bad string
    reaches Camofox, which logs "Invalid URL: trumpf.com"), and
    _sitemap_job_urls' own netloc check also came back empty. One place to
    fix, reused at every jobs-block call site that turns metadata.website
    into a URL: the homepage fetch, the path-probe tier, and the sitemap
    probe (_client_domain above already gets this right and stays
    domain-only; the news block's _probe_newsroom_paths has the same
    https:// prepend inline).

    strip -> prepend https:// when there's no scheme -> drop a trailing
    slash -> lower-case the host. '' in, '' out."""
    website = (website or "").strip()
    if not website:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", website):
        website = f"https://{website}"
    p = urlparse(website)
    host = p.netloc.lower()
    if not host:
        return ""
    path = p.path.rstrip("/")
    return f"{p.scheme}://{host}{path}"


# Aggregator/registry/social domains that are never a company's own website
_AGGREGATOR_DOMAINS = {
    "linkedin.com", "xing.com", "facebook.com", "instagram.com", "youtube.com",
    "twitter.com", "x.com", "wikipedia.org", "kununu.com", "glassdoor.com",
    "glassdoor.de", "indeed.com", "stepstone.de", "northdata.de", "dnb.com",
    "creditreform.de", "wlw.de", "gelbeseiten.de", "11880.com", "firmenwissen.de",
    "unternehmensregister.de", "bundesanzeiger.de", "companyhouse.de", "implisense.com",
    "amazon.com", "amazon.de", "crunchbase.com", "bloomberg.com", "reuters.com",
    "handelsblatt.com", "finance.yahoo.com", "yahoo.com",
}

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(gmbh|ag|se|kg|kgaa|ohg|mbh|co|holding|group|gruppe|europa|service|inc|ltd|llc|e\.?v\.?)\b",
    re.IGNORECASE,
)


def _normalize_company_token(text: str) -> str:
    """'Deutsche Leasing AG' → 'deutscheleasing' — for name↔domain matching."""
    text = _LEGAL_SUFFIX_RE.sub(" ", text.lower())
    return re.sub(r"[^a-z0-9]", "", text)


def _simplify_company_name(name: str) -> tuple[str, str]:
    """('Deutscher Fußball-Bund e.V. (DFB)') → ('Deutscher Fußball-Bund', 'dfb').

    Returns (search-friendly name without legal suffixes/parentheticals, acronym).
    Exact-quoted full legal names match nothing in search engines — this is what
    made the first backfill miss well-known companies like Schufa and DFB.
    """
    acronym = ""
    m = re.search(r"\(([A-Za-z]{2,8})\)", name)
    if m:
        acronym = m.group(1).lower()
    simple = re.sub(r"\([^)]*\)", " ", name)
    simple = _LEGAL_SUFFIX_RE.sub(" ", simple)
    simple = re.sub(r"[&.,]", " ", simple)
    simple = re.sub(r"\s+", " ", simple).strip()
    return simple or name, acronym


def _result_domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


async def _openrouter_pick_website(client_name: str, candidates: list[dict], org_id: Optional[int] = None) -> str:
    """LLM fallback: pick the official website from SearXNG candidates.
    Returns a domain that MUST be among the candidates, or '' — the model can
    only choose from presented options, never invent a domain."""
    if not candidates:
        return ""
    listing = "\n".join(
        f"- {_result_domain(c['url'])}: {c.get('title', '')[:80]} — {c.get('content', '')[:120]}"
        for c in candidates
    )
    prompt = (
        f"\"{client_name}\" is a German/European B2B company (a sales prospect).\n"
        f"Which of these domains is its OFFICIAL corporate website?\n"
        f"{listing}\n\n"
        "Rules: answer with exactly one domain from the list, or NONE if you are "
        "not confident. Prefer the company's primary corporate domain over "
        "country subsidiaries. Job boards, universities, media outlets, industry "
        "associations, or similarly-named unrelated organizations are NOT the "
        "company's website — when in doubt, answer NONE. No explanation."
    )
    try:
        answer = (await llm.acomplete(prompt, role="pipeline", timeout=30, org_id=org_id)).lower()
    except Exception as exc:
        console.print(f"[yellow]website resolver: LLM fallback failed for '{client_name}': {exc}[/yellow]")
        return ""
    candidate_domains = {_result_domain(c["url"]) for c in candidates}
    for domain in candidate_domains:
        if domain and domain in answer:
            return domain
    return ""


async def _resolve_client_website(org_id: int, client: dict) -> Optional[str]:
    """Find the client's official website: SearXNG heuristic first (domain name
    resembles company name), LLM pick from candidates as fallback. Writes
    metadata.website + website_source on success. Never overwrites an existing
    website. Returns the website URL or None."""
    meta = client.get("metadata") or {}
    existing = (meta.get("website") or "").strip()
    if existing:
        return existing

    name = client["name"]
    simple_name, acronym = _simplify_company_name(name)
    candidates: list[dict] = []
    seen_domains: set[str] = set()
    # Unquoted, simplified queries — exact-quoted legal names match nothing.
    # Retry once after a pause: back-to-back backfill queries hit engine rate
    # limits and SearXNG then returns empty result sets.
    for attempt in range(2):
        for q in (f"{simple_name} impressum", f"{simple_name} official website"):
            try:
                results = await _searxng_results(q)
            except Exception:
                continue
            for r in results:
                url = (r.get("url") or "").strip()
                if not url.startswith("http"):
                    continue
                domain = _result_domain(url)
                if not domain or domain in seen_domains:
                    continue
                if any(domain == agg or domain.endswith("." + agg) for agg in _AGGREGATOR_DOMAINS):
                    continue
                seen_domains.add(domain)
                candidates.append(r)
        if candidates or attempt == 1:
            break
        await asyncio.sleep(10)

    # Only EXACT name↔domain matches are accepted heuristically. Substring
    # matches go to the LLM with priority — auto-accepting them produced
    # dal.ca for 'DAL Deutsche Anlagen-Leasing' (3-char domain in long name).
    website = ""
    source = ""
    name_token = _normalize_company_token(name)
    simple_token = _normalize_company_token(simple_name)
    partial: list[dict] = []
    for c in candidates:
        domain = _result_domain(c["url"])
        sld_token = _normalize_company_token(domain.rsplit(".", 1)[0].split(".")[-1])
        if not sld_token:
            continue
        # Name-token exact matches need ≥4 chars — 'hsk' == 'hsk' matched
        # hsk.academy; short ambiguous names go through the LLM instead.
        # Parenthetical acronyms ('(DFB)') stay at ≥3: they are deliberate
        # identifiers from the client's own name, not generic tokens.
        if (acronym and len(acronym) >= 3 and sld_token == acronym) or (
            len(sld_token) >= 4 and sld_token in (name_token, simple_token)
        ):
            website = f"https://{domain}"
            source = "heuristic"
            break
        if len(sld_token) >= 5 and (sld_token in name_token or name_token in sld_token):
            partial.append(c)

    if not website:
        ordered = partial + [c for c in candidates if c not in partial]
        domain = await _openrouter_pick_website(name, ordered[:8], org_id)
        if domain:
            website = f"https://{domain}"
            source = "llm"

    if not website:
        return None
    try:
        await db_module.update_client_metadata(
            org_id, name, {"website": website, "website_source": source},
        )
    except Exception as exc:
        console.print(f"[yellow]website resolver: could not save for '{name}': {exc}[/yellow]")
    console.print(f"[dim]website resolver: '{name}' → {website} ({source})[/dim]")
    return website


# Well-known newsroom/press paths, tried directly on the client's own site
# before falling back to homepage-link harvesting or a SearXNG search — a
# client whose newsroom lives at a boring, undiscoverable path (no inbound
# links, not indexed) still gets found this way.
_NEWSROOM_PATHS = (
    "/news", "/newsroom", "/presse", "/press", "/pressemitteilungen", "/aktuelles",
    "/media", "/unternehmen/presse", "/company/news", "/en/news", "/de/presse",
    "/investor-relations", "/investors",
)

# Loose date-like strings (ISO, DD.MM.YYYY, "12. März 2025", "March 12, 2025")
# — a newsroom page reliably has several of these, a generic page doesn't.
_NEWS_DATE_PATTERN_RE = re.compile(
    r"\b(?:20\d\d[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d\d|"
    r"\d{1,2}\.\s*(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|"
    r"Oktober|November|Dezember)\s*20\d\d|"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+20\d\d)\b",
    re.IGNORECASE,
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HTML_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_HTML_HREF_RE = re.compile(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


async def _probe_newsroom_paths(website: str) -> list[dict]:
    """GET each well-known newsroom path directly off the client's own site
    (no LLM). A hit needs: HTTP 200, ≥500 chars of visible text, and either
    ≥3 date-like strings in the text or a news keyword in the title/h1 —
    plain heuristics, same spirit as _fetch_source_fp's readability floor."""
    if not website:
        return []
    base = _site_base(website)  # D11 — same normalization the jobs block uses
    hits: list[dict] = []
    async with httpx.AsyncClient(
        timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
    ) as http:
        for path in _NEWSROOM_PATHS:
            url = f"{base}{path}"
            try:
                resp = await http.get(url)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            html = resp.text or ""
            text = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html)).strip()
            if len(text) < 500:
                continue
            title_m = _HTML_TITLE_RE.search(html)
            h1_m = _HTML_H1_RE.search(html)
            heading = " ".join(
                _HTML_TAG_RE.sub(" ", m.group(1)) for m in (title_m, h1_m) if m
            ).lower()
            has_keyword = any(k in heading for k in _SOURCE_KEYWORDS)
            has_dates = len(_NEWS_DATE_PATTERN_RE.findall(text)) >= 3
            if not (has_keyword or has_dates):
                continue
            label = (
                (_HTML_TAG_RE.sub(" ", title_m.group(1)).strip() if title_m else "")
                or path.strip("/").replace("/", " ").title()
            )
            hits.append({"url": url, "label": label[:60]})
    return hits


async def _harvest_links_news(website: str, keys: tuple, own_domain: str) -> list[dict]:
    """Homepage link harvest for newsroom/press pages: GET the homepage and
    pull out <a href> links whose href or visible text mentions one of `keys`,
    restricted to `own_domain` (or a subdomain of it).

    Deliberately separate from the jobs block's `_harvest_links(html,
    base_url, keys, own_domain)`: that one parses HTML it is handed and
    returns bare URLs, this one does its own GET and keeps the link text as
    a label (used to name the discovered source). Kept as one mockable unit
    so `TestDiscoverSources` never makes a real HTTP call.
    """
    if not website:
        return []
    base = _site_base(website)  # D11 — same normalization the jobs block uses
    html = ""
    try:
        async with httpx.AsyncClient(
            timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
        ) as http:
            resp = await http.get(base)
            if resp.status_code == 200:
                html = resp.text or ""
    except Exception:
        return []
    if not html:
        return []
    hits: list[dict] = []
    seen: set[str] = set()
    for m in _HTML_HREF_RE.finditer(html):
        href = m.group(1)
        text = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", m.group(2))).strip()
        haystack = f"{href} {text}".lower()
        if not any(k in haystack for k in keys):
            continue
        url = urljoin(base, href)
        host = _result_domain(url)
        if not own_domain or not (host == own_domain or host.endswith("." + own_domain)):
            continue
        if url in seen:
            continue
        seen.add(url)
        hits.append({"url": url, "label": text[:60] or urlparse(url).path})
    return hits


async def _discover_client_sources(org_id: int, client: dict) -> list[dict]:
    """Find newsroom/press pages for a client — no LLM, own domain only.

    Order: existing (user-added) sources are kept as-is; then well-known
    newsroom paths are probed directly; then the homepage is harvested for
    matching links; then a SearXNG site: search fills any remainder. Merges
    into existing sources, caps at _MAX_MONITORED_SOURCES, and stamps
    sources_discovered_at (also when there's no website at all, so the sweep
    doesn't retry every cycle — see _discovery_marker_stale).
    """
    meta = client.get("metadata") or {}
    existing = list(meta.get("monitored_sources") or [])
    seen = {_normalize_source_url(s.get("url", "")) for s in existing}
    name = client["name"]
    now_iso = datetime.now(timezone.utc).isoformat()

    # No website on record → resolve it first (heuristic, LLM fallback) so the
    # precise own-domain discovery query can run
    if not (meta.get("website") or "").strip():
        website = await _resolve_client_website(org_id, client)
        if website:
            meta["website"] = website
            client["metadata"] = meta

    website = (meta.get("website") or "").strip()
    if not website:
        try:
            await db_module.update_client_metadata(
                org_id, name, {"sources_discovered_at": now_iso},
            )
        except Exception as exc:
            console.print(f"[yellow]source discovery: could not save for '{name}': {exc}[/yellow]")
        return existing

    domain = _client_domain(client)

    candidates: list[tuple[int, str, str]] = []

    try:
        probe_hits = await _probe_newsroom_paths(website)
    except Exception:
        probe_hits = []
    for h in probe_hits:
        candidates.append((0, h["url"], h.get("label", "")))

    try:
        harvested = await _harvest_links_news(website, _SOURCE_KEYWORDS, domain)
    except Exception:
        harvested = []
    for h in harvested:
        candidates.append((1, h["url"], h.get("label", "")))

    if domain:
        try:
            results = await _searxng_results(
                f"site:{domain} news OR presse OR newsroom", limit=10, categories="general",
            )
        except Exception:
            results = []
        for r in results:
            url = (r.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            host = _result_domain(url)
            if not (host == domain or host.endswith("." + domain)):
                continue
            candidates.append((2, url, (r.get("title") or "")[:60]))

    added: list[dict] = []
    for _prio, url, title in sorted(candidates, key=lambda t: t[0]):
        norm = _normalize_source_url(url)
        if norm in seen:
            continue
        seen.add(norm)
        added.append({"url": url, "label": title or urlparse(url).netloc, "added": now_iso})
        if len(added) >= 4 or len(existing) + len(added) >= _MAX_MONITORED_SOURCES:
            break

    merged = existing + added
    try:
        await db_module.update_client_metadata(
            org_id, name, {"monitored_sources": merged, "sources_discovered_at": now_iso},
        )
    except Exception as exc:
        console.print(f"[yellow]source discovery: could not save for '{name}': {exc}[/yellow]")

    if added:
        try:
            import playbook  # type: ignore
        except ImportError:
            playbook = None
        if playbook is not None:
            try:
                await playbook.record(
                    org_id, domain,
                    {"newsroom": {"urls": [a["url"] for a in added], "last_success_at": now_iso}},
                    website=website,
                )
            except Exception as exc:
                console.print(f"[yellow]source discovery: playbook record failed for '{name}': {exc}[/yellow]")

    return merged


# Camofox (Firefox + fingerprint spoofing) tab API — mirrors
# agent_service_ts/src/search.ts's fetchPageCamofox: POST /tabs to open a
# rendered tab, GET /tabs/{id}/snapshot for the visible-text snapshot, and
# always DELETE /tabs/{id} to release it. Used as a last-resort tier for
# bot-protected sites where plain GET and the Playwright browser-service
# both come back blocked or empty.
#
# Its own userId (distinct from the TS agent runtime's "pi"): Camofox keys
# tab/browser-profile isolation off userId, so using a separate id here
# keeps this backend's own automated fetches from sharing fingerprint/session
# state with the "pi" agent's Camofox sessions — same choice already made by
# routers/knowledge.py's _fetch_event_for_mail.
_CAMOFOX_USER_ID = "server"

# Bounded ring of the last 50 page fetches, for diagnostics (which tier is
# actually working on a given deployment). Not persisted; process-local.
_FETCH_TIER_LOG: deque = deque(maxlen=50)


def _record_fetch_tier(url: str, tier: str, chars: int) -> None:
    _FETCH_TIER_LOG.append({
        "url": url,
        "tier": tier,
        "chars": chars,
        "at": datetime.now(timezone.utc).isoformat(),
    })


def _ws_norm(s: str) -> str:
    """Collapse all whitespace to single spaces and strip. Used to compare
    candidate texts from different tiers on equal footing — the plain-GET
    tier's tag-stripped text isn't collapsed until the very end of
    _fetch_page_text, so comparing raw len() against an already-collapsed
    browser/Camofox candidate silently favours the plain tier's leftover
    markup whitespace."""
    return re.sub(r"\s+", " ", s or "").strip()


# Playwright/Camofox ARIA-snapshot link lines, e.g.:
#   - link "Karriere" [e12]:
#     - /url: /de_DE/karriere/
_CAMOFOX_LINK_RE = re.compile(r'-\s*link\s+"([^"]*)"\s*\[[^\]]*\]:\s*\n\s*-\s*/url:\s*(\S+)')


def _camofox_links_html(snapshot: str, base_url: str) -> str:
    """Rebuild an <a href="..">text</a> list from a Camofox accessibility
    snapshot's link entries, so _harvest_links can rank them exactly like it
    does for plain-GET HTML. Relative hrefs are resolved against base_url.
    Returns '' when the snapshot has no link entries."""
    links = []
    for text, href in _CAMOFOX_LINK_RE.findall(snapshot or ""):
        try:
            resolved = urljoin(base_url, href)
        except Exception:
            resolved = href
        safe_text = text.replace("<", "&lt;").replace(">", "&gt;")
        links.append(f'<a href="{resolved}">{safe_text}</a>')
    return "\n".join(links)


async def _fetch_page_camofox(url: str, *, wait_ms: int = 2500, max_chars: int = 18000) -> tuple[str, str]:
    """Fetch a page via Camofox (Firefox + fingerprint spoofing), for
    bot-protected sites where plain GET and the browser-service both fail.
    Mirrors search.ts's fetchPageCamofox: opens a tab, waits wait_ms for it
    to render, reads the accessibility snapshot, then always releases the
    tab. Never raises — any failure (missing CAMOFOX_URL, non-2xx response,
    timeout, bad JSON) yields ('', '').

    Returns (text, links_html): text is the same first-pass normalization
    search.ts applies (collapse blank-line runs, drop NULs, cap length) —
    _fetch_page_text flattens it further afterward, same as it already does
    for the browser tier, so don't read this as byte-identical to what
    search.ts's own caller sees. links_html is the snapshot's link entries
    rebuilt as <a href> tags (see _camofox_links_html) for callers that want
    to harvest links off a JS-only page (_fetch_page_raw).

    wait_ms is clamped to [2000, 4000]ms and the three HTTP calls use 8/12/5s
    timeouts so one call stays well under a 30s budget even when a caller
    (e.g. a jobs scan harvesting a few sub-links) makes several in a row."""
    camofox_url = os.environ.get("CAMOFOX_URL", "").rstrip("/")
    if not camofox_url:
        return "", ""

    wait_ms = min(max(wait_ms, 2000), 4000)
    tab_id: Optional[str] = None
    text = ""
    links_html = ""
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            resp = await http.post(
                f"{camofox_url}/tabs",
                json={"userId": _CAMOFOX_USER_ID, "sessionKey": str(uuid.uuid4()), "url": url},
            )
        if resp.status_code not in (200, 201):
            logger.debug("_fetch_page_camofox: /tabs returned %s for %r", resp.status_code, url)
            return "", ""
        body = resp.json()
        tab_id = body.get("tabId") or body.get("id")
        if not tab_id:
            logger.debug("_fetch_page_camofox: no tabId in response for %r", url)
            return "", ""

        await asyncio.sleep(wait_ms / 1000)

        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.get(
                f"{camofox_url}/tabs/{tab_id}/snapshot", params={"userId": _CAMOFOX_USER_ID},
            )
        if resp.status_code == 200:
            snapshot = resp.json().get("snapshot", "")
            text = re.sub(r"\n{3,}", "\n\n", snapshot).replace("\0", "").strip()[:max_chars]
            links_html = _camofox_links_html(snapshot, url)
        else:
            logger.debug("_fetch_page_camofox: snapshot returned %s for %r", resp.status_code, url)
    except Exception as exc:
        logger.debug("_fetch_page_camofox: error for %r: %s", url, exc)
        text, links_html = "", ""
    finally:
        if tab_id:
            try:
                async with httpx.AsyncClient(timeout=5.0) as http:
                    await http.delete(f"{camofox_url}/tabs/{tab_id}", params={"userId": _CAMOFOX_USER_ID})
            except Exception as exc:
                logger.debug("_fetch_page_camofox: tab cleanup failed for %r: %s", url, exc)

    return text, links_html


async def _fetch_rendered_tier(
    url: str,
    text_so_far: str,
    max_chars: int,
    wait_ms: int,
    *,
    prefer_camofox: bool = False,
) -> tuple[str, str, str]:
    """Shared browser-service -> Camofox fallback stage, used by both
    _fetch_page_text (which only wants the text) and _fetch_page_raw (which
    also wants Camofox's links_html for link harvesting on JS-only pages —
    calling this once, rather than _fetch_page_raw invoking
    _fetch_page_camofox again on its own, avoids opening a second real
    Camofox tab for the same page).

    text_so_far is whatever the plain-GET tier already produced (possibly
    '', tag-stripped but not necessarily whitespace-collapsed).

    If text_so_far is already substantial (>=500 normalized chars), it's
    returned immediately — the common case stays exactly as fast as before.
    Otherwise it's thin/blocked, and from here browser-service and Camofox
    are compared only against EACH OTHER, never against the thin plain-GET
    leftovers: a nav/footer stub's whitespace-inflated raw length must not
    out-score a shorter but genuine rendered result (this was the WP11
    review's blocker — main always replaced `text` unconditionally once a
    later tier answered; a naive length-based rewrite of that regressed on
    exactly these thin, nav-only pages, e.g. a 160-char "Impressum ..." stub
    beating a real 109-char job listing because it was compared against the
    stub's un-collapsed ~309-char length). If every later tier fails or
    comes back empty, text_so_far is kept as a last resort rather than being
    thrown away for nothing.

    Returns (text, tier, camofox_links_html)."""
    plain_text = text_so_far
    plain_thin = prefer_camofox or len(_ws_norm(plain_text)) < 500
    if not plain_thin:
        return plain_text, "http", ""

    text, tier, links_html = "", "none", ""
    browser_thin_or_failed = prefer_camofox

    browser_url = os.environ.get("BROWSER_SERVICE_URL", "http://localhost:3000").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=40.0) as http:
            resp = await http.post(
                f"{browser_url}/fetch", json={"url": url, "max_chars": max_chars, "wait_ms": wait_ms},
            )
            if resp.status_code == 200:
                browser_text = resp.json().get("text", "")
                browser_thin_or_failed = len(_ws_norm(browser_text)) < 500
                if _ws_norm(browser_text):
                    text, tier = browser_text, "browser"
            else:
                browser_thin_or_failed = True
    except Exception:
        browser_thin_or_failed = True

    if browser_thin_or_failed:
        camofox_text, links_html = await _fetch_page_camofox(url, wait_ms=wait_ms, max_chars=max_chars)
        if len(_ws_norm(camofox_text)) > len(_ws_norm(text)):
            text, tier = camofox_text, "camofox"

    if not _ws_norm(text):
        text, tier = plain_text, ("http" if plain_text else "none")

    return text, tier, links_html


async def _fetch_page_text(
    url: str,
    max_chars: int = 9000,
    wait_ms: int = 1500,
    *,
    prefer_camofox: bool = False,
) -> str:
    """Fetch a page's visible text.

    Default order: plain GET -> browser-service -> Camofox. Camofox (last
    resort) is only tried when the plain GET is thin (<500 chars, normalized
    — this also covers a blocked 403/503/429 response, which never sets any
    text) AND the browser-service also came back thin (<500 chars) or
    failed/timed out — this keeps the common case (an early tier already
    returning real content) exactly as fast as before.

    Pass prefer_camofox=True when the caller already knows the site needs
    anti-detection (a playbook needs_js/cookie_wall hit, or a previous 403)
    to skip the plain GET and go browser-service -> Camofox directly. No
    production caller yet passes this — WP8's jobs path-probe is expected to,
    for playbook needs_js/cookie_wall hits.

    Returns normalized text (possibly '')."""
    text = ""
    plain_status: Optional[int] = None

    if not prefer_camofox:
        try:
            async with httpx.AsyncClient(
                timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
            ) as http:
                resp = await http.get(url)
                plain_status = resp.status_code
                if resp.status_code == 200:
                    text = _HTML_TAG_RE.sub(" ", resp.text)
        except Exception:
            pass

    if plain_status and plain_status != 200:
        logger.debug("_fetch_page_text: plain GET returned %s for %r", plain_status, url)

    text, tier, _links_html = await _fetch_rendered_tier(
        url, text, max_chars, wait_ms, prefer_camofox=prefer_camofox,
    )

    text = re.sub(r"\s+", " ", text).strip()
    _record_fetch_tier(url, tier if text else "none", len(text))
    return text


async def _fetch_source_fp(url: str) -> Optional[str]:
    """Fingerprint a monitored page without any LLM. Returns sha256 of the
    normalized visible text, or None when unreadable (callers must treat None
    as 'no signal', never as a change)."""
    text = await _fetch_page_text(url)
    if len(text) < 200:
        return None
    return hashlib.sha256(text[:8000].encode()).hexdigest()


_NEWS_SIGNAL_SCORING_HINT = (
    " For every significant development also write a type='signal' document with "
    "metadata.relevance_score (1-5; 5 = major strategic event such as M&A, CEO/CTO change, "
    "large investment, or regulatory action). Score honestly — scores of 4 or higher "
    "trigger a deeper product-match analysis. Pass source_url for each signal — the URL of "
    "the page the development came from — so the reader can open the original article. "
    "Use web_search with category='news' and time_range='month' first to find recent coverage, "
    "and include each article's publication date in the signal."
)


async def _fire_news_research(org_id: int, client_name: str, *, autonomous: bool = False) -> Optional[int]:
    """Fire the news-OSINT Pi run for one client, awaited. Returns the agent_run id.
    autonomous=True stamps trigger_type='autonomous' (agent-decided provenance)."""
    from routers.agents import _fire_agent_service, _watch_agent_service_run
    client_task = _NEWS_OSINT_TASK.format(subject=client_name) + _NEWS_SIGNAL_SCORING_HINT
    child_run_id = await db_module.create_agent_run(
        org_id=org_id, agent_type="osint", task=client_task,
        trigger_type=autonomy.TRIGGER if autonomous else "heartbeat",
    )
    try:
        svc_url, svc_run_id = await _fire_agent_service(
            client_name, org_id,
            brain="", model="",   # "" = the choke point decides (org subscription, else config default)
            task=client_task, agent_type="osint",
        )
        await db_module.update_agent_run(
            child_run_id, "running", output={"service_run_id": svc_run_id},
        )
        await _watch_agent_service_run(child_run_id, svc_url, svc_run_id, subject=client_name)
        return child_run_id
    except Exception as exc:
        await db_module.update_agent_run(child_run_id, "failed", error=str(exc))
        return None


async def _maybe_escalate_match(org_id: int, client_name: str, agent_run_id: Optional[int]) -> bool:
    """Agent-decided escalation: if the news research scored any signal at or
    above match_escalation_min_relevance, re-run the match analysis (which keeps
    its own products-exist and 7-day-report gates)."""
    threshold = int(context.config.get("match_escalation_min_relevance", 4))
    if not agent_run_id or threshold <= 0:
        return False
    try:
        async with db_module._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT MAX((metadata->>'relevance_score')::float) AS max_rel
                   FROM documents
                   WHERE org_id = $1 AND agent_run_id = $2 AND type = 'signal'""",
                org_id, agent_run_id,
            )
        max_rel = row["max_rel"] if row else None
    except Exception as exc:
        console.print(f"[yellow]match escalation check failed for '{client_name}': {exc}[/yellow]")
        return False
    if max_rel is None or max_rel < threshold:
        return False
    from routers.agents import _maybe_trigger_pain_point_research
    console.print(f"[dim]source monitor: '{client_name}' signal relevance {max_rel:.0f} ≥ {threshold} — escalating to match analysis[/dim]")
    await _maybe_trigger_pain_point_research(org_id, client_name)
    return True


# ---------------------------------------------------------------------------
# Client news scan — Python/textonly pipeline replacing the old "News is just
# a prompt instruction" approach. SearXNG gathers dated candidates, ONE
# llm.acomplete call scores them, and relevant ones become client-linked
# type='signal' documents — no Pi run, no tool calls, subscription-friendly.
# ---------------------------------------------------------------------------

# Aggregator/social/job-board hosts that are never a genuine news source about
# a client — same spirit as _AGGREGATOR_DOMAINS above but for news candidates.
_NEWS_SKIP_HOSTS = {
    "linkedin.com", "xing.com", "facebook.com", "instagram.com", "youtube.com",
    "twitter.com", "x.com", "wikipedia.org", "kununu.com", "glassdoor.com",
    "glassdoor.de", "indeed.com", "stepstone.de", "pinterest.com", "tiktok.com",
}

# {rules} defaults to "" (no learned rules injected) until WP4's playbook
# lessons land and start passing lessons_block("news") in.
_NEWS_SCORE_PROMPT = (
    "Below are {n} candidate news search results about \"{subject}\". Score each for how "
    "genuinely relevant and specific it is to {subject} — score 1 = irrelevant, generic, or "
    "about someone/something else entirely (a namesake); anything not clearly about {subject} "
    "is 1. Use 4-5 only for a major, credible, verifiable development.\n"
    "{rules}"
    "Return STRICT JSON ONLY — a list, one object per candidate, in the same order, no prose, "
    "no markdown fences:\n"
    '[{{"i": <candidate index>, "relevance": <1-5>, '
    '"signal_type": "opportunity|risk|pain_point|news", '
    '"headline": "<=90 chars, a short factual headline", '
    '"why": "<=160 chars, the concrete fact that makes this relevant"}}]\n\n'
    "CANDIDATES:\n{listing}"
)


def _rules_block(rules: str) -> str:
    """Normalize a learned-rules block for a prompt's {rules} slot — shared by
    _NEWS_SCORE_PROMPT, _JOBS_EXTRACT_PROMPT, and the careers-selection
    prompt. '' stays '' (no dangling blank line before the following
    instruction), and any other text always gets exactly one trailing
    newline — whether or not the caller's text already ends in one — so the
    next instruction always starts on its own line. The slot this feeds must
    always sit BEFORE the prompt's final instruction / JSON-output contract
    and never after untrusted content (page text, search-result titles) —
    landing after would both bury it in a region a malicious page could spoof
    and push the real instruction out of the position models weight most."""
    rules = (rules or "").rstrip("\n")
    return f"{rules}\n" if rules else ""


async def _news_lessons_block(org_id: int) -> str:
    """Approved cross-site lessons (scope='news', WP4), for _rules_block's
    `rules` argument. '' when playbook isn't available, org_id is falsy, or
    there are no approved news/all-scope lessons — _rules_block already
    collapses '' to no dangling text, so callers pass this straight through."""
    if not org_id:
        return ""
    try:
        import playbook  # type: ignore
    except ImportError:
        return ""
    try:
        lessons = await playbook.lessons_load(org_id)
        return playbook.lessons_block(lessons, "news")
    except Exception:
        return ""


# YYYY/MM/DD or YYYY-MM-DD embedded in a URL path/slug, e.g. .../2025/03/12/...
_URL_DATE_RE = re.compile(r"(20\d\d)[/-](\d\d)[/-](\d\d)")


def _norm_news_url(url: str) -> str:
    """Normalize a news URL for de-dup / existing-signal comparison: lowercase
    host, strip 'www.', drop utm_*/fbclid/gclid query params, the fragment,
    and a trailing slash."""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")
    ]
    path = (parsed.path or "").rstrip("/")
    normalized = f"{host}{path}"
    if kept:
        normalized += f"?{urlencode(kept)}"
    return normalized


def _parse_published(r: dict) -> Optional[str]:
    """'YYYY-MM-DD' from a SearXNG result: publishedDate first (ISO, any
    precision), else a YYYY[/-]MM[/-]DD date embedded in the URL, else None —
    callers must treat None as 'undated', never guess a date."""
    raw = (r.get("publishedDate") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
        except (ValueError, TypeError):
            pass
    m = _URL_DATE_RE.search(r.get("url") or "")
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Date recovery (step 3) — for SearXNG results with neither a publishedDate
# nor a URL date, and for dating <a href> items harvested straight off a
# client's own newsroom page (own-newsroom tier, below).
# ---------------------------------------------------------------------------

_TIME_TAG_RE = re.compile(r'<time\b[^>]*\bdatetime=["\']([^"\']+)["\']', re.I)
_TEXT_ISO_DATE_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_TEXT_DE_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b")
_META_PUBLISHED_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|date)["\'][^>]*content=["\']([^"\']+)["\']',
    re.I,
)
_META_PUBLISHED_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:article:published_time|date)["\']',
    re.I,
)
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)


def _coerce_iso_date(raw: str) -> Optional[str]:
    """'YYYY-MM-DD' from an arbitrary date string (full ISO datetime, bare
    ISO date, or an embedded YYYY[/-]MM[/-]DD) — None if nothing parses."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        pass
    m = _URL_DATE_RE.search(raw)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _extract_published_from_html(html: str) -> Optional[str]:
    """A single article page's own publish date: `<meta
    property="article:published_time">`, `<meta name="date">`, `<time
    datetime>`, then JSON-LD `datePublished` — first match wins. Used by the
    bounded page-header probe (step 3) for SearXNG results with no
    publishedDate/URL date."""
    for pattern in (_META_PUBLISHED_RE, _META_PUBLISHED_RE_REV, _TIME_TAG_RE, _JSONLD_DATE_RE):
        m = pattern.search(html or "")
        if m:
            iso = _coerce_iso_date(m.group(1))
            if iso:
                return iso
    return None


async def _probe_published_date(url: str) -> Optional[str]:
    """Bounded page-header probe (step 3): plain GET, 8s. None on any
    failure or when nothing parses — callers must drop the candidate, never
    guess a date."""
    try:
        async with httpx.AsyncClient(
            timeout=8.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
        ) as http:
            resp = await http.get(url)
            if resp.status_code != 200:
                return None
            html = resp.text
    except Exception:
        return None
    return _extract_published_from_html(html)


_ANCHOR_OPEN_RE = re.compile(r"<a\b", re.I)
_ANCHOR_CLOSE_RE = re.compile(r"</a>", re.I)
_BR_TAG_RE = re.compile(r"<br\b", re.I)

# The tags real listings wrap one item in — <li>/<article>/<tr>/<section>/
# <div> — used to bound _extract_date_near's search to THIS item's own
# markup. Neighbouring-anchor bounds (below) are only a fallback for
# markup with no such wrapper at all: a real listing's item boundary is
# this tag, not merely "wherever the next/previous <a> happens to be".
_CONTAINER_CLOSE_RE = re.compile(r"</(?:li|article|tr|section|div)\b", re.I)
_CONTAINER_OPEN_RE = re.compile(r"<(?:li|article|tr|section|div)\b", re.I)

# WP9 re-review: forward-only search (the first fix) was wrong for a "date
# BEFORE the title link" convention (e.g. German <span>12.09.2026</span>
# <a>Titel</a> listings) — every anchor's forward region ran up to the NEXT
# `<a`, exactly where the next item's own leading date sits, so each item
# silently inherited its successor's date. Bounding both directions by the
# neighbouring <a> (the second fix) still let an UNDATED item inherit a
# neighbour's marker whenever that neighbour's own container was smaller
# than the (generous, unbounded-by-markup) 120-char cap — a plain compact
# `<li>` item is well under 120 chars end to end. Bounding by the enclosing
# item CONTAINER instead of just the neighbouring anchor fixes this
# properly: an item's own container never includes a NEIGHBOUR's marker,
# so there is nothing left to leak regardless of the cap's exact value.
# The cap stays as a last-resort guard for markup with no container tags at
# all (the neighbouring-anchor fallback below).
_DATE_NEAR_MAX_DISTANCE = 120


def _extract_date_near(html: str, start: int, end: int, window: int = 300) -> Optional[str]:
    """The date belonging to ONE <a> match on a listing page (a newsroom
    index lists many items, each with its own date) — unlike
    _extract_published_from_html (a whole single-article page), this
    handles BOTH a trailing-marker convention (title link, then its own
    date) and a date-before convention (date, then the title link).

    Searches a forward region and a backward region, each bounded first by
    this item's own enclosing container tag (the first `</li|</article|
    </tr|</section|</div` after the anchor forward; the last matching open
    tag before it backward) — never a neighbour's, since a container never
    contains another item's markup. Only when no such tag exists on that
    side at all does it fall back to the neighbouring anchor (`<a` forward,
    `</a>` backward, WP9's first two fixes) AND the nearest bare `<br>`
    line-break on that side, if any (a wrapper-less listing like `<a>1</a>
    date<br><a>2</a> date<br><a>3</a>` still separates items even with no
    <li>/<article>/... at all) — further capped at _DATE_NEAR_MAX_DISTANCE
    chars, a last resort for markup with no separator whatsoever, where
    "far away" is the only signal left that a match belongs to some OTHER
    item.

    Every `<time datetime>` / ISO / German (dd.mm.yyyy) match found in
    EITHER region is a candidate; the NEAREST one to the anchor (by
    character distance) wins, not whichever direction or pattern is tried
    first. When nothing qualifies, returns None — an item with no marker
    of its own must never inherit a neighbour's; a dropped date is better
    than a wrong one."""
    window_fwd_limit = min(len(html), end + window)
    container_close = _CONTAINER_CLOSE_RE.search(html, end, window_fwd_limit)
    if container_close:
        fwd_limit = container_close.start()
    else:
        fwd_limit = window_fwd_limit
        next_anchor = _ANCHOR_OPEN_RE.search(html, end)
        if next_anchor:
            fwd_limit = min(fwd_limit, next_anchor.start())
        # No <li>/<article>/... wrapper at all — a bare <br> is the only
        # other common item separator (e.g. <a>1</a> date<br><a>2</a>...);
        # without this, an anchor-only bound still lets a wrapper-less
        # listing's last (undated) item reach backward past the <br> into
        # its predecessor's trailing date.
        br = _BR_TAG_RE.search(html, end, fwd_limit)
        if br:
            fwd_limit = min(fwd_limit, br.start())

    window_back_limit = max(0, start - window)
    container_open = None
    for m in _CONTAINER_OPEN_RE.finditer(html, window_back_limit, start):
        container_open = m
    if container_open:
        back_limit = container_open.start()
    else:
        back_limit = window_back_limit
        prev_close = None
        for m in _ANCHOR_CLOSE_RE.finditer(html, window_back_limit, start):
            prev_close = m
        if prev_close is not None:
            back_limit = max(back_limit, prev_close.end())
        br = None
        for m in _BR_TAG_RE.finditer(html, back_limit, start):
            br = m
        if br is not None:
            back_limit = max(back_limit, br.end())

    candidates: list[tuple[int, str]] = []

    def _collect(segment: str, base_offset: int, anchor_pos: int) -> None:
        for m in _TIME_TAG_RE.finditer(segment):
            iso = _coerce_iso_date(m.group(1))
            if iso:
                candidates.append((abs(base_offset + m.start() - anchor_pos), iso))
        # Length-preserving tag strip (spaces equal to each tag's own
        # length, not a single space) so a text-pattern match's offset
        # inside `text_seg` still lines up with its real position in
        # `segment`/`html` — _DATE_NEAR_MAX_DISTANCE only means anything
        # if distances are actual character counts, not stripped-text ones.
        text_seg = _HTML_TAG_RE.sub(lambda tm: " " * len(tm.group(0)), segment)
        for m in _TEXT_ISO_DATE_RE.finditer(text_seg):
            y, mo, d = (int(g) for g in m.groups())
            try:
                iso = date(y, mo, d).isoformat()
            except ValueError:
                continue
            candidates.append((abs(base_offset + m.start() - anchor_pos), iso))
        for m in _TEXT_DE_DATE_RE.finditer(text_seg):
            d, mo, y = (int(g) for g in m.groups())
            try:
                iso = date(y, mo, d).isoformat()
            except ValueError:
                continue
            candidates.append((abs(base_offset + m.start() - anchor_pos), iso))

    _collect(html[end:fwd_limit], end, end)
    _collect(html[back_limit:start], back_limit, start)

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    best_distance, best_iso = candidates[0]
    if best_distance > _DATE_NEAR_MAX_DISTANCE:
        return None
    return best_iso


# ---------------------------------------------------------------------------
# Own-newsroom tier (WP9) — backend-independent of SearXNG: harvest dated
# items straight off the client's own known newsroom/press pages, so a
# working newsroom still yields signals when every search engine is blocked.
# ---------------------------------------------------------------------------

_NEWSROOM_MAX_PAGES = 3
_NEWSROOM_CAP = 10
_ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def _client_newsroom_urls(client: dict, pb: Optional[dict]) -> list[str]:
    """Newsroom URLs already known for this client: playbook.newsroom.urls
    first (org-wide, written by _discover_client_sources' playbook.record
    call), then client.metadata.monitored_sources (per-client, same origin —
    see _discover_client_sources). Order preserved, deduped."""
    urls: list[str] = []
    seen: set = set()
    for u in ((pb or {}).get("newsroom") or {}).get("urls") or []:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    meta = client.get("metadata") or {}
    for src in meta.get("monitored_sources") or []:
        u = (src.get("url") or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


async def _newsroom_candidates(org_id: int, client: dict) -> tuple[list[dict], list[dict]]:
    """Fetch up to _NEWSROOM_MAX_PAGES of the client's own newsroom pages and
    harvest same-domain, dated <a href> items — plain GET, 12s, _SOURCE_UA;
    on 403/503, and only for the first page, falls back to the shared
    browser-service -> Camofox tier (_fetch_rendered_tier, the same one
    _fetch_page_text/_fetch_page_raw use) with text_so_far="" — it already
    knows not to re-GET a URL that just failed. Only Camofox's links_html
    is usable here: it rebuilds real <a href> markup from the
    accessibility snapshot (harvestable by _ANCHOR_RE below), whereas the
    browser-service tier alone returns plain innerText with no anchors at
    all. Dates come from _parse_published (URL), then _extract_date_near
    (a <time> tag or ISO/German date near the anchor). Dedupes by
    _norm_news_url, drops anything older than 90 days, caps at
    _NEWSROOM_CAP.

    Returns (candidates, blocked) — candidates are shaped like a SearXNG
    result (url/title/content/_norm_url/_published/query/engine) so they
    slot straight into _client_news_scan's candidate list; blocked is
    {"url", "kind", "at"} entries (kind one of "403"/"4xx"/"fetch_error"/
    "no_content") for the playbook.record(...) blocked_urls patch the news
    scan already makes.
    """
    domain = _client_domain(client)
    if not domain:
        return [], []

    try:
        import playbook  # type: ignore
    except ImportError:
        playbook = None
    pb = None
    if playbook is not None:
        try:
            pb = await playbook.load(org_id, domain)
        except Exception:
            pb = None

    urls = _client_newsroom_urls(client, pb)
    if not urls:
        return [], []

    today = datetime.now(timezone.utc).date()
    now_iso = datetime.now(timezone.utc).isoformat()
    candidates: list[dict] = []
    blocked: list[dict] = []
    norm_seen: set = set()

    for i, page_url in enumerate(urls[:_NEWSROOM_MAX_PAGES]):
        html = ""
        status_code: Optional[int] = None
        fetch_failed = False
        try:
            async with httpx.AsyncClient(
                timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
            ) as http:
                resp = await http.get(page_url)
                status_code = resp.status_code
                if status_code == 200:
                    html = resp.text
        except Exception:
            fetch_failed = True

        if not html and not fetch_failed and status_code in (403, 503) and i == 0:
            try:
                _rendered_text, _tier, links_html = await _fetch_rendered_tier(
                    page_url, "", 18000, 1500,
                )
                html = links_html
            except Exception:
                pass

        if not html:
            if fetch_failed:
                kind = "fetch_error"
            elif status_code == 403:
                kind = "403"
            elif status_code is not None and status_code >= 400:
                kind = "4xx"
            else:
                kind = "no_content"
            blocked.append({"url": page_url, "kind": kind, "at": now_iso})
            continue

        for m in _ANCHOR_RE.finditer(html):
            href, inner = m.group(1).strip(), m.group(2)
            if href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            full = urljoin(page_url, href)
            if not full.startswith("http"):
                continue
            host = _result_domain(full)
            if not (host == domain or host.endswith("." + domain)):
                continue
            norm = _norm_news_url(full)
            if not norm or norm in norm_seen:
                continue

            published = _parse_published({"url": full}) or _extract_date_near(html, m.start(), m.end())
            if not published:
                continue
            try:
                if (today - date.fromisoformat(published)).days > 90:
                    continue
            except ValueError:
                continue

            norm_seen.add(norm)
            title = _HTML_TAG_RE.sub(" ", inner).strip()[:120] or urlparse(full).path
            candidates.append({
                "url": full, "title": title, "content": "",
                "_norm_url": norm, "_published": published, "query": "",
                "engine": "newsroom",
            })
            if len(candidates) >= _NEWSROOM_CAP:
                return candidates, blocked

    return candidates, blocked


async def _existing_signal_urls(org_id: int, client_id: int) -> set[str]:
    """Normalized source_url of every type='signal' document already linked to
    this client — so a news scan never re-scores (and re-writes) the same
    article twice."""
    try:
        docs = await db_module.list_documents(org_id, client_id=client_id)
    except Exception:
        return set()
    urls: set[str] = set()
    for d in docs:
        if d.get("type") != "signal":
            continue
        meta = d.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        url = meta.get("source_url")
        if url:
            urls.add(_norm_news_url(url))
    return urls


def _news_result_allowed(url: str) -> tuple[bool, str]:
    """(is_allowed, host) — drops skip-hosts and non-http(s) results."""
    if not url.startswith("http"):
        return False, ""
    host = _result_domain(url)
    if not host or host in _NEWS_SKIP_HOSTS or any(
        host.endswith("." + h) for h in _NEWS_SKIP_HOSTS
    ):
        return False, host
    return True, host


async def _news_candidates(org_id: int, client: dict) -> dict:
    """Gather candidate news articles for a client via SearXNG: exact-name
    news search, name+industry, own-domain site search, and (only when those
    come up thin) a generic name+news fallback. Drops skip-host results,
    dedupes by normalized URL, and requires a resolvable published date
    within the last 90 days: publishedDate or a date embedded in the URL
    (_parse_published applies both), then — for up to 5 results still
    undated — a bounded page-header probe (step 3, _probe_published_date).
    Anything still undated after all three is dropped. Caps at 15.

    Uses _searxng_query (not _searxng_results) so degraded-backend signals
    survive to the caller. Returns {"candidates": [...], "unresponsive":
    [[engine, reason], ...], "undated_total": n, "news_zero_all": bool}:
    undated_total counts results that passed the host/dedup filter but never
    got a date, even after the page probe; news_zero_all is True iff every
    categories="news" query in this call came back with zero raw results —
    the WP7 pattern (brave/startpage/qwant/mojeek suspended, only bing news
    answering, and bing news carries no publishedDate) lands in one or both
    of these, not silently as found=0/error=None.

    Raises when every SearXNG query in this call failed (a real outage) so
    _client_news_scan can distinguish "SearXNG is down" from "degraded/no
    news found"; a partial failure (some queries ok) is not treated as an
    error.
    """
    from routers.agents import _ascii_name

    name = client["name"]
    ascii_name = _ascii_name(name)
    meta = client.get("metadata") or {}
    industry = (meta.get("industry") or "").strip()
    domain = _client_domain(client)
    simple_name, _acronym = _simplify_company_name(name)

    queries: list[tuple[str, str, str]] = [(f'"{ascii_name}"', "news", "month")]
    if industry:
        queries.append((f'"{ascii_name}" {industry}', "news", "month"))
    if domain:
        queries.append((f"site:{domain}", "general", "month"))

    candidates: list[dict] = []
    undated_pending: list[dict] = []
    seen: set[str] = set()
    unresponsive_all: list = []
    today = datetime.now(timezone.utc).date()
    attempted = 0
    failed = 0
    news_query_count = 0
    news_zero_count = 0

    async def _collect(query: str, categories: str, time_range: str) -> None:
        nonlocal attempted, failed, news_query_count, news_zero_count
        attempted += 1
        try:
            data = await _searxng_query(query, limit=15, categories=categories, time_range=time_range)
        except Exception:
            failed += 1
            return
        results = data.get("results") or []
        unresponsive_all.extend(data.get("unresponsive") or [])
        if categories == "news":
            news_query_count += 1
            if not results:
                news_zero_count += 1
        for r in results:
            url = (r.get("url") or "").strip()
            allowed, _host = _news_result_allowed(url)
            if not allowed:
                continue
            norm = _norm_news_url(url)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            published = _parse_published(r)
            entry = {**r, "_norm_url": norm, "_published": published, "query": query}
            if not published:
                undated_pending.append(entry)
                continue
            try:
                if (today - date.fromisoformat(published)).days > 90:
                    continue
            except ValueError:
                continue
            candidates.append(entry)

    for q, cat, tr in queries:
        await _collect(q, cat, tr)

    if len(candidates) < 3:
        await _collect(f'"{simple_name}" news', "general", "month")

    if attempted and failed == attempted:
        raise ConnectionError("searxng unreachable")

    undated_total = len(undated_pending)
    probe_targets = undated_pending[:5]
    if probe_targets:
        # Concurrent, bounded to 3 in flight: sequential 8s-timeout probes
        # would cost up to 5*8s=40s worst case; a semaphore(3) caps it at
        # ceil(5/3)*8s ~= 16s. candidates.append() from each task is safe —
        # asyncio has no real parallelism, only interleaving.
        probe_sem = asyncio.Semaphore(3)

        async def _probe_one(entry: dict) -> None:
            async with probe_sem:
                published = await _probe_published_date(entry["url"])
            if not published:
                return
            try:
                if (today - date.fromisoformat(published)).days > 90:
                    return
            except ValueError:
                return
            entry["_published"] = published
            candidates.append(entry)

        await asyncio.gather(*(_probe_one(e) for e in probe_targets))

    return {
        "candidates": candidates[:15],
        "unresponsive": _dedupe_unresponsive(unresponsive_all),
        "undated_total": undated_total,
        "news_zero_all": bool(news_query_count) and news_zero_count == news_query_count,
    }


def _news_listing(candidates: list[dict]) -> str:
    return "\n".join(
        f"{i}. {(c.get('title') or '')[:120]} — {(c.get('content') or '')[:200]} "
        f"(url: {c.get('url', '')})"
        for i, c in enumerate(candidates)
    )


def _write_news_signal_content(why: str, published: Optional[str], url: str) -> str:
    return f"{why}\n\nPublished: {published or 'unknown'}\nSource: {url}\n\n## Sources\n- {url}"


async def _client_news_scan(
    org_id: int, client: dict, *, run_id: Optional[int] = None, max_write: int = 8,
) -> dict:
    """Score fresh news candidates for one client with a single text LLM call
    and write the relevant ones as client-linked type='signal' documents.

    Flow: SearXNG candidates (_news_candidates) plus the client's own-
    newsroom tier (_newsroom_candidates, backend-independent of SearXNG)
    minus already-known signal URLs → one llm.acomplete call → keep
    relevance ≥2 → index_document + link_document for each (capped at
    max_write).

    Before scoring, a degraded SearXNG backend is detected and reported
    instead of silently returning found=0/error=None: either every
    news-category query came back with zero raw results while ≥1 engine was
    unresponsive, or SearXNG results came back but 100% stayed undated even
    after the page-header probe (step 3). Either sets result["error"] and
    writes nothing — UNLESS the newsroom tier alone found ≥3 candidates, in
    which case there is real news regardless of the search backend and the
    scan proceeds with result["warning"] set instead of failing the part.
    A total SearXNG outage (every query raised) is retried once after 20s
    before giving up as before; an LLM failure writes nothing. Returns
    {found, scored, written, max_relevance, error}, plus warning/
    unresponsive/newsroom_found when relevant."""
    name = client["name"]
    client_id = client["id"]
    result: dict = {"found": 0, "scored": 0, "written": 0, "max_relevance": 0, "error": None}

    data: Optional[dict] = None
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            data = await _news_candidates(org_id, client)
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(20)
    if data is None:
        console.print(f"[yellow]news scan: SearXNG unreachable for '{name}': {last_exc}[/yellow]")
        result["error"] = "searxng unreachable"
        return result

    candidates = data.get("candidates") or []
    unresponsive = data.get("unresponsive") or []
    undated_total = data.get("undated_total", 0)
    news_zero_all = data.get("news_zero_all", False)

    try:
        newsroom_candidates, newsroom_blocked = await _newsroom_candidates(org_id, client)
    except Exception as exc:
        console.print(f"[yellow]news scan: newsroom tier failed for '{name}': {exc}[/yellow]")
        newsroom_candidates, newsroom_blocked = [], []

    if newsroom_candidates:
        result["newsroom_found"] = len(newsroom_candidates)

    if newsroom_blocked:
        domain = _client_domain(client)
        if domain:
            try:
                import playbook  # type: ignore
                await playbook.record(org_id, domain, {"blocked_urls": newsroom_blocked}, run_id=run_id)
            except Exception as exc:
                console.print(f"[yellow]news scan: playbook blocked_urls record failed for '{name}': {exc}[/yellow]")

    degraded_reason = None
    if not candidates and unresponsive and news_zero_all:
        reasons = ", ".join(f"{e}: {r}" for e, r in unresponsive[:3])
        degraded_reason = f"search degraded: {len(unresponsive)} engines unresponsive ({reasons})"
    elif not candidates and undated_total > 0:
        degraded_reason = "search results undated"

    newsroom_saves_it = len(newsroom_candidates) >= 3

    if degraded_reason and not newsroom_saves_it:
        result["error"] = degraded_reason
        if unresponsive:
            result["unresponsive"] = unresponsive
        return result

    if degraded_reason and newsroom_saves_it:
        # News exists (the newsroom tier alone found enough) — don't fail
        # the part, but still say the search backend was degraded.
        result["warning"] = degraded_reason
    elif news_zero_all and unresponsive:
        # Nit: candidates can be non-empty here (e.g. the site: domain
        # query still worked) even though every news-category query was
        # degraded — that reads as healthy unless flagged explicitly.
        reasons = ", ".join(f"{e}: {r}" for e, r in unresponsive[:3])
        result["warning"] = f"search degraded: {len(unresponsive)} engines unresponsive ({reasons})"

    if unresponsive:
        result["unresponsive"] = unresponsive

    all_candidates = candidates + newsroom_candidates

    existing = await _existing_signal_urls(org_id, client_id)
    fresh = [c for c in all_candidates if c["_norm_url"] not in existing]
    result["found"] = len(fresh)
    if not fresh:
        return result

    prompt = _NEWS_SCORE_PROMPT.format(
        subject=name, n=len(fresh), listing=_news_listing(fresh),
        rules=_rules_block(await _news_lessons_block(org_id)),
    )
    try:
        reply = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
    except Exception as exc:
        console.print(f"[yellow]news scan: LLM scoring failed for '{name}': {exc}[/yellow]")
        result["error"] = f"llm scoring failed: {exc}"
        return result

    scores = _parse_json_list(reply)
    result["scored"] = len(scores)

    now_iso = datetime.now(timezone.utc).isoformat()
    good_queries: set[str] = set()
    written = 0
    max_rel = 0
    for item in scores:
        if written >= max_write:
            break
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
            relevance = int(item.get("relevance"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(fresh)) or relevance < 2:
            continue
        cand = fresh[idx]
        norm = cand["_norm_url"]
        url = cand.get("url", "")
        headline = str(item.get("headline") or cand.get("title") or "")[:90]
        why = str(item.get("why") or "")[:160]
        signal_type = str(item.get("signal_type") or "news").strip().lower()
        if signal_type not in ("opportunity", "risk", "pain_point", "news"):
            signal_type = "news"
        published = cand.get("_published")
        doc_id = f"news-{client_id}-{hashlib.sha1(norm.encode()).hexdigest()[:10]}"
        doc_db_id = await db_module.index_document(
            org_id=org_id,
            doc_id=doc_id,
            doc_type="signal",
            title=headline,
            content=_write_news_signal_content(why, published, url),
            metadata={
                "source_url": url,
                "published_at": published,
                "signal_type": signal_type,
                "relevance_score": relevance,
                "subject": name,
                "from_news_scan": True,
                "query": cand.get("query", ""),
                "service": "python",
            },
            embedding=[],
            source="agent",
            agent_run_id=run_id,
        )
        if doc_db_id and doc_db_id > 0:
            await db_module.link_document(doc_db_id, "client", client_id)
            written += 1
            max_rel = max(max_rel, relevance)
            if cand.get("query"):
                good_queries.add(cand["query"])

    result["written"] = written
    result["max_relevance"] = max_rel

    if written:
        try:
            import playbook  # type: ignore
        except ImportError:
            playbook = None
        if playbook is not None:
            domain = _client_domain(client)
            if domain:
                try:
                    await playbook.record(
                        org_id, domain,
                        {"news": {"good_queries": sorted(good_queries), "last_success_at": now_iso}},
                        run_id=run_id,
                    )
                except Exception as exc:
                    console.print(f"[yellow]news scan: playbook record failed for '{name}': {exc}[/yellow]")

    return result


def _discovery_marker_stale(meta: dict) -> bool:
    """True when source discovery should (re)run for a source-less client.

    Discovery can come up empty for transient reasons (search-engine rate
    limits, SearXNG outage), so the sweep retries every source_rediscover_days
    instead of giving up forever after the first attempt.
    """
    marker = meta.get("sources_discovered_at")
    if not marker:
        return True
    retry_days = int(context.config.get("source_rediscover_days", 7))
    try:
        marker_dt = datetime.fromisoformat(marker)
    except (ValueError, TypeError):
        return True
    return (datetime.now(timezone.utc) - marker_dt) > timedelta(days=retry_days)


async def _monitor_client(org_id: int, client: dict, fire_research: bool = True) -> dict:
    """Check one client's monitored sources. Focus clients with changes get the
    news research fired (+ possible match escalation); non-focus clients get the
    news_pending flag for manual follow-up."""
    meta = client.get("metadata") or {}
    summary: dict = {
        "client": client["name"], "changed": [], "discovered": 0,
        "researched": False, "escalated": False, "flagged": False, "news_written": 0,
    }

    sources = list(meta.get("monitored_sources") or [])
    if not sources and _discovery_marker_stale(meta):
        sources = await _discover_client_sources(org_id, client)
        summary["discovered"] = len(sources)

    # Virtual "news search" source — only counts as a change when a baseline
    # fingerprint already existed (first sweep sets baselines, no storm). A
    # change here runs the (cheap, no-Pi-slot) news scan for every client —
    # focus or not — before the focus/autonomy logic below decides whether to
    # also fire the heavier Pi news-OSINT research.
    had_news_fp = meta.get("news_fp") is not None
    if await _client_news_changed(org_id, client, fail_open=False) and had_news_fp:
        summary["changed"].append("news search")
        # Mirror _run_market_monitor's _fire(): a real agent_run per scan, so
        # a degraded backend shows up as a failed run (agent history, the
        # "no API key"/tracebacks log watch) instead of only living inside
        # this sweep's own in-memory summary, which the caller folds into
        # ONE update_agent_run for the whole multi-client sweep.
        news_run_id = await db_module.create_agent_run(
            org_id=org_id, agent_type="news_scan",
            task=f"Source-monitor news scan: {client['name']}", trigger_type="heartbeat",
        )
        try:
            news_scan = await _client_news_scan(org_id, client, run_id=news_run_id)
            summary["news_written"] = news_scan.get("written", 0)
            if news_scan.get("error"):
                summary["news_error"] = news_scan["error"]
            if news_scan.get("warning"):
                summary["news_warning"] = news_scan["warning"]
            if news_scan.get("unresponsive"):
                summary["news_unresponsive"] = news_scan["unresponsive"]
            status = "failed" if news_scan.get("error") else "done"
            await db_module.update_agent_run(
                news_run_id, status, output=news_scan, error=news_scan.get("error"),
            )
        except Exception as exc:
            console.print(f"[yellow]source monitor: news scan failed for '{client['name']}': {exc}[/yellow]")
            summary["news_error"] = str(exc)
            try:
                await db_module.update_agent_run(news_run_id, "failed", error=str(exc))
            except Exception:
                pass

    now_iso = datetime.now(timezone.utc).isoformat()
    for src in sources:
        fp = await _fetch_source_fp(src.get("url", ""))
        src["last_checked_at"] = now_iso
        if fp is None:
            continue
        if src.get("last_fp") and src["last_fp"] != fp:
            src["last_changed_at"] = now_iso
            summary["changed"].append(src.get("label") or src.get("url"))
        src["last_fp"] = fp

    patch: dict = {"monitored_sources": sources}
    if summary["changed"]:
        # Autonomy seam (Phase 2): "is this change worth acting on?" — legacy
        # answers with the focus star; at level >= 2 the agent decides (non-focus
        # clients included), budgeted + logged. Level 1 logs the decision and
        # keeps legacy behaviour. Fallback on LLM failure = legacy (focus star).
        act_now = bool(meta.get("is_focus")) and fire_research
        auto_level = await autonomy.level(org_id)
        if fire_research and auto_level >= autonomy.LEVEL_OBSERVE:
            decision = await autonomy.decide(org_id, autonomy.DecisionContext(
                seam="monitor", client_name=client["name"],
                signals=[f"source changed: {c}" for c in summary["changed"]],
                facts={"is_focus": bool(meta.get("is_focus")),
                       "last_autonomous_run_at": meta.get("last_autonomous_run_at") or "never",
                       "already_news_pending": bool(meta.get("news_pending")),
                       "_client": client},
                allowed_actions=("skip", "research"),
                fallback_action="research" if meta.get("is_focus") else "skip",
            ))
            summary["decision"] = {"action": decision.action, "reason": decision.reason,
                                   "review_run_id": decision.review_run_id}
            if auto_level >= autonomy.LEVEL_ACT:
                act_now = decision.acts
        if act_now:
            autonomous = auto_level >= autonomy.LEVEL_ACT
            run_id = await _fire_news_research(org_id, client["name"], autonomous=autonomous)
            summary["researched"] = run_id is not None
            if autonomous and run_id is not None:
                await autonomy.mark_client_acted(org_id, client["name"])
            summary["escalated"] = await _maybe_escalate_match(org_id, client["name"], run_id)
            patch["news_pending"] = False
        else:
            patch.update({
                "news_pending": True,
                "news_pending_at": now_iso,
                "news_pending_reason": summary["changed"],
            })
            summary["flagged"] = True
    try:
        await db_module.update_client_metadata(org_id, client["name"], patch)
    except Exception as exc:
        console.print(f"[yellow]source monitor: could not save state for '{client['name']}': {exc}[/yellow]")
    return summary


def _parse_json_list(text: str) -> list:
    """Tolerant parse of an LLM JSON-list reply (handles ```json fences)."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t).rstrip("`").strip()
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(t[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _parse_json_obj(text: str) -> dict:
    """Tolerant parse of an LLM JSON-object reply (handles ```json fences)."""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t).rstrip("`").strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        data = json.loads(t[start:end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _apply_market_signals(org_id: int) -> int:
    """Map fresh, high-relevance scope='market' signals onto the clients they
    affect: shortlist clients by matching industry, ask the LLM which are
    genuinely affected (+ a one-line why), and write a client-linked signal for
    each. Idempotent — each market signal is marked applied and the per-client
    signal has a deterministic doc_id. Returns client signals written."""
    # Lower the bar: any signal Pi bothered to write is "notable", and the LLM
    # confirm below is the real relevance-to-client gate. Keep unscored signals.
    threshold = int(context.config.get("market_apply_min_relevance", 3))
    market_sigs = await db_module.list_signals(
        org_id, scope="market", days=4, limit=40,
    )
    def _rel(s):
        try:
            return int(float((s.get("metadata") or {}).get("relevance_score")))
        except (TypeError, ValueError):
            return None
    market_sigs = [
        s for s in market_sigs
        if not (s.get("metadata") or {}).get("applied")
        and (_rel(s) is None or _rel(s) >= threshold)
    ]
    if not market_sigs:
        return 0

    clients = await db_module.list_clients(org_id)
    def _ind_tokens(s: str) -> set:
        # Significant words of an industry label, minus generic filler — so
        # "Cosmetics & Personal Care" matches "Consumer Goods / Personal Care".
        words = re.findall(r"[a-z0-9]+", (s or "").lower())
        return {w for w in words if len(w) >= 4 and w not in _INDUSTRY_STOPWORDS}

    written = 0

    for sig in market_sigs[:5]:
        meta = sig.get("metadata") or {}
        sig_tokens = _ind_tokens(meta.get("industry") or "")
        # Shortlist: clients whose industry shares a significant word with the signal.
        shortlist = [
            c for c in clients
            if sig_tokens & _ind_tokens((c.get("metadata") or {}).get("industry") or "")
        ][:12]
        if not shortlist:
            await db_module.update_document(org_id, sig["doc_id"], {"metadata": {"applied": True}})
            continue

        prompt = (
            "A market/industry development:\n"
            f"TITLE: {sig.get('title','')}\nINDUSTRY: {meta.get('industry','')}\n"
            f"DETAIL: {(sig.get('content') or '')[:800]}\n\n"
            "Which of these clients is this development materially relevant to? Return STRICT JSON: "
            'a list of {"client": "<exact name>", "reason": "<one sentence why it matters to them>"}. '
            "Only include clients it genuinely affects; return [] if none.\n\nCLIENTS:\n"
            + "\n".join(f"- {c['name']} (industry: {(c.get('metadata') or {}).get('industry','?')})"
                        for c in shortlist)
        )
        try:
            reply = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
            matches = _parse_json_list(reply)
        except Exception as exc:
            console.print(f"[yellow]market apply LLM failed: {exc}[/yellow]")
            matches = []

        by_name = {c["name"].strip().lower(): c for c in shortlist}
        for m in matches:
            if not isinstance(m, dict):
                continue
            target = by_name.get(str(m.get("client", "")).strip().lower())
            reason = str(m.get("reason", "")).strip()
            if not target or not reason:
                continue
            doc_db_id = await db_module.index_document(
                org_id=org_id,
                doc_id=f"market-applied-{sig['doc_id']}-{target['id']}",
                doc_type="signal",
                title=f"Market: {sig.get('title','')}",
                content=reason,
                metadata={
                    "signal_type": "opportunity",
                    "relevance_score": meta.get("relevance_score"),
                    "source_url": meta.get("source_url", ""),
                    "subject": target["name"],
                    "from_market": True,
                    "industry": meta.get("industry", ""),
                },
                embedding=[],
                source="agent",
            )
            if doc_db_id and doc_db_id > 0:
                await db_module.link_document(doc_db_id, "client", target["id"])
                written += 1
        await db_module.update_document(org_id, sig["doc_id"], {"metadata": {"applied": True}})

    return written


async def _market_news_scan(
    org_id: int, industry: str, focus: str, *, run_id: Optional[int] = None, max_write: int = 8,
) -> dict:
    """Score fresh market/industry news with a single text LLM call and write
    the relevant ones as unlinked type='signal' documents (scope='market',
    NOT client-linked — _apply_market_signals maps them onto clients
    afterwards). Dedupes against scope='market' signals already written in
    the last 30 days. `industry` drives the search terms when set; for a
    source-change-triggered scan (no specific industry) `focus`'s free-text
    description is used instead. Writes are capped at max_write, like
    _client_news_scan. Gets the same degraded-backend detection as the
    client scan (WP9): if every one of these (categories="news") queries
    comes back with zero raw results while ≥1 engine was unresponsive,
    result["error"] is set and nothing is written instead of a silent
    found=0/error=None. Returns
    {found, scored, written, max_relevance, error}, plus unresponsive when
    relevant."""
    result: dict = {"found": 0, "scored": 0, "written": 0, "max_relevance": 0, "error": None}
    term = (industry or focus or "market").strip()
    queries = [
        (f"{term} regulation OR compliance OR Regulierung", "news", "week"),
        (f"{term} market news Branche", "news", "week"),
    ]

    candidates: list[dict] = []
    seen: set[str] = set()
    unresponsive_all: list = []
    raw_result_count = 0
    for q, cat, tr in queries:
        try:
            data = await _searxng_query(q, limit=15, categories=cat, time_range=tr)
        except Exception as exc:
            console.print(f"[yellow]market news scan: SearXNG failed for '{term}': {exc}[/yellow]")
            continue
        results = data.get("results") or []
        unresponsive_all.extend(data.get("unresponsive") or [])
        raw_result_count += len(results)
        for r in results:
            url = (r.get("url") or "").strip()
            allowed, _host = _news_result_allowed(url)
            if not allowed:
                continue
            norm = _norm_news_url(url)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            candidates.append({**r, "_norm_url": norm, "_published": _parse_published(r), "query": q})

    unresponsive = _dedupe_unresponsive(unresponsive_all)
    if raw_result_count == 0 and unresponsive:
        reasons = ", ".join(f"{e}: {r}" for e, r in unresponsive[:3])
        result["error"] = f"search degraded: {len(unresponsive)} engines unresponsive ({reasons})"
        result["unresponsive"] = unresponsive
        return result
    if unresponsive:
        result["unresponsive"] = unresponsive

    if not candidates:
        return result

    try:
        existing_rows = await db_module.list_signals(org_id, scope="market", days=30, limit=200)
    except Exception:
        existing_rows = []
    existing: set[str] = set()
    for row in existing_rows:
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        url = meta.get("source_url")
        if url:
            existing.add(_norm_news_url(url))

    fresh = [c for c in candidates if c["_norm_url"] not in existing]
    result["found"] = len(fresh)
    if not fresh:
        return result

    prompt = _NEWS_SCORE_PROMPT.format(
        subject=f"the {term} industry", n=len(fresh), listing=_news_listing(fresh),
        rules=_rules_block(await _news_lessons_block(org_id)),
    )
    try:
        reply = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
    except Exception as exc:
        console.print(f"[yellow]market news scan: LLM scoring failed for '{term}': {exc}[/yellow]")
        result["error"] = f"llm scoring failed: {exc}"
        return result

    scores = _parse_json_list(reply)
    result["scored"] = len(scores)

    written = 0
    max_rel = 0
    for item in scores:
        if written >= max_write:
            break
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
            relevance = int(item.get("relevance"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(fresh)) or relevance < 2:
            continue
        cand = fresh[idx]
        norm = cand["_norm_url"]
        url = cand.get("url", "")
        headline = str(item.get("headline") or cand.get("title") or "")[:90]
        why = str(item.get("why") or "")[:160]
        signal_type = str(item.get("signal_type") or "news").strip().lower()
        if signal_type not in ("opportunity", "risk", "pain_point", "news"):
            signal_type = "news"
        published = cand.get("_published")
        doc_id = f"market-news-{hashlib.sha1(norm.encode()).hexdigest()[:10]}"
        doc_db_id = await db_module.index_document(
            org_id=org_id,
            doc_id=doc_id,
            doc_type="signal",
            title=headline,
            content=_write_news_signal_content(why, published, url),
            metadata={
                "scope": "market",
                "industry": industry,
                "relevance_score": relevance,
                "source_url": url,
                "published_at": published,
                "signal_type": signal_type,
                "from_news_scan": True,
                "service": "python",
            },
            embedding=[],
            source="agent",
            agent_run_id=run_id,
        )
        if doc_db_id and doc_db_id > 0:
            written += 1
            max_rel = max(max_rel, relevance)

    result["written"] = written
    result["max_relevance"] = max_rel
    return result


async def _run_market_monitor(org_id: int) -> dict:
    """Market/industry news monitor — the org-level analogue of source_monitor.

    Fingerprints curated economics/news pages; when one changes, runs a
    Python/textonly market news scan on the general news. Also rotates
    through the distinct industries of the org's clients, scanning a few per
    run. _market_news_scan writes scope='market' signals tagged by industry;
    the apply-to-clients mapping (industry shortlist + LLM confirm) runs
    afterwards via _apply_market_signals.
    """
    cfg = await db_module.get_market_config(org_id)
    sources = list(cfg.get("sources") or [])
    if not sources and not cfg.get("seeded"):
        sources = [dict(s) for s in _DEFAULT_MARKET_SOURCES]
        cfg["seeded"] = True

    now_iso = datetime.now(timezone.utc).isoformat()
    changed: list[dict] = []
    for src in sources:
        fp = await _fetch_source_fp(src.get("url", ""))
        src["last_checked_at"] = now_iso
        if fp is None:
            continue
        if src.get("last_fp") and src["last_fp"] != fp:
            src["last_changed_at"] = now_iso
            changed.append(src)
        src["last_fp"] = fp

    # Distinct client industries, least-recently-scanned first (rotation so a big
    # industry list doesn't burn the token budget on every run).
    clients = await db_module.list_clients(org_id)
    industries = sorted({
        ((c.get("metadata") or {}).get("industry") or "").strip() for c in clients
    } - {""})
    scans = dict(cfg.get("industry_scans") or {})
    max_ind = int(context.config.get("market_max_industries_per_run", 2))
    picked = sorted(industries, key=lambda i: scans.get(i, ""))[:max_ind]

    fired: list[dict] = []

    async def _fire(focus: str, industry: str) -> None:
        child = await db_module.create_agent_run(
            org_id=org_id, agent_type="market_news",
            task=f"Market news scan: {focus}", trigger_type="heartbeat",
        )
        try:
            scan = await _market_news_scan(org_id, industry, focus, run_id=child)
            status = "failed" if scan.get("error") else "done"
            await db_module.update_agent_run(child, status, output=scan, error=scan.get("error"))
            fired.append({"focus": focus, "industry": industry, "run_id": child, **scan})
        except Exception as exc:
            await db_module.update_agent_run(child, "failed", error=str(exc))
            fired.append({"focus": focus, "industry": industry, "run_id": child, "error": str(exc)})

    for src in changed:
        label = src.get("label") or src.get("url")
        await _fire(f"general business and economics news (triggered by an update on {label})", "")
    for industry in picked:
        await _fire(f"the {industry} sector", industry)
        scans[industry] = now_iso

    cfg["sources"] = sources
    cfg["industry_scans"] = scans
    await db_module.save_market_config(org_id, cfg)

    # Map fresh high-relevance market signals onto the clients they affect.
    applied = await _apply_market_signals(org_id)

    return {
        "sources_checked": len(sources),
        "sources_changed": len(changed),
        "industries_scanned": len(picked),
        "runs_fired": len(fired),
        "clients_tagged": applied,
    }


# ---------------------------------------------------------------------------
# Open-positions (jobs) monitoring → inferred needs → match analysis
# ---------------------------------------------------------------------------

_CAREERS_KEYS = ("career", "careers", "jobs", "job", "stellen", "stellenangebote", "karriere",
                 "vacanc", "join-us", "join", "positions", "joboffers", "jobangebote")

_JOBS_EXTRACT_PROMPT = (
    "Below is the text of {client}'s careers/jobs page. Extract the OPEN POSITIONS and infer what "
    "initiatives or needs the hiring suggests — for B2B technology/consulting sales intelligence.\n\n"
    "PRIORITISE (list these first) IT / digital / engineering / data / product / cybersecurity / "
    "cloud roles AND management / leadership / strategy / transformation roles (CIO, CTO, Head of IT, "
    "IT Project/Program Manager, Software/Cloud/Data/Security Engineer, Digitalisation Lead, Change "
    "Manager, department heads, directors). You MAY also include other professional/office roles "
    "(finance, HR, procurement, consulting, project management) when they hint at a digital, "
    "growth or transformation initiative.\n"
    "EXCLUDE blue-collar / operational roles that carry no IT/strategy signal: facility management, "
    "cleaning, security guards, warehouse/logistics floor, drivers, production-line/factory workers, "
    "retail shop-floor, catering, gardening, trades/craftsmen, nursing/care staff.\n"
    "CRITICAL: only return ACTUAL individual job postings with a specific role title (e.g. 'Senior "
    "Cloud Engineer (m/f/d)', 'Head of IT'). Do NOT return department / category / business-area "
    "names (e.g. 'IT & Digitalisation', 'Facility Management', 'Finance, Legal & Administration', "
    "'Strategy & Consulting') — those are navigation categories, not positions; skip them.\n\n"
    "{rules}"
    "Return STRICT JSON ONLY:\n"
    '{{"positions": [{{"title": "...", "location": "...", "team": "...", "summary": "..."}}], '
    '"inferred_needs": ["short need statement"]}}\n'
    "Clean up each title — proper capitalisation and spacing, keep the (w/m/d) marker (e.g. "
    "'projektleiter sap transformation' -> 'Projektleiter SAP Transformation'); if a title looks "
    "truncated, keep what's there but tidy it.\n"
    "summary = a short ~6-12 word plain-English description of what the role does / what it implies "
    "the company is working on (e.g. 'Leads SAP S/4HANA migration projects', 'Builds and runs the "
    "cloud security operations'). Infer it from the title if no description is given.\n"
    "Rules: at most 20 positions, IT/management ones first; use \"\" for unknown location/team; "
    "inferred_needs = 2-6 concise statements leaning toward IT/digital/management initiatives the "
    "company is likely investing in or struggling with (e.g. 'Scaling cloud/Kubernetes "
    "infrastructure', 'Building a data/ML team', 'SAP S/4HANA migration', 'Expanding cybersecurity "
    "& compliance', 'Driving a digital-transformation program'). If the page shows no concrete "
    'individual job listings, return {{"positions": [], "inferred_needs": []}}.\n\nPAGE TEXT:\n{page}'
)


async def _jobs_lessons_block(org_id: int) -> str:
    """Approved cross-site lessons (scope='jobs', WP4) formatted for inline
    injection into a jobs prompt — appended verbatim after the prompt is
    otherwise fully formatted, never as a new format() placeholder. '' when
    playbook isn't available, org_id is falsy, or there are no approved
    jobs/all-scope lessons; callers append/format this and never rely on it
    being non-empty."""
    if not org_id:
        return ""
    try:
        import playbook  # type: ignore
    except ImportError:
        return ""
    try:
        lessons = await playbook.lessons_load(org_id)
        return playbook.lessons_block(lessons, "jobs")
    except Exception:
        return ""


# Job-LISTING link terms only (the landing page is already "career/karriere" — we
# want the link through to the actual openings). Deliberately excludes bare
# "position" (matches "politische-positionen"), "career"/"karriere" and "search".
_JOB_LINK_KEYS = ("/jobs", "jobs/", "=jobs", "stellenangebote", "stellenanzeigen",
                  "stellensuche", "stellenmarkt", "offene-stellen", "open-positions",
                  "vacanc", "joblist", "job-search", "joboffers", "jobangebote",
                  "all-jobs", "/stellen", "/job/", "joblisting")

# Applicant-tracking-system hosts — a link to one is almost always the real listing.
# Deliberately excludes a bare "jobs." entry: see _ats_match's docstring.
_ATS_HOSTS = ("personio.", "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.",
              "successfactors.", "smartrecruiters.", "softgarden.", "join.com", "recruitee.",
              "jobvite.", "icims.com", "taleo.net", "concludis.", "prescreen.", "d-vinci.",
              "rexx-systems.", "guidecom.", "umantis.")

# D1 — own-domain path-probe tier: well-known careers paths (German first,
# since most clients are DE) tried directly against the client's own site
# before ever falling back to a sitemap crawl or SearXNG. karriere./jobs./
# careers.<domain> subdomain probes are appended after the paths; the whole
# list is capped to _PATH_PROBE_MAX below, so with 13 paths already at the
# cap the subdomains only get a look-in once the path list is trimmed down.
_CAREERS_PATHS = ("/karriere", "/karriere/", "/de/karriere", "/de-de/karriere", "/de_DE/karriere/",
                   "/careers", "/career", "/en/careers", "/jobs", "/stellenangebote",
                   "/unternehmen/karriere", "/company/careers", "/about/careers")
_PATH_PROBE_MAX = 10
_PATH_PROBE_CONCURRENCY = 4
_PATH_PROBE_STOP_AFTER_HITS = 2
_PATH_PROBE_BROWSER_RETRY_MAX = 2
_PATH_PROBE_BLOCK_DAYS = 14


def _ats_match(host: str) -> bool:
    """True when `host` IS one of _ATS_HOSTS (label-anchored) or a subdomain of
    one — never merely a substring.

    _ATS_HOSTS has two shapes: a full domain ("greenhouse.io", "lever.co",
    "myworkdayjobs.com", "join.com", "icims.com", "taleo.net") — anchored the
    obvious way, host == a or host.endswith("." + a); and a bare label with a
    trailing dot ("personio.", "workday.", "smartrecruiters.", ...) for
    vendors that operate under several TLDs (personio.de, personio.com, ...),
    where stripping the dot and applying the same suffix check would require
    the label to be the WHOLE remaining host (never true once a TLD follows)
    and silently stop matching every one of these 13 entries. Anchor those on
    the label instead: it must be the second-from-last DNS label — i.e. the
    one immediately before the TLD — so "xyz.personio.de" matches (personio is
    second-to-last) but "personio.evil.com" does not (evil is second-to-last,
    not the vendor label).

    There is deliberately no "jobs." entry: "jobs" is a generic subdomain
    word, not a vendor name, so anchoring it the same way would accept
    jobs.<anything>.<tld> wholesale — a client's own jobs.<domain> (already
    covered by _own_or_ats's own-domain arm, e.g. jobs.apleona.com is
    own-domain for apleona.com) as well as an attacker's jobs.evil.com or an
    unrelated jobs.de/jobs.com."""
    labels = host.split(".") if host else []
    for a in _ATS_HOSTS:
        if a.endswith("."):
            label = a.rstrip(".")
            if len(labels) >= 2 and labels[-2] == label:
                return True
        elif host == a or host.endswith("." + a):
            return True
    return False


async def _fetch_page_raw(url: str, wait_ms: int = 1500, max_chars: int = 18000) -> tuple[str, str]:
    """Return (visible_text, raw_html). raw_html is the plain GET body, or —
    when that's empty/thin and the fallback ends up using Camofox — Camofox's
    link entries rebuilt as <a href> tags (see _camofox_links_html), so a
    JS-only page's links are still harvestable. '' when neither is available.
    Calls _fetch_rendered_tier (shared with _fetch_page_text) directly rather
    than going through _fetch_page_text, so a Camofox tab is opened at most
    once per call instead of twice (once for text, once for links)."""
    html = ""
    try:
        async with httpx.AsyncClient(
            timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
        ) as http:
            resp = await http.get(url)
            if resp.status_code == 200:
                html = resp.text
    except Exception:
        pass
    text = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html)).strip()
    tier = "http" if text else "none"
    if len(text) < 500:
        # Trust _fetch_rendered_tier's own choice of winner outright — it
        # already discards a thin plain-GET stub for comparison purposes
        # (see its docstring) and only falls back to it if nothing better
        # came back, so re-comparing raw lengths here would just reopen the
        # same bug (a whitespace-inflated stub outscoring a shorter, real
        # rendered result).
        rendered, tier, links_html = await _fetch_rendered_tier(url, text, max_chars, wait_ms)
        text = re.sub(r"\s+", " ", rendered).strip()
        if links_html:
            # Camofox rendered the page: its links are the only ones a JS shell
            # (`<div id="root"></div>` + scripts) ever exposes, so append them
            # to whatever the plain GET returned instead of only replacing an
            # empty body — _harvest_links then sees both.
            html = f"{html}\n{links_html}" if html.strip() else links_html
    _record_fetch_tier(url, tier if text else "none", len(text))
    return text, html


def _harvest_links(html: str, base_url: str, keys: tuple, own_domain: str = "") -> list[str]:
    """Rank links on a page by relevance to `keys` (substring match against the
    full URL) plus ATS-host / own-domain bonuses. Generalises the old
    _career_listing_links so the same code harvests a careers-page link off a
    homepage (keys=_CAREERS_KEYS) or a job-listing link off a careers landing
    page (keys=_JOB_LINK_KEYS) today, and a newsroom link (WP3) later."""
    from urllib.parse import urljoin
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
    own_domain = (own_domain or base_host).lower().replace("www.", "")
    ranked: list[tuple[int, str]] = []
    seen: set = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        href = m.group(1).strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        full = urljoin(base_url, href)
        if not full.startswith("http") or full.rstrip("/").lower() == base_url.rstrip("/").lower():
            continue
        low = full.lower()
        host = urlparse(full).netloc.lower().replace("www.", "")
        is_ats = _ats_match(host) and host != base_host
        is_match = any(k in low for k in keys)
        if full in seen or not (is_ats or is_match):
            continue
        seen.add(full)
        score = (3 if is_ats else 0) + (1 if own_domain and own_domain in host else 0) \
                  + (1 if any(t in low for t in ("stellenangebote", "all-jobs", "open-positions", "joblist", "stellensuche")) else 0)
        ranked.append((score, full))
    ranked.sort(reverse=True)
    return [u for _, u in ranked[:3]]


def _blocked_recently(url: str, blocked_urls: list, days: int = _PATH_PROBE_BLOCK_DAYS) -> bool:
    """True when `url` appears in a playbook's blocked_urls with an `at`
    timestamp within the last `days` days (D7) — used to skip re-probing a
    path we already know 403s/fails, without ever touching the metadata/
    playbook careers URL itself (callers only ever pass path-probe URLs
    here, never that one)."""
    now = datetime.now(timezone.utc)
    for b in blocked_urls or []:
        if not isinstance(b, dict) or b.get("url") != url:
            continue
        at = b.get("at")
        if not at:
            return True
        try:
            return (now - datetime.fromisoformat(at)) <= timedelta(days=days)
        except (ValueError, TypeError):
            return True
    return False


def _careers_probe_urls(website: str, domain: str, blocked_urls: Optional[list] = None) -> list[str]:
    """Own-domain path probes (_CAREERS_PATHS) with the karriere/jobs/careers
    subdomain probes interleaved at positions 2-4 (WP8 review nit: 13 paths
    alone already exceed _PATH_PROBE_MAX=10, which left the subdomains dead —
    always trimmed off the end before a single one was ever tried), minus
    anything blocked in the last _PATH_PROBE_BLOCK_DAYS days (D7), capped at
    _PATH_PROBE_MAX total."""
    # D11 — normalize defensively here too (not just at _careers_candidates'
    # call site): website may be a bare domain with no scheme, and
    # urlparse() on that yields an empty netloc, silently returning [].
    website = _site_base(website)
    p = urlparse(website)
    if not p.netloc:
        return []
    base = f"{p.scheme}://{p.netloc}"
    path_urls = [base + path for path in _CAREERS_PATHS]
    subdomain_urls = (
        [f"https://karriere.{domain}/", f"https://jobs.{domain}/", f"https://careers.{domain}/"]
        if domain else []
    )
    urls = path_urls[:1] + subdomain_urls + path_urls[1:]
    urls = [u for u in urls if not _blocked_recently(u, blocked_urls)]
    return urls[:_PATH_PROBE_MAX]


def _page_title_h1(html: str) -> tuple[str, str]:
    """Cleaned (whitespace-normalized, not lowercased) <title> and <h1> text,
    '' for either that's absent."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.I | re.S)
    title = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", title_m.group(1))).strip() if title_m else ""
    h1 = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", h1_m.group(1))).strip() if h1_m else ""
    return title, h1


_CONSENT_BANNER_RE = re.compile(r"akzeptieren|accept all|alle akzeptieren|cookie", re.IGNORECASE)
_BROWSER_FALLBACK_HEADING_CHARS = 800
_CONSENT_BANNER_SEARCH_CHARS = 600


def _strip_leading_consent_banner(text: str) -> str:
    """WP8 re-check nit 2: a cookie/consent banner rendered before the real
    page content pushes the actual heading (and its careers keyword) out of
    the browser-fallback keyword window. If akzeptieren|accept all|alle
    akzeptieren|cookie appears within the first _CONSENT_BANNER_SEARCH_CHARS
    chars, drop everything up to and including that match; otherwise return
    `text` unchanged. Deliberately simple (first match only, no attempt to
    detect the banner's actual end) — good enough for the common case of a
    single leading banner naming its own dismiss action."""
    window = text[:_CONSENT_BANNER_SEARCH_CHARS]
    m = _CONSENT_BANNER_RE.search(window)
    return text[m.end():] if m else text


def _probe_hit_keyword(html: str) -> bool:
    """A careers keyword in the page's <title>/<h1> — NEVER the URL. Every
    probed path already contains a careers word by construction (/karriere,
    /careers, ...), so folding the URL into the haystack made this check
    trivially true for every single probe (WP8 review BLOCKER 1: an
    <title>Impressum</title> page served at /karriere was being accepted)."""
    title, h1 = _page_title_h1(html)
    return any(k in f"{title} {h1}".lower() for k in _CAREERS_KEYS)


def _probe_title(html: str) -> str:
    return _page_title_h1(html)[0]


def _is_spa_shell(html: str, home_title_h1: Optional[tuple]) -> bool:
    """True when this probe's <title>/<h1> are IDENTICAL to the homepage's
    (WP8 review BLOCKER 1) — a single-page-app that 200s the same shell at
    every path proves nothing about this specific path, no matter what its
    title/h1 says. '' when there's no homepage baseline to compare against
    (home_title_h1 is None, or the homepage itself had neither tag)."""
    if not home_title_h1 or not any(home_title_h1):
        return False
    return _page_title_h1(html) == home_title_h1


async def _probe_careers_path(url: str, *, own_domain: str, hits: list, http: httpx.AsyncClient,
                               home_title_h1: Optional[tuple] = None) -> dict:
    """One GET at a candidate careers path/subdomain (D1), via the shared
    `http` client for this whole discovery pass (WP8 review nit: one
    AsyncClient reused across probes instead of one per probe). Returns
    {"url", "status", "accepted", "blocked", "title_h1"} — `accepted` is a
    candidate dict ({"url", "tier", "title"}) or None; `blocked` is a
    {"url","kind","at"} dict when the own-domain probe came back
    403/4xx/errored (D7 bookkeeping), else None; `title_h1` is the accepted
    page's (title, h1) pair (or None) — used by _probe_careers_paths for
    in-pass SPA-shell detection across every probe in this call, since a
    single probe has no way to compare itself to its siblings.

    `hits` is a list shared across every concurrent probe in this discovery
    pass, checked only BEFORE the request starts (so a probe already at the
    cap never fires one) — an already-in-flight request's result is always
    kept (never discarded after the fact just because a sibling landed
    first): _probe_careers_paths' shell-detection needs to see every
    completed probe's (title, h1), not just the first _PATH_PROBE_
    STOP_AFTER_HITS of them, or a genuinely distinct real page dispatched in
    the same concurrent wave as two shell-page hits would be thrown away
    before shell-detection ever got a chance to disqualify those two."""
    if len(hits) >= _PATH_PROBE_STOP_AFTER_HITS:
        return {"url": url, "status": None, "accepted": None, "blocked": None, "title_h1": None}
    status = None
    html = ""
    final_url = url
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        resp = await http.get(url)
        status = resp.status_code
        final_url = str(resp.url)
        if status == 200:
            html = resp.text
    except Exception:
        # D7 — a fetch that raised (timeout, connection refused, ...) is as
        # much an own-domain failure as an explicit 403/4xx.
        return {"url": url, "status": None, "accepted": None,
                "blocked": {"url": url, "kind": "fetch_error", "at": now_iso}, "title_h1": None}

    final_host = urlparse(final_url).netloc.lower().replace("www.", "")
    if final_url != url and _ats_match(final_host):
        # A redirect to a known ATS host is a real careers link on its own —
        # no content/keyword check needed (the ATS page itself is the proof).
        accepted = {"url": final_url, "tier": "path-probe", "title": ""}
        hits.append(accepted)
        console.print(f"[dim]jobs discovery: path-probe {url} redirected to ATS host {final_host} "
                       f"(tier=path-probe)[/dim]")
        return {"url": url, "status": status, "accepted": accepted, "blocked": None, "title_h1": None}

    blocked = None
    if status == 403:
        blocked = {"url": url, "kind": "403", "at": now_iso}
    elif status is not None and status >= 400:
        blocked = {"url": url, "kind": "4xx", "at": now_iso}

    accepted = None
    title_h1 = None
    if status == 200:
        text = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html)).strip()
        if len(text) >= 500 and not _is_spa_shell(html, home_title_h1):
            job_links = _harvest_links(html, final_url, _JOB_LINK_KEYS, own_domain)
            if _probe_hit_keyword(html) or len(job_links) >= 3:
                accepted = {"url": final_url, "tier": "path-probe", "title": _probe_title(html)}
                title_h1 = _page_title_h1(html)
    if accepted:
        hits.append(accepted)
        console.print(f"[dim]jobs discovery: path-probe found a careers page at {final_url} "
                       f"(tier=path-probe)[/dim]")
    return {"url": url, "status": status, "accepted": accepted, "blocked": blocked, "title_h1": title_h1}


_PATH_PROBE_BLOCKED_CAP = 5


async def _probe_careers_paths(website: str, domain: str, blocked_urls: Optional[list] = None,
                                home_title_h1: Optional[tuple] = None,
                                prefer_camofox: bool = False) -> tuple[list, list]:
    """Run the D1 own-domain path-probe tier: cheap GETs at well-known
    careers paths/subdomains, concurrently (semaphore-bounded, capped at
    _PATH_PROBE_MAX total, one shared httpx.AsyncClient), stopping early
    once _PATH_PROBE_STOP_AFTER_HITS candidates are accepted. Falls back to
    _fetch_rendered_tier's shared browser-service -> Camofox stage (WP11)
    for the best _PATH_PROBE_BROWSER_RETRY_MAX probes that came back
    403/503 on the plain GET — those codes usually mean bot-protection, not
    "nothing here". `prefer_camofox` (the playbook's needs_js/cookie_wall
    flag) skips straight to browser-service -> Camofox for the fallback,
    same as _fetch_page_text's caller contract. Returns (accepted_
    candidates, blocked_entries) — the latter for D7's playbook.blocked_urls
    bookkeeping, capped at _PATH_PROBE_BLOCKED_CAP per scan (WP8 review nit:
    a single 403-walled scan could otherwise push 10 entries into the
    org-wide 20-cap blocked_urls list, evicting Pi/news-run history)."""
    urls = _careers_probe_urls(website, domain, blocked_urls)
    if not urls:
        return [], []
    sem = asyncio.Semaphore(_PATH_PROBE_CONCURRENCY)
    hits: list = []

    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
    ) as http:
        async def _bounded(u: str) -> dict:
            async with sem:
                return await _probe_careers_path(u, own_domain=domain, hits=hits, http=http,
                                                  home_title_h1=home_title_h1)

        results = await asyncio.gather(*[_bounded(u) for u in urls])

    accepted_results = [r for r in results if r["accepted"]]
    blocked = [r["blocked"] for r in results if r["blocked"]][:_PATH_PROBE_BLOCKED_CAP]

    # WP8 re-check nit 1 — in-pass SPA-shell detection for when there's no
    # homepage baseline to compare against (home_title_h1 is None/empty,
    # e.g. Trumpf/DATEV's homepage 403/503s): _is_spa_shell inside
    # _probe_careers_path is then a no-op, so an SPA serving an IDENTICAL
    # shell at every path would otherwise sail through as up to
    # _PATH_PROBE_STOP_AFTER_HITS "distinct" hits. Any (title, h1) pair
    # shared by >=2 accepted hits IS the shell — drop every hit carrying it;
    # a hit with a distinct pair (or no title/h1 at all, e.g. an ATS
    # redirect) survives untouched.
    pair_counts: dict = {}
    for r in accepted_results:
        pair = r.get("title_h1")
        if pair and any(pair):
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
    shell_pairs = {p for p, n in pair_counts.items() if n >= 2}
    if shell_pairs:
        accepted_results = [r for r in accepted_results if r.get("title_h1") not in shell_pairs]

    accepted = [r["accepted"] for r in accepted_results][:_PATH_PROBE_STOP_AFTER_HITS]

    if len(accepted) < _PATH_PROBE_STOP_AFTER_HITS:
        retryable = [r for r in results if r["status"] in (403, 503) and not r["accepted"]]
        for r in retryable[:_PATH_PROBE_BROWSER_RETRY_MAX]:
            if len(hits) >= _PATH_PROBE_STOP_AFTER_HITS:
                break
            # WP11 rebase — go through the shared browser-service -> Camofox
            # fallback stage directly (the prefer_camofox caller
            # _fetch_rendered_tier's own docstring anticipated), instead of
            # a private browser-service-only call: a 403/503 probe now also
            # gets a Camofox attempt, and prefer_camofox (this site's known
            # needs_js/cookie_wall) skips straight to browser-service ->
            # Camofox rather than re-trying a plain GET that already failed.
            text, _tier, links_html = await _fetch_rendered_tier(
                r["url"], "", 18000, 3000, prefer_camofox=prefer_camofox,
            )
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 500:
                continue
            # Neither tier _fetch_rendered_tier can land on ever returns real
            # <title>/<h1> markup — Camofox's links_html is link entries only
            # (_camofox_links_html) — so the heading region of the rendered
            # TEXT keeps standing in for title/h1 (BLOCKER 1/2's rule: never
            # the URL), and links_html, when Camofox produced it, replaces
            # the plain word-count heuristic for the >=3-job-links check.
            job_links = _harvest_links(links_html, r["url"], _JOB_LINK_KEYS, domain) if links_html else []
            # WP8 re-check nit 2: strip a leading cookie/consent banner
            # before windowing — a banner rendered ahead of the real content
            # otherwise pushes the actual heading (and its careers keyword)
            # past a narrow window. Widened 300 -> 800 chars on top of that.
            low = _strip_leading_consent_banner(text.lower())
            heading = low[:_BROWSER_FALLBACK_HEADING_CHARS]
            job_word_hits = sum(1 for k in _JOB_LINK_KEYS if k in low)
            # An "Impressum und rechtliche Hinweise..." page has none of
            # these (WP8 review BLOCKER 2, measured: all probes 403, browser
            # fallback returned an Impressum page, accepted).
            hit = any(k in heading for k in _CAREERS_KEYS) or len(job_links) >= 3 or job_word_hits >= 3
            if hit and len(hits) < _PATH_PROBE_STOP_AFTER_HITS:
                cand = {"url": r["url"], "tier": "path-probe", "title": ""}
                hits.append(cand)
                accepted.append(cand)
                console.print(f"[dim]jobs discovery: path-probe found a careers page at {r['url']} "
                               f"via browser fallback (tier=path-probe)[/dim]")
    return accepted, blocked


def _own_or_ats(url: str, domain: str) -> bool:
    """True when `url` is on the client's own domain (or a subdomain of it)
    or a known ATS host. Module-level (was a _discover_careers_url closure)
    so _careers_candidates can also use it to decide whether the path-probe
    tier is still worth running (WP8 review nit 1)."""
    host = urlparse(url).netloc.lower().replace("www.", "")
    return bool(domain and (host == domain or host.endswith("." + domain))) or _ats_match(host)


def _playbook_careers_fresh(careers_pb: dict) -> bool:
    """True when careers_pb['last_success_at'] is within the last 60 days —
    shared by _careers_candidates' playbook short-circuit and
    _scan_client_jobs' tier precedence (D5)."""
    last_success = (careers_pb or {}).get("last_success_at")
    if not last_success:
        return False
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last_success)) <= timedelta(days=60)
    except (ValueError, TypeError):
        return False


async def _careers_candidates(org_id: int, client: dict, pb: Optional[dict] = None) -> list[dict]:
    """Ordered candidate careers/jobs URLs, cheapest and most reliable first:
    a fresh (<=60 days) playbook careers URL short-circuits everything else
    (no HTTP, no SearXNG); then the client's own recorded careers_url; then a
    homepage link harvest; then a sitemap probe of the site root; then the
    legacy SearXNG queries as the last resort. Each candidate carries the tier
    it came from so the caller never has to re-derive it."""
    name = client["name"]
    meta = client.get("metadata") or {}
    candidates: list[dict] = []
    seen: set = set()

    def _add(url: str, tier: str, title: str = "") -> None:
        url = (url or "").strip()
        if not url.startswith("http") or url in seen:
            return
        seen.add(url)
        candidates.append({"url": url, "tier": tier, "title": title[:120]})

    careers_pb = (pb or {}).get("careers") or {}
    pb_blocked = (pb or {}).get("blocked_urls") or []
    pb_url = (careers_pb.get("url") or "").strip()
    if pb_url:
        fresh = _playbook_careers_fresh(careers_pb)
        if fresh:
            return [{"url": pb_url, "tier": "playbook", "title": ""}]
        _add(pb_url, "playbook")

    # D2 — a careers/ATS URL a Pi run cited but the scanner hasn't confirmed
    # yet: one rung below a confirmed playbook URL, still ahead of whatever
    # was just typed into metadata.
    _add((careers_pb.get("candidate_url") or "").strip(), "pi-run")

    _add((meta.get("careers_url") or "").strip(), "metadata")

    if not (meta.get("website") or "").strip():
        website = await _resolve_client_website(org_id, client)
        if website:
            meta["website"] = website
            client["metadata"] = meta
    # D11 — normalize once here: metadata.website can be a bare domain with
    # no scheme (internal_create_client stores it verbatim), and every
    # consumer below (homepage fetch, path-probe tier, sitemap probe) turns
    # it into a URL. See _site_base's docstring for the exact failure mode.
    website = _site_base(meta.get("website") or "")
    domain = _client_domain(client)

    if website:
        _home_text, home_html = await _fetch_page_raw(website)
        if home_html:
            for link in _harvest_links(home_html, website, _CAREERS_KEYS, domain):
                _add(link, "homepage")

        # D1 — own-domain path-probe tier: only worth the extra requests when
        # NOTHING own-domain-or-ATS has surfaced yet (WP8 review nit 1) — an
        # earlier off-domain candidate (e.g. the homepage's only "jobs" link
        # points at linkedin.com/company/acme/jobs, or a stale playbook/
        # metadata URL that turns out to be off-domain) must not suppress
        # this tier; that was the WP7 symptom verbatim. A bare "any
        # candidates at all" check let it through untouched.
        if not any(_own_or_ats(c["url"], domain) for c in candidates):
            home_title_h1 = _page_title_h1(home_html) if home_html else None
            # WP11 rebase: a playbook that already knows this site needs
            # anti-detection (needs_js from a prior scan, or a cookie wall)
            # tells the path-probe's rendered-fallback stage to skip
            # straight to browser-service -> Camofox instead of wasting a
            # plain GET it already knows will come back thin.
            prefer_camofox = bool((pb or {}).get("needs_js"))
            probe_hits, probe_blocked = await _probe_careers_paths(
                website, domain, pb_blocked, home_title_h1, prefer_camofox,
            )
            for cand in probe_hits:
                _add(cand["url"], cand["tier"], cand.get("title", ""))
            if probe_blocked:
                # Smuggled back to _scan_client_jobs the same way _sitemap_cache
                # is below — every frozen signature in this file stays as-is.
                client["_probe_blocked"] = (client.get("_probe_blocked") or []) + probe_blocked

        sitemap_jobs = await _sitemap_job_urls(website)
        # Cache the crawl on the client dict (keyed by the site's own host) so
        # _scan_client_jobs can reuse it instead of crawling the same sitemap
        # a second time right after discovery returns.
        site_host = urlparse(website).netloc.lower().replace("www.", "")
        if site_host:
            client["_sitemap_cache"] = {"host": site_host, "jobs": sitemap_jobs}
        if sitemap_jobs:
            _add(_sitemap_listing_url(sitemap_jobs[0][1]), "sitemap")

    # Cheap, own-domain-ish tiers already found something — SearXNG is the
    # last resort, not a blanket cross-check run on every discovery.
    if candidates:
        return candidates

    queries = [f'"{name}" careers open positions', f'"{name}" jobs karriere stellenangebote']
    if domain:
        queries.insert(0, f"site:{domain} careers jobs stellen")
    for q in queries:
        try:
            results = await _searxng_results(q)
        except Exception:
            continue
        for r in results:
            _add((r.get("url") or "").strip(), "searxng", r.get("title") or "")

    return candidates


async def _discover_careers_url(org_id: int, client: dict, pb: Optional[dict] = None) -> tuple[str, str]:
    """Find a client's careers/jobs page. Cheap candidates (playbook, known
    metadata, homepage harvest, sitemap probe) come first and, if only one
    surfaces, it is used directly with no LLM call at all. Once SearXNG
    candidates are in the mix, the LLM picks the best one — but the pick is
    only accepted when it lands on the client's own domain or a known ATS
    host, so it can never wander off to an unrelated URL. Falls back to a
    own-domain + careers-keyword heuristic ranking when the LLM is
    unavailable, unsure, or picks something off-domain."""
    candidates = await _careers_candidates(org_id, client, pb)
    domain = _client_domain(client)

    # Constrain EVERY path (single-candidate short-circuit, LLM pick, heuristic
    # fallback) up front — filtering only inside the LLM branch let an
    # off-domain lone candidate (or an LLM pick from an all-off-domain pool)
    # through untouched, and that URL then persists in clients.metadata
    # forever once a scan writes it back.
    candidates = [c for c in candidates if _own_or_ats(c["url"], domain)]
    if not candidates:
        return "", ""
    if len(candidates) == 1:
        return candidates[0]["url"], candidates[0]["tier"]

    name = client["name"]

    listing = "\n".join(
        f"{i+1}. {c['title'] or c['url']} — {c['url']}" for i, c in enumerate(candidates[:15])
    )
    # rules= is filled BEFORE the "Reply with ONLY..." final instruction and,
    # critically, before {listing} — candidate titles come from search
    # results (untrusted) — never appended after (see _extract_jobs above).
    rules = _rules_block(await _jobs_lessons_block(org_id))
    prompt = (
        f"Which of these URLs is {name}'s official careers / open-positions listing page "
        f"(where you can browse their current job openings)? Prefer a page on the company's own "
        f"domain{(' (' + domain + ')') if domain else ''} or its official applicant-tracking system "
        f"(e.g. Personio, Greenhouse, SuccessFactors, Workday).\n"
        f"{rules}"
        f"Reply with ONLY the single best URL, or 'none' if none qualify.\n\n{listing}"
    )
    try:
        reply = (await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)).strip()
        m = re.search(r"https?://\S+", reply)
        if m:
            picked = m.group(0).rstrip(").,>\"'")
            match = next((c for c in candidates if c["url"] == picked), None)
            if match and _own_or_ats(picked, domain):
                return match["url"], match["tier"]
    except Exception as exc:
        console.print(f"[yellow]careers-url LLM pick failed for {name}: {exc}[/yellow]")

    # Heuristic fallback: own-domain / ATS-host + careers-ish keyword wins, tier
    # preserved. An ATS host counts on its own (score 2, same as own-domain) —
    # a bare "https://company.personio.de/" with no careers keyword in the URL
    # is still a real candidate worth returning, not a 0-score drop, when the
    # LLM pick is unavailable.
    ranked: list[tuple[int, dict]] = []
    for c in candidates:
        host = urlparse(c["url"]).netloc.lower().replace("www.", "")
        score = (2 if domain and (host == domain or host.endswith("." + domain)) else 0) \
              + (2 if _ats_match(host) else 0) \
              + (1 if any(k in c["url"].lower() for k in _CAREERS_KEYS) else 0)
        if score:
            ranked.append((score, c))
    if ranked:
        ranked.sort(key=lambda t: t[0], reverse=True)
        return ranked[0][1]["url"], ranked[0][1]["tier"]
    return "", ""


async def _extract_jobs(name: str, text: str, org_id: Optional[int] = None,
                         *, min_len: int = 200) -> tuple[list, list]:
    """One LLM call → (positions, inferred_needs) from careers-page text.
    `min_len` is lower for the sitemap-derived listing (already known-real
    titles, just possibly few of them since the accept threshold is now a
    single hit) than for raw fetched page text (where a short body usually
    means an empty/JS-only page not worth an LLM call)."""
    if len(text) < min_len:
        return [], []
    # rules= is filled BEFORE "Return STRICT JSON ONLY:" and, critically,
    # before {page} — the untrusted page text is the last thing in the
    # prompt, so any learned rules must land ahead of it, never appended
    # after (a prior version appended post-format, landing the rules inside
    # the untrusted-page region and pushing the JSON contract out of place).
    rules = _rules_block(await _jobs_lessons_block(org_id))
    prompt = _JOBS_EXTRACT_PROMPT.format(client=name, page=text[:16000], rules=rules)
    try:
        # Subscription bridge is text-only: acomplete (never a tool-using
        # chat/agent loop) — it warms the org overlay itself.
        reply = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
    except Exception as exc:
        console.print(f"[yellow]jobs extract LLM failed for {name}: {exc}[/yellow]")
        return [], []
    data = _parse_json_obj(reply)
    positions = [p for p in (data.get("positions") or []) if isinstance(p, dict) and p.get("title")][:25]
    needs = [str(n).strip() for n in (data.get("inferred_needs") or []) if str(n).strip()][:8]
    return positions, needs


# Sitemap URL substrings that mark an individual job posting (vs a category page).
_JOB_DETAIL_KEYS = ("/offer/", "/offers/", "/job/", "/jobs/", "/stelle/", "/stellen/",
                    "/stellenangebot", "/vacancy/", "/vacancies/", "/position/", "/positions/",
                    "/karriere/job", "/en/job", "/joblist/", "/jobad", "-job-", "/opening/")
# Titles to push to the front so IT/management roles survive the cap on big lists.
_IT_MGMT_TITLE_KEYS = ("it", "digital", "software", "develop", "engineer", "ingenieur", "data",
                       "security", "cyber", "cloud", "system", "informatik", "manager", "leiter",
                       "leitung", "head", "director", "projekt", "project", "consultant", "berater",
                       "architekt", "product", "analyst", "controlling", "transformation", "scrum")

# Apprentice/intern/student/thesis titles — dropped by _filter_positions unless
# an IT/management word (below) also appears in the title. Covers the common
# German inflections too (Studentische, Studentin/Studenten, Bachelorand/in,
# Masterand/in) — a bare "student" boundary doesn't match "Studentische"
# since German compounds have no internal word boundary to anchor on.
_JUNIOR_TITLE_RE = re.compile(
    r"\b(ausbildung|azubi|praktikum|werkstudent|duales studium|dh-studium|trainee|"
    r"intern(ship)?|bachelor|bachelorand|master thesis|masterand|abschlussarbeit|"
    r"ferienjob|minijob|aushilfe|student(ische?|in|en)?)\b",
    re.IGNORECASE,
)

# Deliberately narrow — used ONLY to decide whether a title that already looks
# junior (_JUNIOR_TITLE_RE matched) should be kept anyway. This is NOT the
# same pool as _IT_MGMT_TITLE_KEYS above (that one just ranks sitemap titles
# for a truncation cap, where over-matching is harmless): here a broad word
# list reintroduces exactly the junior titles the filter exists to drop —
# "system" kept "Ausbildung Fachkraft für Systemgastronomie", "digital" kept
# "Ausbildung Mediengestalter Digital und Print", "projekt" kept "Trainee
# Projektmanagement". Only unambiguous IT/senior-tech or senior-leadership
# terms qualify as an override.
_IT_MGMT_WORD_RE = re.compile(
    r"fachinformatiker|informatik|software|entwickler|developer|engineer|data|cloud|"
    r"security|cyber|devops|sap|erp|\bit\b|architekt|architect|cio|cto|ciso|head of|leiter",
    re.IGNORECASE,
)


def _filter_positions(positions: list) -> list:
    """Drop apprenticeship/internship/student/thesis/minijob roles unless the
    title also carries an IT/management word, dedupe titles case-insensitively,
    and cap at 20. Applied after every _extract_jobs call so junior/duplicate
    noise never reaches the stored jobs doc, the findings, or the brief."""
    out: list = []
    seen_titles: set = set()
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen_titles:
            continue
        if _JUNIOR_TITLE_RE.search(title) and not _IT_MGMT_WORD_RE.search(title):
            continue
        seen_titles.add(key)
        out.append(p)
        if len(out) >= 20:
            break
    return out


def _sitemap_listing_url(url: str) -> str:
    """Best-guess listing/landing URL for a job posting found via the sitemap
    (e.g. .../jobs/backend-engineer-123 -> .../jobs/) — used as the discovered
    careers-page candidate. The sitemap step itself only ever needs the domain
    (see _sitemap_job_urls below), so an imperfect path here only affects the
    page-text fallback, never the sitemap re-scan."""
    p = urlparse(url)
    path = p.path
    low = path.lower()
    end = 0
    for k in _JOB_DETAIL_KEYS:
        idx = low.find(k)
        if idx != -1:
            end = max(end, idx + len(k))
    trimmed = path[:end] if end else (path.rsplit("/", 1)[0] + "/")
    if not trimmed.endswith("/"):
        trimmed = trimmed.rsplit("/", 1)[0] + "/"
    return f"{p.scheme}://{p.netloc}{trimmed or '/'}"


async def _sitemap_job_urls(base_url: str, limit: int = 130) -> list[tuple[str, str]]:
    """Harvest individual job-posting URLs from the site's sitemap(s) — the
    reliable, JS-free source of the actual openings (JS/ATS careers pages load
    listings client-side, but their sitemap still lists every posting). Returns
    [(title_guess, url)], IT/management titles ranked first."""
    from urllib.parse import urlparse as _up
    p = _up(base_url)
    if not p.netloc:
        return []

    async def _get(u: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
            ) as http:
                r = await http.get(u)
                ct = r.headers.get("content-type", "").lower()
                head = r.text[:300]
                ok = r.status_code == 200 and ("xml" in ct or "<urlset" in head or "<sitemapindex" in head)
                return r.text if ok else ""
        except Exception:
            return ""

    queue = [f"{p.scheme}://{p.netloc}/sitemap.xml", f"{p.scheme}://{p.netloc}/sitemap_index.xml"]
    seen_sm: set = set()
    jobs: list[tuple[str, str]] = []
    seen: set = set()
    fetched = 0
    while queue and fetched < 15 and len(jobs) < limit * 3:
        u = queue.pop(0)
        if u in seen_sm:
            continue
        seen_sm.add(u)
        xml = await _get(u)
        if not xml:
            continue
        fetched += 1
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml, re.I):
            low = loc.lower()
            if low.endswith(".xml") and loc not in seen_sm:
                queue.append(loc)
                continue
            if loc in seen or not any(k in low for k in _JOB_DETAIL_KEYS):
                continue
            seen.add(loc)
            segs = [s for s in _up(loc).path.split("/") if s]
            # last segment is often a uuid/id — take the slug before it
            slug = segs[-2] if (len(segs) >= 2 and re.fullmatch(r"[0-9a-fA-F-]{8,}|\d+", segs[-1])) else (segs[-1] if segs else "")
            title = re.sub(r"[-_]+", " ", slug).strip()
            title = re.sub(r"\b([wmd])(\s+[wmd]){1,2}\b", "(w/m/d)", title)  # tidy gender markers
            if len(title) >= 3:
                jobs.append((title, loc))
    # IT/management titles first so they survive the cap.
    jobs.sort(key=lambda t: -sum(1 for k in _IT_MGMT_TITLE_KEYS if k in t[0].lower()))
    return jobs[:limit]


async def _map_needs_to_products(org_id: int, client_name: str, needs: list) -> list:
    """For each hiring-inferred need, which of the seller's products address it
    and why (one LLM call). Returns [{need, products:[{name, why}]}] aligned to
    `needs`. Empty product lists when nothing fits."""
    if not needs:
        return []
    products = await db_module.list_products(org_id, focus_only=True)
    if not products:
        products = await db_module.list_products(org_id)
    if not products:
        return [{"need": n, "products": []} for n in needs]

    plist = "\n".join(f"- {p['name']}: {((p.get('description') or '')[:160])}" for p in products[:30])
    nlist = "\n".join(f"{i+1}. {n}" for i, n in enumerate(needs))
    prompt = (
        f"SELLER PRODUCTS:\n{plist}\n\n"
        f"{client_name} — needs inferred from their open roles:\n{nlist}\n\n"
        "For EACH need, which seller products genuinely address it, and why? Return STRICT JSON only: "
        '[{"need":"<exact need text>","products":[{"name":"<exact product name>","why":"<one concise sentence>"}]}]. '
        "Include only products that truly fit; use an empty products list if none fit."
    )
    try:
        reply = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
        raw = _parse_json_list(reply)
    except Exception as exc:
        console.print(f"[yellow]need→product map failed for {client_name}: {exc}[/yellow]")
        raw = []

    valid = {p["name"].strip().lower(): p["name"] for p in products}
    by_need: dict = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        prods = []
        for pr in (item.get("products") or []):
            if not isinstance(pr, dict):
                continue
            nm = valid.get(str(pr.get("name", "")).strip().lower())
            why = str(pr.get("why", "")).strip()
            if nm and why:
                prods.append({"name": nm, "why": why})
        by_need[str(item.get("need", "")).strip().lower()] = prods
    return [{"need": n, "products": by_need.get(n.strip().lower(), [])} for n in needs]


_POSTING_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_POSTING_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)


async def _fetch_posting_title(url: str) -> str:
    """D10: plain-GET a single job-posting page and return a cleaned
    <title>/<h1> (company suffix after ' - '/' | ' stripped) — used to
    replace a sitemap-slug-derived title, which is only ever as good as the
    URL's slug and gets truncated mid-word for a longer role name. '' on any
    failure (no response, no title/h1, or an empty one after cleanup) — the
    caller falls back to the slug title in that case."""
    html = ""
    try:
        async with httpx.AsyncClient(
            timeout=8.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
        ) as http:
            resp = await http.get(url)
            if resp.status_code == 200:
                html = resp.text
    except Exception:
        return ""
    for pattern in (_POSTING_TITLE_RE, _POSTING_H1_RE):
        m = pattern.search(html or "")
        if not m:
            continue
        raw = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", m.group(1))).strip()
        for sep in (" - ", " | "):
            if sep in raw:
                raw = raw.split(sep)[0].strip()
        if raw:
            return raw
    return ""


async def _scan_client_jobs(org_id: int, client: dict, careers_url: str = "", *,
                             run_id: Optional[int] = None) -> dict:
    """Fetch a client's careers page, extract open positions + inferred needs,
    store a singleton type='jobs' doc, and write the inferred needs as
    type='finding' docs so the match synthesis picks them up automatically.

    Sources, in order of reliability: (1) the site's sitemap of actual job
    postings — works even for JS/ATS pages that render listings client-side;
    (2) the careers-page text; (3) job-listing links followed from the landing
    page. The LLM filters to IT/management roles and rejects category names;
    _filter_positions then drops junior/apprentice noise on top.

    Every attempt — success or failure — is recorded: a failure stamps the
    existing jobs doc's last_attempt/last_error/attempts (bumping updated_at
    so _run_jobs_monitor's LRU rotation stops retrying a never-yielding client
    first) without ever clobbering a prior good scan's positions, and, once
    playbook.py exists (WP4), records the careers tier so later scans and
    other agents' tasks can skip straight to what worked."""
    name = client["name"]
    meta = client.get("metadata") or {}
    domain = _client_domain(client)
    jobs_doc_id = f"jobs-{client['id']}"
    now_iso = datetime.now(timezone.utc).isoformat()

    # WP4 hasn't landed yet — the writer/reader must work fine without it.
    try:
        import playbook
    except ImportError:
        playbook = None
    pb: Optional[dict] = None
    if playbook is not None and domain:
        try:
            pb = await playbook.load(org_id, domain)
        except Exception:
            pb = None

    # D5 — tier precedence, most specific/confirmed first: a fresh (<=60 day)
    # playbook careers URL wins outright (discovery is skipped entirely, not
    # just short-circuited inside _discover_careers_url); then an explicit
    # careers_url argument (a human/admin-triggered scan); then whatever's
    # cached on the client's own metadata; only then real discovery.
    careers_pb = (pb or {}).get("careers") or {}
    pb_url = (careers_pb.get("url") or "").strip()
    arg_url = (careers_url or "").strip()
    meta_url = (meta.get("careers_url") or "").strip()
    if pb_url and _playbook_careers_fresh(careers_pb):
        url, tier = pb_url, "playbook"
    elif arg_url:
        url, tier = arg_url, "argument"
    elif meta_url:
        url, tier = meta_url, "metadata"
    else:
        url, tier = await _discover_careers_url(org_id, client, pb)

    # D1/D7 — own-domain 403/4xx hit while probing for the careers page
    # (path-probe tier), smuggled back on the client dict since
    # _careers_candidates' signature is frozen. Consumed here (not left for
    # a later reader) so it is reported exactly once, alongside this scan's
    # playbook patch.
    probe_blocked = client.pop("_probe_blocked", None) or []

    summary = {"client": name, "careers_url": url, "positions": 0, "needs": 0,
               "found": False, "tier": tier, "error": None}

    async def _record_playbook(*, success: bool, reason: Optional[str], needs_js: bool,
                                report_tier: Optional[str] = None) -> None:
        if playbook is None or not domain:
            return
        effective_tier = report_tier if report_tier is not None else tier
        careers_patch: dict = {}
        # Omit "url" entirely when there's nothing to report (e.g. the "no
        # careers page found" path) — an empty string here would clobber a
        # good previously-recorded playbook URL. last_failure_at / error
        # below still get recorded either way.
        if url:
            careers_patch["url"] = url
        # D5 — "metadata"/"playbook" mean "we already knew the URL", not a
        # fresh discovery: writing them back as `tier` would erase whatever
        # more specific tier (searxng, sitemap, path-probe, ...) actually got
        # this client working the first time. Only a genuine discovery run,
        # or an explicit `careers_url` argument, is worth recording as tier —
        # and, the first time it happens, as the permanent discovered_tier.
        if effective_tier and effective_tier not in ("metadata", "playbook"):
            careers_patch["tier"] = effective_tier
            if success and not careers_pb.get("discovered_tier"):
                careers_patch["discovered_tier"] = effective_tier
        if success:
            careers_patch["last_success_at"] = now_iso
            careers_patch["error"] = None
        else:
            careers_patch["last_failure_at"] = now_iso
            careers_patch["error"] = reason
        patch: dict = {"careers": careers_patch}
        if needs_js:
            patch["needs_js"] = True
        if probe_blocked:
            patch["blocked_urls"] = probe_blocked
        try:
            await playbook.record(org_id, domain, patch, run_id=run_id)
        except Exception as exc:
            console.print(f"[yellow]playbook record failed for {name}: {exc}[/yellow]")

    async def _stamp_failure(reason: str) -> dict:
        summary["error"] = reason
        existing = await db_module.get_document(org_id, jobs_doc_id)
        existing_meta = (existing or {}).get("metadata") if existing else None
        if isinstance(existing_meta, str):
            try:
                existing_meta = json.loads(existing_meta)
            except Exception:
                existing_meta = {}
        existing_meta = existing_meta or {}
        attempts = int(existing_meta.get("attempts") or 0) + 1
        patch_meta = {"tier": tier, "last_attempt": now_iso, "last_error": reason, "attempts": attempts}
        if existing:
            # Merge-only patch (documents.metadata is a shallow `metadata || patch`
            # merge) — a prior good scan's positions/careers_url are untouched.
            await db_module.update_document(org_id, jobs_doc_id, {"metadata": patch_meta})
        else:
            new_id = await db_module.index_document(
                org_id=org_id, doc_id=jobs_doc_id, doc_type="jobs",
                title=f"Open positions — {name}",
                content=f"# Open positions — {name}\n\n(no data yet — {reason})",
                metadata={**patch_meta, "careers_url": url, "positions": [], "inferred_needs": [],
                          "needs_mapped": [], "subject": name, "filtered_out": 0},
                embedding=[], source="agent", agent_run_id=run_id,
            )
            # Link it exactly like the success path does below — an unlinked
            # placeholder is invisible to _run_jobs_monitor's
            # "MAX(updated_at) JOIN document_links" rotation query, so a
            # never-successful client would keep heading the retry queue
            # every run, the exact starvation this bookkeeping exists to fix.
            if new_id and new_id > 0:
                await db_module.link_document(new_id, "client", client["id"])
        await _record_playbook(success=False, reason=reason, needs_js=False)
        return summary

    if not url:
        return await _stamp_failure("no careers page found")

    original_url = url  # the URL the scan started from, for tier reporting below
    effective_url = url
    positions: list = []
    needs: list = []
    filtered_out = 0
    needs_js = False

    # (1) Sitemap of actual postings — the JS-free ground truth. A JS-heavy careers
    # page (own-domain or ATS-hosted) only exposes category filters to a fetch, but
    # its sitemap lists every real opening (e.g. jobs.apleona.com — apleona's own
    # domain, not an ATS host — → /offer/<slug>/<uuid>). A single hit is trusted
    # (lowered from 3): the sitemap can't lie about what's posted.
    # Reuse the crawl _careers_candidates already did during discovery instead
    # of hitting the same sitemap a second time when the host matches.
    sitemap_cache = client.pop("_sitemap_cache", None)
    url_host = urlparse(url).netloc.lower().replace("www.", "")
    if sitemap_cache and sitemap_cache.get("host") == url_host:
        sitemap_jobs = sitemap_cache.get("jobs") or []
    else:
        sitemap_jobs = await _sitemap_job_urls(url)
    if len(sitemap_jobs) >= 1:
        listing = "ACTUAL OPEN POSITIONS — these are real individual job postings (titles from the "
        listing += "company's job sitemap, NOT categories). Extract and filter them per the rules:\n"
        listing += "\n".join(f"- {t}" for t, _ in sitemap_jobs)
        raw_positions, needs = await _extract_jobs(name, listing, org_id, min_len=40)
        positions = _filter_positions(raw_positions)
        filtered_out = len(raw_positions) - len(positions)
        # D10 — a title here is only ever as good as the sitemap URL's slug
        # (e.g. "IT Solution Architect Customer Serv", cut mid-word); the
        # per-job-URL match below tries to replace it with the real posting
        # page's <title>/<h1>.
        for p in positions:
            p["title_source"] = "slug"

    # (2) Careers-page text (good for sites that list roles inline).
    if not positions:
        text, html = await _fetch_page_raw(url, wait_ms=3500)
        plain_len = len(re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", html)).strip()) if html else 0
        raw_positions, needs = await _extract_jobs(name, text, org_id)
        positions = _filter_positions(raw_positions)
        filtered_out = len(raw_positions) - len(positions)
        for p in positions:
            p["title_source"] = "page"  # from the actual page text, never a slug
        if positions and plain_len < 500:
            needs_js = True  # only the browser-rendered fallback found anything
        # (3) Landing page with no roles → follow its job-listing links.
        if not positions and html:
            for link in _harvest_links(html, url, _JOB_LINK_KEYS, domain):
                sub_text, sub_html = await _fetch_page_raw(link, wait_ms=5000)
                sub_plain_len = len(re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", sub_html)).strip()) if sub_html else 0
                p2, n2 = await _extract_jobs(name, sub_text, org_id)
                p2f = _filter_positions(p2)
                if p2f:
                    positions, needs, effective_url = p2f, n2, link
                    filtered_out = len(p2) - len(p2f)
                    if sub_plain_len < 500:
                        needs_js = True
                    for p in positions:
                        p["title_source"] = "page"
                    break

    if effective_url and effective_url != meta.get("careers_url"):
        await db_module.update_client_metadata(org_id, name, {"careers_url": effective_url})

    # From here on `url` always means the URL actually used/found — both the
    # failure stamp below and the success write further down must persist
    # this (effective_url), not the originally discovered/known one; a
    # step-(3) listing-link follow-through can differ from it.
    url = effective_url
    summary["careers_url"] = url

    # Nothing found anywhere — keep any prior good scan, just record we looked.
    if not positions and not needs:
        return await _stamp_failure("no positions found on careers page")

    # Attach each position's own posting URL (the sitemap path gives per-job URLs;
    # match the LLM-cleaned title back to the closest sitemap title).
    if sitemap_jobs and positions:
        def _norm(t: str) -> str:
            t = re.sub(r"\(.*?\)", " ", (t or "").lower())
            return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())
        sm = [(_norm(t), u) for t, u in sitemap_jobs]
        for p in positions:
            pn = _norm(p.get("title", ""))
            ptoks = set(pn.split())
            if not ptoks:
                continue
            best, best_score = "", 0.0
            for snorm, surl in sm:
                stoks = set(snorm.split())
                if not stoks:
                    continue
                score = len(ptoks & stoks) / max(len(ptoks), 1)
                if pn and (pn in snorm or snorm in pn):
                    score += 0.5
                if score > best_score:
                    best, best_score = surl, score
            if best and best_score >= 0.5:
                p["url"] = best

        # D10 — replace a slug-derived title with the real posting page's
        # <title>/<h1> for the (at most 8) positions that got a posting URL
        # above; a fetch that fails or turns up nothing leaves the slug title
        # in place (title_source stays "slug").
        slug_positions = [p for p in positions if p.get("title_source") == "slug" and p.get("url")][:8]
        if slug_positions:
            page_titles = await asyncio.gather(
                *[_fetch_posting_title(p["url"]) for p in slug_positions]
            )
            for p, page_title in zip(slug_positions, page_titles):
                if page_title:
                    p["title"] = page_title
                    p["title_source"] = "page"

            # BLOCKER 3 (WP8 review) — a page title fetched above can turn out
            # to be junior (the slug looked fine, the real posting doesn't)
            # or collide with another position's real title (many distinct
            # slugs all resolving to the same generic posting page title,
            # e.g. every one titled "Praktikant Marketing (m/w/d) - Acme").
            # _filter_positions already ran once on the ORIGINAL slug titles;
            # re-run it now that titles may have changed, and fold any newly
            # dropped/deduped count into filtered_out rather than overwrite it.
            before_refilter = len(positions)
            positions = _filter_positions(positions)
            filtered_out += before_refilter - len(positions)

    # Map each inferred need to the seller's products, with a one-line justification.
    needs_mapped = await _map_needs_to_products(org_id, name, needs)

    lines = [f"# Open positions — {name}", f"Source: {url}", ""]
    for p in positions:
        extra = " · ".join(x for x in (p.get("team") or "", p.get("location") or "") if x)
        lines.append(f"- **{p['title']}**" + (f" ({extra})" if extra else ""))
    if needs:
        lines.append("\n## Inferred needs")
        lines += [f"- {n}" for n in needs]
    jobs_doc_id_int = await db_module.index_document(
        org_id=org_id, doc_id=jobs_doc_id, doc_type="jobs",
        title=f"Open positions — {name}", content="\n".join(lines),
        metadata={"careers_url": url, "positions": positions, "inferred_needs": needs,
                  "needs_mapped": needs_mapped, "last_scanned": now_iso, "subject": name,
                  "tier": tier, "last_attempt": now_iso, "last_error": None, "attempts": 0,
                  "filtered_out": filtered_out},
        embedding=[], source="agent", agent_run_id=run_id,
    )
    if jobs_doc_id_int and jobs_doc_id_int > 0:
        await db_module.link_document(jobs_doc_id_int, "client", client["id"])

    # Inferred needs → findings (deterministic ids = idempotent; match synthesis reads findings).
    for i, need in enumerate(needs):
        fid = await db_module.index_document(
            org_id=org_id, doc_id=f"jobs-need-{client['id']}-{i}", doc_type="finding",
            title=f"Hiring signal: {need[:60]}",
            content=(f"Inferred from {name}'s open roles ({len(positions)} positions on their "
                     f"careers page): {need}."),
            metadata={"source_url": url, "from_jobs": True, "subject": name},
            embedding=[], source="agent", agent_run_id=run_id,
        )
        if fid and fid > 0:
            await db_module.link_document(fid, "client", client["id"])

    # A step-(3) listing-link follow-through means the URL that actually
    # yielded positions is not the one discovery/metadata handed us — report
    # that to the playbook instead of crediting the original discovery tier
    # for a page that in fact returned nothing.
    followed_link = url != original_url
    await _record_playbook(success=True, reason=None, needs_js=needs_js,
                            report_tier=("listing-link" if followed_link else tier))

    summary.update({"positions": len(positions), "needs": len(needs),
                    "careers_url": url, "found": True, "tier": tier, "error": None})
    return summary


async def _run_jobs_monitor(org_id: int) -> dict:
    """Scan focus clients' careers pages every run, plus a rotating few others
    (job postings change slowly). Writes positions + inferred-need findings."""
    clients = await db_module.list_clients(org_id)
    focus = [c for c in clients if (c.get("metadata") or {}).get("is_focus")]
    focus_ids = {c["id"] for c in focus}
    # Least-recently-scanned non-focus clients first (jobs doc updated_at; missing = never).
    last: dict = {}
    if getattr(db_module, "_pool", None):
        async with db_module._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT dl.entity_id AS cid, MAX(d.updated_at) AS ts
                   FROM documents d JOIN document_links dl
                     ON dl.document_id = d.id AND dl.entity_type = 'client'
                   WHERE d.org_id = $1 AND d.type = 'jobs' GROUP BY dl.entity_id""",
                org_id,
            )
        last = {r["cid"]: r["ts"] for r in rows}
    max_other = int(context.config.get("jobs_max_per_run", 5))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rest = [c for c in clients if c["id"] not in focus_ids]

    # D2 — a client whose playbook picked up a fresh Pi-run careers candidate
    # (candidate_at newer than the last recorded failure, and no confirmed
    # success yet) jumps the ordinary LRU queue: the whole point of a pi-run
    # candidate is for the very next scan to try it, not to wait its turn.
    try:
        import playbook
    except ImportError:
        playbook = None

    async def _has_fresh_pi_candidate(c: dict) -> bool:
        if playbook is None:
            return False
        domain = _client_domain(c)
        if not domain:
            return False
        try:
            pb = await playbook.load(org_id, domain)
        except Exception:
            return False
        careers = (pb or {}).get("careers") or {}
        candidate_at = careers.get("candidate_at")
        if not candidate_at or careers.get("last_success_at"):
            return False
        last_failure = careers.get("last_failure_at")
        if not last_failure:
            return True
        try:
            return datetime.fromisoformat(candidate_at) > datetime.fromisoformat(last_failure)
        except (ValueError, TypeError):
            return False

    # WP8 review nit 4 — only bother checking clients whose jobs doc hasn't
    # been touched (success OR failure) in the last 7 days; a recently-
    # scanned client isn't a rotation-jump candidate regardless of what its
    # playbook says, so skip the playbook.load round-trip for it. Bounds
    # what was an unconditional per-client SQL/document read (one extra
    # query for every non-focus client, every run) down to just the ones
    # that could actually change the ordering below.
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    def _stale_or_never_scanned(c: dict) -> bool:
        ts = last.get(c["id"])
        if not ts:
            return True
        try:
            return ts < seven_days_ago
        except TypeError:
            return True

    check_candidates = [c for c in rest if _stale_or_never_scanned(c)]

    prioritized: list = []
    if playbook is not None and check_candidates:
        flags = await asyncio.gather(*[_has_fresh_pi_candidate(c) for c in check_candidates])
        jump_ids = {c["id"] for c, f in zip(check_candidates, flags) if f}
        prioritized = [c for c in rest if c["id"] in jump_ids]
        rest = [c for c in rest if c["id"] not in jump_ids]

    others = (prioritized + sorted(rest, key=lambda c: last.get(c["id"]) or epoch))[:max_other]

    scanned = []
    for c in focus + others:
        try:
            scanned.append(await _scan_client_jobs(org_id, c))
        except Exception as exc:
            console.print(f"[yellow]jobs scan failed for {c['name']}: {exc}[/yellow]")
    found = [s for s in scanned if s.get("found")]
    return {"clients_scanned": len(scanned), "with_jobs": len(found),
            "positions_total": sum(s.get("positions", 0) for s in scanned),
            "needs_total": sum(s.get("needs", 0) for s in scanned)}


# ---------------------------------------------------------------------------
# Per-rep client digest (every 2 days) — engagement nudge, admin review/send
# ---------------------------------------------------------------------------

def _esc_html(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _signal_subject(s: dict) -> str:
    """The client/company a signal is about (used to group + label news)."""
    meta = s.get("metadata") or {}
    return (meta.get("subject") if isinstance(meta, dict) else "") or s.get("client_name") or s.get("subject") or ""


def _diversify_signals(signals: list, per_client: int = 2, total: int = 6) -> list:
    """Round-robin signals across clients so a single loud client can't monopolise
    the 'What's new' section. Newest signal from each client first, then a second
    pass, capped at `per_client` items/client and `total` overall. Preserves the
    incoming (recency) order within each client."""
    from collections import OrderedDict
    by_client: "OrderedDict[str, list]" = OrderedDict()
    for s in signals:
        by_client.setdefault(_signal_subject(s) or "—", []).append(s)
    picked: list = []
    for round_no in range(per_client):
        added = False
        for items in by_client.values():
            if len(items) > round_no:
                picked.append(items[round_no])
                added = True
                if len(picked) >= total:
                    return picked
        if not added:
            break
    return picked


def _signal_url(s: dict) -> Optional[str]:
    meta = s.get("metadata") or {}
    return s.get("source_url") or (meta.get("source_url") if isinstance(meta, dict) else None) or None


def _render_digest_html(rep_name: str, client_names: list, top_actions: list,
                        signals: list, overlooked: Optional[list] = None) -> str:
    # Base URL for links in digest emails. Set `public_url` in config.yaml to the
    # externally reachable URL of this deployment. Without it, links degrade to
    # relative paths (fine inside the web UI, not clickable from a mail client).
    server_url = context.config.get("public_url") or context.config.get("server_url") or ""
    p = [f"<p>Hi {_esc_html(rep_name or 'there')},</p>",
         f"<p>Here's what moved across your {len(client_names)} client(s) recently — "
         f"and who to reach out to next.</p>"]
    if top_actions:
        p.append("<p><b>👉 Top next actions</b></p><ul>")
        for a in top_actions:
            client = _esc_html(a.get('client', ''))
            link = a.get('action_link') or ''
            client_html = f'<a href="{server_url}{_esc_html(link)}">{client}</a>' if link else client
            p.append(f"<li><b>{client_html}</b> — "
                     f"{_esc_html(a.get('suggested_action', ''))}: {_esc_html(a.get('reason', ''))}</li>")
        p.append("</ul>")
    if signals:
        p.append("<p><b>🆕 What's new</b></p><ul>")
        for s in signals:
            subj = _signal_subject(s)
            url = _signal_url(s)
            title = _esc_html(s.get('title', ''))
            title_html = f'<a href="{_esc_html(url)}">{title}</a>' if url else title
            p.append(f"<li>{('<b>'+_esc_html(subj)+'</b>: ') if subj else ''}{title_html}</li>")
        p.append("</ul>")
    if overlooked:
        p.append("<p><b>🔎 Opportunities to act on</b></p><ul>")
        for o in overlooked[:3]:
            client = _esc_html(o.get('client', ''))
            link = o.get('link') or ''
            client_html = f'<a href="{server_url}{_esc_html(link)}">{client}</a>' if link else client
            age = o.get('age_days')
            age_txt = f" ({age}d)" if age is not None else ""
            p.append(f"<li><b>{client_html}</b>{age_txt} — {_esc_html(o.get('why', ''))}</li>")
        p.append("</ul>")
    if not top_actions and not signals and not overlooked:
        p.append("<p>No major changes recently — a good moment for a proactive "
                 "check-in with a key account.</p>")
    if server_url:
        p.append(f'<p style="color:#888;font-size:13px">Open Buzzowl to act on these → '
                 f'<a href="{_esc_html(server_url)}">{_esc_html(server_url)}</a></p>')
    else:
        p.append('<p style="color:#888;font-size:13px">Open Buzzowl to act on these.</p>')
    return "\n".join(p)


async def _build_rep_digests(org_id: int) -> dict:
    """Build a per-rep client digest (what's new + top actions), store each as a
    pending doc for admin review/send, and Telegram-remind the admin. When
    digest_auto_send is on (and SMTP is configured), email reps directly."""
    from routers.today import compute_nba_queue, compute_overlooked
    all_clients = await db_module.list_clients(org_id)
    owned: dict = {}
    for c in all_clients:
        ids: set = set()
        if c.get("created_by"):
            ids.add(int(c["created_by"]))
        meta = c.get("metadata") or {}
        if isinstance(meta, dict):
            for oid in (meta.get("owner_ids") or []):
                try:
                    ids.add(int(oid))
                except (TypeError, ValueError):
                    pass
        for oid in ids:
            owned.setdefault(oid, []).append(c["name"])

    try:
        users = {u["id"]: u for u in await db_module.list_users(org_id)}
    except Exception:
        users = {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()
    auto = bool(context.config.get("digest_auto_send"))
    built = sent = no_email = 0
    blocked_reps: list[str] = []

    for oid, client_names in sorted(owned.items()):
        u = users.get(oid)
        if not u:
            continue
        try:
            snap = await compute_nba_queue(org_id, owner_id=oid)
            top = (snap.get("queue") or [])[:3]
        except Exception:
            top = []
        # Fetch a wide pool then diversify: a per-client cap stops one loud client
        # from filling every slot. Fall back to a 7-day window when 2 days is empty.
        try:
            signals = await db_module.list_signals(org_id, subjects=client_names, days=2, limit=40)
            if not signals:
                signals = await db_module.list_signals(org_id, subjects=client_names, days=7, limit=40)
            signals = _diversify_signals(signals, per_client=2, total=6)
        except Exception:
            signals = []
        try:
            overlooked = await compute_overlooked(org_id, owner_id=oid, limit=3)
        except Exception:
            overlooked = []
        rep_name = u.get("display_name") or u.get("username") or f"user {oid}"
        email = u.get("email") or ""
        html = _render_digest_html(rep_name, client_names, top, signals, overlooked)

        # A rep with no email can never receive the digest — don't store it as a
        # misleading 'pending' doc; mark it blocked and surface it to the admin.
        if not email:
            no_email += 1
            blocked_reps.append(rep_name)
            status, sent_at = "blocked_no_email", None
        else:
            status, sent_at = "pending", None
            if auto:
                try:
                    import mailer
                    ok, _msg = mailer.send_email(email, f"Your client update — {today}", html)
                    if ok:
                        status, sent_at, sent = "sent", now_iso, sent + 1
                except Exception:
                    pass

        try:
            await db_module.index_document(
                org_id=org_id, doc_id=f"rep-digest-{oid}-{today}", doc_type="note",
                title=f"Client digest — {rep_name} {today}", content=html,
                metadata={"brief_type": "rep_digest", "digest_status": status,
                          "rep_user_id": oid, "rep_email": email, "rep_name": rep_name,
                          "generated_date": today, "client_count": len(client_names),
                          "sent_at": sent_at, "subject": rep_name},
                embedding=[], source="agent",
            )
            built += 1
            # Supersede this rep's earlier pending digests so only today's is
            # reviewable/sendable — older runs must not linger or be sent.
            if db_module._pool:
                async with db_module._pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE documents
                           SET metadata = jsonb_set(metadata, '{digest_status}', '"superseded"')
                           WHERE org_id = $1 AND metadata->>'brief_type' = 'rep_digest'
                             AND metadata->>'rep_user_id' = $2
                             AND COALESCE(metadata->>'digest_status','pending') = 'pending'
                             AND doc_id <> $3""",
                        org_id, str(oid), f"rep-digest-{oid}-{today}",
                    )
        except Exception as exc:
            console.print(f"[yellow]rep_digest build failed for {rep_name}: {exc}[/yellow]")

    pending = built - sent - no_email
    if built:
        try:
            import notifications as _notify
            blocked = (f", {no_email} blocked (no email: {', '.join(blocked_reps)})"
                       if no_email else "")
            await _notify.notify_org(org_id,
                f"📬 {built} rep digest(s) built — {sent} sent, {pending} pending review"
                f"{blocked} — open /insights",
                "digest", roles=("admin",))
        except Exception:
            pass
    return {"digests_built": built, "auto_sent": sent, "pending": pending,
            "without_email": no_email, "blocked_reps": blocked_reps,
            "reps": sorted(owned.keys())}


async def _select_heartbeat_clients(org_id: int, agent_type: str) -> tuple[list[dict], dict]:
    """Tiered client selection for the research/osint heartbeats.

    - Focus clients (metadata.is_focus) are always candidates; for osint-type
      runs they are additionally gated by the news change-detection fingerprint.
    - Non-focus clients get a small trickle: at most heartbeat_max_nonfocus_per_run,
      only those whose newest linked document is older than heartbeat_stale_days,
      oldest first — so nothing goes permanently unmonitored but a big client
      list can't burn the token budget.
    """
    stale_days = int(context.config.get("heartbeat_stale_days", 14))
    max_nonfocus = int(context.config.get("heartbeat_max_nonfocus_per_run", 3))
    news_gate = bool(context.config.get("news_change_detection", True)) and agent_type == "osint"

    all_clients = await db_module.list_clients(org_id)
    # Shared clients (Phase 6a): only the monitor org researches/monitors a shared
    # client; the other members receive the results through the share sync.
    try:
        skip_shared = await db_module.sharing_non_monitor_client_ids(org_id)
    except Exception:
        skip_shared = set()
    if skip_shared:
        all_clients = [c for c in all_clients if c.get("id") not in skip_shared]
    focus = [c for c in all_clients if (c.get("metadata") or {}).get("is_focus")]

    selected: list[dict] = []
    skipped_unchanged = 0
    for c in focus:
        if news_gate and not await _client_news_changed(org_id, c):
            skipped_unchanged += 1
            continue
        selected.append(c)

    last_docs = await db_module.get_client_last_doc_dates(org_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    focus_ids = {c["id"] for c in focus}
    stale_nonfocus = sorted(
        (c for c in all_clients
         if c["id"] not in focus_ids and (last_docs.get(c["id"]) or epoch) < cutoff),
        key=lambda c: last_docs.get(c["id"]) or epoch,
    )
    selected.extend(stale_nonfocus[:max_nonfocus])

    summary = {
        "focus_selected": len(focus) - skipped_unchanged,
        "focus_skipped_unchanged": skipped_unchanged,
        "stale_nonfocus_selected": len(stale_nonfocus[:max_nonfocus]),
        "skipped_total": len(all_clients) - len(selected),
    }

    # Autonomy seam (Phase 2, selection triage): at level >= 2 with a large
    # candidate list, one batched LLM pass re-ranks/filters by change deltas
    # so the per-client decisions downstream spend budget where it matters.
    # Deterministic tiering above is the result when the LLM is unavailable
    # or the list is small. Level 0/1 = untouched.
    try:
        if selected and len(selected) > int(context.config.get("autonomy_triage_min_candidates", 5)) \
                and await autonomy.level(org_id) >= autonomy.LEVEL_ACT:
            selected, triage_info = await _triage_selection(org_id, selected, agent_type)
            summary["triage"] = triage_info
    except Exception as exc:
        console.print(f"[yellow]selection triage skipped: {exc}[/yellow]")
    console.print(
        f"[dim]Heartbeat {agent_type}: {summary['focus_selected']} focus "
        f"({skipped_unchanged} skipped unchanged), "
        f"{summary['stale_nonfocus_selected']} stale non-focus, "
        f"{summary['skipped_total']} skipped of {len(all_clients)} total[/dim]"
    )
    return selected, summary


async def _triage_selection(org_id: int, candidates: list[dict], agent_type: str) -> tuple[list[dict], dict]:
    """One batched LLM pass over change deltas → ordered subset of candidates.
    Returns (clients, info). On any parse problem returns the input unchanged."""
    max_keep = int(context.config.get("autonomy_triage_max_keep", 8))
    items = []
    for c in candidates:
        meta = c.get("metadata") or {}
        items.append({
            "client": c["name"],
            "is_focus": bool(meta.get("is_focus")),
            "news_pending": bool(meta.get("news_pending")),
            "news_pending_reason": (meta.get("news_pending_reason") or [])[:3],
            "last_autonomous_run_at": meta.get("last_autonomous_run_at") or "never",
            "last_activity": str(c.get("last_activity") or "unknown"),
        })
    prompt = (
        f"You triage which clients a sales-research agent should {agent_type} today. "
        f"Given the candidates below, return the ones most worth acting on now, most "
        f"urgent first, at most {max_keep}. Prefer clients with pending news changes, "
        f"focus clients, and the longest gaps since the last autonomous run. Reply with "
        f'ONLY a JSON array of client names: ["<name>", ...]\n\nCANDIDATES:\n'
        + json.dumps(items, ensure_ascii=False)
    )
    text = await llm.acomplete(prompt, role="triage", max_tokens=400, timeout=60, org_id=org_id)
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return candidates, {"applied": False, "reason": "unparseable"}
    names = [str(n).strip().lower() for n in json.loads(m.group(0)) if isinstance(n, str)]
    by_name = {c["name"].strip().lower(): c for c in candidates}
    kept = [by_name[n] for n in names if n in by_name][:max_keep]
    if not kept:
        return candidates, {"applied": False, "reason": "no known names"}
    return kept, {"applied": True, "before": len(candidates), "after": len(kept),
                  "dropped": [c["name"] for c in candidates if c not in kept]}


def _heartbeat_decision_ctx(client: dict, agent_type: str, task: str) -> "autonomy.DecisionContext":
    """Fact bundle the triage brain sees for one heartbeat candidate."""
    meta = client.get("metadata") or {}
    signals: list[str] = []
    if meta.get("news_pending"):
        signals.append(f"news pending since {meta.get('news_pending_at', '?')}: "
                       f"{', '.join(map(str, meta.get('news_pending_reason') or []))[:200]}")
    if meta.get("news_fp") is None:
        signals.append("no news baseline yet")
    facts = {
        "is_focus": bool(meta.get("is_focus")),
        "last_activity": str(client.get("last_activity") or "unknown"),
        "last_autonomous_run_at": meta.get("last_autonomous_run_at") or "never",
        "industry": meta.get("industry") or "unknown",
        "heartbeat": agent_type,
        "task_hint": (task or "")[:120],
        "_client": client,
    }
    return autonomy.DecisionContext(
        seam="heartbeat", client_name=client["name"], signals=signals, facts=facts,
        allowed_actions=("skip", "research") if agent_type == "research" else ("skip", "osint"),
        # deterministic fallback = legacy behaviour (the heartbeat used to always run)
        fallback_action="research" if agent_type == "research" else "osint",
    )


async def _run_heartbeat_job(hb_id: int, org_id: int, agent_type: str, task: str) -> None:
    """Execute one heartbeat job — called by APScheduler for each cron entry."""
    # Hosted: a suspended tenant (subscription lapsed) runs no scheduled work.
    try:
        from routers.operator import is_suspended as _is_suspended
        if await _is_suspended(org_id):
            console.print(f"[dim]heartbeat {agent_type} skipped — org {org_id} suspended[/dim]")
            return
    except Exception:
        pass
    if not DB_AVAILABLE:
        return
    console.print(f"[dim]Heartbeat: {agent_type} — {task[:60]}[/dim]")
    try:
        run_id = await db_module.create_agent_run(
            org_id=org_id, agent_type=agent_type, task=task, trigger_type="heartbeat",
        )
        backend = context.config.get("agent_service_backend", "python")

        if agent_type == "monitor" and backend in ("pi", "hermes", "split"):
            # Monitor agent: survey all clients, return stale list → callback fires
            # research (budgeted + autonomous-stamped at autonomy level >= 2).
            # Revived for the Pi backend in Phase 2 — was dead under backend "pi".
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            svc_url, svc_run_id = await _fire_agent_service(
                "org", org_id,
                brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                task=task, agent_type="monitor",
            )
            await db_module.update_agent_run(
                run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            asyncio.create_task(_watch_agent_service_run(run_id, svc_url, svc_run_id, subject="org"))

        elif agent_type in ("quality_digest", "org") and backend in ("pi", "split"):
            # Pi handles org hygiene and quality digest
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            svc_url, svc_run_id = await _fire_agent_service(
                "org", org_id,
                brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                task=task, agent_type=agent_type,
            )
            await db_module.update_agent_run(
                run_id, "running",
                output={"service_run_id": svc_run_id, "service_url": svc_url},
            )
            asyncio.create_task(_watch_agent_service_run(run_id, svc_url, svc_run_id, subject="org"))

        elif agent_type == "quality_digest":
            from agents._legacy.quality_digest import run_quality_digest
            result = await run_quality_digest(org_id, run_id)
            await db_module.update_agent_run(run_id, "done", output=result)

        elif agent_type == "weekly_digest":
            from routers.notifications import _build_digest_stats
            import notifications as _notify
            stats = await _build_digest_stats(org_id)
            await _notify.notify_org(org_id, _notify.weekly_digest_text(stats), "digest")
            await db_module.update_agent_run(run_id, "done", output=stats)

        elif agent_type == "stale_clients":
            from routers.notifications import _get_stale_clients
            import notifications as _notify
            stale = await _get_stale_clients(org_id, days=30)
            if stale:
                await _notify.notify_org(org_id, _notify.stale_clients_text(stale), "digest")
            await db_module.update_agent_run(run_id, "done", output={"stale_count": len(stale)})

        elif agent_type in ("research", "osint") and backend in ("pi", "hermes", "split"):
            # Route through Pi's orchestrate agent — Pi reads the KB, detects gaps,
            # and calls trigger_run only when needed. Runs are awaited sequentially
            # (one client at a time) to prevent Pi being flooded.
            # Client selection is tiered (focus + stale trickle + news gate) —
            # running every client burned the token budget at scale.
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            clients, selection = await _select_heartbeat_clients(org_id, agent_type)
            # Autonomy seam (Phase 2): at level >= 2 the agent DECIDES per client
            # whether to act (triage on change deltas + KB facts, budgeted,
            # logged); when it acts it runs the gap-reasoning `orchestrate`
            # agent (which itself only calls trigger_run when needed) with
            # trigger_type='autonomous'. Level 0 = legacy research_prep path,
            # byte-for-byte. Level 1 = decisions logged, legacy path executes.
            auto_level = await autonomy.level(org_id)
            triggered = []
            skipped_by_agent = []
            for c in clients:
                child_type = "research_prep"
                child_trigger = "heartbeat"
                if auto_level >= autonomy.LEVEL_OBSERVE:
                    decision = await autonomy.decide(org_id, _heartbeat_decision_ctx(c, agent_type, task))
                    if auto_level >= autonomy.LEVEL_ACT:
                        if not decision.acts:
                            skipped_by_agent.append({"name": c["name"], "reason": decision.reason,
                                                     "review_run_id": decision.review_run_id})
                            continue
                        child_type = "orchestrate"
                        child_trigger = autonomy.TRIGGER
                orch_task = f"Subject: {c['name']}\n\nCustom task hint: {task}"
                child_run_id = await db_module.create_agent_run(
                    org_id=org_id, agent_type=child_type,
                    task=orch_task, trigger_type=child_trigger,
                )
                try:
                    svc_url, svc_run_id = await _fire_agent_service(
                        c["name"], org_id,
                        brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                        task=orch_task, agent_type=child_type,
                    )
                    await db_module.update_agent_run(
                        child_run_id, "running",
                        output={"service_run_id": svc_run_id},
                    )
                    if child_trigger == autonomy.TRIGGER:
                        await autonomy.mark_client_acted(org_id, c["name"])
                    await _watch_agent_service_run(child_run_id, svc_url, svc_run_id, subject=c["name"])
                    triggered.append({"name": c["name"], "run_id": child_run_id,
                                      "autonomous": child_trigger == autonomy.TRIGGER})
                except Exception as fire_exc:
                    await db_module.update_agent_run(child_run_id, "failed", error=str(fire_exc))
                    triggered.append({"name": c["name"], "run_id": child_run_id, "error": str(fire_exc)})
            await db_module.update_agent_run(
                run_id, "done",
                output={"clients_triggered": len(triggered), "triggered": triggered,
                        "selection": selection, "autonomy_level": auto_level,
                        "skipped_by_agent": skipped_by_agent},
            )

        elif agent_type == "match_monitor":
            # Focus clients only — the 10-angle pain-point research + match
            # synthesis is the most expensive job per client. Manual "Run Match"
            # remains available for any client.
            from routers.agents import _maybe_trigger_pain_point_research
            clients = await db_module.list_focus_clients(org_id)
            queued = 0
            for c in clients:
                await _maybe_trigger_pain_point_research(org_id, c["name"])
                queued += 1
            await db_module.update_agent_run(run_id, "done", output={"focus_clients_queued": queued})

        elif agent_type == "focus_osint":
            from routers.agents import _fire_agent_service, _watch_agent_service_run
            focus = await db_module.list_focus_clients(org_id)
            news_gate = bool(context.config.get("news_change_detection", True))
            triggered = []
            for c in focus:
                if news_gate and not await _client_news_changed(org_id, c):
                    continue
                client_task = _NEWS_OSINT_TASK.format(subject=c["name"])
                child_run_id = await db_module.create_agent_run(
                    org_id=org_id, agent_type="osint",
                    task=client_task, trigger_type="heartbeat",
                )
                try:
                    svc_url, svc_run_id = await _fire_agent_service(
                        c["name"], org_id,
                        brain="", model="",   # "" = the choke point decides (org subscription, else config default)
                        task=client_task, agent_type="osint",
                    )
                    await db_module.update_agent_run(
                        child_run_id, "running",
                        output={"service_run_id": svc_run_id},
                    )
                    await _watch_agent_service_run(child_run_id, svc_url, svc_run_id, subject=c["name"])
                    triggered.append({"name": c["name"], "run_id": child_run_id})
                except Exception as fire_exc:
                    await db_module.update_agent_run(child_run_id, "failed", error=str(fire_exc))
                    triggered.append({"name": c["name"], "run_id": child_run_id, "error": str(fire_exc)})
            await db_module.update_agent_run(
                run_id, "done",
                output={"focus_clients": len(triggered), "triggered": triggered},
            )

        elif agent_type == "source_monitor":
            # Daily no-LLM sweep over ALL clients: fingerprint monitored pages,
            # auto-research focus clients on change, badge the rest.
            import notifications as _notify
            clients = await db_module.list_clients(org_id)
            try:
                skip_shared = await db_module.sharing_non_monitor_client_ids(org_id)
            except Exception:
                skip_shared = set()
            summaries = []
            for c in clients:
                if c.get("id") in skip_shared:
                    continue   # another org monitors this shared client
                try:
                    summaries.append(await _monitor_client(org_id, c))
                except Exception as exc:
                    console.print(f"[yellow]source monitor failed for '{c['name']}': {exc}[/yellow]")

            researched = [s for s in summaries if s["researched"]]
            escalated = [s for s in summaries if s["escalated"]]
            flagged = [s for s in summaries if s["flagged"]]
            news_errors = [s for s in summaries if s.get("news_error")]
            discovered = sum(s["discovered"] for s in summaries)
            console.print(
                f"[dim]Source monitor: {len(summaries)} clients checked, "
                f"{len(researched)} researched, {len(escalated)} escalated, "
                f"{len(flagged)} flagged, {len(news_errors)} news scans degraded, "
                f"{discovered} sources discovered[/dim]"
            )
            if researched or flagged or news_errors:
                lines = ["📡 *Source monitor*"]
                if researched:
                    lines.append(
                        "Researched (focus): " + ", ".join(s["client"] for s in researched)
                        + (f" — match re-analysis: {', '.join(s['client'] for s in escalated)}" if escalated else "")
                    )
                if flagged:
                    lines.append(
                        "New info (research manually): " + ", ".join(s["client"] for s in flagged)
                    )
                if news_errors:
                    lines.append(
                        "News scan degraded: " + ", ".join(
                            f"{s['client']} ({s['news_error']})" for s in news_errors
                        )
                    )
                await _notify.notify_org(org_id, "\n".join(lines), "signals")
            await db_module.update_agent_run(
                run_id, "done",
                output={
                    "clients_checked": len(summaries),
                    "researched": [s["client"] for s in researched],
                    "escalated": [s["client"] for s in escalated],
                    "flagged": [s["client"] for s in flagged],
                    "sources_discovered": discovered,
                    "news_errors": [{"client": s["client"], "error": s["news_error"]} for s in news_errors],
                },
            )

        elif agent_type == "nba_queue":
            # Daily next-best-action queue, now PER REP: pre-warm one snapshot per
            # distinct client owner (primary + co-owners) so each seller's
            # Today/home shows their own book. Deterministic scoring + one batched
            # LLM call per rep; snapshots served by /api/next-actions.
            from routers.today import compute_nba_queue
            all_clients = await db_module.list_clients(org_id)
            owners: set[int] = set()
            for c in all_clients:
                if c.get("created_by"):
                    owners.add(int(c["created_by"]))
                meta = c.get("metadata") or {}
                if isinstance(meta, dict):
                    for oid in (meta.get("owner_ids") or []):
                        try:
                            owners.add(int(oid))
                        except (TypeError, ValueError):
                            pass
            ranked = 0
            for oid in sorted(owners):
                snap = await compute_nba_queue(org_id, owner_id=oid)
                ranked += len(snap.get("queue", []))
            await db_module.update_agent_run(
                run_id, "done",
                output={"reps_pre_warmed": len(owners), "clients_ranked": ranked},
            )

        elif agent_type == "market_monitor":
            # Org-level market/industry news: fingerprint curated economics pages,
            # research on change, rotate through client industries, then apply the
            # important developments to the clients they affect.
            summary = await _run_market_monitor(org_id)
            await db_module.update_agent_run(run_id, "done", output=summary)

        elif agent_type == "jobs_monitor":
            # Scan clients' careers pages → list open positions + infer needs →
            # write need findings that feed the match analysis.
            summary = await _run_jobs_monitor(org_id)
            await db_module.update_agent_run(run_id, "done", output=summary)

        elif agent_type == "rep_digest":
            # Per-rep "what's new + top actions" digest → pending docs for the
            # admin to review/send (Insights), plus a Telegram reminder.
            summary = await _build_rep_digests(org_id)
            await db_module.update_agent_run(run_id, "done", output=summary)

        elif agent_type == "task_reminder":
            # Email each rep the to-dos they have due today or overdue.
            from routers.tasks import send_task_reminders
            summary = await send_task_reminders(org_id)
            await db_module.update_agent_run(run_id, "done", output={"summary": summary})

        elif agent_type == "research_qa":
            # Deterministic no-LLM QA reviewer: sample recent agent research and
            # flag stale synthesis, cross-client contamination, and unsourced
            # claims. Flags are written into each doc's metadata + a QA summary doc.
            from agents.research_qa import run_research_qa
            summary = await run_research_qa(org_id, run_id)
            await db_module.update_agent_run(
                run_id, "done",
                output={
                    "output": summary.get("output"),
                    "scanned": summary.get("scanned"),
                    "stale_count": summary.get("stale_count"),
                    "contamination_count": summary.get("contamination_count"),
                    "no_sources_count": summary.get("no_sources_count"),
                },
            )

        elif agent_type == "lessons_review":
            # Self-improving agent (WP4): propose cross-site navigation lessons
            # from this week's site playbooks + failed runs. Never auto-approves —
            # a human decides via POST /api/agents/lessons/{id}/decision.
            import playbook
            summary = await playbook.lessons_propose(org_id, run_id=run_id)
            await db_module.update_agent_run(
                run_id, "done",
                output={"proposed": len(summary.get("proposed") or [])},
            )

        else:
            from agents.runner import run_agent
            await run_agent(run_id, org_id, agent_type, task)

        await db_module.update_heartbeat_last_run(hb_id)
    except Exception as exc:
        console.print(f"[yellow]Heartbeat job {hb_id} ({agent_type}) failed: {exc}[/yellow]")


async def _start_heartbeat_scheduler() -> None:
    """Load heartbeat rows from DB and start APScheduler. Mutates context._scheduler."""
    if not SCHEDULER_AVAILABLE:
        console.print("  [yellow]APScheduler not installed — heartbeats disabled[/yellow]")
        return

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    context._scheduler = AsyncIOScheduler()

    job_count = 0
    if DB_AVAILABLE:
        try:
            # Seed any heartbeat types added after the org was created — the
            # explicit existing_types check is the idempotency guard (the
            # heartbeats table has no unique constraint on org_id+agent_type).
            late_additions = {
                "source_monitor": ("0 6 * * *",
                    "Daily no-LLM sweep: fingerprint every client's monitored news/press "
                    "pages; auto-research focus clients on change, flag the rest with a "
                    "New-info badge."),
                "nba_queue": ("30 7 * * *",
                    "Compute the daily next-best-action queue: score every client from "
                    "fresh signals, source changes, open outreach, and staleness; write "
                    "the ranked snapshot with reasons."),
                "market_monitor": ("0 5 * * *",
                    "Fingerprint curated economics/industry news pages; on change research "
                    "the development, rotate through client industries, write market signals, "
                    "and apply the important ones to the clients they affect."),
                "jobs_monitor": ("0 4 * * 1",
                    "Scan clients' careers pages for open positions; list them and infer "
                    "what the company likely needs from the role mix, feeding the match "
                    "analysis."),
                "rep_digest": ("0 8 */2 * *",
                    "Every 2 days: build a short per-rep client digest (what's new + top "
                    "next actions) for each seller; store as pending for admin review/send "
                    "and Telegram-remind the admin."),
                "task_reminder": ("0 8 * * *",
                    "Email each rep the tasks they have due today or overdue, so follow-ups "
                    "don't slip."),
                "research_qa": ("30 6 * * *",
                    "Sample recent agent-written research and flag quality problems (no LLM): "
                    "stale synthesis that lags newer findings, cross-client contamination, and "
                    "claims with no sources. Write flags into each doc + a QA summary report."),
                "lessons_review": ("0 7 * * 1",
                    "Propose cross-site navigation lessons from this week's playbooks and "
                    "failed runs (human approval required)."),
            }
            # every org (multi-tenant): each existing org gets the types it lacks
            for org_row in await db_module.list_orgs():
                org_id = org_row["id"]
                existing_types = {h["agent_type"] for h in await db_module.list_all_heartbeats(org_id)}
                for hb_type, (cron_expr, hb_task) in late_additions.items():
                    if hb_type not in existing_types:
                        await db_module.create_heartbeat(org_id, hb_type, cron_expr, hb_task)
                        console.print(f"  [green]{hb_type} heartbeat created for org {org_id} ({cron_expr})[/green]")
        except Exception as exc:
            console.print(f"  [yellow]Could not seed late-addition heartbeats: {exc}[/yellow]")
        try:
            heartbeats = await db_module.list_heartbeats()
            for hb in heartbeats:
                parts = hb["cron_expr"].split()
                if len(parts) == 5:
                    minute, hour, day, month, day_of_week = parts
                    context._scheduler.add_job(
                        _run_heartbeat_job,
                        "cron",
                        id=f"hb_{hb['id']}",
                        args=[hb["id"], hb["org_id"], hb["agent_type"], hb["task"]],
                        minute=minute, hour=hour, day=day,
                        month=month, day_of_week=day_of_week,
                        misfire_grace_time=60,
                    )
                    job_count += 1
        except Exception as exc:
            console.print(f"  [yellow]Could not load heartbeats: {exc}[/yellow]")

    # Supervised-outreach send worker (Phase 3): claims approved mails one at
    # a time under the org guardrails. Cheap when nothing is approved.
    try:
        from routers.outreach import worker_tick as _outreach_tick
        context._scheduler.add_job(_outreach_tick, "interval", id="outreach_worker",
                                   minutes=1, misfire_grace_time=30, coalesce=True)
        job_count += 1
    except Exception as exc:
        console.print(f"  [yellow]Outreach worker not scheduled: {exc}[/yellow]")
    # Intake sweeper (WP5): reconciles lost callbacks and closes out collection
    # points whose deadline/absolute cap has passed. Cheap when nothing is open.
    try:
        import intake
        context._scheduler.add_job(intake.sweep, "interval", id="intake_sweeper",
                                   seconds=60, coalesce=True, max_instances=1,
                                   misfire_grace_time=30)
        job_count += 1
    except Exception as exc:
        console.print(f"  [yellow]Intake sweeper not scheduled: {exc}[/yellow]")
    try:
        import imap_sync as _imap
        context._scheduler.add_job(_imap.poll_once, "interval", id="imap_sync",
                                   minutes=5, misfire_grace_time=60, coalesce=True)
        job_count += 1
    except Exception as exc:
        console.print(f"  [yellow]IMAP sync not scheduled: {exc}[/yellow]")
    # Telegram bot inbound (Phase 6b): poll getUpdates when no webhook is configured
    # (links chats to users via /start <code>; answers linked users from their org).
    try:
        import notifications as _tg
        if _tg.polling_enabled():
            context._scheduler.add_job(_tg.poll_updates, "interval", id="telegram_poll",
                                       seconds=15, misfire_grace_time=20, coalesce=True, max_instances=1)
            job_count += 1
    except Exception as exc:
        console.print(f"  [yellow]Telegram polling not scheduled: {exc}[/yellow]")
    # Shared clients (Phase 6a): drain the sync outbox filled by DB triggers.
    try:
        import sharing as _sharing
        context._scheduler.add_job(_sharing.process_outbox, "interval", id="sharing_outbox",
                                   seconds=20, misfire_grace_time=30, coalesce=True, max_instances=1)
        job_count += 1
    except Exception as exc:
        console.print(f"  [yellow]Sharing outbox worker not scheduled: {exc}[/yellow]")
    # Retention prune (nightly): strips agent_runs.tool_calls payloads and deletes
    # aged agent_runs / prompt_log rows. Telemetry only — knowledge is never pruned.
    try:
        import retention as _retention
        _ret = _retention.get_settings(context.config)
        if _ret["enabled"]:
            _parts = _ret["cron"].split()
            if len(_parts) != 5:
                raise ValueError(f"retention.cron must be 5 parts, got {_ret['cron']!r}")
            _min, _hr, _day, _mon, _dow = _parts
            context._scheduler.add_job(
                _retention.run_retention, "cron", id="retention_prune",
                minute=_min, hour=_hr, day=_day, month=_mon, day_of_week=_dow,
                misfire_grace_time=3600, coalesce=True, max_instances=1,
                replace_existing=True,
            )
            job_count += 1
        else:
            console.print("  [dim]Retention prune disabled (retention.enabled: false)[/dim]")
    except Exception as exc:
        console.print(f"  [yellow]Retention prune not scheduled: {exc}[/yellow]")

    context._scheduler.start()
    console.print(f"  Heartbeat scheduler: [green]{job_count} job(s) loaded[/green]")


# ---------------------------------------------------------------------------
# Heartbeat API
# ---------------------------------------------------------------------------

@router.get("/api/heartbeats/types")
async def list_heartbeat_types(user: dict = Depends(current_user)):
    """Return the valid agent_type values and their human-readable names."""
    return {"types": [{"value": k, "label": v} for k, v in _HB_NAMES.items()]}


@router.get("/api/heartbeats")
async def list_heartbeat_jobs(user: dict = Depends(current_user)):
    """Return all heartbeat jobs for the user's org with last/next run times."""
    if not DB_AVAILABLE:
        return {"heartbeats": []}
    hbs = await db_module.list_all_heartbeats(user["org_id"])
    result = []
    for hb in hbs:
        job_id = f"hb_{hb['id']}"
        next_fire = None
        if context._scheduler:
            try:
                job = context._scheduler.get_job(job_id)
                if job and job.next_fire_time:
                    next_fire = job.next_fire_time.isoformat()
            except Exception:
                pass
        result.append({
            "id": hb["id"],
            "agent_type": hb["agent_type"],
            "name": _HB_NAMES.get(hb["agent_type"], hb["agent_type"]),
            "cron_expr": hb["cron_expr"],
            "task": hb["task"],
            "enabled": hb["enabled"],
            "last_run_at": hb["last_run_at"].isoformat() if hb.get("last_run_at") else None,
            "next_run_at": next_fire,
        })
    return {"heartbeats": result}


@router.post("/api/heartbeats")
async def create_heartbeat_job(body: dict, user: dict = Depends(current_user)):
    """Create a new heartbeat job and register it with APScheduler."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    agent_type = (body.get("agent_type") or "").strip()
    cron_expr  = (body.get("cron_expr") or "").strip()
    task       = (body.get("task") or "").strip()
    enabled    = bool(body.get("enabled", True))
    if not all([agent_type, cron_expr, task]):
        raise HTTPException(400, "agent_type, cron_expr, and task are required")
    parts = cron_expr.split()
    if len(parts) != 5:
        raise HTTPException(400, "cron_expr must be 5 space-separated parts (e.g. '0 8 * * 1-5')")
    hb = await db_module.create_heartbeat(user["org_id"], agent_type, cron_expr, task, enabled)
    if enabled and context._scheduler and SCHEDULER_AVAILABLE:
        minute, hour, day, month, dow = parts
        context._scheduler.add_job(
            _run_heartbeat_job,
            "cron",
            id=f"hb_{hb['id']}",
            args=[hb["id"], hb["org_id"], hb["agent_type"], hb["task"]],
            minute=minute, hour=hour, day=day, month=month, day_of_week=dow,
            misfire_grace_time=60,
            replace_existing=True,
        )
    return {"ok": True, "heartbeat": {k: str(v) if hasattr(v, 'isoformat') else v for k, v in hb.items()}}


@router.patch("/api/heartbeats/{hb_id}")
async def update_heartbeat_job(hb_id: int, body: dict, user: dict = Depends(current_user)):
    """Update a heartbeat job and re-register with APScheduler."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    existing = await db_module.get_heartbeat(hb_id, user["org_id"])
    if not existing:
        raise HTTPException(404, "Heartbeat not found")
    agent_type = (body.get("agent_type") or existing["agent_type"]).strip()
    cron_expr  = (body.get("cron_expr") or existing["cron_expr"]).strip()
    task       = (body.get("task") or existing["task"]).strip()
    enabled    = body.get("enabled") if "enabled" in body else existing["enabled"]
    parts = cron_expr.split()
    if len(parts) != 5:
        raise HTTPException(400, "cron_expr must be 5 space-separated parts")
    hb = await db_module.update_heartbeat(hb_id, user["org_id"], agent_type, cron_expr, task, enabled)
    if not hb:
        raise HTTPException(404, "Heartbeat not found")
    job_id = f"hb_{hb_id}"
    if context._scheduler and SCHEDULER_AVAILABLE:
        try:
            context._scheduler.remove_job(job_id)
        except Exception:
            pass
        if enabled:
            minute, hour, day, month, dow = parts
            context._scheduler.add_job(
                _run_heartbeat_job,
                "cron",
                id=job_id,
                args=[hb["id"], hb["org_id"], hb["agent_type"], hb["task"]],
                minute=minute, hour=hour, day=day, month=month, day_of_week=dow,
                misfire_grace_time=60,
                replace_existing=True,
            )
    return {"ok": True, "heartbeat": {k: str(v) if hasattr(v, 'isoformat') else v for k, v in hb.items()}}


@router.delete("/api/heartbeats/{hb_id}")
async def delete_heartbeat_job(hb_id: int, user: dict = Depends(current_user)):
    """Delete a heartbeat job and remove it from APScheduler."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    deleted = await db_module.delete_heartbeat(hb_id, user["org_id"])
    if not deleted:
        raise HTTPException(404, "Heartbeat not found")
    if context._scheduler and SCHEDULER_AVAILABLE:
        try:
            context._scheduler.remove_job(f"hb_{hb_id}")
        except Exception:
            pass
    return {"ok": True}


@router.post("/api/heartbeats/{hb_id}/run")
async def run_heartbeat_now(hb_id: int, user: dict = Depends(current_user)):
    """Trigger immediate one-shot execution of a heartbeat job (including disabled ones)."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    hb = await db_module.get_heartbeat(hb_id, user["org_id"])
    if not hb:
        raise HTTPException(404, "Heartbeat not found")
    # Note the current latest run so the UI can wait for the NEW one it triggers.
    prev_run_id = 0
    if db_module._pool:
        async with db_module._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT max(id) AS m FROM agent_runs WHERE org_id=$1 AND agent_type=$2 AND trigger_type='heartbeat'",
                hb["org_id"], hb["agent_type"],
            )
        prev_run_id = (row and row["m"]) or 0
    asyncio.create_task(_run_heartbeat_job(hb["id"], hb["org_id"], hb["agent_type"], hb["task"]))
    return {"ok": True, "agent_type": hb["agent_type"], "prev_run_id": prev_run_id}


@router.get("/api/heartbeats/{hb_id}/last-run")
async def get_heartbeat_last_run(hb_id: int, user: dict = Depends(current_user)):
    """Return the most recent agent_run triggered by this heartbeat job."""
    if not DB_AVAILABLE or not db_module._pool:
        raise HTTPException(503, "DB unavailable")
    hbs = await db_module.list_heartbeats(org_id=user["org_id"])
    hb = next((h for h in hbs if h["id"] == hb_id), None)
    if not hb:
        raise HTTPException(404, "Heartbeat not found")
    import json as _json
    async with db_module._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, agent_type, status, task, tool_calls, output, error,
                   created_at, completed_at
            FROM agent_runs
            WHERE org_id = $1
              AND trigger_type = 'heartbeat'
              AND agent_type = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user["org_id"], hb["agent_type"],
        )
    if not row:
        return {"run": None}
    d = dict(row)
    for field in ("tool_calls", "output"):
        if isinstance(d.get(field), str):
            try:
                d[field] = _json.loads(d[field])
            except Exception:
                d[field] = []
    d["created_at"] = str(d["created_at"]) if d.get("created_at") else None
    d["completed_at"] = str(d["completed_at"]) if d.get("completed_at") else None
    return {"run": d}


# ---------------------------------------------------------------------------
# Market-news monitoring config + manual trigger
# ---------------------------------------------------------------------------

@router.get("/api/market/sources")
async def get_market_sources(user: dict = Depends(current_user)):
    """The org's curated market-news sources + last-check status."""
    if not DB_AVAILABLE:
        return {"sources": []}
    cfg = await db_module.get_market_config(user["org_id"])
    return {"sources": cfg.get("sources") or [], "defaults": _DEFAULT_MARKET_SOURCES}


@router.put("/api/market/sources")
async def put_market_sources(body: dict, user: dict = Depends(current_user)):
    """Replace the curated market-news source list (admin only)."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    incoming = body.get("sources")
    if not isinstance(incoming, list):
        raise HTTPException(400, "sources must be a list")
    cfg = await db_module.get_market_config(user["org_id"])
    existing = {s.get("url"): s for s in (cfg.get("sources") or [])}
    cleaned = []
    for s in incoming:
        url = (s.get("url") or "").strip() if isinstance(s, dict) else ""
        if not url.startswith("http"):
            continue
        prev = existing.get(url, {})
        cleaned.append({
            "url": url,
            "label": (s.get("label") or prev.get("label") or url).strip(),
            # preserve fingerprint/timestamps so re-saving doesn't reset change detection
            "last_fp": prev.get("last_fp"),
            "last_checked_at": prev.get("last_checked_at"),
            "last_changed_at": prev.get("last_changed_at"),
        })
    cfg["sources"] = cleaned
    cfg["seeded"] = True
    await db_module.save_market_config(user["org_id"], cfg)
    return {"sources": cleaned}


@router.post("/api/market/check")
async def check_market_now(user: dict = Depends(current_user)):
    """Run the market monitor immediately (admin only). Fires Pi research."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    summary = await _run_market_monitor(user["org_id"])
    return {"ok": True, **summary}


@router.post("/api/market/apply")
async def apply_market_now(user: dict = Depends(current_user)):
    """Re-run only the apply-to-clients mapping over existing market signals
    (admin only). No Pi research — just the industry shortlist + LLM confirm."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    written = await _apply_market_signals(user["org_id"])
    return {"ok": True, "clients_tagged": written}


# ---------------------------------------------------------------------------
# Per-client open positions (jobs)
# ---------------------------------------------------------------------------

@router.get("/api/clients/{name}/jobs")
async def get_client_jobs(name: str, user: dict = Depends(current_user)):
    """Latest scanned open positions + inferred needs for a client."""
    if not DB_AVAILABLE or not db_module._pool:
        return {"positions": [], "inferred_needs": [], "careers_url": "", "last_scanned": None}
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(404, "Client not found")
    async with db_module._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM documents WHERE org_id=$1 AND doc_id=$2",
            user["org_id"], f"jobs-{client['id']}",
        )
    meta = (row["metadata"] if row else None) or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return {
        "positions": meta.get("positions") or [],
        "inferred_needs": meta.get("inferred_needs") or [],
        "needs_mapped": meta.get("needs_mapped") or [],
        "careers_url": meta.get("careers_url") or (client.get("metadata") or {}).get("careers_url", ""),
        "last_scanned": meta.get("last_scanned"),
    }


@router.post("/api/clients/{name}/jobs/scan")
async def scan_client_jobs(name: str, body: dict = None, user: dict = Depends(current_user)):
    """Scan this client's careers page now (fetch + extract + infer needs).
    Optional body {careers_url} sets/overrides the page."""
    if not DB_AVAILABLE:
        raise HTTPException(503, "DB unavailable")
    client = await db_module.get_client(user["org_id"], name)
    if not client:
        raise HTTPException(404, "Client not found")
    careers_url = (body or {}).get("careers_url", "") if isinstance(body, dict) else ""
    summary = await _scan_client_jobs(user["org_id"], client, careers_url=careers_url)
    return {"ok": True, **summary}


# ---------------------------------------------------------------------------
# Pipeline sweep
# ---------------------------------------------------------------------------

async def _pipeline_sweep() -> None:
    """Find staged sessions not yet promoted and trigger enrichment or retry."""
    staged_dir = BASE_DIR / "data" / "staged"
    if not staged_dir.exists():
        return

    org_id: Optional[int] = None
    if DB_AVAILABLE:
        try:
            org = await db_module.get_first_org()
            org_id = org["id"] if org else None
        except Exception:
            pass

    triggered = 0
    loop = asyncio.get_event_loop()
    for session_dir in staged_dir.iterdir():
        if not session_dir.is_dir():
            continue
        meta   = _read_session_metadata(session_dir.name)
        status = (meta or {}).get("status", "staged")

        if status in ("promoted", "agent_working"):
            continue

        # Skip if Ollama is still writing the summary
        if not (BASE_DIR / "data" / "staged" / session_dir.name / "summary.md").exists():
            continue

        if status in ("staged", "failed"):
            asyncio.create_task(_trigger_enrichment(session_dir.name, (meta or {}).get("org_id") or org_id))
            triggered += 1
        elif status == "agent_done":
            # Agent finished but promotion failed — retry promote only
            asyncio.create_task(loop.run_in_executor(executor, _promote_session, session_dir.name))
            triggered += 1

    if triggered:
        console.print(f"  [dim]Pipeline sweep: triggered {triggered} session(s)[/dim]")


async def _pipeline_sweep_loop() -> None:
    """Run _pipeline_sweep on startup (after DB init) then on a configurable interval."""
    await asyncio.sleep(10)  # let DB pool finish initialising
    while True:
        try:
            await _pipeline_sweep()
        except Exception as e:
            console.print(f"[yellow]Pipeline sweep error: {e}[/yellow]")
        interval_s = int(config.get("pipeline_sweep_interval_min", 10)) * 60
        await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@router.get("/api/pipeline/staged")
async def list_pipeline_staged():
    staged_dir = BASE_DIR / "data" / "staged"
    if not staged_dir.exists():
        return {"sessions": []}
    sessions = []
    for session_dir in sorted(staged_dir.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        meta = _read_session_metadata(session_dir.name)
        sessions.append(
            meta or {"session_id": session_dir.name, "status": "staged", "title": None, "created_at": None}
        )
    return {"sessions": sessions}


@router.get("/api/pipeline/staged/{session_id}")
async def get_pipeline_session(session_id: str):
    meta = _read_session_metadata(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Session not found")
    return meta


@router.get("/api/transcript/session/{session_id}")
async def get_transcript_session(session_id: str):
    """Poll processing status of an ingest session. Used by external ingest clients for reconnect."""
    meta = _read_session_metadata(session_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Session not found")
    return meta


@router.post("/api/pipeline/staged/{session_id}/promote")
async def promote_pipeline_session(session_id: str):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(executor, _promote_session, session_id)
    except Exception as e:
        console.print(f"[red]Promote error for {session_id}: {e}[/red]")
        return {"ok": False, "error": str(e)}


@router.post("/api/export")
async def export_to_obsidian(body: dict):
    """Backward-compat endpoint — delegates to _promote_session."""
    session_id = body.get("session_id", "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(executor, _promote_session, session_id)
    except Exception as e:
        console.print(f"[red]Export error: {e}[/red]")
        return {"ok": False, "error": str(e)}


class _TranscriptChunk(BaseModel):
    session_id: str
    transcript_chunk: str = ""
    is_final: bool = False
    speaker_label: Optional[str] = None


@router.post("/api/transcript/ingest")
async def ingest_transcript_chunk(body: _TranscriptChunk, request: Request):
    """Receive a transcript chunk from an external ingest client.

    Chunks are buffered in memory. On is_final=True the full transcript is staged
    and the standard enrichment pipeline fires.
    Auth: Bearer user session token (same as current_user). Dev-mode open when DB unavailable
    and agent_service_token is blank.
    """
    auth_header = request.headers.get("Authorization", "")
    org_id: Optional[int] = None
    user: Optional[dict] = None

    if auth_header.startswith("Bearer "):
        raw_token = auth_header.removeprefix("Bearer ").strip()
        if DB_AVAILABLE:
            user = await db_module.get_user_by_token(raw_token)
            if not user:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            org_id = user["org_id"]
    elif config.get("agent_service_token", ""):
        raise HTTPException(status_code=401, detail="Authorization required")

    if org_id is None:
        org_id = await context._default_org_id()

    session_id = body.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    chunk = body.transcript_chunk.strip()
    if chunk:
        _transcript_buffers.setdefault(session_id, []).append(chunk)

    if not body.is_final:
        return {"ok": True}

    # Final chunk — flush buffer, write to disk, stage, trigger pipeline
    chunks = _transcript_buffers.pop(session_id, [])
    if not chunks:
        return {"ok": True, "session_id": session_id, "warning": "no chunks buffered"}

    now = datetime.now(timezone.utc)
    server_session_id = now.strftime("%Y%m%d-%H%M%S")

    raw_dir    = BASE_DIR / "data" / "raw"    / server_session_id
    staged_dir = BASE_DIR / "data" / "staged" / server_session_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    staged_dir.mkdir(parents=True, exist_ok=True)

    (raw_dir / "transcript.txt").write_text("\n".join(chunks), encoding="utf-8")
    _write_session_metadata(server_session_id, {
        "session_id":       server_session_id,
        "status":           "staged",
        "created_at":       now.isoformat(),
        "duration_s":       0,
        "speakers":         1,
        "language":         config.get("language", "en"),
        "title":            None,
        "visibility":       "shared",
        "entities":         None,
        "agent_run_id":     None,
        "promoted_at":      None,
        "error":            None,
        "source":           "app",
        "created_by":       user["id"] if user else None,
        "created_by_name":  user.get("display_name") or user.get("username") if user else None,
    })

    asyncio.create_task(_trigger_enrichment(server_session_id, org_id))

    console.print(f"[cyan]App transcript staged: {server_session_id} ({len(chunks)} chunk(s))[/cyan]")
    return {"ok": True, "session_id": server_session_id}


@router.post("/api/sessions/text")
async def create_text_session(body: dict, user: dict = Depends(current_user)):
    """Accept a typed/pasted transcript, stage it, and kick off the enrichment pipeline
    for the caller's org (multi-tenant: never a deployment-wide default org)."""
    text = body.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}

    title      = body.get("title", "").strip() or None
    language   = body.get("language", "en") or "en"
    visibility = body.get("visibility", "shared")

    now        = datetime.now(timezone.utc)
    session_id = now.strftime("%Y%m%d-%H%M%S")

    raw_dir    = BASE_DIR / "data" / "raw"    / session_id
    staged_dir = BASE_DIR / "data" / "staged" / session_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    staged_dir.mkdir(parents=True, exist_ok=True)

    (raw_dir / "transcript.txt").write_text(text, encoding="utf-8")
    _write_session_metadata(session_id, {
        "session_id": session_id, "status": "staged",
        "created_at": now.isoformat(), "duration_s": 0,
        "speakers": 1, "language": language,
        "title": title, "visibility": visibility, "entities": None, "agent_run_id": None,
    })

    org_id = user["org_id"]
    asyncio.create_task(_trigger_enrichment(session_id, org_id))

    console.print(f"[cyan]Text session created: {session_id}[/cyan]")
    return {"ok": True, "session_id": session_id}
