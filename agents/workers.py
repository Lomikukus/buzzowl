"""
agents/workers.py — Atomic research workers for Phase 7 Deep Research Engine.

Workers are deterministic single-purpose functions. They do NOT use the Agent
loop. They follow the enrichment.py pattern: direct Ollama /api/chat calls,
think: False, async def with sync I/O via run_in_executor.

Workers:
  run_link_collector    — web_search/profile_lookup task → spawn fetch_url children
  run_page_reader       — fetch_url task → read page, score relevance, spawn analyze children
  run_content_analyzer  — analyze task → extract facts, score, write finding if score >= threshold

Aggregators (also in this file):
  run_company_aggregator  — synthesise findings for a company into a type=osint doc
  run_person_aggregator   — synthesise findings for a person into a type=contact_research doc
"""
import asyncio
import hashlib
import json
import logging
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import db as _db
import llm
import notifications as _notify
from agents.events import emit as _emit
def _slugify(name: str) -> str:
    import re as _re
    return _re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
from agents.tools import _fetch_page, _web_search

logger = logging.getLogger(__name__)


def _display_subject(s: str) -> str:
    """Strip benchmark isolation tag from subject name: 'Acme [qwen3.5]' → 'Acme'."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip() or s


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    try:
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_worker_config() -> tuple[str, str, int, int, Optional[Path]]:
    """Return (brain, model, num_ctx, interest_threshold, vault_path)."""
    cfg = _load_config()
    brain = cfg.get("research_brain") or cfg.get("agent_brain", "openrouter")
    model = cfg.get("research_model") or cfg.get("agent_model", "deepseek/deepseek-v4-flash")
    num_ctx = int(cfg.get("agent_num_ctx", 16384))
    threshold = int(cfg.get("research_interest_threshold", 4))
    vp = cfg.get("vault_path", "")
    vault_path = Path(vp) if vp else None
    return brain, model, num_ctx, threshold, vault_path


async def _synthesize(prompt: str, model: str, num_ctx: int, brain: str = "openrouter") -> str:
    """Research-role LLM call via llm.py. Returns "" on failure — workers
    treat a missing synthesis as a soft skip."""
    try:
        return await llm.acomplete(prompt, role="research", model=model, timeout=120)
    except Exception as exc:
        logger.warning("LLM synthesis failed (model=%s): %s", model, exc)
        return ""


def _url_slug(url: str) -> str:
    """Derive a short filesystem-safe slug from a URL."""
    parts = urllib.parse.urlparse(url)
    raw = parts.netloc.replace("www.", "") + parts.path
    return _slugify(raw)[:40]


def _build_sources_section(findings: list, today: str) -> str:
    """Build an authoritative ## Sources section for an aggregated report.

    Findings with relevance_score >= 4 get a [[wikilink]] (they have vault files).
    All findings with a source_url appear in the URLs list.
    """
    wikilinks: list[str] = []
    urls: list[str] = []

    for f in findings:
        meta = f.get("metadata", {})
        source_url = meta.get("source_url", "")
        if not source_url:
            continue
        try:
            score = int(meta.get("relevance_score", 0))
        except (TypeError, ValueError):
            score = 0
        urls.append(source_url)
        if score >= 4:
            fetched_at = meta.get("fetched_at", today)
            wikilinks.append(f"[[{fetched_at}-{_url_slug(source_url)}]]")

    parts = ["## Sources"]
    if wikilinks:
        parts.append("\n**Finding files:**\n" + "\n".join(f"- {w}" for w in wikilinks))
    if urls:
        parts.append("\n**URLs:**\n" + "\n".join(f"- {u}" for u in urls))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Finding persistence
# ---------------------------------------------------------------------------

async def _db_check_source_url(org_id: int, source_url: str) -> Optional[dict]:
    """Return existing finding document with this source_url, or None."""
    if not _db._pool:
        return None
    async with _db._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, doc_id, content, metadata
            FROM documents
            WHERE org_id = $1
              AND type = 'finding'
              AND metadata->>'source_url' = $2
            LIMIT 1
            """,
            org_id, source_url,
        )
        return dict(row) if row else None


def _write_finding_vault_file(
    vault_path: Path,
    subject: str,
    title: str,
    content: str,
    source_url: str,
    source_type: str,
    relevance_score: int,
    agent_run_id: Optional[int],
    today: str,
) -> Optional[Path]:
    """Write a finding to north-info/research/{slug}/{today}-{url-slug}.md."""
    try:
        slug = _slugify(subject)
        uslug = _url_slug(source_url)
        research_dir = vault_path / "research" / slug
        research_dir.mkdir(parents=True, exist_ok=True)
        file_path = research_dir / f"{today}-{uslug}.md"

        frontmatter = (
            f"---\n"
            f"source: agent\n"
            f"agent_run_id: {agent_run_id if agent_run_id is not None else 'null'}\n"
            f"source_url: {source_url}\n"
            f"source_type: {source_type}\n"
            f"subject: {subject}\n"
            f"relevance_score: {relevance_score}\n"
            f"fetched_at: {today}\n"
            f"---\n\n"
        )
        file_path.write_text(frontmatter + f"# {title}\n\n" + content, encoding="utf-8")
        logger.info("Finding vault file written: %s", file_path)
        return file_path
    except Exception as exc:
        logger.warning("Failed to write finding vault file: %s", exc)
        return None


async def write_finding(
    org_id: int,
    agent_run_id: Optional[int],
    subject: str,
    subject_type: str,
    source_url: str,
    source_type: str,
    title: str,
    content: str,
    relevance_score: int,
    extracted_facts: list,
    vault_path: Optional[Path],
    today: str,
) -> Optional[dict]:
    """Persist a research finding to DB and optionally vault.

    Invariants:
    1. source_url must be non-empty — returns None if missing (rule 9).
    2. Dedup: if a document with the same source_url already exists and
       the new content is not substantially longer, return the existing doc.
    3. doc_id is deterministic (sha256 of source_url) — ON CONFLICT at DB level too.
    4. Vault write only if relevance_score >= 4.
    """
    if not source_url or not source_url.strip():
        logger.warning("write_finding: discarding finding with no source_url (subject=%s)", subject)
        return None

    existing = await _db_check_source_url(org_id, source_url)
    if existing:
        if len(content) <= len(existing.get("content", "")):
            logger.info("write_finding: duplicate source_url, skipping (url=%s)", source_url)
            # Still ensure the finding is linked to the current subject's client
            # (same URL may be found by multiple isolated benchmark runs)
            if existing.get("id") and subject_type == "company":
                try:
                    client = await _db.get_client(org_id, subject)
                    if not client:
                        await _db.upsert_client(org_id, subject, {}, [], None, None)
                        client = await _db.get_client(org_id, subject)
                    if client:
                        await _db.link_document(existing["id"], "client", client["id"])
                except Exception:
                    pass
            return existing

    doc_id = "finding-" + hashlib.sha256(source_url.encode()).hexdigest()[:16]

    metadata = {
        "source_url": source_url,
        "source_type": source_type,
        "subject": subject,
        "subject_type": subject_type,
        "relevance_score": relevance_score,
        "extracted_facts": extracted_facts,
        "fetched_at": today,
    }

    embedding = await _db.embed_text(f"{title}\n{content[:2000]}")
    db_id = await _db.index_document(
        org_id=org_id,
        doc_id=doc_id,
        doc_type="finding",
        title=title,
        content=content,
        metadata=metadata,
        embedding=embedding,
        source="agent",
        agent_run_id=agent_run_id,
    )

    if db_id > 0:
        if subject_type == "company":
            client = await _db.get_client(org_id, subject)
            if not client:
                try:
                    await _db.upsert_client(org_id, subject, {}, [], None, None)
                    client = await _db.get_client(org_id, subject)
                except Exception as exc:
                    logger.warning("write_finding: could not auto-create client %r: %s", subject, exc)
            if client:
                await _db.link_document(db_id, "client", client["id"])
        elif subject_type == "person":
            contact = await _db.get_contact(org_id, subject)
            if contact:
                await _db.link_document(db_id, "contact", contact["id"])

    if relevance_score >= 4 and vault_path:
        _write_finding_vault_file(
            vault_path=vault_path,
            subject=subject,
            title=title,
            content=content,
            source_url=source_url,
            source_type=source_type,
            relevance_score=relevance_score,
            agent_run_id=agent_run_id,
            today=today,
        )


    return {"doc_id": doc_id, "db_id": db_id, "title": title, "relevance_score": relevance_score}


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

_SIGNAL_EXTRACTION_PROMPT = """\
You are a B2B sales intelligence analyst. Read the research report below and extract \
actionable signals for a sales team.

Subject: {subject}

Report:
{report}

Extract up to 5 distinct, concrete signals. Each signal must be one of:
- pain_point: a problem, challenge, or friction the subject is experiencing
- opportunity: a growth area, hiring signal, new initiative, or opening for a sales conversation
- risk: a leadership change, financial risk, regulatory issue, or competitive threat
- news: a noteworthy recent development (funding, acquisition, product launch, expansion)

Respond with a JSON array only — no markdown fences, no explanation:
[
  {{
    "signal_type": "pain_point|opportunity|risk|news",
    "headline": "One specific sentence (max 100 chars)",
    "evidence": "1-2 sentences from the report supporting this signal",
    "source_url": "URL if present in report, else empty string",
    "relevance_score": <1-5 integer>
  }}
]

Rules:
- Only extract signals explicitly supported by the report text.
- Headlines must be specific — no generic statements like "they are growing".
- Return [] if no clear signals are present.
"""


async def _extract_signals(
    org_id: int,
    subject: str,
    subject_type: str,
    report: str,
    model: str,
    num_ctx: int,
    today: str,
    brain: str = "openrouter",
) -> int:
    """Extract signals from a synthesized report and write them as type=signal docs.

    Returns the number of signals saved. Best-effort — caller wraps in try/except.
    """
    if not report.strip():
        return 0

    prompt = _SIGNAL_EXTRACTION_PROMPT.format(
        subject=_display_subject(subject),
        report=report[:4000],
    )
    response = await _synthesize(prompt, model, num_ctx, brain)

    signals: list = []
    m = re.search(r"\[.*\]", response, re.DOTALL)
    if m:
        try:
            signals = json.loads(m.group(0))
        except Exception:
            pass

    if not isinstance(signals, list):
        return 0

    saved = 0
    for sig in signals[:5]:
        if not isinstance(sig, dict):
            continue
        headline = (sig.get("headline") or "").strip()[:200]
        if not headline:
            continue
        signal_type = sig.get("signal_type", "news")
        if signal_type not in ("pain_point", "opportunity", "risk", "news"):
            signal_type = "news"
        evidence = (sig.get("evidence") or "").strip()[:500]
        source_url = (sig.get("source_url") or "").strip()
        try:
            rel_score = int(sig.get("relevance_score", 3))
        except (TypeError, ValueError):
            rel_score = 3

        doc_id = "signal-" + hashlib.sha256(f"{subject}:{headline}".encode()).hexdigest()[:16]
        metadata = {
            "signal_type": signal_type,
            "subject": subject,
            "source_url": source_url,
            "relevance_score": rel_score,
            "extracted_date": today,
        }
        embedding = await _db.embed_text(f"{headline}\n{evidence}")
        db_id = await _db.index_document(
            org_id=org_id,
            doc_id=doc_id,
            doc_type="signal",
            title=headline,
            content=evidence,
            metadata=metadata,
            embedding=embedding,
            source="agent",
        )
        if db_id > 0 and subject_type == "company":
            client = await _db.get_client(org_id, subject)
            if client:
                await _db.link_document(db_id, "client", client["id"])
        if db_id > 0:
            saved += 1

    logger.info("Signal extraction: %d signals saved for %r", saved, subject)
    return saved


# ---------------------------------------------------------------------------
# Worker 1: LinkCollectorAgent
# ---------------------------------------------------------------------------

async def run_link_collector(
    task_id: int,
    org_id: int,
    subject: str,
    subject_type: str,
    payload: dict,
    depth: int,
    max_depth: int,
) -> tuple[dict, list[dict]]:
    """web_search task: run a search, emit fetch_url child tasks for each result URL."""
    query = payload.get("query", subject)
    logger.info("LinkCollector: query=%r depth=%d", query, depth)

    ts = datetime.now(timezone.utc).isoformat()
    asyncio.create_task(_emit({
        "type": "searching",
        "task_id": task_id,
        "subject": subject,
        "query": query,
        "depth": depth,
        "ts": ts,
    }))

    search_result = await _web_search(query, n_results=8)
    results = search_result.get("results", [])
    source = search_result.get("source", "unknown")

    child_tasks: list[dict] = []
    urls_found: list[str] = []

    for r in results:
        url = r.get("url", "").strip()
        if not url:
            continue
        urls_found.append(url)
        child_tasks.append({
            "task_type": "fetch_url",
            "payload": {
                "url": url,
                "snippet": r.get("snippet", "")[:300],
                "source_query": query,
            },
            "priority": 5,
        })

    asyncio.create_task(_emit({
        "type": "search_done",
        "task_id": task_id,
        "subject": subject,
        "query": query,
        "urls_found": urls_found,
        "source": source,
        "ts": datetime.now(timezone.utc).isoformat(),
    }))

    result = {
        "query": query,
        "urls_found": urls_found,
        "source": source,
        "child_tasks_spawned": len(child_tasks),
    }
    logger.info("LinkCollector: found %d URLs for query %r", len(urls_found), query)
    return result, child_tasks


# ---------------------------------------------------------------------------
# Worker 2: PageReaderAgent
# ---------------------------------------------------------------------------

async def run_page_reader(
    task_id: int,
    org_id: int,
    subject: str,
    subject_type: str,
    payload: dict,
    depth: int,
    max_depth: int,
    model: str,
    num_ctx: int,
    interest_threshold: int,
    brain: str = "openrouter",
) -> tuple[dict, list[dict]]:
    """fetch_url task: fetch a page, score relevance, emit analyze children."""
    url = payload.get("url", "").strip()
    if not url:
        return {"error": "no url in payload"}, []

    logger.info("PageReader: url=%r depth=%d", url, depth)

    asyncio.create_task(_emit({
        "type": "fetching",
        "task_id": task_id,
        "subject": subject,
        "url": url,
        "depth": depth,
        "ts": datetime.now(timezone.utc).isoformat(),
    }))

    fetch_result = await _fetch_page(url)

    if fetch_result.get("error"):
        asyncio.create_task(_emit({
            "type": "fetch_done",
            "task_id": task_id,
            "subject": subject,
            "url": url,
            "relevance_score": 0,
            "error": fetch_result["error"],
            "ts": datetime.now(timezone.utc).isoformat(),
        }))
        return {
            "url": url,
            "relevance_score": 0,
            "fetch_error": fetch_result["error"],
        }, []

    page_text = (fetch_result.get("text") or "")[:1000]

    # Score relevance 1-5 via Ollama
    _disp = _display_subject(subject)
    score_prompt = (
        f'Score the relevance of this web page content to the research subject "{_disp}" '
        f"on a scale of 1-5.\n"
        f"1=unrelated, 2=tangential, 3=relevant, 4=highly relevant, 5=essential.\n"
        f"Respond with ONLY the integer score and one sentence explaining why.\n\n"
        f"Subject: {_disp}\nContent: {page_text[:800]}"
    )
    score_response = await _synthesize(score_prompt, model, num_ctx, brain)

    score = 3  # default if Ollama offline or parse fails
    m = re.search(r"\b([1-5])\b", score_response)
    if m:
        score = int(m.group(1))

    logger.info("PageReader: url=%r relevance=%d", url, score)

    asyncio.create_task(_emit({
        "type": "fetch_done",
        "task_id": task_id,
        "subject": subject,
        "url": url,
        "relevance_score": score,
        "content_preview": page_text[:120],
        "ts": datetime.now(timezone.utc).isoformat(),
    }))

    child_tasks: list[dict] = []

    if score >= 3:
        child_tasks.append({
            "task_type": "analyze",
            "payload": {
                "url": url,
                "content": page_text,
                "relevance_score": score,
                "source_query": payload.get("source_query", ""),
            },
            "priority": score + 3,
        })

    if score >= interest_threshold and depth < max_depth:
        # Spawn a follow-up search based on the snippet topic
        snippet = payload.get("snippet", "")
        if snippet:
            child_tasks.append({
                "task_type": "web_search",
                "payload": {"query": f'"{_display_subject(subject)}" {snippet[:60]}'},
                "priority": score + 2,
            })

    return {
        "url": url,
        "relevance_score": score,
        "content_length": len(page_text),
        "content_preview": page_text[:200],
        "fetch_error": None,
    }, child_tasks


# ---------------------------------------------------------------------------
# Worker 3: ContentAnalyzerAgent
# ---------------------------------------------------------------------------

_ANALYZER_PROMPT = """\
You are a B2B sales intelligence analyst extracting structured facts from web content.

Subject of research: {subject}
Source URL: {url}

Content:
{content}

Extract the following in JSON format:
{{
  "title": "page title or best description (1 line)",
  "source_type": "article|profile|social|news|earnings|talk|other",
  "relevance_score": <1-5 integer>,
  "key_facts": ["fact 1", "fact 2"],
  "notable_signals": ["funding round", "leadership change", "product launch"],
  "named_entities": {{"people": ["name1"], "companies": ["co1"]}},
  "summary": "2-3 sentence summary of what this page says about {subject}"
}}

Rules:
- relevance_score: 1=unrelated, 2=tangential, 3=relevant, 4=highly relevant, 5=essential
- key_facts: only facts directly about {subject}, max 5 bullets
- notable_signals: ONLY if the page explicitly mentions funding / leadership change / product launch
- Do not invent facts. If content is about a different entity, set relevance_score to 1.
- Return valid JSON only, no markdown code fences.
"""


async def run_content_analyzer(
    task_id: int,
    org_id: int,
    subject: str,
    subject_type: str,
    payload: dict,
    depth: int,
    max_depth: int,
    model: str,
    num_ctx: int,
    interest_threshold: int,
    vault_path: Optional[Path],
    today: str,
    agent_run_id: Optional[int] = None,
    brain: str = "openrouter",
) -> tuple[dict, list[dict]]:
    """analyze task: extract facts, score, write finding if score >= 3."""
    url = payload.get("url", "").strip()
    if not url:
        return {"error": "no url in payload — discarding (rule 9: no source_url)"}, []

    content = payload.get("content", "")
    logger.info("ContentAnalyzer: url=%r depth=%d", url, depth)

    asyncio.create_task(_emit({
        "type": "analyzing",
        "task_id": task_id,
        "subject": subject,
        "url": url,
        "depth": depth,
        "ts": datetime.now(timezone.utc).isoformat(),
    }))

    prompt = _ANALYZER_PROMPT.format(
        subject=_display_subject(subject),
        url=url,
        content=content[:1000],
    )
    response = await _synthesize(prompt, model, num_ctx, brain)

    # Parse JSON from response
    analysis: dict = {}
    m = re.search(r"\{.*\}", response, re.DOTALL)
    if m:
        try:
            analysis = json.loads(m.group(0))
        except Exception:
            pass

    score = analysis.get("relevance_score", payload.get("relevance_score", 3))
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 3

    if score < 3:
        return {
            "url": url,
            "relevance_score": score,
            "saved": False,
            "reason": "below relevance threshold",
        }, []

    title = analysis.get("title") or f"Finding: {subject}"
    source_type = analysis.get("source_type", "article")
    key_facts = analysis.get("key_facts", [])
    summary = analysis.get("summary", content[:300])
    notable_signals = analysis.get("notable_signals", [])
    named_entities = analysis.get("named_entities", {})

    finding = await write_finding(
        org_id=org_id,
        agent_run_id=agent_run_id,
        subject=subject,
        subject_type=subject_type,
        source_url=url,
        source_type=source_type,
        title=title,
        content=summary,
        relevance_score=score,
        extracted_facts=key_facts,
        vault_path=vault_path,
        today=today,
    )

    asyncio.create_task(_emit({
        "type": "finding",
        "task_id": task_id,
        "subject": subject,
        "url": url,
        "title": title,
        "relevance_score": score,
        "saved": finding is not None,
        "key_facts": key_facts[:3],
        "notable_signals": notable_signals,
        "ts": datetime.now(timezone.utc).isoformat(),
    }))

    child_tasks: list[dict] = []
    if notable_signals and score >= interest_threshold and depth < max_depth:
        for person_name in named_entities.get("people", []):
            person_name = person_name.strip()
            if person_name:
                child_tasks.append({
                    "task_type": "profile_lookup",
                    "subject_type": "person",
                    "subject": person_name,
                    "payload": {
                        "name": person_name,
                        "company": _display_subject(subject),
                        "angle": "linkedin background publications",
                    },
                    "priority": 6,
                })

    return {
        "url": url,
        "title": title,
        "relevance_score": score,
        "saved": finding is not None,
        "doc_id": finding.get("doc_id") if finding else None,
        "key_facts": key_facts,
        "notable_signals": notable_signals,
        "named_entities": named_entities,
    }, child_tasks


# ---------------------------------------------------------------------------
# Aggregator: CompanyAggregatorAgent
# ---------------------------------------------------------------------------

_COMPANY_AGGREGATOR_PROMPT = """\
You are a B2B sales intelligence analyst. Synthesise the following research findings
into a comprehensive OSINT report for {subject}.

Research findings:
{findings_block}

Write a structured report in markdown with these sections:
## Company Overview
## Products & Services
## Recent Developments
## Financial Signals
## Leadership & Key People
## Sales Intelligence
## Sources

Rules:
- Only include facts supported by the findings above.
- Mark uncertain facts with "(unconfirmed)".
- In the ## Sources section, list every source URL from the findings.
- Be specific. No generic statements like "they are a global leader".
"""


async def run_company_aggregator(
    task_id: int,
    org_id: int,
    subject: str,
    payload: dict,
    model: str,
    num_ctx: int,
    vault_path: Optional[Path],
    today: str,
    brain: str = "openrouter",
) -> dict:
    """Collect all findings for a company and synthesise into a type=osint doc."""
    logger.info("CompanyAggregator: subject=%r", subject)

    client = await _db.get_client(org_id, subject)
    client_id = client["id"] if client else None

    all_docs = await _db.list_documents(org_id, client_id=client_id)
    findings = [d for d in all_docs if d.get("type") == "finding"]
    # Exclude human-overridden findings (rated irrelevant via feedback endpoint)
    findings = [d for d in findings if not d.get("metadata", {}).get("relevance_override")]

    if not findings:
        return {"subject": subject, "findings_used": 0, "error": "no findings available"}

    findings.sort(key=lambda d: d.get("metadata", {}).get("relevance_score", 0), reverse=True)
    top_findings = findings[:10]

    findings_block = ""
    total_chars = 0
    for f in top_findings:
        meta = f.get("metadata", {})
        source_url = meta.get("source_url", "(no url)")
        score = meta.get("relevance_score", "?")
        content_chunk = (f.get("content") or "")[:600]
        entry = f"### [{f['title']}]({source_url}) (score: {score})\n{content_chunk}\n\n"
        if total_chars + len(entry) > 8000:
            break
        findings_block += entry
        total_chars += len(entry)

    prompt = _COMPANY_AGGREGATOR_PROMPT.format(subject=_display_subject(subject), findings_block=findings_block)
    synthesized = await _synthesize(prompt, model, num_ctx, brain)

    if not synthesized:
        synthesized = findings_block

    # Strip any LLM-generated ## Sources section and replace with authoritative one
    # that includes wikilinks for vault findings (score >= 4) + all URLs.
    synthesized = re.sub(r"\n## Sources\b.*$", "", synthesized, flags=re.DOTALL).rstrip()
    synthesized += "\n\n" + _build_sources_section(top_findings, today)

    doc_id_str = f"osint-agg-{_slugify(subject)}-{today}"
    embedding = await _db.embed_text(f"{subject} OSINT report\n{synthesized[:2000]}")
    db_id = await _db.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="osint",
        title=f"OSINT Report: {_display_subject(subject)} (aggregated {today})",
        content=synthesized,
        metadata={
            "subject": subject,
            "osint_date": today,
            "findings_used": len(top_findings),
            "aggregated": True,
        },
        embedding=embedding,
        source="agent",
    )
    if client_id and db_id > 0:
        await _db.link_document(db_id, "client", client_id)

    if vault_path:
        try:
            slug = _slugify(subject)
            research_dir = vault_path / "research" / slug
            research_dir.mkdir(parents=True, exist_ok=True)
            overview_path = research_dir / "overview.md"
            frontmatter = (
                f"---\n"
                f"source: agent\n"
                f"subject: {subject}\n"
                f"type: osint\n"
                f"aggregated: true\n"
                f"osint_date: {today}\n"
                f"findings_used: {len(top_findings)}\n"
                f"---\n\n"
            )
            # Strip leading h1 if the LLM already emitted one (prevents duplicate)
            body = re.sub(r"^#\s+OSINT Report:.*\n\n?", "", synthesized, count=1)
            overview_path.write_text(
                frontmatter + f"# OSINT Report: {_display_subject(subject)}\n\n" + body,
                encoding="utf-8",
            )
            logger.info("Aggregator vault overview written: %s", overview_path)
        except Exception as exc:
            logger.warning("Failed to write aggregator vault file: %s", exc)

    try:
        log_dir = Path(__file__).parent.parent / "data" / "agent_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"aggregator_{today.replace('-', '')}.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n## {ts} — {subject} (company aggregation)\n")
            lf.write(f"- Findings used: {len(top_findings)}\n")
            lf.write(f"- DB doc_id: {doc_id_str}\n")
    except Exception as exc:
        logger.warning("Failed to write aggregator log: %s", exc)

    # Orchestrator gap accuracy: compare planned gaps vs. findings actually saved
    try:
        task_row = await _db.get_research_task(task_id)
        run_id = task_row.get("assigned_agent_run_id") if task_row else None
        if run_id:
            run = await _db.get_agent_run(run_id)
            if run:
                gaps = (run.get("output") or {}).get("gaps") or []
                if gaps:
                    gap_count = len(gaps)
                    found_count = len(top_findings)
                    await _db.patch_agent_run_output(run_id, {
                        "gap_accuracy": {
                            "gaps_planned": gap_count,
                            "findings_saved": found_count,
                            "coverage_ratio": round(min(found_count, gap_count) / gap_count, 2),
                            "unexpected_findings": max(0, found_count - gap_count),
                        }
                    })
    except Exception as exc:
        logger.debug("gap accuracy update skipped: %s", exc)

    # Signal extraction (best-effort — never blocks the aggregator result)
    try:
        await _extract_signals(
            org_id=org_id,
            subject=subject,
            subject_type="company",
            report=synthesized,
            model=model,
            num_ctx=num_ctx,
            today=today,
            brain=brain,
        )
    except Exception as exc:
        logger.warning("Signal extraction failed for %r: %s", subject, exc)

    # Industry research trigger (best-effort)
    try:
        if client_id:
            fresh_client = await _db.get_client(org_id, subject)
            industry = ""
            if fresh_client:
                cmeta = fresh_client.get("metadata") or {}
                if isinstance(cmeta, str):
                    try:
                        cmeta = json.loads(cmeta)
                    except Exception:
                        cmeta = {}
                industry = cmeta.get("industry", "")
            if industry:
                from routers.pipeline import _trigger_industry_research
                await _trigger_industry_research(industry, org_id)
    except Exception as exc:
        logger.warning("Industry research trigger skipped: %s", exc)

    # Send single consolidated research report
    try:
        recent_signals = await _db.list_signals(org_id, client_name=subject, days=1) if _db._pool else []
        _notify.notify_research_report(
            subject=subject,
            findings=top_findings,
            signals=recent_signals,
            synthesized_report=synthesized,
            today=today,
        )
    except Exception as exc:
        logger.warning("Research report notification failed: %s", exc)

    return {
        "subject": subject,
        "doc_id": doc_id_str,
        "db_id": db_id,
        "findings_used": len(top_findings),
    }


# ---------------------------------------------------------------------------
# Aggregator: PersonAggregatorAgent
# ---------------------------------------------------------------------------

_PERSON_AGGREGATOR_PROMPT = """\
You are a B2B sales intelligence analyst. Synthesise the following research findings
into a comprehensive person profile for {subject}.

Research findings:
{findings_block}

Write in markdown with these sections:
## Role & Background
## What They Talk About (public posts, articles, talks)
## Recent Public Statements
## Areas of Expertise
## Sales Engagement Notes (talking points based on their interests)
## Sources

Rules:
- Only include facts supported by the findings.
- Mark uncertain facts with "(unconfirmed)".
- List all source URLs in ## Sources.
"""


async def run_person_aggregator(
    task_id: int,
    org_id: int,
    subject: str,
    payload: dict,
    model: str,
    num_ctx: int,
    vault_path: Optional[Path],
    today: str,
    brain: str = "openrouter",
) -> dict:
    """Collect all findings for a person and synthesise into a type=contact_research doc."""
    logger.info("PersonAggregator: subject=%r", subject)

    contact = await _db.get_contact(org_id, subject)
    contact_id = contact["id"] if contact else None

    all_docs = await _db.list_documents(org_id, contact_id=contact_id)
    findings = [d for d in all_docs if d.get("type") == "finding"]
    # Exclude human-overridden findings (rated irrelevant via feedback endpoint)
    findings = [d for d in findings if not d.get("metadata", {}).get("relevance_override")]

    if not findings:
        return {"subject": subject, "findings_used": 0, "error": "no findings available"}

    findings.sort(key=lambda d: d.get("metadata", {}).get("relevance_score", 0), reverse=True)
    top_findings = findings[:10]

    findings_block = ""
    total_chars = 0
    for f in top_findings:
        meta = f.get("metadata", {})
        source_url = meta.get("source_url", "(no url)")
        score = meta.get("relevance_score", "?")
        content_chunk = (f.get("content") or "")[:600]
        entry = f"### [{f['title']}]({source_url}) (score: {score})\n{content_chunk}\n\n"
        if total_chars + len(entry) > 8000:
            break
        findings_block += entry
        total_chars += len(entry)

    prompt = _PERSON_AGGREGATOR_PROMPT.format(subject=_display_subject(subject), findings_block=findings_block)
    synthesized = await _synthesize(prompt, model, num_ctx, brain)

    if not synthesized:
        synthesized = findings_block

    synthesized = re.sub(r"\n## Sources\b.*$", "", synthesized, flags=re.DOTALL).rstrip()
    synthesized += "\n\n" + _build_sources_section(top_findings, today)

    doc_id_str = f"contact-research-agg-{_slugify(subject)}-{today}"
    embedding = await _db.embed_text(f"{subject} contact research\n{synthesized[:2000]}")
    db_id = await _db.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="contact_research",
        title=f"Profile: {_display_subject(subject)} (aggregated {today})",
        content=synthesized,
        metadata={
            "subject": subject,
            "aggregated_date": today,
            "findings_used": len(top_findings),
            "aggregated": True,
        },
        embedding=embedding,
        source="agent",
    )
    if contact_id and db_id > 0:
        await _db.link_document(db_id, "contact", contact_id)

    if vault_path:
        try:
            slug = _slugify(subject)
            research_dir = vault_path / "research" / slug
            research_dir.mkdir(parents=True, exist_ok=True)
            profile_path = research_dir / "profile.md"
            frontmatter = (
                f"---\n"
                f"source: agent\n"
                f"subject: {subject}\n"
                f"type: contact_research\n"
                f"aggregated: true\n"
                f"aggregated_date: {today}\n"
                f"findings_used: {len(top_findings)}\n"
                f"---\n\n"
            )
            body = re.sub(r"^#\s+Profile:.*\n\n?", "", synthesized, count=1)
            profile_path.write_text(
                frontmatter + f"# Profile: {_display_subject(subject)}\n\n" + body,
                encoding="utf-8",
            )
            logger.info("Person aggregator vault profile written: %s", profile_path)
        except Exception as exc:
            logger.warning("Failed to write person aggregator vault file: %s", exc)

    # Orchestrator gap accuracy: compare planned gaps vs. findings actually saved
    try:
        task_row = await _db.get_research_task(task_id)
        run_id = task_row.get("assigned_agent_run_id") if task_row else None
        if run_id:
            run = await _db.get_agent_run(run_id)
            if run:
                gaps = (run.get("output") or {}).get("gaps") or []
                if gaps:
                    gap_count = len(gaps)
                    found_count = len(top_findings)
                    await _db.patch_agent_run_output(run_id, {
                        "gap_accuracy": {
                            "gaps_planned": gap_count,
                            "findings_saved": found_count,
                            "coverage_ratio": round(min(found_count, gap_count) / gap_count, 2),
                            "unexpected_findings": max(0, found_count - gap_count),
                        }
                    })
    except Exception as exc:
        logger.debug("gap accuracy update skipped: %s", exc)

    # Signal extraction (best-effort)
    try:
        await _extract_signals(
            org_id=org_id,
            subject=subject,
            subject_type="person",
            report=synthesized,
            model=model,
            num_ctx=num_ctx,
            today=today,
            brain=brain,
        )
    except Exception as exc:
        logger.warning("Signal extraction failed for %r: %s", subject, exc)

    return {
        "subject": subject,
        "doc_id": doc_id_str,
        "db_id": db_id,
        "findings_used": len(top_findings),
    }


# ---------------------------------------------------------------------------
# Aggregator: IndustryAggregatorAgent
# ---------------------------------------------------------------------------

_INDUSTRY_AGGREGATOR_PROMPT = """\
You are a senior industry analyst. Synthesise the research findings below into a comprehensive
industry intelligence report for the "{industry}" industry.

Research findings:
{findings_block}

Write in markdown with these exact sections:

## Overview
What this industry is, key players, scale, and strategic direction.

## Key Trends
The 3–5 most important trends shaping this industry right now.

## Regulations & Laws
Recent or upcoming regulations and legal requirements relevant to this industry.
Include jurisdiction, effective date, and business impact where known.

## Recent News
The most significant recent news events (2025–2026) affecting this industry.

## Opportunities & Risks
Key opportunities a B2B sales rep should know about, and risks to watch.

## References
[n] Title — URL

---
Citation rules: use [n] numbers in text for every factual claim. List all cited sources
in ## References as [n] Title — URL. Use (unconfirmed) for claims with no traceable source.
"""


async def run_industry_aggregator(
    task_id: int,
    org_id: int,
    subject: str,
    payload: dict,
    model: str,
    num_ctx: int,
    vault_path: Optional[Path],
    today: str,
    brain: str = "openrouter",
) -> dict:
    """Collect all findings for an industry and synthesise into a type=industry_research doc."""
    logger.info("IndustryAggregator: industry=%r", subject)

    if not _db._pool:
        return {"subject": subject, "error": "db offline"}

    async with _db._pool.acquire() as conn:
        finding_rows = await conn.fetch(
            """
            SELECT d.title, d.content, d.metadata, d.created_at
            FROM documents d
            WHERE d.org_id = $1
              AND d.type = 'finding'
              AND (d.metadata->>'subject' ILIKE $2 OR d.title ILIKE $3)
            ORDER BY (d.metadata->>'relevance_score')::int DESC NULLS LAST,
                     d.created_at DESC
            LIMIT 15
            """,
            org_id, f"%{subject}%", f"%{subject}%",
        )

    findings = [dict(r) for r in finding_rows]
    if not findings:
        return {"subject": subject, "findings_used": 0, "error": "no findings for industry"}

    findings_block = ""
    total_chars = 0
    for f in findings:
        meta = f.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        src = meta.get("source_url", "(no url)")
        content_chunk = (f.get("content") or "")[:500]
        entry = f"### [{f['title']}]({src})\n{content_chunk}\n\n"
        if total_chars + len(entry) > 8000:
            break
        findings_block += entry
        total_chars += len(entry)

    prompt = _INDUSTRY_AGGREGATOR_PROMPT.format(industry=subject, findings_block=findings_block)
    synthesized = await _synthesize(prompt, model, num_ctx, brain)
    if not synthesized:
        synthesized = findings_block

    doc_id_str = f"industry-{_slugify(subject)}-{today}"
    embedding = await _db.embed_text(f"{subject} industry report\n{synthesized[:2000]}")
    db_id = await _db.index_document(
        org_id=org_id,
        doc_id=doc_id_str,
        doc_type="industry_research",
        title=f"Industry Report: {subject.title()} ({today})",
        content=synthesized,
        metadata={"industry": subject, "report_date": today, "findings_used": len(findings)},
        embedding=embedding,
        source="agent",
    )

    # Link to all clients in this industry
    clients_linked = 0
    if db_id > 0 and _db._pool:
        async with _db._pool.acquire() as conn:
            client_rows = await conn.fetch(
                "SELECT id FROM clients WHERE org_id = $1 AND metadata->>'industry' ILIKE $2",
                org_id, f"%{subject}%",
            )
        for cr in client_rows:
            await _db.link_document(db_id, "client", cr["id"])
        clients_linked = len(client_rows)
        logger.info("Industry report linked to %d clients", clients_linked)

    if vault_path:
        try:
            industry_dir = vault_path / "research" / f"industry-{_slugify(subject)}"
            industry_dir.mkdir(parents=True, exist_ok=True)
            out_path = industry_dir / f"{today}-overview.md"
            frontmatter = (
                f"---\nsource: agent\nindustry: {subject}\ntype: industry_research\n"
                f"report_date: {today}\n---\n\n"
            )
            out_path.write_text(
                frontmatter + f"# Industry Report: {subject.title()}\n\n" + synthesized,
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to write industry vault file: %s", exc)

    return {"subject": subject, "doc_id": doc_id_str, "db_id": db_id, "findings_used": len(findings)}
