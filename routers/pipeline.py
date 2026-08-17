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
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
    _model_cache,
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

router = APIRouter()

# In-memory buffer for app-sourced transcript chunks, keyed by client session_id
_transcript_buffers: dict[str, list[str]] = {}

_NEWS_OSINT_TASK = (
    "News and signal scan for {subject}: find the most recent developments from the last 60 days. "
    "Search for latest news, press releases, leadership changes, strategic announcements, M&A activity, "
    "earnings results, and industry signals. Write individual findings (type='finding') as you go. "
    "End with a summary signal report (type='osint'). Only include events with verifiable source URLs."
)

# Market/industry-wide news scan (not tied to one client). Pi writes scope='market'
# signals tagged by industry; high-relevance ones get applied to clients afterwards.
_MARKET_NEWS_TASK = (
    "Scan for {focus}: the most important developments from roughly the last 14 days — regulation and "
    "compliance changes, market shifts, major M&A, funding rounds, macro/sector trends, and technology "
    "disruption. Search broadly and fetch the most credible sources (Reuters, Bloomberg, Handelsblatt, "
    "FT, official regulators). For each significant development, write a type='signal' document with "
    "scope='market', industry set to the sector it concerns, metadata.relevance_score (1-5; 5 = "
    "sector-defining), and source_url set to the originating article. If a development scores 4 or "
    "higher, research it deeper before writing. Do NOT tie these to a single company. End with a short "
    "type='osint' summary. Only include developments with verifiable source URLs."
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


def _call_pipeline_brain(prompt: str) -> str:
    """Call the configured pipeline brain with a plain-text prompt. Returns the text response.

    Provider/model come from the llm.py "pipeline" role (config llm: block, or
    legacy pipeline_brain/pipeline_model keys).
    Returns empty string on failure — pipeline callers handle missing output gracefully.
    """
    try:
        return llm.complete(prompt, role="pipeline", timeout=120)
    except Exception as e:
        console.print(f"[yellow]Pipeline brain failed: {e}[/yellow]")
        return ""


def extract_entities(transcript: str) -> dict:
    """Call Ollama to extract companies, people, and topics from a transcript.

    Companies are returned as [{"name": str, "confidence": str}].
    People are returned as [{"name": str, "role": str, "confidence": str}].
    Falls back to empty arrays on any failure — never raises.
    """
    prompt = ENTITY_EXTRACTION_PROMPT.format(transcript=transcript)
    try:
        raw = _call_pipeline_brain(prompt) or "{}"
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


def _generate_summary(transcript: str, language: str) -> str:
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
    result = _call_pipeline_brain(prompt)
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
        entities = extract_entities(transcript_text)

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
            org = db_module._run_coro_from_thread(db_module.get_first_org())
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
                "promoted_at": None, "error": None,
            })
        transcript_path = BASE_DIR / "data" / "raw"    / session_id / "transcript.txt"
        summary_path    = BASE_DIR / "data" / "staged" / session_id / "summary.md"
        if not transcript_path.exists():
            return
        transcript_text = transcript_path.read_text(encoding="utf-8")
        if not summary_path.exists():
            lang = (_read_session_metadata(session_id) or {}).get("language", "en") or "en"
            summary_text = _generate_summary(transcript_text, lang)
            summary_path.write_text(summary_text, encoding="utf-8")
        else:
            summary_text = summary_path.read_text(encoding="utf-8")
        title    = extract_title_from_summary(summary_text) if summary_text else "Untitled"
        entities = extract_entities(transcript_text)
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
                brain=config.get("agent_service_brain", "openrouter"),
                model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
                brain=config.get("agent_service_brain", "openrouter"),
                model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
                brain=context.config.get("agent_service_brain", "openrouter"),
                model=context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
                brain=config.get("agent_service_brain", "openrouter"),
                model=config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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

async def _searxng_results(query: str, limit: int = 10) -> list[dict]:
    """Raw SearXNG JSON results — shared by news gate + source discovery. [] on failure."""
    searxng_url = context.config.get("searxng_url", "http://localhost:8080").rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(
            f"{searxng_url}/search",
            params={"q": query, "format": "json", "safesearch": 0},
        )
        resp.raise_for_status()
        return (resp.json().get("results") or [])[:limit]


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


async def _openrouter_pick_website(client_name: str, candidates: list[dict]) -> str:
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
        answer = (await llm.acomplete(prompt, role="pipeline", timeout=30)).lower()
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
        domain = await _openrouter_pick_website(name, ordered[:8])
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


async def _discover_client_sources(org_id: int, client: dict) -> list[dict]:
    """Find newsroom/press pages for a client via SearXNG heuristics — no LLM.

    Prefers pages on the client's own domain; requires a news-ish keyword in the
    URL. Merges into any existing (user-added) sources, caps at
    _MAX_MONITORED_SOURCES, and stamps sources_discovered_at.
    """
    meta = client.get("metadata") or {}
    existing = list(meta.get("monitored_sources") or [])
    seen = {_normalize_source_url(s.get("url", "")) for s in existing}

    # No website on record → resolve it first (heuristic, LLM fallback) so the
    # precise own-domain discovery query can run
    if not (meta.get("website") or "").strip():
        website = await _resolve_client_website(org_id, client)
        if website:
            meta["website"] = website
            client["metadata"] = meta

    domain = _client_domain(client)
    name = client["name"]

    queries = [f'"{name}" newsroom press releases', f'"{name}" pressemitteilungen news']
    if domain:
        queries.insert(0, f"site:{domain} news press")

    candidates: list[tuple[int, str, str]] = []
    for q in queries:
        try:
            results = await _searxng_results(q)
        except Exception:
            continue
        for r in results:
            url = (r.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            path = (parsed.path or "").lower()
            if not any(k in path or k in host for k in _SOURCE_KEYWORDS):
                continue
            own_domain = bool(domain) and (host == domain or host.endswith("." + domain))
            candidates.append((0 if own_domain else 1, url, (r.get("title") or "")[:60]))

    now_iso = datetime.now(timezone.utc).isoformat()
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
    return merged


async def _fetch_page_text(url: str, max_chars: int = 9000, wait_ms: int = 1500) -> str:
    """Fetch a page's visible text. Plain GET first; falls back to the
    browser-service for JS-rendered pages (ATS/careers pages often need it —
    pass a larger wait_ms so the job listings have time to load).
    Returns normalized text (possibly '')."""
    text = ""
    try:
        async with httpx.AsyncClient(
            timeout=12.0, follow_redirects=True, headers={"User-Agent": _SOURCE_UA},
        ) as http:
            resp = await http.get(url)
            if resp.status_code == 200:
                text = _HTML_TAG_RE.sub(" ", resp.text)
    except Exception:
        pass

    if len(text.strip()) < 500:
        browser_url = os.environ.get("BROWSER_SERVICE_URL", "http://localhost:3000").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=40.0) as http:
                resp = await http.post(
                    f"{browser_url}/fetch", json={"url": url, "max_chars": max_chars, "wait_ms": wait_ms},
                )
                if resp.status_code == 200:
                    text = resp.json().get("text", "")
        except Exception:
            pass

    return re.sub(r"\s+", " ", text).strip()


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
    "the page the development came from — so the reader can open the original article."
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
            brain=context.config.get("agent_service_brain", "openrouter"),
            model=context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
        "researched": False, "escalated": False, "flagged": False,
    }

    sources = list(meta.get("monitored_sources") or [])
    if not sources and _discovery_marker_stale(meta):
        sources = await _discover_client_sources(org_id, client)
        summary["discovered"] = len(sources)

    # Virtual "news search" source — only counts as a change when a baseline
    # fingerprint already existed (first sweep sets baselines, no storm).
    had_news_fp = meta.get("news_fp") is not None
    if await _client_news_changed(org_id, client, fail_open=False) and had_news_fp:
        summary["changed"].append("news search")

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

    loop = asyncio.get_running_loop()
    from routers.knowledge import _call_brain_sync
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
            reply = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
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


async def _run_market_monitor(org_id: int) -> dict:
    """Market/industry news monitor — the org-level analogue of source_monitor.

    Fingerprints curated economics/news pages; when one changes, fires a Pi
    market_news run on the general news. Also rotates through the distinct
    industries of the org's clients, scanning a few per run. Pi writes
    scope='market' signals tagged by industry; the apply-to-clients mapping
    (industry shortlist + LLM confirm) runs afterwards via _apply_market_signals.
    """
    from routers.agents import _fire_agent_service, _watch_agent_service_run

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

    brain = context.config.get("agent_service_brain", "openrouter")
    model = context.config.get("agent_service_model", "deepseek/deepseek-v4-flash")
    fired: list[dict] = []

    async def _fire(focus: str, industry: str, subject: str) -> None:
        task = _MARKET_NEWS_TASK.format(focus=focus)
        child = await db_module.create_agent_run(
            org_id=org_id, agent_type="market_news", task=task, trigger_type="heartbeat",
        )
        try:
            svc_url, svc_run = await _fire_agent_service(
                subject, org_id, brain=brain, model=model, task=task, agent_type="market_news",
            )
            await db_module.update_agent_run(child, "running", output={"service_run_id": svc_run})
            await _watch_agent_service_run(child, svc_url, svc_run, subject=subject)
            fired.append({"focus": focus, "industry": industry, "run_id": child})
        except Exception as exc:
            await db_module.update_agent_run(child, "failed", error=str(exc))
            fired.append({"focus": focus, "industry": industry, "run_id": child, "error": str(exc)})

    for src in changed:
        label = src.get("label") or src.get("url")
        await _fire(
            f"general business and economics news (triggered by an update on {label})",
            "", f"market: {label}",
        )
    for industry in picked:
        await _fire(f"the {industry} sector", industry, f"market: {industry}")
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

_CAREERS_KEYS = ("career", "careers", "jobs", "job", "stellen", "karriere",
                 "vacanc", "join-us", "positions", "joboffers", "jobangebote")

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


async def _discover_careers_url(org_id: int, client: dict) -> str:
    """Find a client's careers/jobs page. Gathers SearXNG candidates, then lets
    the LLM pick the company's official open-positions listing (it's better at
    spotting the right page than URL heuristics); falls back to own-domain /
    careers-ish ranking if the LLM is unavailable or unsure."""
    meta = client.get("metadata") or {}
    if not (meta.get("website") or "").strip():
        website = await _resolve_client_website(org_id, client)
        if website:
            meta["website"] = website
            client["metadata"] = meta
    domain = _client_domain(client)
    name = client["name"]
    queries = [f'"{name}" careers open positions', f'"{name}" jobs karriere stellenangebote']
    if domain:
        queries.insert(0, f"site:{domain} careers jobs stellen")

    candidates: list[dict] = []
    seen: set = set()
    for q in queries:
        try:
            results = await _searxng_results(q)
        except Exception:
            continue
        for r in results:
            url = (r.get("url") or "").strip()
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            candidates.append({"url": url, "title": (r.get("title") or "")[:120]})
    if not candidates:
        return ""

    # LLM picks the best careers/open-positions URL from the candidates.
    loop = asyncio.get_running_loop()
    from routers.knowledge import _call_brain_sync
    listing = "\n".join(f"{i+1}. {c['title']} — {c['url']}" for i, c in enumerate(candidates[:15]))
    prompt = (
        f"Which of these URLs is {name}'s official careers / open-positions listing page "
        f"(where you can browse their current job openings)? Prefer a page on the company's own "
        f"domain{(' (' + domain + ')') if domain else ''} or its official applicant-tracking system "
        f"(e.g. Personio, Greenhouse, SuccessFactors, Workday). Reply with ONLY the single best URL, "
        f"or 'none' if none qualify.\n\n{listing}"
    )
    try:
        reply = (await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))).strip()
        m = re.search(r"https?://\S+", reply)
        if m:
            picked = m.group(0).rstrip(").,>\"'")
            if any(picked == c["url"] for c in candidates) or (domain and domain in picked):
                return picked
    except Exception as exc:
        console.print(f"[yellow]careers-url LLM pick failed for {name}: {exc}[/yellow]")

    # Heuristic fallback: own-domain + careers-ish keyword.
    ranked: list[tuple[int, str]] = []
    for c in candidates:
        url = c["url"]
        host = urlparse(url).netloc.lower().replace("www.", "")
        score = (2 if domain and domain in host else 0) + (1 if any(k in url.lower() for k in _CAREERS_KEYS) else 0)
        if score:
            ranked.append((score, url))
    if ranked:
        ranked.sort(reverse=True)
        return ranked[0][1]
    return ""


# Job-LISTING link terms only (the landing page is already "career/karriere" — we
# want the link through to the actual openings). Deliberately excludes bare
# "position" (matches "politische-positionen"), "career"/"karriere" and "search".
_JOB_LINK_KEYS = ("/jobs", "jobs/", "=jobs", "stellenangebote", "stellenanzeigen",
                  "stellensuche", "stellenmarkt", "offene-stellen", "open-positions",
                  "vacanc", "joblist", "job-search", "joboffers", "jobangebote",
                  "all-jobs", "/stellen", "/job/", "joblisting")

# Applicant-tracking-system hosts — a link to one is almost always the real listing.
_ATS_HOSTS = ("personio.", "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.",
              "successfactors.", "smartrecruiters.", "softgarden.", "join.com", "recruitee.",
              "jobvite.", "icims.com", "taleo.net", "concludis.", "prescreen.", "d-vinci.",
              "rexx-systems.", "guidecom.", "umantis.", "jobs.")


async def _fetch_page_raw(url: str, wait_ms: int = 1500, max_chars: int = 18000) -> tuple[str, str]:
    """Return (visible_text, raw_html). raw_html is the plain GET body ('' if the
    page is JS-only); useful for harvesting links to a deeper listing page."""
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
    if len(text) < 500:
        rendered = await _fetch_page_text(url, max_chars=max_chars, wait_ms=wait_ms)
        if len(rendered) > len(text):
            text = rendered
    return text, html


def _career_listing_links(html: str, base_url: str) -> list[str]:
    """Job-listing links on a careers landing page (so we can follow through to
    the actual openings when the landing page itself lists none)."""
    from urllib.parse import urljoin
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
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
        is_ats = any(a in host for a in _ATS_HOSTS) and host != base_host
        is_listing = any(k in low for k in _JOB_LINK_KEYS)
        if full in seen or not (is_ats or is_listing):
            continue
        seen.add(full)
        score = (3 if is_ats else 0) + (1 if base_host and base_host in host else 0) \
                  + (1 if any(t in low for t in ("stellenangebote", "all-jobs", "open-positions", "joblist", "stellensuche")) else 0)
        ranked.append((score, full))
    ranked.sort(reverse=True)
    return [u for _, u in ranked[:3]]


async def _extract_jobs(name: str, text: str) -> tuple[list, list]:
    """One LLM call → (positions, inferred_needs) from careers-page text."""
    if len(text) < 200:
        return [], []
    loop = asyncio.get_running_loop()
    from routers.knowledge import _call_brain_sync
    prompt = _JOBS_EXTRACT_PROMPT.format(client=name, page=text[:16000])
    try:
        reply = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
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
                return r.text if r.status_code == 200 and "xml" in r.headers.get("content-type", "") + r.text[:100] else ""
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

    loop = asyncio.get_running_loop()
    from routers.knowledge import _call_brain_sync
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
        reply = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
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


async def _scan_client_jobs(org_id: int, client: dict, careers_url: str = "") -> dict:
    """Fetch a client's careers page, extract open positions + inferred needs,
    store a singleton type='jobs' doc, and write the inferred needs as
    type='finding' docs so the match synthesis picks them up automatically.

    Sources, in order of reliability: (1) the site's sitemap of actual job
    postings — works even for JS/ATS pages that render listings client-side;
    (2) the careers-page text; (3) job-listing links followed from the landing
    page. The LLM filters to IT/management roles and rejects category names."""
    name = client["name"]
    meta = client.get("metadata") or {}
    url = (careers_url or meta.get("careers_url") or "").strip()
    if not url:
        url = await _discover_careers_url(org_id, client)
    summary = {"client": name, "careers_url": url, "positions": 0, "needs": 0, "found": False}
    if not url:
        return summary

    effective_url = url
    positions: list = []
    needs: list = []

    # (1) Sitemap of actual postings — the JS-free ground truth. A JS/ATS careers
    # page only exposes category filters to a fetch, but its sitemap lists every
    # real opening (e.g. jobs.apleona.com → /offer/<slug>/<uuid>).
    sitemap_jobs = await _sitemap_job_urls(url)
    if len(sitemap_jobs) >= 3:
        listing = "ACTUAL OPEN POSITIONS — these are real individual job postings (titles from the "
        listing += "company's job sitemap, NOT categories). Extract and filter them per the rules:\n"
        listing += "\n".join(f"- {t}" for t, _ in sitemap_jobs)
        positions, needs = await _extract_jobs(name, listing)

    # (2) Careers-page text (good for sites that list roles inline).
    if not positions:
        text, html = await _fetch_page_raw(url, wait_ms=3500)
        positions, needs = await _extract_jobs(name, text)
        # (3) Landing page with no roles → follow its job-listing links.
        if not positions and html:
            for link in _career_listing_links(html, url):
                sub_text, _ = await _fetch_page_raw(link, wait_ms=5000)
                p2, n2 = await _extract_jobs(name, sub_text)
                if p2:
                    positions, needs, effective_url = p2, n2, link
                    break

    now_iso = datetime.now(timezone.utc).isoformat()
    if effective_url and effective_url != meta.get("careers_url"):
        await db_module.update_client_metadata(org_id, name, {"careers_url": effective_url})

    # Nothing found anywhere — keep any prior good scan, just record we looked.
    if not positions and not needs:
        return summary

    url = effective_url

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

    # Map each inferred need to the seller's products, with a one-line justification.
    needs_mapped = await _map_needs_to_products(org_id, name, needs)

    lines = [f"# Open positions — {name}", f"Source: {url}", ""]
    for p in positions:
        extra = " · ".join(x for x in (p.get("team") or "", p.get("location") or "") if x)
        lines.append(f"- **{p['title']}**" + (f" ({extra})" if extra else ""))
    if needs:
        lines.append("\n## Inferred needs")
        lines += [f"- {n}" for n in needs]
    jobs_doc_id = await db_module.index_document(
        org_id=org_id, doc_id=f"jobs-{client['id']}", doc_type="jobs",
        title=f"Open positions — {name}", content="\n".join(lines),
        metadata={"careers_url": url, "positions": positions, "inferred_needs": needs,
                  "needs_mapped": needs_mapped, "last_scanned": now_iso, "subject": name},
        embedding=[], source="agent",
    )
    if jobs_doc_id and jobs_doc_id > 0:
        await db_module.link_document(jobs_doc_id, "client", client["id"])

    # Inferred needs → findings (deterministic ids = idempotent; match synthesis reads findings).
    for i, need in enumerate(needs):
        fid = await db_module.index_document(
            org_id=org_id, doc_id=f"jobs-need-{client['id']}-{i}", doc_type="finding",
            title=f"Hiring signal: {need[:60]}",
            content=(f"Inferred from {name}'s open roles ({len(positions)} positions on their "
                     f"careers page): {need}."),
            metadata={"source_url": url, "from_jobs": True, "subject": name},
            embedding=[], source="agent",
        )
        if fid and fid > 0:
            await db_module.link_document(fid, "client", client["id"])

    summary.update({"positions": len(positions), "needs": len(needs),
                    "careers_url": effective_url, "found": bool(positions or needs)})
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
    others = sorted((c for c in clients if c["id"] not in focus_ids),
                    key=lambda c: last.get(c["id"]) or epoch)[:max_other]

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
            _notify.notify(
                f"📬 {built} rep digest(s) built — {sent} sent, {pending} pending review"
                f"{blocked} — open /insights"
            )
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
    text = await llm.acomplete(prompt, role="triage", max_tokens=400, timeout=60)
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
                brain=context.config.get("agent_service_brain", "openrouter"),
                model=context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
                brain=context.config.get("agent_service_brain", "openrouter"),
                model=context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
            _notify.notify_weekly_digest(stats)
            await db_module.update_agent_run(run_id, "done", output=stats)

        elif agent_type == "stale_clients":
            from routers.notifications import _get_stale_clients
            import notifications as _notify
            stale = await _get_stale_clients(org_id, days=30)
            if stale:
                _notify.notify_stale_clients(stale)
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
                        brain=context.config.get("agent_service_brain", "openrouter"),
                        model=context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
                        brain=context.config.get("agent_service_brain", "openrouter"),
                        model=context.config.get("agent_service_model", "deepseek/deepseek-v4-flash"),
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
            summaries = []
            for c in clients:
                try:
                    summaries.append(await _monitor_client(org_id, c))
                except Exception as exc:
                    console.print(f"[yellow]source monitor failed for '{c['name']}': {exc}[/yellow]")

            researched = [s for s in summaries if s["researched"]]
            escalated = [s for s in summaries if s["escalated"]]
            flagged = [s for s in summaries if s["flagged"]]
            discovered = sum(s["discovered"] for s in summaries)
            console.print(
                f"[dim]Source monitor: {len(summaries)} clients checked, "
                f"{len(researched)} researched, {len(escalated)} escalated, "
                f"{len(flagged)} flagged, {discovered} sources discovered[/dim]"
            )
            if researched or flagged:
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
                _notify.notify("\n".join(lines))
            await db_module.update_agent_run(
                run_id, "done",
                output={
                    "clients_checked": len(summaries),
                    "researched": [s["client"] for s in researched],
                    "escalated": [s["client"] for s in escalated],
                    "flagged": [s["client"] for s in flagged],
                    "sources_discovered": discovered,
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
            }
            org_id = await context._default_org_id()
            if org_id:
                existing_types = {h["agent_type"] for h in await db_module.list_all_heartbeats(org_id)}
                for hb_type, (cron_expr, hb_task) in late_additions.items():
                    if hb_type not in existing_types:
                        await db_module.create_heartbeat(org_id, hb_type, cron_expr, hb_task)
                        console.print(f"  [green]{hb_type} heartbeat created ({cron_expr})[/green]")
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
            asyncio.create_task(_trigger_enrichment(session_dir.name, org_id))
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
    """Poll processing status of an ingest session. Used by the Mac app for reconnect."""
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
    """Receive a transcript chunk from the Mac app (buzzowl-mac).

    Chunks are buffered in memory. On is_final=True the full transcript is staged
    and the standard enrichment pipeline fires — identical to a browser recording session.
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
        # Broadcast to any browser clients connected via /ws so live transcript updates
        dead: set = set()
        msg = json.dumps({"type": "live", "text": chunk, "start": 0, "end": 0})
        for ws in context._live_ws_connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        context._live_ws_connections.difference_update(dead)

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
async def create_text_session(body: dict):
    """Accept a typed/pasted transcript, stage it, and kick off the enrichment pipeline."""
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

    org_id = await context._default_org_id()
    asyncio.create_task(_trigger_enrichment(session_id, org_id))

    console.print(f"[cyan]Text session created: {session_id}[/cyan]")
    return {"ok": True, "session_id": session_id}
