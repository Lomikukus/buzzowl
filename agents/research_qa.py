"""
agents/research_qa.py — Scheduled research-QA reviewer.

Samples recent agent-written research and flags quality problems using
deterministic heuristics only (no LLM). Motivated by three real failure modes
found in a manual fact-check:

  1. Stale synthesis — a client's newest synthesis doc (research / brief /
     client_brief) predates its newest `finding` by more than N days, so the
     synthesis no longer reflects the latest findings (e.g. a Globex research doc
     that still said "~100,000 lawsuits" after newer findings corrected it).
     Flag: qa_flag='stale'.

  2. Cross-client contamination — a research/brief doc for client A contains a
     strong mention of a DIFFERENT org client's name in a leadership / contact
     context (e.g. Acme's CIO "Jane Example" wrongly listed as a Globex
     contact). Flag: qa_flag='contamination'.

  3. Unsourced claims — an agent-written research/brief doc has NO `## Sources`
     section and no "(unconfirmed)" / "(inferred)" markers, so its claims are
     untraceable. Flag: qa_flag='no_sources'.

The heuristics are pure functions (see the "Heuristics" section) so they can be
unit-tested with in-memory fixtures. The agent itself writes flags into each
flagged document's metadata (`db.update_document` merge) at runtime and writes a
QA summary document, mirroring how agents/org.py writes its health summary.

Wired as the `research_qa` heartbeat — see routers/pipeline.py `_run_heartbeat_job`
and db.py `seed_default_heartbeats`.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import db as _db

logger = logging.getLogger("whisper.agents.research_qa")

_cfg_path = Path(__file__).parent.parent / "config.yaml"

# Document types treated as "synthesis" (lag behind findings when stale)
_SYNTHESIS_TYPES = ("research", "brief", "client_brief")
# Document types treated as agent-written research subject to the sources check
_RESEARCH_TYPES = ("research", "brief", "client_brief", "osint")

_DEFAULT_STALE_DAYS = 10
_DEFAULT_SAMPLE_SIZE = 200

# Leadership / contact keywords — a client name appearing within this window of
# any of these is a "leadership context" mention (used for contamination).
_LEADERSHIP_KEYWORDS = (
    "cio", "cto", "ceo", "cfo", "coo", "ciso", "cmo", "cdo", "chro",
    "chief", "head of", "vp ", "vice president", "director", "geschäftsführer",
    "geschäftsführung", "vorstand", "leiter", "leiterin", "manager",
    "contact", "kontakt", "ansprechpartner", "decision maker", "decision-maker",
    "entscheider", "board", "executive", "president", "founder", "gründer",
    "owner", "inhaber",
)

# Sources / unconfirmed markers — presence of any means the doc is NOT unsourced.
_SOURCES_HEADING_RE = re.compile(r"(?im)^\s{0,3}#{1,6}\s*(sources|quellen|references|referenzen)\b")
_UNCONFIRMED_RE = re.compile(r"\((?:unconfirmed|inferred|unverified|unbestätigt|vermutet)\)", re.IGNORECASE)
# A bare URL also counts as a traceable source even without a heading.
_URL_RE = re.compile(r"https?://\S+")

# How close (in characters) a foreign client name must be to a leadership keyword
# to count as a leadership-context mention.
_CONTAM_WINDOW = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        return yaml.safe_load(open(_cfg_path)) or {}
    except Exception:
        return {}


def _to_dt(value) -> Optional[datetime]:
    """Coerce a datetime or ISO string to an aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_meta(doc: dict) -> dict:
    """Return a doc's metadata as a dict whether stored as dict, JSON str, or None."""
    import json
    meta = doc.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta if isinstance(meta, dict) else {}


# Common words that must NEVER be treated as a client-name variant on their own —
# they cause cross-client false positives (e.g. a client "Count + Care" reduced to
# "count" matching "Executive Summary ... discount"). A single-token variant is
# only kept when the whole simplified name is already one word AND not a stopword.
_NAME_STOPWORDS = {
    "count", "care", "manage", "bank", "now", "group", "gruppe", "holding",
    "service", "services", "solutions", "systems", "system", "technologies",
    "technology", "digital", "data", "media", "global", "international", "next",
    "new", "one", "first", "best", "smart", "cloud", "net", "web", "board",
    "management", "finance", "capital", "invest", "consulting", "partner",
    "partners", "deutsche", "deutscher", "deutschland", "europa", "verlag",
    "power", "energy", "health", "pharma", "food", "auto", "logistik",
    "logistics", "trade", "market", "markt", "plus", "pro", "max", "lease",
    "leasing", "modemärkte", "maschinenfabrik", "gesellschaft", "healthcare",
}


def _name_variants(name: str) -> list[str]:
    """Distinct lowercased forms of a client name to match in prose.

    Strips legal suffixes and parentheticals so 'Globex AG' also matches a bare
    'Globex'. A single-token short form is ONLY produced when the whole simplified
    name is already one distinctive word (e.g. 'Acme', 'Globex') — for
    multi-word names ('Count + Care', 'Bank Verlag') only the full phrase is
    matched, so a generic first word can't collide with common prose. Blank,
    too-short, and stopword variants are dropped.
    """
    forms: set[str] = set()
    base = (name or "").strip().lower()
    if not base:
        return []
    forms.add(base)
    # drop parentheticals: "Deutscher Fußball-Bund (DFB)" -> "deutscher fußball-bund"
    no_paren = re.sub(r"\([^)]*\)", " ", base)
    # drop legal suffixes / generic tails
    simple = re.sub(
        r"\b(gmbh|ag|se|kg|kgaa|ohg|mbh|co|holding|group|gruppe|inc|ltd|llc|plc|e\.?v\.?)\b",
        " ", no_paren,
    )
    simple = re.sub(r"[&+.,]", " ", simple)
    simple = re.sub(r"\s+", " ", simple).strip()
    if simple:
        forms.add(simple)
    # Only single-word simplified names contribute a bare token (multi-word names
    # would otherwise reduce to a generic first word). Guard with the stopword set.
    if simple and " " not in simple and simple not in _NAME_STOPWORDS:
        forms.add(simple)
    return [
        f for f in forms
        if len(f) >= 5 and (" " in f or f not in _NAME_STOPWORDS)
    ]


# ---------------------------------------------------------------------------
# Heuristics (pure — unit-tested with in-memory fixtures)
# ---------------------------------------------------------------------------

def detect_stale_synthesis(docs: list[dict], stale_days: int) -> Optional[dict]:
    """Given all docs linked to ONE client, return a stale flag or None.

    A flag is returned when the newest synthesis doc (research/brief/client_brief)
    is older than the newest `finding` doc by more than `stale_days`. The returned
    dict names the synthesis doc to flag (`doc_id`) and the lag in days.
    """
    newest_synth: Optional[dict] = None
    newest_synth_dt: Optional[datetime] = None
    newest_finding_dt: Optional[datetime] = None

    for d in docs:
        dt = _to_dt(d.get("created_at"))
        if dt is None:
            continue
        dtype = d.get("type")
        if dtype in _SYNTHESIS_TYPES:
            if newest_synth_dt is None or dt > newest_synth_dt:
                newest_synth_dt, newest_synth = dt, d
        elif dtype == "finding":
            if newest_finding_dt is None or dt > newest_finding_dt:
                newest_finding_dt = dt

    if not newest_synth or newest_synth_dt is None or newest_finding_dt is None:
        return None

    lag_days = (newest_finding_dt - newest_synth_dt).days
    if lag_days > stale_days:
        return {
            "doc_id": newest_synth.get("doc_id"),
            "title": newest_synth.get("title"),
            "type": newest_synth.get("type"),
            "lag_days": lag_days,
            "synthesis_date": newest_synth_dt.date().isoformat(),
            "newest_finding_date": newest_finding_dt.date().isoformat(),
        }
    return None


def detect_contamination(
    content: str,
    own_names: list[str],
    other_client_names: list[str],
) -> list[dict]:
    """Return foreign-client leadership mentions found in a doc's content.

    Scans `content` for any name in `other_client_names` (excluding the doc's own
    client `own_names`) that appears within `_CONTAM_WINDOW` characters of a
    leadership/contact keyword. Each hit is a dict {client, variant, keyword, excerpt}.
    """
    if not content:
        return []
    text = content.lower()
    own_variants: set[str] = set()
    for n in own_names:
        own_variants.update(_name_variants(n))

    # Pre-locate leadership keyword positions once — word-boundary matched so a
    # generic short keyword ('coo', 'board') doesn't match inside a larger word
    # ('cooperation', 'cardboard'). Multi-word keywords ('head of') still work.
    keyword_spans: list[tuple[int, int, str]] = []
    for kw in _LEADERSHIP_KEYWORDS:
        for m in re.finditer(r"\b" + re.escape(kw.strip()) + r"\b", text):
            keyword_spans.append((m.start(), m.end(), kw.strip()))

    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for client_name in other_client_names:
        for variant in _name_variants(client_name):
            if variant in own_variants:
                continue  # this variant also matches the doc's own client — skip
            # whole-word (phrase) match for the foreign variant
            for m in re.finditer(r"\b" + re.escape(variant) + r"\b", text):
                v_start, v_end = m.start(), m.end()
                for k_start, k_end, kw in keyword_spans:
                    # distance between the two spans
                    if k_start >= v_end:
                        gap = k_start - v_end
                    elif v_start >= k_end:
                        gap = v_start - k_end
                    else:
                        gap = 0
                    if gap <= _CONTAM_WINDOW:
                        key = (client_name, variant)
                        if key in seen:
                            break
                        seen.add(key)
                        lo = max(0, min(v_start, k_start) - 30)
                        hi = min(len(content), max(v_end, k_end) + 30)
                        hits.append({
                            "client": client_name,
                            "variant": variant,
                            "keyword": kw.strip(),
                            "excerpt": content[lo:hi].replace("\n", " ").strip(),
                        })
                        break
                else:
                    continue
                break  # matched this variant; move to next variant
    return hits


def detect_no_sources(content: str) -> bool:
    """True when a doc's content has NO traceable sourcing.

    A doc is considered sourced if it has a `## Sources` (or Quellen/References)
    heading, contains at least one URL, or explicitly marks unconfirmed/inferred
    claims. Empty content is treated as unsourced.
    """
    if not content or not content.strip():
        return True
    if _SOURCES_HEADING_RE.search(content):
        return False
    if _URL_RE.search(content):
        return False
    if _UNCONFIRMED_RE.search(content):
        return False
    return True


# ---------------------------------------------------------------------------
# DB-backed passes (thin wrappers over the pure heuristics)
# ---------------------------------------------------------------------------

async def _scan_stale(org_id: int, clients: list[dict], stale_days: int) -> list[dict]:
    """Per-client stale-synthesis scan. Returns list of stale flags (with client)."""
    flags: list[dict] = []
    for c in clients:
        try:
            docs = await _db.list_documents(org_id, client_id=c["id"])
        except Exception as exc:
            logger.warning("stale scan: list_documents failed for %s: %s", c.get("name"), exc)
            continue
        flag = detect_stale_synthesis(docs, stale_days)
        if flag:
            flag["client"] = c["name"]
            flags.append(flag)
    return flags


async def _scan_content_flags(
    org_id: int,
    candidate_docs: list[dict],
    client_names: list[str],
    doc_client_names: dict,
) -> tuple[list[dict], list[dict]]:
    """Fetch content for each candidate agent research doc and run the
    contamination + no-sources heuristics. Returns (contamination_flags, no_source_flags).

    `doc_client_names` maps a document's doc_id -> list of its own linked client names,
    used to exclude the doc's own client from contamination matching.
    """
    contamination: list[dict] = []
    no_sources: list[dict] = []
    for d in candidate_docs:
        doc_id = d.get("doc_id")
        try:
            full = await _db.get_document(org_id, doc_id)
        except Exception as exc:
            logger.warning("content scan: get_document failed for %s: %s", doc_id, exc)
            continue
        if not full:
            continue
        content = full.get("content") or ""
        own = doc_client_names.get(doc_id, [])
        others = [n for n in client_names if n not in own]

        hits = detect_contamination(content, own, others)
        if hits:
            contamination.append({
                "doc_id": doc_id,
                "title": full.get("title"),
                "type": full.get("type"),
                "own_clients": own,
                "hits": hits,
            })

        if detect_no_sources(content):
            no_sources.append({
                "doc_id": doc_id,
                "title": full.get("title"),
                "type": full.get("type"),
                "own_clients": own,
            })
    return contamination, no_sources


async def _flag_document(org_id: int, doc_id: str, flag: str, detail: dict) -> None:
    """Write a qa_flag into a document's metadata (merge — never clobbers).

    Best-effort: a failed flag write must not abort the QA run.
    """
    if not doc_id:
        return
    patch = {
        "qa_flag": flag,
        "qa_flag_detail": detail,
        "qa_flagged_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await _db.update_document(org_id, doc_id, {"metadata": patch})
        logger.info("QA flag '%s' written to doc %s", flag, doc_id)
    except Exception as exc:
        logger.warning("QA flag write failed for doc %s: %s", doc_id, exc)


# ---------------------------------------------------------------------------
# QA summary document
# ---------------------------------------------------------------------------

def _build_summary_content(
    date_str: str,
    stale: list[dict],
    contamination: list[dict],
    no_sources: list[dict],
    scanned: int,
) -> str:
    def _stale_lines() -> str:
        return "\n".join(
            f"- **{f['client']}** — synthesis `{f.get('title') or f['doc_id']}` "
            f"({f['synthesis_date']}) lags newest finding ({f['newest_finding_date']}) "
            f"by {f['lag_days']}d"
            for f in stale
        ) or "- None"

    def _contam_lines() -> str:
        out = []
        for f in contamination:
            names = ", ".join(sorted({h["client"] for h in f["hits"]}))
            out.append(
                f"- `{f.get('title') or f['doc_id']}` (own: {', '.join(f['own_clients']) or '—'}) "
                f"→ foreign leadership mention: {names}"
            )
        return "\n".join(out) or "- None"

    def _nosrc_lines() -> str:
        return "\n".join(
            f"- `{f.get('title') or f['doc_id']}` [{f.get('type')}]"
            for f in no_sources
        ) or "- None"

    return f"""## Research QA — {date_str}

Scanned {scanned} recent agent research document(s).

### Stale synthesis ({len(stale)})
Synthesis docs that predate their client's newest finding.
{_stale_lines()}

### Cross-client contamination ({len(contamination)})
Docs mentioning a *different* client's name in a leadership/contact context.
{_contam_lines()}

### Unsourced claims ({len(no_sources)})
Agent research docs with no `## Sources`, no URL, and no "(unconfirmed)" markers.
{_nosrc_lines()}
"""


async def _write_qa_summary(
    org_id: int,
    run_id: Optional[int],
    stale: list[dict],
    contamination: list[dict],
    no_sources: list[dict],
    scanned: int,
) -> None:
    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    content = _build_summary_content(date_str, stale, contamination, no_sources, scanned)
    try:
        await _db.index_document(
            org_id=org_id,
            doc_id=f"research-qa-{date_str}",
            doc_type="research_qa",
            title=f"Research QA Report — {date_str}",
            content=content,
            metadata={
                "brief_type": "research_qa",
                "date": date_str,
                "scanned": scanned,
                "stale_count": len(stale),
                "contamination_count": len(contamination),
                "no_sources_count": len(no_sources),
                "stale_docs": [f.get("doc_id") for f in stale],
                "contamination_docs": [f.get("doc_id") for f in contamination],
                "no_sources_docs": [f.get("doc_id") for f in no_sources],
            },
            embedding=[],
            source="agent",
            agent_run_id=run_id,
        )
    except Exception as exc:
        logger.warning("QA summary write failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_research_qa(
    org_id: int,
    run_id: Optional[int] = None,
    sample_size: Optional[int] = None,
) -> dict:
    """Sample recent agent research and flag quality problems (deterministic).

    1. Stale-synthesis scan (per client).
    2. Cross-client contamination scan (agent research/brief docs).
    3. Unsourced-claims scan (agent research/brief docs).
    Each flagged doc gets a `qa_flag` written into its metadata, and a QA summary
    document is written. Returns a counts dict.

    sample_size caps the number of recent agent research docs scanned for the
    content-based checks (contamination + no_sources). None → config default.
    """
    cfg = _load_config()
    stale_days = int(cfg.get("research_qa_stale_days", _DEFAULT_STALE_DAYS))
    if sample_size is None:
        sample_size = int(cfg.get("research_qa_sample_size", _DEFAULT_SAMPLE_SIZE))

    logger.info(
        "research_qa run %s | org %d | stale_days=%d sample_size=%s",
        run_id, org_id, stale_days, sample_size,
    )

    clients = await _db.list_clients(org_id)
    client_names = [c["name"] for c in clients]
    client_id_to_name = {c["id"]: c["name"] for c in clients}

    # ── Pass 1: stale synthesis (per client) ─────────────────────────────
    stale_flags = await _scan_stale(org_id, clients, stale_days)

    # ── Build the candidate set for content checks: recent agent-written
    #    research/brief docs, newest first, capped at sample_size. ─────────
    candidates: list[dict] = []
    for dtype in _RESEARCH_TYPES:
        try:
            docs = await _db.list_documents(org_id, doc_type=dtype)
        except Exception as exc:
            logger.warning("candidate fetch failed for type %s: %s", dtype, exc)
            continue
        for d in docs:
            if (d.get("source") or "") == "agent":
                candidates.append(d)
    # newest first, then cap
    candidates.sort(key=lambda d: _to_dt(d.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    candidates = candidates[:sample_size]

    # Map each candidate doc -> its own linked client name(s), so contamination
    # matching excludes the doc's own client.
    doc_client_names: dict = {}
    for c in clients:
        try:
            linked = await _db.list_documents(org_id, client_id=c["id"])
        except Exception:
            continue
        for d in linked:
            doc_client_names.setdefault(d.get("doc_id"), []).append(c["name"])

    # ── Pass 2 + 3: contamination + no_sources (content-based) ───────────
    contamination_flags, no_source_flags = await _scan_content_flags(
        org_id, candidates, client_names, doc_client_names,
    )

    # ── Write flags into each flagged document's metadata ────────────────
    for f in stale_flags:
        await _flag_document(org_id, f["doc_id"], "stale", {
            "client": f["client"], "lag_days": f["lag_days"],
            "synthesis_date": f["synthesis_date"],
            "newest_finding_date": f["newest_finding_date"],
        })
    for f in contamination_flags:
        await _flag_document(org_id, f["doc_id"], "contamination", {
            "own_clients": f["own_clients"],
            "foreign_clients": sorted({h["client"] for h in f["hits"]}),
            "hits": f["hits"][:5],
        })
    for f in no_source_flags:
        await _flag_document(org_id, f["doc_id"], "no_sources", {
            "own_clients": f["own_clients"], "type": f.get("type"),
        })

    await _write_qa_summary(
        org_id, run_id, stale_flags, contamination_flags, no_source_flags, len(candidates),
    )

    summary = (
        f"Research QA complete. Scanned {len(candidates)} agent research docs. "
        f"Flagged: {len(stale_flags)} stale, {len(contamination_flags)} contamination, "
        f"{len(no_source_flags)} unsourced."
    )
    logger.info(summary)
    return {
        "output": summary,
        "scanned": len(candidates),
        "stale": stale_flags,
        "contamination": contamination_flags,
        "no_sources": no_source_flags,
        "stale_count": len(stale_flags),
        "contamination_count": len(contamination_flags),
        "no_sources_count": len(no_source_flags),
    }
