"""
agents/org.py — Org hygiene agent.

Two-stage pipeline:

Stage 1 — Deterministic (fast, no LLM):
  1. Candidate duplicate contacts — similarity ≥ 0.85
  2. Candidate duplicate clients  — similarity ≥ 0.85
  3. Exact-name orphan linking    — meeting docs with entity metadata but no links
  4. Stale client flagging        — no activity in ≥ 30 days

Stage 2 — LLM sorting (qwen3.5, optional sample cap):
  A. Validate duplicate pairs    — confirmed / possible / false_positive
  B. Semantic orphan linking     — match remaining unlinked docs to clients by
                                   title/content understanding, not just exact name

Stage 1 runs on full data. Stage 2 respects sample_size for testing.
Actions are only applied for high-confidence LLM decisions.

Called from agents/runner.py when agent_type == "org".
"""

import json
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import yaml

import db as _db
import llm

logger = logging.getLogger("whisper.agents.org")

_cfg_path   = Path(__file__).parent.parent / "config.yaml"
_STALE_DAYS = 30
_DEDUP_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# Client-name normalization + duplicate classification
# ---------------------------------------------------------------------------
#
# The goal is to catch real-world dup variants the raw similarity ratio misses:
#   "Acme" vs "Acme AG"        (legal-form suffix)
#   "BBraun"     vs "B.Braun"              (punctuation)
#   "IGS-Apleona-724" vs "Apleona GmbH"    (id-suffix + junk prefix + legal form)
#   "Eumetsat …" vs "EUMETSAT"            (case / long-form)
# while NOT collapsing genuine subsidiaries that differ by a descriptive token:
#   "Bosch Shared Service GmbH" vs "Bosch"  → distinct, flag for review.

# Legal-form / corporate tokens stripped during normalization. Order matters for
# multi-word tokens ("& co", "e.v."): handled by regex below, not this set.
_LEGAL_TOKENS = {
    "ag", "gmbh", "se", "kg", "mbh", "co", "kgaa", "holding", "holdings",
    "group", "gruppe", "ltd", "limited", "inc", "incorporated", "llc", "llp",
    "plc", "corp", "corporation", "company", "sa", "sas", "spa", "srl", "bv",
    "nv", "oy", "ab", "as", "aps", "ohg", "ug", "ev",
}

# Descriptive tokens that, when present in ONE normalized name but not the other,
# signal a distinct subsidiary / division rather than a duplicate. If the extra
# tokens are all "meaningful" like these, we never auto-merge — we flag instead.
_SUBSIDIARY_TOKENS = {
    "shared", "service", "services", "digital", "solutions", "solution",
    "automotive", "engineering", "finance", "financial", "services",
    "systems", "system", "international", "global", "consulting", "logistics",
    "technology", "technologies", "tech", "software", "hardware", "mobility",
    "energy", "energie", "pharma", "pharmaceuticals", "healthcare", "health",
    "medical", "industries", "industrial", "manufacturing", "research",
    "labs", "laboratories", "ventures", "capital", "partners", "management",
    "marketing", "sales", "retail", "insurance", "bank", "banking", "media",
    "deutschland", "germany", "europe", "european", "usa", "america", "asia",
    "iberia", "france", "italia", "espana", "nordic", "benelux", "uk",
    "north", "south", "east", "west", "central", "region", "division",
    "americas", "emea", "apac", "chemicals", "chemie", "materials", "packaging",
    "aerospace", "defense", "defence", "rail", "marine", "aviation", "telecom",
    "telecommunications", "electronics", "electric", "power", "water",
    "infrastructure", "construction", "real", "estate", "properties",
}

# Two-word legal fragments and standalone symbols removed before tokenizing.
_MULTI_LEGAL_RE = re.compile(r"\b(&\s*co|e\.?\s*v\.?|and co)\b", flags=re.IGNORECASE)
_ID_SUFFIX_RE = re.compile(r"[-_\s]+\d+$")          # trailing "-724", "_12"
_ID_PREFIX_RE = re.compile(r"^(?:[a-z]{1,4}[-_])+")   # junk code prefix "igs-"


def _domain(url: str) -> str:
    """Extract a bare registrable-ish domain from a website value.

    'https://www.acme.com/de' -> 'acme.com'. Best-effort: strips
    scheme, leading 'www.', path/query, and lowercases. Returns '' if empty.
    """
    if not url or not isinstance(url, str):
        return ""
    u = url.strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", u)   # scheme
    u = u.split("/")[0].split("?")[0].split("#")[0]  # host only
    u = u.split("@")[-1]                              # strip userinfo
    u = u.split(":")[0]                               # strip port
    if u.startswith("www."):
        u = u[4:]
    return u.strip(". ")


def _normalize_client_name(name: str) -> str:
    """Normalize a client name for duplicate comparison.

    lowercase → strip 'GmbH & Co'-style fragments → strip punctuation →
    drop junk code prefixes and trailing '-<digits>' id-suffixes →
    remove legal-form tokens → collapse whitespace.
    """
    if not name:
        return ""
    s = name.lower().strip()
    s = _MULTI_LEGAL_RE.sub(" ", s)
    # Strip a trailing numeric id-suffix before punctuation removal so the
    # dash boundary is still present ("igs-apleona-724" -> "igs-apleona").
    s = _ID_SUFFIX_RE.sub("", s)
    s = _ID_PREFIX_RE.sub("", s)
    # Replace remaining punctuation with spaces; letters/digits/space survive.
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = s.replace("_", " ")
    tokens = [t for t in s.split() if t and t not in _LEGAL_TOKENS]
    return " ".join(tokens).strip()


def _is_subsidiary_pair(a_norm: str, b_norm: str) -> bool:
    """True if the two normalized names look like distinct subsidiaries/divisions
    of the same parent (same distinctive brand stem, differing only by a
    descriptive division token).

    Guards against auto-merging e.g. 'Bosch Shared Service' vs 'Bosch'. To avoid
    over-flagging unrelated companies that merely share a generic word
    ('X Deutschland' vs 'Y Deutschland'), we require BOTH:
      1. the two names share their FIRST (lead/brand) token, AND
      2. the shorter name's tokens are a subset of the longer's (the longer name
         is the shorter brand plus only extra tokens), AND
      3. at least one of those extra tokens is a known descriptive/division token.
    """
    if not a_norm or not b_norm or a_norm == b_norm:
        return False
    ta, tb = a_norm.split(), b_norm.split()
    if not ta or not tb:
        return False
    # 1. Shared distinctive lead token.
    if ta[0] != tb[0]:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # 2. Shorter brand fully contained in the longer name (proper subset —
    #    equal sets can't happen here because a_norm != b_norm).
    if not set(short).issubset(set(long_)):
        return False
    # 3. The extra tokens must include a descriptive/division marker.
    extra = set(long_) - set(short)
    extra = {t for t in extra if not t.isdigit()}
    return any(t in _SUBSIDIARY_TOKENS for t in extra)


def _classify_client_pair(a: dict, b: dict) -> Optional[dict]:
    """Classify a client pair as a duplicate candidate.

    Returns a dict with keys {confidence: 'high'|'needs_review', reason, ...}
    or None if the pair isn't a duplicate candidate at all.

    HIGH (auto-merge): ONLY when normalized names are equal (incl. ignoring
        spacing), and the subsidiary guard doesn't trip. Nothing else auto-merges.
    NEEDS_REVIEW: shared website domain, long-form/acronym match, or high raw
        similarity that isn't an exact normalized match; or the subsidiary guard.
    """
    a_norm = _normalize_client_name(a["name"])
    b_norm = _normalize_client_name(b["name"])

    # A blank name (or one that normalizes to nothing) is never a dup candidate.
    if not (a.get("name") or "").strip() or not (b.get("name") or "").strip():
        return None
    if not a_norm and not b_norm:
        return None

    meta_a = a.get("metadata") or {}
    meta_b = b.get("metadata") or {}
    if isinstance(meta_a, str):
        try:
            meta_a = json.loads(meta_a)
        except Exception:
            meta_a = {}
    if isinstance(meta_b, str):
        try:
            meta_b = json.loads(meta_b)
        except Exception:
            meta_b = {}
    dom_a = _domain(meta_a.get("website", "") or "")
    dom_b = _domain(meta_b.get("website", "") or "")
    shared_domain = bool(dom_a) and dom_a == dom_b

    subsidiary = _is_subsidiary_pair(a_norm, b_norm)
    norm_equal = bool(a_norm) and a_norm == b_norm
    # Spaceless equality catches punctuation-only splits the space-preserving
    # normalization misses, e.g. "BBraun" vs "B.Braun" -> both "bbraun".
    a_flat, b_flat = a_norm.replace(" ", ""), b_norm.replace(" ", "")
    flat_equal = len(a_flat) >= 3 and a_flat == b_flat
    raw_score = _similarity(a["name"], b["name"])

    # Subsidiary guard always wins: never auto-merge a likely subsidiary.
    if subsidiary:
        return {
            "confidence": "needs_review",
            "reason": "likely distinct subsidiary/division (shared stem + descriptive token)",
            "normalized": [a_norm, b_norm],
            "similarity": round(raw_score, 3),
        }

    if norm_equal or flat_equal:
        return {
            "confidence": "high",
            "reason": "normalized names identical" if norm_equal else "normalized names identical ignoring spacing",
            "normalized": [a_norm, b_norm],
            "similarity": round(raw_score, 3),
        }
    if shared_domain:
        # A shared domain alone is NOT enough to auto-merge: umbrella / parent
        # domains (e.g. ihk.de, big-corp domains shared across legal entities)
        # would cause false merges. Require name affinity too — a shared lead
        # brand token, a subset relationship, or decent raw similarity.
        ta_s, tb_s = set(a_norm.split()), set(b_norm.split())
        ta_l, tb_l = a_norm.split(), b_norm.split()
        shared_lead = bool(ta_l) and bool(tb_l) and ta_l[0] == tb_l[0]
        subset = bool(ta_s) and bool(tb_s) and (ta_s <= tb_s or tb_s <= ta_s)
        name_affinity = shared_lead or subset or raw_score >= _DEDUP_THRESHOLD
        # Even with name affinity, if the two names differ by a descriptive
        # division token (e.g. "Bilfinger Construction" vs "Bilfinger & Berger
        # Bau"), they're likely distinct divisions on the same corporate domain
        # — flag, don't auto-merge.
        differ_by_descriptive = bool(
            (ta_s ^ tb_s) & _SUBSIDIARY_TOKENS
        )
        # A shared domain never auto-merges — only exact normalized-name equality
        # does. Same-domain distinct legal entities (holding vs opco, former
        # names, umbrella domains) are held for human review instead.
        if not name_affinity:
            reason = f"shared website domain ({dom_a}) but dissimilar names — verify (umbrella domain?)"
        elif differ_by_descriptive:
            reason = f"shared website domain ({dom_a}) but names differ by a division token — verify"
        else:
            reason = f"shared website domain ({dom_a}) + similar name — verify (not an exact match)"
        return {
            "confidence": "needs_review",
            "reason": reason,
            "normalized": [a_norm, b_norm],
            "similarity": round(raw_score, 3),
        }
    # Long-form vs distinctive brand token: one name is a single distinctive
    # token that is also the FIRST token of the other (e.g. "EUMETSAT" vs
    # "Eumetsat Europäische Meteorologische Satelliten"). Subsidiary guard has
    # already ruled out descriptive-token divisions, so this is a safe HIGH.
    ta, tb = a_norm.split(), b_norm.split()
    short_toks, long_toks = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if (
        len(short_toks) == 1
        and len(long_toks) > 1
        and len(short_toks[0]) >= 5
        and long_toks
        and short_toks[0] == long_toks[0]
    ):
        # Long-form / acronym expansions are usually the same entity, but can be
        # a holding vs an operating company (Takko Holding vs Takko Fashion), so
        # they're held for review — only exact normalized equality auto-merges.
        return {
            "confidence": "needs_review",
            "reason": "same lead brand token, differing extra words — verify (holding vs op-co?)",
            "normalized": [a_norm, b_norm],
            "similarity": round(raw_score, 3),
        }
    if raw_score >= _DEDUP_THRESHOLD:
        return {
            "confidence": "needs_review",
            "reason": f"high name similarity ({round(raw_score, 3)}) but not an exact match",
            "normalized": [a_norm, b_norm],
            "similarity": round(raw_score, 3),
        }
    return None


def _days_since(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _load_brain_config() -> tuple[str, str]:
    """Return (provider, model) for the default agent role (llm.py)."""
    provider, model = llm.resolve("default")
    return provider.name, model


def _parse_json_response(raw: str) -> dict:
    """Extract the first JSON object from an LLM response."""
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Stage 1 — Deterministic passes
# ---------------------------------------------------------------------------

async def _find_duplicate_contacts(org_id: int) -> list[dict]:
    contacts = await _db.list_contacts(org_id)
    duplicates, seen = [], set()
    for i, a in enumerate(contacts):
        for b in contacts[i + 1:]:
            key = tuple(sorted([a["id"], b["id"]]))
            if key in seen:
                continue
            score = _similarity(a["name"], b["name"])
            if score >= _DEDUP_THRESHOLD:
                seen.add(key)
                duplicates.append({
                    "a": {"id": a["id"], "name": a["name"]},
                    "b": {"id": b["id"], "name": b["name"]},
                    "similarity": round(score, 3),
                    "entity_type": "contact",
                })
    return duplicates


async def _find_duplicate_clients(org_id: int) -> list[dict]:
    """Find candidate duplicate clients using normalization + website domain.

    Each returned pair carries a "confidence" ('high' | 'needs_review'):
      - high         → safe to auto-merge (normalized-equal or shared domain,
                       and NOT a suspected subsidiary)
      - needs_review → tag for human review (subsidiary guard tripped, or only
                       fuzzy name similarity)
    """
    clients = await _db.list_clients(org_id)
    duplicates, seen = [], set()
    for i, a in enumerate(clients):
        for b in clients[i + 1:]:
            key = tuple(sorted([a["id"], b["id"]]))
            if key in seen:
                continue
            verdict = _classify_client_pair(a, b)
            if verdict is None:
                continue
            seen.add(key)
            duplicates.append({
                "a": {"id": a["id"], "name": a["name"]},
                "b": {"id": b["id"], "name": b["name"]},
                "similarity": verdict["similarity"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "entity_type": "client",
            })
    return duplicates


async def _merge_clients(org_id: int, dupe_id: int, canonical_id: int) -> Optional[dict]:
    """Safely merge duplicate client `dupe_id` into `canonical_id`.

    Delegates the transactional work to db.merge_clients (re-point
    document_links + contacts.client_id, union owner_ids/focus_user_ids, delete
    dupe — all in one transaction). Returns the merge summary dict, or None on
    failure. Best-effort: any exception is logged and swallowed so a hygiene run
    keeps going.
    """
    if dupe_id == canonical_id:
        return None
    try:
        result = await _db.merge_clients(org_id, dupe_id, canonical_id)
        if result:
            logger.info(
                "Merged client %s (#%d) into %s (#%d): %d links moved, %d dropped, %d contacts",
                result.get("dupe_name"), dupe_id,
                result.get("canonical_name"), canonical_id,
                result.get("links_moved", 0), result.get("links_dropped", 0),
                result.get("contacts_moved", 0),
            )
        return result
    except Exception as e:
        logger.warning("Merge failed (dupe #%d -> canonical #%d): %s", dupe_id, canonical_id, e)
        return None


async def _auto_merge_high_confidence(org_id: int, client_pairs: list[dict]) -> list[dict]:
    """Auto-merge every HIGH-confidence client dup pair.

    Canonical = the client with MORE linked documents; tiebreak = older
    created_at. Handles transitive chains (A↔B, B↔C) via union-find remapping so
    an already-merged id is never targeted again. Non-high pairs are ignored
    here (they are tagged 'needs_review' elsewhere).

    Returns a list of merge summary dicts (one per successful merge).
    """
    high = [p for p in client_pairs if p.get("confidence") == "high"]
    if not high:
        return []

    # doc-count + created_at drive canonical selection.
    try:
        doc_counts = await _db.count_client_document_links(org_id)
    except Exception as e:
        logger.warning("count_client_document_links failed, defaulting to 0: %s", e)
        doc_counts = {}

    clients = {c["id"]: c for c in await _db.list_clients(org_id)}

    def _created_at(cid: int):
        c = clients.get(cid) or {}
        return c.get("created_at")

    def _pick_canonical(x: int, y: int) -> tuple[int, int]:
        """Return (canonical_id, dupe_id). More docs wins; tiebreak older created_at;
        final tiebreak smaller id (stable)."""
        cx, cy = doc_counts.get(x, 0), doc_counts.get(y, 0)
        if cx != cy:
            return (x, y) if cx > cy else (y, x)
        ax, ay = _created_at(x), _created_at(y)
        if ax is not None and ay is not None and ax != ay:
            return (x, y) if ax < ay else (y, x)
        return (x, y) if x < y else (y, x)

    # Union-find so transitive dups collapse to a single canonical and we never
    # merge into an id that itself got merged away this run.
    parent: dict[int, int] = {}

    def _find(i: int) -> int:
        parent.setdefault(i, i)
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    merges: list[dict] = []
    for pair in high:
        a_id, b_id = pair["a"]["id"], pair["b"]["id"]
        ra, rb = _find(a_id), _find(b_id)
        if ra == rb:
            continue  # already unified
        canonical, dupe = _pick_canonical(ra, rb)
        result = await _merge_clients(org_id, dupe, canonical)
        if result:
            merges.append(result)
            # doc links now live on canonical — fold the count for later picks.
            doc_counts[canonical] = doc_counts.get(canonical, 0) + doc_counts.get(dupe, 0)
            doc_counts.pop(dupe, None)
            parent[dupe] = canonical
            parent[canonical] = canonical
        else:
            # Merge failed — still union so we don't retry the pair endlessly.
            parent[dupe] = canonical
            parent[canonical] = canonical
    return merges


async def _tag_needs_review_clients(org_id: int, client_pairs: list[dict]) -> list[dict]:
    """Tag each NEEDS_REVIEW client dup pair's records with
    metadata.duplicate_status='needs_review' (+ duplicate_of the other name).
    Preserves the previous 'tag, don't merge' behaviour for uncertain pairs.
    Returns the list of tagged pairs (for the health report)."""
    review = [p for p in client_pairs if p.get("confidence") == "needs_review"]
    for pair in review:
        a_name, b_name = pair["a"]["name"], pair["b"]["name"]
        for name, other in ((a_name, b_name), (b_name, a_name)):
            try:
                await _db.update_client_metadata(
                    org_id, name,
                    {"duplicate_status": "needs_review", "duplicate_of": other},
                )
            except Exception as e:
                logger.warning("Tag needs_review failed for %s: %s", name, e)
    return review


def _resolve_meta(doc: dict) -> dict:
    """Return metadata as a dict regardless of whether it was stored as str or dict."""
    meta = doc.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta


async def _link_orphans_exact(org_id: int) -> tuple[int, list[dict]]:
    """Exact-name match across meetings, findings, osint, and research docs.

    For meetings: matches metadata.companies / metadata.people.
    For findings/osint/research: also checks metadata.subject against clients.
    Returns (links_created, remaining_unlinked_docs).
    """
    all_types = ["meeting", "finding", "osint", "research"]
    orphans: list[dict] = []
    for doc_type in all_types:
        orphans.extend(await _db.list_unlinked_documents(org_id, doc_type=doc_type))

    clients  = {c["name"].lower(): c for c in await _db.list_clients(org_id)}
    contacts = {c["name"].lower(): c for c in await _db.list_contacts(org_id)}
    linked   = 0
    still_unlinked = []

    for doc in orphans:
        meta      = _resolve_meta(doc)
        companies = meta.get("companies", []) or []
        people    = meta.get("people", []) or []
        subject   = (meta.get("subject") or "").strip()
        doc_linked = False

        # subject field used by findings, osint, research
        if subject:
            client = clients.get(subject.lower())
            if client:
                try:
                    await _db.link_document(doc["id"], "client", client["id"])
                    linked += 1
                    doc_linked = True
                    logger.info("Exact link (subject): doc %s → client %s", doc["doc_id"], subject)
                except Exception:
                    pass

        for company in companies:
            name   = (company if isinstance(company, str) else company.get("name", "")).strip()
            client = clients.get(name.lower())
            if client:
                try:
                    await _db.link_document(doc["id"], "client", client["id"])
                    linked += 1
                    doc_linked = True
                    logger.info("Exact link (company): doc %s → client %s", doc["doc_id"], name)
                except Exception:
                    pass

        for person in people:
            name    = (person if isinstance(person, str) else person.get("name", "")).strip()
            contact = contacts.get(name.lower())
            if contact:
                try:
                    await _db.link_document(doc["id"], "contact", contact["id"])
                    linked += 1
                    doc_linked = True
                    logger.info("Exact link (person): doc %s → contact %s", doc["doc_id"], name)
                except Exception:
                    pass

        if not doc_linked:
            still_unlinked.append(doc)

    return linked, still_unlinked


async def _flag_stale_clients(org_id: int) -> list[str]:
    clients = await _db.list_clients(org_id)
    flagged = []
    for client in clients:
        days = _days_since(client.get("last_activity"))
        already_stale = (client.get("metadata") or {}).get("stale", False)
        if days is not None and days >= _STALE_DAYS and not already_stale:
            await _db.update_client_metadata(
                org_id, client["name"],
                {"stale": True, "stale_since": datetime.now(timezone.utc).date().isoformat()},
            )
            flagged.append(client["name"])
            logger.info("Stale: %s (%d days)", client["name"], days)
        elif days is not None and days < _STALE_DAYS and already_stale:
            await _db.update_client_metadata(org_id, client["name"], {"stale": False, "stale_since": None})
    return flagged


# ---------------------------------------------------------------------------
# Stage 2 — LLM sorting
# ---------------------------------------------------------------------------

async def _llm_sort_duplicates(
    org_id: int,
    candidates: list[dict],
    model: str,
    sample_size: Optional[int],
) -> dict:
    """Ask LLM to classify each candidate pair. Returns verdict dict."""
    sample = candidates[:sample_size] if sample_size else candidates
    if not sample:
        return {"contact_pairs": [], "client_pairs": []}

    contact_pairs = [c for c in sample if c["entity_type"] == "contact"]
    client_pairs  = [c for c in sample if c["entity_type"] == "client"]

    prompt = f"""You are auditing a sales CRM for duplicate records.

Contact pairs flagged as potential duplicates (by name similarity):
{json.dumps([{"a": p["a"]["name"], "b": p["b"]["name"], "similarity": p["similarity"]} for p in contact_pairs], indent=2)}

Client/company pairs flagged as potential duplicates:
{json.dumps([{"a": p["a"]["name"], "b": p["b"]["name"], "similarity": p["similarity"]} for p in client_pairs], indent=2)}

For each pair, classify:
- "confirmed": definitely the same entity (same name variant, abbreviation, or clear duplicate)
- "possible": could be the same, needs human review
- "false_positive": different entities with similar names

Respond with JSON only, no explanation outside the JSON:
{{
  "contact_pairs": [{{"a": "Name A", "b": "Name B", "verdict": "confirmed|possible|false_positive", "reason": "one sentence"}}],
  "client_pairs":  [{{"a": "Name A", "b": "Name B", "verdict": "confirmed|possible|false_positive", "reason": "one sentence"}}]
}}"""

    raw = await llm.acomplete(prompt, role="default", model=model, timeout=180, org_id=org_id)
    logger.info("LLM dedup raw: %s", raw[:300])
    return _parse_json_response(raw)


_ORPHAN_BATCH = 15  # docs per LLM call to stay within context + timeout


async def _llm_sort_orphans(
    org_id: int,
    unlinked_docs: list[dict],
    model: str,
    sample_size: Optional[int],
) -> list[dict]:
    """Ask LLM to match unlinked docs to clients. Processes in batches.
    Returns list of link decisions."""
    sample  = unlinked_docs[:sample_size] if sample_size else unlinked_docs
    clients = await _db.list_clients(org_id)
    if not sample or not clients:
        return []

    client_names = [c["name"] for c in clients]
    all_links: list[dict] = []

    for batch_start in range(0, len(sample), _ORPHAN_BATCH):
        batch = sample[batch_start: batch_start + _ORPHAN_BATCH]
        doc_summaries = []
        for d in batch:
            meta = _resolve_meta(d)
            doc_summaries.append({
                "doc_id": d["doc_id"],
                "type": d["type"],
                "title": d["title"],
                "subject": meta.get("subject", ""),
                "topics": meta.get("topics", [])[:5],
            })

        prompt = f"""You are linking research documents and meeting notes to client accounts in a sales CRM.

Known clients:
{json.dumps(client_names, indent=2)}

Unlinked documents (type, title, subject tag, topic tags):
{json.dumps(doc_summaries, indent=2)}

For each document, identify the best matching client from the known clients list, or null if none fits.
Use the subject field and title as primary signals. Only set confidence high/medium if you are reasonably sure.

Respond with JSON only:
{{
  "links": [
    {{"doc_id": "...", "client": "ClientName or null", "confidence": "high|medium|low", "reason": "one sentence"}}
  ]
}}"""

        try:
            raw = await llm.acomplete(prompt, role="default", model=model, timeout=180, org_id=org_id)
            logger.info("LLM orphan batch %d raw: %s", batch_start, raw[:200])
            data = _parse_json_response(raw)
            all_links.extend(data.get("links", []))
        except Exception as e:
            logger.warning("LLM orphan batch %d failed: %s", batch_start, e)

    return all_links


async def _apply_llm_decisions(
    org_id: int,
    dup_verdicts: dict,
    orphan_links: list[dict],
    all_candidates: list[dict],
    unlinked_docs: list[dict],
) -> dict:
    """Apply LLM decisions to the DB. Returns summary of actions taken."""
    confirmed_dups, possible_dups, fp_dups = [], [], []
    semantic_links = 0

    # --- Duplicate verdicts ---
    contact_lookup = {c["name"]: c for c in await _db.list_contacts(org_id)}
    client_lookup  = {c["name"]: c for c in await _db.list_clients(org_id)}

    for verdict in dup_verdicts.get("contact_pairs", []):
        v = verdict.get("verdict", "")
        pair = {"a": verdict.get("a"), "b": verdict.get("b"), "reason": verdict.get("reason", ""), "entity_type": "contact"}
        if v == "confirmed":
            confirmed_dups.append(pair)
            # tag both records with duplicate_of in metadata
            for name in [verdict.get("a"), verdict.get("b")]:
                other = verdict.get("b") if name == verdict.get("a") else verdict.get("a")
                if name and name in contact_lookup:
                    await _db.update_contact_metadata(org_id, name, {"duplicate_of": other, "duplicate_status": "confirmed"})
        elif v == "possible":
            possible_dups.append(pair)
            for name in [verdict.get("a"), verdict.get("b")]:
                if name and name in contact_lookup:
                    await _db.update_contact_metadata(org_id, name, {"duplicate_status": "needs_review"})
        else:
            fp_dups.append(pair)

    for verdict in dup_verdicts.get("client_pairs", []):
        v = verdict.get("verdict", "")
        pair = {"a": verdict.get("a"), "b": verdict.get("b"), "reason": verdict.get("reason", ""), "entity_type": "client"}
        if v == "confirmed":
            confirmed_dups.append(pair)
            for name in [verdict.get("a"), verdict.get("b")]:
                other = verdict.get("b") if name == verdict.get("a") else verdict.get("a")
                if name and name in client_lookup:
                    await _db.update_client_metadata(org_id, name, {"duplicate_of": other, "duplicate_status": "confirmed"})
        elif v == "possible":
            possible_dups.append(pair)
            for name in [verdict.get("a"), verdict.get("b")]:
                if name and name in client_lookup:
                    await _db.update_client_metadata(org_id, name, {"duplicate_status": "needs_review"})
        else:
            fp_dups.append(pair)

    # --- Orphan links (high + medium confidence only) ---
    doc_id_map = {d["doc_id"]: d for d in unlinked_docs}
    for link in orphan_links:
        doc_id     = link.get("doc_id")
        client_name = link.get("client")
        confidence  = link.get("confidence", "low")
        if not doc_id or not client_name or confidence == "low":
            continue
        doc    = doc_id_map.get(doc_id)
        client = client_lookup.get(client_name)
        if doc and client:
            try:
                await _db.link_document(doc["id"], "client", client["id"])
                semantic_links += 1
                logger.info("Semantic link (%s): doc %s → client %s", confidence, doc_id, client_name)
            except Exception:
                pass

    return {
        "confirmed_duplicates": confirmed_dups,
        "possible_duplicates":  possible_dups,
        "false_positives":      fp_dups,
        "semantic_links":       semantic_links,
    }


# ---------------------------------------------------------------------------
# Health summary
# ---------------------------------------------------------------------------

async def _write_health_summary(
    org_id: int,
    run_id: int,
    exact_links: int,
    stale_clients: list[str],
    llm_decisions: dict,
    sample_size: Optional[int],
    client_merges: Optional[list[dict]] = None,
    review_client_pairs: Optional[list[dict]] = None,
) -> None:
    now      = datetime.now(timezone.utc)
    date_str = now.date().isoformat()

    client_merges       = client_merges or []
    review_client_pairs = review_client_pairs or []

    confirmed = llm_decisions.get("confirmed_duplicates", [])
    possible  = llm_decisions.get("possible_duplicates", [])
    fp        = llm_decisions.get("false_positives", [])
    sem_links = llm_decisions.get("semantic_links", 0)

    sample_note = f" *(sample: first {sample_size} candidates)*" if sample_size else ""

    def _dup_lines(pairs):
        return "\n".join(
            f"- **{p['a']}** ↔ **{p['b']}** [{p['entity_type']}] — {p.get('reason','')}"
            for p in pairs
        ) or "- None"

    def _merge_lines(merges):
        return "\n".join(
            f"- **{m.get('dupe_name')}** → **{m.get('canonical_name')}** "
            f"({m.get('links_moved', 0)} links moved, {m.get('contacts_moved', 0)} contacts)"
            for m in merges
        ) or "- None"

    def _review_lines(pairs):
        return "\n".join(
            f"- **{p['a']['name']}** ↔ **{p['b']['name']}** — {p.get('reason', '')}"
            for p in pairs
        ) or "- None"

    content = f"""## Org Health — {date_str}

### Clients auto-merged ({len(client_merges)})
{_merge_lines(client_merges)}

### Clients flagged for review ({len(review_client_pairs)})
{_review_lines(review_client_pairs)}

### Duplicate records — confirmed ({len(confirmed)}){sample_note}
{_dup_lines(confirmed)}

### Duplicate records — needs review ({len(possible)}){sample_note}
{_dup_lines(possible)}

### Duplicate records — false positives ({len(fp)}){sample_note}
{_dup_lines(fp)}

### Documents linked
- Exact-name matches: {exact_links}
- Semantic matches (LLM): {sem_links}{sample_note}

### Stale clients ({len(stale_clients)})
{chr(10).join(f'- {n}' for n in stale_clients) or '- None'}
"""

    cfg = yaml.safe_load(open(_cfg_path))
    vault_path_str = cfg.get("vault_path", "").strip()
    if vault_path_str:
        vault_path = Path(vault_path_str) / "research"
        vault_path.mkdir(parents=True, exist_ok=True)
        (vault_path / f"{date_str}-org-health.md").write_text(
            f"---\nsource: agent\nagent_run_id: {run_id}\ntype: org_health\ndate: {date_str}\n---\n\n{content}",
            encoding="utf-8",
        )

    await _db.index_document(
        org_id=org_id,
        doc_id=f"org-health-{date_str}",
        doc_type="note",
        title=f"Org Health Report — {date_str}",
        content=content,
        metadata={
            "date": date_str,
            "clients_merged": len(client_merges),
            "clients_flagged_review": len(review_client_pairs),
            "confirmed_duplicates": len(confirmed),
            "possible_duplicates": len(possible),
            "exact_links": exact_links,
            "semantic_links": sem_links,
            "stale_flagged": len(stale_clients),
        },
        embedding=[],
        source="agent",
        agent_run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_org_agent(
    run_id: int,
    org_id: int,
    task: str,
    sample_size: Optional[int] = None,
) -> dict:
    """
    Run org hygiene — deterministic passes first, then LLM sorting.

    sample_size: if set, only the first N candidates are sent to the LLM.
                 Pass a small number (e.g. 5) for testing.
    """
    logger.info("Org agent run %d | org %d | sample_size=%s", run_id, org_id, sample_size)
    brain, model = _load_brain_config()

    # ── Stage 1: deterministic ────────────────────────────────────────────
    dup_contacts             = await _find_duplicate_contacts(org_id)
    dup_clients              = await _find_duplicate_clients(org_id)
    exact_links, still_unlinked = await _link_orphans_exact(org_id)
    stale_clients            = await _flag_stale_clients(org_id)

    # ── Stage 1b: auto-merge HIGH-confidence client dups ─────────────────
    # HIGH-confidence client pairs get merged here (deterministic, no LLM).
    # NEEDS_REVIEW client pairs are tagged, not merged. Both are removed from
    # the candidate list sent to the LLM (the merged rows no longer exist).
    high_client_pairs   = [p for p in dup_clients if p.get("confidence") == "high"]
    review_client_pairs = [p for p in dup_clients if p.get("confidence") == "needs_review"]
    client_merges = await _auto_merge_high_confidence(org_id, high_client_pairs)
    await _tag_needs_review_clients(org_id, review_client_pairs)

    # Contacts still flow through the LLM path; merged/flagged clients do not.
    all_candidates = dup_contacts
    logger.info(
        "Stage 1: %d contact dups, %d client dups (%d high→merged %d, %d review), "
        "%d exact links, %d still unlinked, %d stale",
        len(dup_contacts), len(dup_clients), len(high_client_pairs), len(client_merges),
        len(review_client_pairs), exact_links, len(still_unlinked), len(stale_clients),
    )

    # ── Stage 2: LLM sorting (contacts + orphan links) ───────────────────
    llm_decisions = {"confirmed_duplicates": [], "possible_duplicates": [], "false_positives": [], "semantic_links": 0}
    try:
        dup_verdicts = await _llm_sort_duplicates(org_id, all_candidates, model, sample_size)
        orphan_links = await _llm_sort_orphans(org_id, still_unlinked, model, sample_size)
        llm_decisions = await _apply_llm_decisions(
            org_id, dup_verdicts, orphan_links, all_candidates, still_unlinked,
        )
        logger.info(
            "Stage 2: %d confirmed, %d possible, %d fp, %d semantic links",
            len(llm_decisions["confirmed_duplicates"]),
            len(llm_decisions["possible_duplicates"]),
            len(llm_decisions["false_positives"]),
            llm_decisions["semantic_links"],
        )
    except Exception as e:
        logger.warning("LLM sort failed (deterministic results still applied): %s", e)

    await _write_health_summary(
        org_id, run_id, exact_links, stale_clients, llm_decisions, sample_size,
        client_merges=client_merges, review_client_pairs=review_client_pairs,
    )

    summary = (
        f"Org hygiene complete. "
        f"Clients merged: {len(client_merges)}, flagged for review: {len(review_client_pairs)}. "
        f"Contact dup candidates: {len(all_candidates)} "
        f"(confirmed {len(llm_decisions['confirmed_duplicates'])}, "
        f"review {len(llm_decisions['possible_duplicates'])}, "
        f"fp {len(llm_decisions['false_positives'])}). "
        f"Links: {exact_links} exact + {llm_decisions['semantic_links']} semantic. "
        f"Stale clients: {len(stale_clients)}."
    )
    logger.info(summary)
    return {
        "output": summary,
        "tool_calls": [],
        "iterations": 2,
        "llm_decisions": llm_decisions,
        "client_merges": client_merges,
        "review_client_pairs": review_client_pairs,
        "exact_links": exact_links,
        "stale_clients": stale_clients,
    }
