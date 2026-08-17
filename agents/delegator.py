"""
DelegatorOrchestrator — qwen3.5 as a pure research goal generator.

Architecture:
  qwen3.5's ONLY job: output a JSON list of search goals.
  It never decides "should I research?" — it always generates goals.

  Round loop (max MAX_ROUNDS):
    1. Call qwen3.5 (plain chat, no tools) → JSON list of goals
    2. Python spawns each goal as a web_search task
    3. Wait (async polling) until all spawned tasks are terminal
    4. Collect new findings from DB
    5. Feed findings summary back to qwen3.5 → next goals (or [] if done)
  Stop when qwen3.5 returns [] or max rounds reached.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import db as _db
import llm
from agents import research_log as _rlog

logger = logging.getLogger(__name__)


def _display_subject(s: str) -> str:
    """Strip benchmark isolation tag: 'Acme [qwen3.5]' → 'Acme'."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip() or s


MAX_ROUNDS = 3
GOALS_PER_ROUND = 10
POLL_INTERVAL_S = 5

SECTIONS = ["Overview", "Leadership", "Financial", "Products", "Recent News", "Market", "Sales Intel"]

# ── Prompts ────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a research delegator for a B2B sales intelligence system.

Your ONLY job: generate specific web search queries for worker agents to execute.
Workers handle all actual searching, page fetching, and fact extraction — you never do that.

A complete company profile needs 7 sections:
  Overview    — what the company does, size, HQ, founded
  Leadership  — CEO, CTO, CFO and other key executives with their roles
  Financial   — revenue, profit, growth rate, recent earnings / results
  Products    — key products and services, recent launches
  Recent News — press releases, deals, partnerships in the last 30 days
  Market      — competitors, market share, industry positioning
  Sales Intel — strategic priorities, investment areas, buying signals

Output ONLY a valid JSON array — no explanation, no markdown, no text around it:
[
  {"query": "<specific targeted search string>", "section": "<one of the 7 sections>", "priority": <1-10>},
  ...
]

If truly all 7 sections already have 3+ recent findings each, output an empty array: []
"""


def _prompt_initial(subject: str, subject_type: str, existing: str) -> str:
    return (
        f"Subject: {subject} ({subject_type})\n\n"
        f"Existing knowledge in the knowledge base:\n{existing}\n\n"
        f"Generate {GOALS_PER_ROUND} targeted web search queries to build a complete "
        f"profile of {subject}. Spread goals across all 7 sections. Prioritise sections "
        f"with no existing coverage (priority 9-10). Make every query specific — "
        f'good: "{subject} CEO 2026 leadership team", bad: "{subject}".\n\n'
        f"Respond with ONLY a JSON array."
    )


def _prompt_followup(
    subject: str,
    subject_type: str,
    round_num: int,
    findings_count: int,
    findings_summary: str,
    covered: list[str],
    missing: list[str],
) -> str:
    covered_str = ", ".join(covered) if covered else "none yet"
    missing_str = ", ".join(missing) if missing else "all covered"
    return (
        f"Subject: {subject} ({subject_type})\n\n"
        f"Round {round_num} workers found {findings_count} new findings:\n"
        f"{findings_summary}\n\n"
        f"Sections with coverage (3+ findings): {covered_str}\n"
        f"Sections still missing or sparse: {missing_str}\n\n"
        f"Generate up to {GOALS_PER_ROUND} more search queries targeting the missing sections. "
        f"If all 7 sections are well covered, respond with [].\n\n"
        f"Respond with ONLY a JSON array."
    )


# ── Brain call (llm.py research role) ─────────────────────────────────────────

def _call_brain_sync(model: str, user_msg: str, brain: str = "ollama", org_id: Optional[int] = None) -> str:
    """Call the research brain for plain text (no tool-calling). Returns response text.

    Provider selection lives in llm.py's "research" role config; the brain
    argument is kept for signature stability with existing callers.
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    return llm.complete(messages=messages, role="research", model=model, timeout=180, org_id=org_id)


async def _call_brain(model: str, user_msg: str, brain: str = "ollama") -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _call_brain_sync, model, user_msg, brain)


# ── JSON parsing ───────────────────────────────────────────────────────────────

def _parse_goals(text: str) -> list[dict]:
    """Extract a JSON array of goals from qwen's response."""
    # Strip thinking tags
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    # Find the first [...] block
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        logger.warning("Delegator: no JSON array in response: %.200s", text)
        return []
    try:
        goals = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("Delegator: JSON parse error %s in: %.200s", exc, text)
        return []
    if not isinstance(goals, list):
        return []
    cleaned = []
    for g in goals:
        if isinstance(g, dict) and g.get("query"):
            cleaned.append({
                "query": str(g["query"])[:250],
                "section": str(g.get("section", "Overview")),
                "priority": max(1, min(10, int(g.get("priority", 5)))),
            })
    return cleaned


# ── Task helpers ───────────────────────────────────────────────────────────────

async def _spawn_goals(
    goals: list[dict],
    org_id: int,
    subject: str,
    subject_type: str,
    depth: int,
    agent_run_id: Optional[int],
    brain_override: str = "",
    model_override: str = "",
) -> list[int]:
    """Enqueue one web_search task per goal. Returns list of new task IDs."""
    ids = []
    for g in goals:
        payload: dict = {"query": g["query"]}
        if brain_override:
            payload["_brain_override"] = brain_override
        if model_override:
            payload["_model_override"] = model_override
        tid = await _db.enqueue_research_task(
            org_id=org_id,
            subject_type=subject_type,
            subject=subject,
            task_type="web_search",
            payload=payload,
            depth=depth,
            priority=g["priority"],
            agent_run_id=agent_run_id,
        )
        if tid and tid > 0:
            ids.append(tid)
            logger.info("Delegator spawned task %d [%s]: %s", tid, g["section"], g["query"][:70])
    return ids


async def _wait_for_tasks(org_id: int, subject: str, task_ids: list[int], timeout: int = 900) -> None:
    """Poll until ALL non-orchestrate/aggregate tasks for the subject are terminal.

    We wait on the whole subject queue (not just the spawned IDs) because web_search
    tasks complete quickly but spawn fetch_url children, which in turn spawn analyze
    children. If we only waited for the web_search tasks, we'd check for findings
    before the content_analyzer has run.
    """
    if not _db._pool:
        return
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with _db._pool.acquire() as conn:
            pending_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM research_tasks
                WHERE org_id = $1
                  AND lower(subject) = lower($2)
                  AND task_type NOT IN ('orchestrate', 'aggregate')
                  AND status IN ('pending', 'running')
                """,
                org_id, subject,
            )
        if pending_count == 0:
            return
        logger.debug("Delegator: %d worker tasks still pending for %s...", pending_count, subject)
        await asyncio.sleep(POLL_INTERVAL_S)
    logger.warning("Delegator: wait_for_tasks timed out after %ds", timeout)


# ── Findings helpers ───────────────────────────────────────────────────────────

async def _get_findings_since(org_id: int, subject: str, since: datetime) -> list[dict]:
    """Return type=finding documents for this subject created after `since`."""
    if not _db._pool:
        return []
    async with _db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.title, d.content, d.metadata, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
            JOIN clients c ON c.id = dl.entity_id
            WHERE d.org_id = $1
              AND d.type = 'finding'
              AND d.created_at >= $2
              AND (lower(c.name) = lower($3) OR lower(c.name) LIKE '%' || lower($3) || '%')
            ORDER BY d.created_at DESC
            LIMIT 100
            """,
            org_id, since, subject,
        )
        return [dict(r) for r in rows]


async def _get_existing_summary(org_id: int, subject: str) -> str:
    """Return a short text summary of any existing KB documents for this subject."""
    if not _db._pool:
        return "No existing knowledge found."
    async with _db._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.type, d.title, LEFT(d.content, 300) as snippet
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
            JOIN clients c ON c.id = dl.entity_id
            WHERE d.org_id = $1
              AND (lower(c.name) = lower($2) OR lower(c.name) LIKE '%' || lower($2) || '%')
            ORDER BY d.created_at DESC
            LIMIT 10
            """,
            org_id, subject,
        )
    if not rows:
        return "No existing knowledge found — this is a brand new subject."
    parts = []
    for r in rows:
        parts.append(f"[{r['type']}] {r['title']}\n{r['snippet']}")
    return "\n\n".join(parts)


def _summarize_findings(findings: list[dict]) -> tuple[str, list[str], list[str]]:
    """Return (text_summary, covered_sections, missing_sections)."""
    if not findings:
        return "No findings collected.", [], SECTIONS[:]

    # Count findings per section from metadata
    section_counts: dict[str, int] = {s: 0 for s in SECTIONS}
    bullets = []
    for f in findings[:15]:  # cap summary length
        meta = f.get("metadata") or {}
        title = f.get("title", "untitled")
        bullets.append(f"- {title}")
        # Try to infer section from title/content (rough heuristic)
        text = (title + " " + (f.get("content") or "")).lower()
        for sec in SECTIONS:
            if sec.lower().split()[0] in text:
                section_counts[sec] += 1
                break
        else:
            section_counts["Overview"] += 1

    covered = [s for s, c in section_counts.items() if c >= 1]
    missing = [s for s, c in section_counts.items() if c == 0]
    summary = "\n".join(bullets)
    return summary, covered, missing


# ── Main entry point ───────────────────────────────────────────────────────────

async def _count_web_searches(org_id: int, subject: str) -> int:
    """Count web_search tasks enqueued for this subject — the delegator's direct 'goals'."""
    if not _db._pool:
        return 0
    async with _db._pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*) FROM research_tasks
            WHERE org_id = $1
              AND lower(subject) = lower($2)
              AND task_type = 'web_search'
            """,
            org_id, subject,
        ) or 0


async def run_delegator_orchestrator(
    task_id: int,
    org_id: int,
    subject: str,
    subject_type: str,
    payload: dict,
    brain_override: str = "",
    model_override: str = "",
) -> dict:
    """
    Drop-in replacement for run_orchestrator when using qwen3.5 as delegator.
    Called by _dispatch in research_runner.py.

    IMPORTANT: The delegator always uses local Ollama (qwen3.5) for goal generation
    regardless of brain_override/model_override. Those overrides only propagate to
    worker tasks (web_search, analyze). The delegator is pure local planning.
    """
    import yaml as _yaml
    _cfg = {}
    try:
        _cfg = _yaml.safe_load(open("config.yaml")) or {}
    except Exception:
        pass
    # Resolve brain and model: use overrides if provided, else fall back to config
    effective_brain = brain_override or _cfg.get("research_brain") or _cfg.get("agent_brain", "ollama")
    if effective_brain == "openrouter":
        effective_model = model_override or _cfg.get("research_model") or _cfg.get("agent_model", "qwen3.5")
    elif effective_brain == "claude":
        effective_model = model_override or _cfg.get("research_model") or "claude-3-5-haiku-20241022"
    else:
        logger.warning("research_brain is not configured — defaulting to openrouter")
        effective_brain = "openrouter"
        effective_model = model_override or _cfg.get("research_model") or _cfg.get("agent_model", "deepseek/deepseek-v4-flash")

    agent_run_id = payload.get("_agent_run_id")
    max_tasks: int = max(5, int(payload.get("_max_tasks", 20)))
    depth = 1  # goals are depth-1 tasks; their children are depth-2+

    logger.info(
        "Delegator starting: subject=%r brain=%s model=%s max_tasks=%d",
        subject, effective_brain, effective_model, max_tasks,
    )

    # ── Build initial KB summary ───────────────────────────────────────────────
    existing_summary = await _get_existing_summary(org_id, subject)

    all_goals: list[dict] = []
    all_findings: list[dict] = []
    round_goals: list[dict] = []
    user_msg = _prompt_initial(_display_subject(subject), subject_type, existing_summary)

    for round_num in range(1, MAX_ROUNDS + 1):
        logger.info("Delegator round %d/%d for %s", round_num, MAX_ROUNDS, subject)
        _rlog.info("delegator", subject, f"Round {round_num}/{MAX_ROUNDS} — calling {effective_brain}/{effective_model} for search goals")

        # ── Budget check ──────────────────────────────────────────────────────
        searches_so_far = await _count_web_searches(org_id, subject)
        remaining_budget = max_tasks - searches_so_far
        if remaining_budget <= 0:
            _rlog.info("delegator", subject,
                f"Budget exhausted ({searches_so_far}/{max_tasks} searches used) — stopping")
            break

        # 1. Ask local model for goals ─────────────────────────────────────────
        try:
            raw = await _call_brain(effective_model, user_msg, effective_brain)
        except Exception as exc:
            _rlog.error("delegator", subject,
                f"Brain call failed ({effective_brain}/{effective_model}): {exc}",
                detail=str(exc))
            logger.error("Delegator: brain call failed: %s", exc)
            break

        logger.debug("Delegator raw response (round %d): %.300s", round_num, raw)

        round_goals = _parse_goals(raw)
        if not round_goals:
            _rlog.warn("delegator", subject,
                f"Model returned 0 goals in round {round_num} — stopping early",
                detail=raw[:300] if raw else "(empty response)")
            logger.info("Delegator: model returned [] — stopping after round %d", round_num - 1)
            break

        # Cap goals to remaining search budget
        if len(round_goals) > remaining_budget:
            round_goals = round_goals[:remaining_budget]

        _rlog.info("delegator", subject,
            f"Round {round_num}: {len(round_goals)} goals generated",
            detail=" | ".join(g["query"] for g in round_goals[:5]))
        logger.info("Delegator round %d: %d goals generated", round_num, len(round_goals))
        all_goals.extend(round_goals)

        # 2. Spawn tasks ───────────────────────────────────────────────────────
        round_start = datetime.now(timezone.utc)
        task_ids = await _spawn_goals(
            round_goals, org_id, subject, subject_type, depth, agent_run_id,
            brain_override=brain_override, model_override=model_override,
        )

        if not task_ids:
            _rlog.warn("delegator", subject, f"Round {round_num}: 0 tasks spawned despite {len(round_goals)} goals")
            break

        _rlog.info("delegator", subject, f"Round {round_num}: {len(task_ids)} worker tasks spawned — waiting...")

        # 3. Wait for workers to finish ────────────────────────────────────────
        await _wait_for_tasks(org_id, subject, task_ids, timeout=900)

        # 4. Collect new findings ──────────────────────────────────────────────
        new_findings = await _get_findings_since(org_id, subject, round_start)
        all_findings.extend(new_findings)
        _rlog.info("delegator", subject, f"Round {round_num} complete — {len(new_findings)} new findings")
        logger.info("Delegator round %d: %d new findings", round_num, len(new_findings))

        if round_num >= MAX_ROUNDS:
            break

        # 5. Build followup prompt ─────────────────────────────────────────────
        summary_text, covered, missing = _summarize_findings(all_findings)
        user_msg = _prompt_followup(
            subject=_display_subject(subject),
            subject_type=subject_type,
            round_num=round_num,
            findings_count=len(new_findings),
            findings_summary=summary_text,
            covered=covered,
            missing=missing,
        )

    # Option A: delegator knows when all research rounds are done — enqueue aggregate
    # directly rather than relying on the auto-trigger in complete_research_task, which
    # has a race condition because the orchestrate task stays 'running' throughout all
    # rounds and the pending-count check always sees it as non-zero.
    if _db._pool:
        try:
            await _db.enqueue_research_task(
                org_id=org_id,
                subject_type=subject_type,
                subject=subject,
                task_type="aggregate",
                payload={"triggered_by": "delegator"},
                depth=0,
                priority=9,
                agent_run_id=agent_run_id,
            )
            logger.info("Delegator: enqueued aggregate task for %s", subject)
        except Exception as exc:
            logger.warning("Delegator: could not enqueue aggregate for %s: %s", subject, exc)

    final_searches = await _count_web_searches(org_id, subject)
    return {
        "delegator": True,
        "rounds_run": round_num if round_goals else round_num - 1,
        "goals_generated": len(all_goals),
        "findings_collected": len(all_findings),
        "gaps": [g["query"] for g in all_goals],
        "searches_used": final_searches,
        "max_searches": max_tasks,
    }
