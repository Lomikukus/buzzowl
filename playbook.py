"""
playbook.py — the self-improving agent (WP4).

Two kinds of memory, both stored as `documents` rows (no new tables, no
migration — `documents.type` is free-form TEXT):

  site_playbook   doc_id=f"site-playbook-{domain}"   org-wide, NOT client-linked
      What a specific website looks like to an agent: its careers URL and
      tier, its newsroom URLs, news queries that worked, whether it needs a
      JS-rendering fetch, a cookie wall, URLs that blocked/failed us, search
      queries that did/didn't pay off, and a handful of free-text navigation
      notes. Written after every research/osint/pain_point_research run via
      reflect_on_run(), and by the Python jobs/news writers via record().

  agent_lessons   doc_id="agent-lessons-org"          org-wide, NOT client-linked
      Cross-website lessons (e.g. "prefer sitemap.xml over homepage crawling
      when the homepage returns no readable content") PROPOSED by a weekly
      LLM pass over all playbooks + this week's failed runs. A lesson is
      never used until a human approves it (POST /api/agents/lessons/{id}
      /decision) — this module never auto-approves anything.

Hard rule (ChatGPT-subscription bridge is text-only): every LLM call in this
file goes through `await llm.acomplete(prompt, role="research", timeout=180,
org_id=org_id)` — never agents/runner.py, never a bare requests/httpx call.

Concurrency: the server is a single uvicorn process (no workers), so a
per-domain asyncio.Lock is enough to keep concurrent research/jobs/news
writers for the same domain from clobbering each other's patch.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from context import db_module
import llm

logger = logging.getLogger("buzzowl.playbook")

# ---------------------------------------------------------------------------
# Bounds (kept small — this content is re-read into every future task/prompt)
# ---------------------------------------------------------------------------

_MAX_BLOCKED_URLS = 20
_MAX_QUERY_LIST = 15
_MAX_NOTES = 8
_MAX_SOURCES_OF_TRUTH = 10
_MAX_BLOCK_CHARS = 1200
_MAX_LESSONS_PROPOSED = 20
_MAX_LESSONS_DECIDED = 100
_MAX_LESSONS_IN_BLOCK = 8
_MAX_LESSON_PROPOSALS_PER_PASS = 5
_DEDUPE_JACCARD_THRESHOLD = 0.6
_REFLECT_LLM_MIN_TOOL_CALLS = 8
_MAX_NAV_NOTES = 4

LESSONS_DOC_ID = "agent-lessons-org"

_VALID_SCOPES = ("jobs", "news", "research", "all")

# Site-playbook writer locks, keyed by "{org_id}:{domain}" — a single
# uvicorn process (no workers) makes an in-process lock sufficient; a
# concurrent research + jobs_scan + news_scan for the same domain is the
# exact race this exists to serialize.
_locks: dict = {}


def _lock_for(key: str):
    import asyncio
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_id(domain: str) -> str:
    return f"site-playbook-{domain}"


# ---------------------------------------------------------------------------
# domain / client helpers
# ---------------------------------------------------------------------------

def domain_of(client: dict) -> str:
    """Bare domain (no www) from a client's metadata.website. Imported inside
    the function — routers.pipeline does not import this module at load time,
    but importing it there at module level would still risk a cycle later."""
    from routers.pipeline import _client_domain
    return _client_domain(client)


async def resolve_client_exact(org_id: int, subject: str) -> Optional[dict]:
    """Exact (case-insensitive) name match on top of db.get_client.

    db.get_client is FUZZY (trigram similarity > 0.6) — it exists to help a
    human search-as-you-type, not to decide whose playbook a task gets. Using
    it directly here would let e.g. subject="org" (the system/monitor agent's
    placeholder subject) silently match a real client and leak its playbook
    into an unrelated run.
    """
    if not subject or db_module is None:
        return None
    try:
        client = await db_module.get_client(org_id, subject)
    except Exception:
        logger.debug("playbook.resolve_client_exact: get_client failed", exc_info=True)
        return None
    if not client:
        return None
    if (client.get("name") or "").strip().lower() != subject.strip().lower():
        return None
    return client


# ---------------------------------------------------------------------------
# site_playbook: load / merge / record
# ---------------------------------------------------------------------------

async def load(org_id: int, domain: str) -> Optional[dict]:
    if not domain or db_module is None:
        return None
    try:
        doc = await db_module.get_document(org_id, _doc_id(domain))
    except Exception:
        logger.debug("playbook.load: get_document failed for domain=%s", domain, exc_info=True)
        return None
    if not doc:
        return None
    meta = doc.get("metadata") or {}
    return meta or None


def _merge_list(old, new, cap: int) -> list:
    """Union, preserving order (existing entries first), bounded to the
    newest `cap` entries (a growing tail — new patches append)."""
    cur = list(old) if isinstance(old, list) else []
    for item in (new or []):
        if item not in cur:
            cur.append(item)
    return cur[-cap:] if cap else cur


def _merge_blocked_urls(old, new, cap: int) -> list:
    """Union by `url`, not by whole-dict equality: every re-block of the same
    URL carries a fresh `at`, so comparing whole dicts (the generic
    `_merge_list`) never sees a repeat as a duplicate and the cap fills up
    with N copies of the same URL instead of N distinct ones. Keeps the
    newest `at` for a given URL and preserves first-seen order."""
    by_url: dict = {}
    order: list = []
    for item in list(old or []) + list(new or []):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        if url not in by_url:
            order.append(url)
        by_url[url] = item  # later entry (newer `at`) wins
    merged = [by_url[u] for u in order]
    return merged[-cap:] if cap else merged


def _merge_dict_field(old: dict, new: dict) -> dict:
    """One level deep: scalars overwrite, lists union+bound. Keys the patch
    doesn't mention (e.g. careers.url when a patch only sets last_failure_at)
    are left untouched."""
    merged = dict(old or {})
    for k, v in (new or {}).items():
        if isinstance(v, list):
            merged[k] = _merge_list(merged.get(k), v, _MAX_QUERY_LIST)
        else:
            merged[k] = v
    return merged


_TOP_DICT_FIELDS = ("careers", "newsroom", "news")
_TOP_LIST_CAPS = {
    "blocked_urls": _MAX_BLOCKED_URLS,
    "good_queries": _MAX_QUERY_LIST,
    "failed_queries": _MAX_QUERY_LIST,
    "notes": _MAX_NOTES,
}


def _merge(existing: dict, patch: dict, *, domain: str, website: str) -> dict:
    """Deterministic merge: scalars overwrite, the three dict fields deep-merge
    one level, list fields union-preserving-order + bound."""
    merged = dict(existing or {})
    merged.setdefault("domain", domain)
    if website:
        merged["website"] = website
    for key, value in (patch or {}).items():
        if key in _TOP_DICT_FIELDS and isinstance(value, dict):
            merged[key] = _merge_dict_field(merged.get(key) or {}, value)
        elif key in _TOP_LIST_CAPS and isinstance(value, list):
            if key == "blocked_urls":
                merged[key] = _merge_blocked_urls(merged.get(key), value, _TOP_LIST_CAPS[key])
            else:
                merged[key] = _merge_list(merged.get(key), value, _TOP_LIST_CAPS[key])
        else:
            merged[key] = value
    return merged


async def _mirror_summary(org_id: int, domain: str, pb: dict) -> None:
    """Best-effort: copy a short summary onto the client whose website matches
    this domain, so it shows up next to the client without another read.
    Never raises — record() treats this as optional."""
    if db_module is None:
        return
    clients = await db_module.list_clients(org_id)
    match = next((c for c in (clients or []) if domain_of(c) == domain), None)
    if not match:
        return
    careers = pb.get("careers") or {}
    bits = []
    if careers.get("url"):
        bits.append(f"careers: {careers['url']} ({careers.get('tier', '?')})")
    if pb.get("needs_js"):
        bits.append("needs JS fetch")
    if pb.get("blocked_urls"):
        bits.append(f"{len(pb['blocked_urls'])} blocked URL(s)")
    summary = " · ".join(bits) or "learned — see site playbook"
    await db_module.update_client_metadata(
        org_id, match["name"], {"site_playbook_summary": summary[:300]}
    )


async def record(
    org_id: int, domain: str, patch: dict, *, run_id: Optional[int] = None, website: str = ""
) -> dict:
    """Upsert the site playbook for `domain` with `patch` merged in.

    Called by WP2 (jobs) and WP3 (news) as
    `await playbook.record(org_id, domain, patch, run_id=..., website=...)`,
    and by reflect_on_run() with the deterministic tool-call classification.
    """
    if not domain or db_module is None:
        return {}
    lock = _lock_for(f"{org_id}:{domain}")
    async with lock:
        existing = await load(org_id, domain) or {}
        merged = _merge(existing, patch or {}, domain=domain, website=website)
        if run_id is not None:
            sot = list(merged.get("sources_of_truth") or [])
            if run_id not in sot:
                sot.append(run_id)
            merged["sources_of_truth"] = sot[-_MAX_SOURCES_OF_TRUTH:]
        merged["updated_at"] = _now_iso()
        merged["version"] = 1

        try:
            await db_module.index_document(
                org_id=org_id,
                doc_id=_doc_id(domain),
                doc_type="site_playbook",
                title=f"Site playbook — {domain}",
                content=render_markdown(merged),
                metadata=merged,
                embedding=[],
                source="agent",
                agent_run_id=run_id,
            )
        except Exception:
            logger.exception("playbook.record: index_document failed for domain=%s", domain)
            return merged

        try:
            await _mirror_summary(org_id, domain, merged)
        except Exception:
            logger.debug("playbook.record: summary mirror failed for domain=%s", domain, exc_info=True)

        return merged


def render_markdown(pb: dict) -> str:
    """Full document content for the site_playbook row, incl. a ## Sources
    section (run ids + every URL this playbook learned about)."""
    domain = pb.get("domain", "")
    website = pb.get("website", "")
    lines = [f"# Site playbook — {domain}", ""]
    if website:
        lines.append(f"Website: {website}")

    careers = pb.get("careers") or {}
    if careers:
        lines += ["", "## Careers"]
        if careers.get("url"):
            lines.append(f"- URL: {careers['url']} (tier: {careers.get('tier', '?')})")
        if careers.get("last_success_at"):
            lines.append(f"- Last success: {careers['last_success_at']}")
        if careers.get("last_failure_at"):
            lines.append(f"- Last failure: {careers['last_failure_at']} ({careers.get('error', '')})")

    newsroom = pb.get("newsroom") or {}
    if newsroom.get("urls"):
        lines += ["", "## Newsroom"] + [f"- {u}" for u in newsroom["urls"]]
        if newsroom.get("last_success_at"):
            lines.append(f"(last success: {newsroom['last_success_at']})")

    news = pb.get("news") or {}
    if news.get("good_queries"):
        lines += ["", "## News queries that work"] + [f"- {q}" for q in news["good_queries"]]

    if pb.get("needs_js"):
        lines += ["", "## Rendering", "- needs_js: true (plain GET returns little/no content — use the browser fetch tool)"]
    if pb.get("cookie_wall"):
        lines.append("- cookie_wall: true (accept/dismiss before reading content)")

    blocked = pb.get("blocked_urls") or []
    if blocked:
        lines += ["", "## Blocked / failed URLs"]
        lines += [f"- {b.get('url', '')} ({b.get('kind', '')}, {b.get('at', '')})" for b in blocked]

    good_q = pb.get("good_queries") or []
    if good_q:
        lines += ["", "## Good search queries"] + [f"- {q}" for q in good_q]

    failed_q = pb.get("failed_queries") or []
    if failed_q:
        lines += ["", "## Failed search queries"] + [f"- {q}" for q in failed_q]

    notes = pb.get("notes") or []
    if notes:
        lines += ["", "## Navigation notes"] + [f"- {n}" for n in notes]

    learned_urls = []
    if careers.get("url"):
        learned_urls.append(careers["url"])
    learned_urls += newsroom.get("urls") or []
    learned_urls += [b["url"] for b in blocked if b.get("url")]

    lines += ["", "## Sources"]
    sot = pb.get("sources_of_truth") or []
    if sot:
        lines.append("Run ids: " + ", ".join(str(s) for s in sot))
    lines += [f"- {u}" for u in learned_urls]

    lines += ["", f"_Updated: {pb.get('updated_at', '')}_"]
    return "\n".join(lines)


def render_block(pb: Optional[dict]) -> str:
    """Compact block appended to a task — ≤1200 chars, empty fields omitted.
    Returns "" when the playbook has nothing worth injecting."""
    if not pb:
        return ""
    lines = ["## Site playbook (learned)"]

    careers = pb.get("careers") or {}
    if careers.get("url"):
        lines.append(f"- Careers page: {careers['url']} (tier: {careers.get('tier', '?')})")

    newsroom = pb.get("newsroom") or {}
    if newsroom.get("urls"):
        lines.append(f"- Newsroom: {', '.join(newsroom['urls'][:3])}")

    news = pb.get("news") or {}
    if news.get("good_queries"):
        lines.append(f"- News queries that work: {', '.join(news['good_queries'][:3])}")

    if pb.get("needs_js"):
        lines.append("- This site needs a JS-rendering fetch (plain GET returns little/no content).")
    if pb.get("cookie_wall"):
        lines.append("- Cookie wall present — accept/dismiss before reading content.")

    blocked = pb.get("blocked_urls") or []
    if blocked:
        shown = blocked[-5:]
        lines.append("- Avoid (previously blocked): " + "; ".join(
            f"{b.get('url', '')} ({b.get('kind', '')})" for b in shown
        ))

    good_q = pb.get("good_queries") or []
    if good_q:
        lines.append(f"- Good queries: {', '.join(good_q[:5])}")

    failed_q = pb.get("failed_queries") or []
    if failed_q:
        lines.append(f"- Avoid these queries (no results before): {', '.join(failed_q[:5])}")

    for n in (pb.get("notes") or [])[:4]:
        lines.append(f"- {n}")

    if len(lines) == 1:
        return ""  # nothing but the header
    block = "\n".join(lines)
    if len(block) > _MAX_BLOCK_CHARS:
        block = block[: _MAX_BLOCK_CHARS - 3] + "..."
    return block


# ---------------------------------------------------------------------------
# classify_tool_calls — pure, deterministic
# ---------------------------------------------------------------------------

_HTTP_ERROR_RE = re.compile(r"^Error:\s*HTTP\s*(\d+)")


def _host_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_own_domain(url: str, domain: str) -> bool:
    if not domain or not url:
        return False
    host = _host_of(url)
    return bool(host) and (host == domain or host.endswith("." + domain))


def classify_tool_calls(tool_calls: list, domain: str) -> dict:
    """Pure function: turn a run's raw tool-call log into playbook signals.

    tool_calls are `{tool, args, result, ts}` dicts (agents.py's
    _clean_tool_call shape). Sentinels (agent_service_ts/src/search.ts):
      fetch_page → "Error: HTTP <code>"      → blocked, kind 403 or 4xx
                 → "Error fetching page..."  → blocked, kind fetch_error
                 → "(no readable content)"   → blocked, kind no_content
                 → "(skipped binary..."      → blocked, kind binary
      web_search → "(no results..."          → failed_queries
    needs_js is set when >=2 own-domain fetches came back no_content — a
    strong sign the plain GET never saw the JS-rendered page.
    good_queries = a web_search query followed within 3 calls by a fetch_page
    whose result was NOT one of the sentinels above.
    """
    blocked_urls: list = []
    good_queries: list = []
    failed_queries: list = []
    no_content_own_domain = 0
    pending_searches: list = []  # (index, query)
    now = _now_iso()

    for i, tc in enumerate(tool_calls or []):
        tool = tc.get("tool", "")
        args = tc.get("args") or {}
        result = str(tc.get("result", "") or "")

        if tool == "web_search":
            query = (args.get("query") or "").strip()
            if query:
                pending_searches.append((i, query))
                if result.startswith("(no results") and query not in failed_queries:
                    failed_queries.append(query)
            continue

        if tool != "fetch_page":
            continue

        url = args.get("url") or ""
        kind = None
        if result.startswith("Error: HTTP"):
            m = _HTTP_ERROR_RE.match(result)
            kind = "403" if (m and m.group(1) == "403") else "4xx"
        elif result.startswith("Error fetching page"):
            kind = "fetch_error"
        elif result.startswith("(no readable content)"):
            kind = "no_content"
            if _is_own_domain(url, domain):
                no_content_own_domain += 1
        elif result.startswith("(skipped binary"):
            kind = "binary"

        if kind and url:
            blocked_urls.append({"url": url, "kind": kind, "at": now})
        elif kind is None and url:
            # A clean fetch — credit the most recent search within 3 calls.
            for j, query in pending_searches:
                if 0 < i - j <= 3 and query not in good_queries:
                    good_queries.append(query)

    return {
        "blocked_urls": blocked_urls[-_MAX_BLOCKED_URLS:],
        "good_queries": good_queries[:_MAX_QUERY_LIST],
        "failed_queries": failed_queries[:_MAX_QUERY_LIST],
        "needs_js": no_content_own_domain >= 2,
    }


def _infer_domain(tool_calls: list) -> str:
    """Best-effort: the most-fetched non-aggregator host across this run's
    fetch_page calls is the site the run actually navigated. More reliable
    than parsing the task string, whose wording varies by agent_type/template
    and isn't guaranteed to carry the client's domain at all."""
    from collections import Counter

    try:
        from routers.pipeline import _AGGREGATOR_DOMAINS
    except Exception:
        _AGGREGATOR_DOMAINS = set()

    counts = Counter()
    for tc in tool_calls or []:
        if tc.get("tool") != "fetch_page":
            continue
        url = (tc.get("args") or {}).get("url") or ""
        host = _host_of(url)
        if host and host not in _AGGREGATOR_DOMAINS:
            counts[host] += 1
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _tool_call_trace(tool_calls: list, limit: int = 40) -> str:
    lines = []
    for tc in (tool_calls or [])[:limit]:
        tool = tc.get("tool", "")
        args = tc.get("args") or {}
        ref = args.get("url") or args.get("query") or ""
        result = str(tc.get("result", "") or "")[:80]
        lines.append(f"- {tool}({ref}) -> {result}")
    return "\n".join(lines)


def _extract_json_list(raw: str) -> list:
    text = (raw or "").strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
    except Exception:
        return []
    return data if isinstance(data, list) else []


_NAV_NOTES_PROMPT = (
    "You are reviewing a web-research agent's tool-call trace for the website {domain}.\n"
    "Write at most {max_notes} short, concrete navigation notes (max ~15 words each) that "
    "would help a FUTURE run on this SAME website move faster: which sections held useful "
    "information, what to avoid, and any quirks you noticed.\n"
    "Return ONLY a JSON array of strings, e.g. [\"note one\", \"note two\"]. "
    "No markdown, no explanation, no code fences.\n\n"
    "Tool calls (tool(url or query) -> outcome):\n{trace}\n"
)


async def _llm_navigation_notes(org_id: int, domain: str, tool_calls: list) -> list:
    prompt = _NAV_NOTES_PROMPT.format(domain=domain, max_notes=_MAX_NAV_NOTES,
                                       trace=_tool_call_trace(tool_calls))
    raw = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
    notes = _extract_json_list(raw)
    return [str(n).strip() for n in notes if str(n).strip()][:_MAX_NAV_NOTES]


async def reflect_on_run(org_id: int, db_run_id: int) -> None:
    """Callback-time reflection for a finished research/osint/pain_point_research
    run (called on both 'done' and 'failed' — a run that got 403'd everywhere
    still teaches useful blocked_urls). Idempotent via output.reflected, since
    both the callback and the watcher backstop may schedule this for the same
    run. Runs at callback time, not later, because agent_runs.tool_calls gets
    compacted by retention after 14 days.
    """
    if db_module is None:
        return
    try:
        run = await db_module.get_agent_run(db_run_id, org_id)
    except Exception:
        logger.exception("playbook.reflect_on_run: get_agent_run(%s) failed", db_run_id)
        return
    if not run:
        return

    output = run.get("output") or {}
    if isinstance(output, str):
        try:
            output = json.loads(output) or {}
        except Exception:
            output = {}
    if output.get("reflected"):
        return

    tool_calls = run.get("tool_calls") or []
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls) or []
        except Exception:
            tool_calls = []

    domain = _infer_domain(tool_calls)
    if domain:
        classified = classify_tool_calls(tool_calls, domain)
        patch = {
            "blocked_urls": classified["blocked_urls"],
            "good_queries": classified["good_queries"],
            "failed_queries": classified["failed_queries"],
            "needs_js": classified["needs_js"],
        }
        if len(tool_calls) >= _REFLECT_LLM_MIN_TOOL_CALLS:
            try:
                notes = await _llm_navigation_notes(org_id, domain, tool_calls)
                if notes:
                    patch["notes"] = notes
            except Exception:
                logger.debug("playbook.reflect_on_run: navigation-notes LLM call failed for run=%s",
                             db_run_id, exc_info=True)
        try:
            await record(org_id, domain, patch, run_id=db_run_id)
        except Exception:
            logger.exception("playbook.reflect_on_run: record failed for domain=%s", domain)

    try:
        await db_module.update_agent_run(
            db_run_id, run.get("status") or "done",
            output={**output, "reflected": True},
        )
    except Exception:
        logger.exception("playbook.reflect_on_run: could not stamp reflected on run=%s", db_run_id)


# ---------------------------------------------------------------------------
# enrich_task — the reader side
# ---------------------------------------------------------------------------

def scope_for(agent_type: str) -> str:
    if agent_type in ("research", "osint", "pain_point_research"):
        return "research"
    if agent_type == "jobs_scan":
        return "jobs"
    if agent_type == "news_scan":
        return "news"
    return "all"


async def enrich_task(org_id: int, subject: str, task: str, agent_type: str) -> tuple:
    """Append the learned site-playbook block + approved-lessons block to a
    task. Gated on an EXACT client match: a subject that only fuzzy-matches a
    client (e.g. "org", the monitor agent's placeholder subject) must never
    pull in another client's playbook or org-wide lessons — so the task comes
    back unchanged in that case. Returns (task, needs_js)."""
    needs_js = False
    client = await resolve_client_exact(org_id, subject)
    if not client:
        return task, needs_js

    try:
        domain = domain_of(client)
        if domain:
            pb = await load(org_id, domain)
            if pb:
                block = render_block(pb)
                if block:
                    task = f"{task}\n\n{block}"
                needs_js = bool(pb.get("needs_js"))
    except Exception:
        logger.debug("playbook.enrich_task: site playbook lookup failed for subject=%s", subject, exc_info=True)

    try:
        lessons = await lessons_load(org_id)
        lblock = lessons_block(lessons, scope_for(agent_type))
        if lblock:
            task = f"{task}\n\n{lblock}"
    except Exception:
        logger.debug("playbook.enrich_task: lessons lookup failed for subject=%s", subject, exc_info=True)

    return task, needs_js


# ---------------------------------------------------------------------------
# agent_lessons: proposed -> human decision -> injected
# ---------------------------------------------------------------------------

async def lessons_load(org_id: int) -> list:
    if db_module is None:
        return []
    try:
        doc = await db_module.get_document(org_id, LESSONS_DOC_ID)
    except Exception:
        logger.debug("playbook.lessons_load: get_document failed", exc_info=True)
        return []
    if not doc:
        return []
    meta = doc.get("metadata") or {}
    return list(meta.get("lessons") or [])


def _render_lessons_markdown(lessons: list) -> str:
    lines = ["# Cross-site agent lessons", ""]
    for status in ("proposed", "approved", "rejected"):
        group = [l for l in lessons if l.get("status") == status]
        if not group:
            continue
        lines.append(f"## {status.capitalize()}")
        lines += [f"- [{l.get('scope', 'all')}] {l.get('text', '')}" for l in group]
        lines.append("")
    # Project rule: every agent-written doc carries a ## Sources section. A
    # lesson has no URL of its own — the run(s) whose failures/playbooks
    # produced it is the closest analog.
    run_ids = sorted({l["proposed_by_run"] for l in lessons if l.get("proposed_by_run") is not None})
    lines.append("## Sources")
    lines.append("Proposed by run ids: " + ", ".join(str(r) for r in run_ids) if run_ids
                  else "(no run ids recorded)")
    return "\n".join(lines)


async def _save_lessons(org_id: int, lessons: list, *, run_id: Optional[int] = None) -> None:
    await db_module.index_document(
        org_id=org_id,
        doc_id=LESSONS_DOC_ID,
        doc_type="agent_lessons",
        title="Cross-site agent lessons",
        content=_render_lessons_markdown(lessons),
        metadata={"lessons": lessons},
        embedding=[],
        source="agent",
        agent_run_id=run_id,
    )


def _bound_lessons(lessons: list) -> list:
    proposed = [l for l in lessons if l.get("status") == "proposed"][-_MAX_LESSONS_PROPOSED:]
    decided = [l for l in lessons if l.get("status") != "proposed"][-_MAX_LESSONS_DECIDED:]
    return decided + proposed


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _is_duplicate_lesson(text: str, lessons: list) -> bool:
    tokens = _tokens(text)
    return any(_jaccard(tokens, _tokens(l.get("text", ""))) >= _DEDUPE_JACCARD_THRESHOLD for l in lessons)


_LESSON_PROPOSAL_PROMPT = (
    "You maintain cross-website navigation lessons for a fleet of B2B research agents.\n"
    "Below are per-website playbooks (what worked/failed there) and this week's failed "
    "research runs. Propose at most {n} GENERAL, actionable lessons that would help future "
    "runs on ANY website — not site-specific facts (those already live in each site's own "
    "playbook). Phrase each as an instruction, e.g. \"Prefer sitemap.xml over homepage "
    "crawling when the homepage returns no readable content.\"\n"
    "Return ONLY a JSON array of objects: "
    '[{{"text": "...", "scope": "jobs|news|research|all", "evidence": ["domain1", ...]}}]\n\n'
    "Site playbooks:\n{playbooks}\n\n"
    "Failed runs (last 7 days):\n{failures}\n"
)


async def lessons_propose(org_id: int, *, run_id: Optional[int] = None) -> dict:
    """Weekly LLM pass: read every site playbook + this week's failed runs,
    propose <=5 cross-domain lessons, dedupe against everything on file, and
    append them as status='proposed'. NEVER auto-approves."""
    if db_module is None:
        return {"proposed": []}

    try:
        docs = await db_module.list_documents(org_id, doc_type="site_playbook")
    except Exception:
        logger.exception("playbook.lessons_propose: list_documents failed")
        docs = []
    playbook_lines = []
    for d in docs or []:
        meta = d.get("metadata") or {}
        playbook_lines.append(
            f"- {meta.get('domain', d.get('doc_id', ''))}: "
            f"careers_tier={((meta.get('careers') or {}).get('tier')) or 'none'}, "
            f"needs_js={meta.get('needs_js', False)}, "
            f"blocked={len(meta.get('blocked_urls') or [])}, "
            f"failed_queries={(meta.get('failed_queries') or [])[:3]}"
        )

    try:
        activity = await db_module.get_agent_activity(org_id, days=7)
    except Exception:
        logger.exception("playbook.lessons_propose: get_agent_activity failed")
        activity = {"runs": []}
    failure_lines = [
        f"- {r.get('agent_type')}: {str(r.get('error') or '')[:200]}"
        for r in (activity.get("runs") or []) if r.get("status") == "failed"
    ]

    prompt = _LESSON_PROPOSAL_PROMPT.format(
        n=_MAX_LESSON_PROPOSALS_PER_PASS,
        playbooks="\n".join(playbook_lines) or "(no site playbooks yet)",
        failures="\n".join(failure_lines) or "(no failed runs this week)",
    )
    try:
        raw = await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)
    except Exception:
        logger.exception("playbook.lessons_propose: llm.acomplete failed")
        return {"proposed": []}

    candidates = _extract_json_list(raw)
    existing = await lessons_load(org_id)
    now = _now_iso()
    added = []
    for c in candidates[:_MAX_LESSON_PROPOSALS_PER_PASS]:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text", "")).strip()
        if not text or _is_duplicate_lesson(text, existing + added):
            continue
        scope = c.get("scope") if c.get("scope") in _VALID_SCOPES else "all"
        added.append({
            "id": f"l-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}",
            "text": text,
            "scope": scope,
            "status": "proposed",
            "evidence": [str(e) for e in (c.get("evidence") or [])][:10],
            "proposed_at": now,
            "decided_at": None,
            "decided_by": None,
            "proposed_by_run": run_id,
        })

    if added:
        try:
            await _save_lessons(org_id, _bound_lessons(existing + added), run_id=run_id)
        except Exception:
            logger.exception("playbook.lessons_propose: _save_lessons failed")

    return {"proposed": added}


async def lessons_decide(org_id: int, lesson_id: str, decision: str, user_id: Optional[int]) -> dict:
    """Human decision on a proposed lesson. The only path that ever sets
    status='approved' — nothing in this module does that on its own."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    lessons = await lessons_load(org_id)
    updated = None
    for l in lessons:
        if l.get("id") == lesson_id:
            l["status"] = "approved" if decision == "approve" else "rejected"
            l["decided_at"] = _now_iso()
            l["decided_by"] = user_id
            updated = l
            break
    if updated is None:
        raise KeyError(lesson_id)
    await _save_lessons(org_id, _bound_lessons(lessons))
    return updated


def lessons_block(lessons: list, scope: str) -> str:
    """'## Learned rules (approved)', <=8 lines — only approved lessons whose
    scope matches (or is 'all') are ever injected into a task."""
    approved = [l for l in (lessons or [])
                if l.get("status") == "approved" and l.get("scope") in (scope, "all")]
    if not approved:
        return ""
    lines = ["## Learned rules (approved)"]
    lines += [f"- {l.get('text', '').strip()}" for l in approved[:_MAX_LESSONS_IN_BLOCK] if l.get("text", "").strip()]
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
