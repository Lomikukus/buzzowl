"""
agents/orchestrator.py — OrchestratorAgent for Phase 7 Deep Research Engine.

Uses Agent.run() because it does open-ended planning: read KB, identify coverage
gaps, emit only the targeted tasks needed to fill them.

This is the ONLY research component that uses the Agent loop. All workers use
direct Ollama calls (_call_ollama pattern from enrichment.py).
"""
import json
import logging
import re
from pathlib import Path
from typing import Optional

import yaml

import db as _db
from agents.base import Agent
from agents.runner import _load_brain
from agents.tools import Tool, build_tools

logger = logging.getLogger(__name__)


def _display_subject(s: str) -> str:
    """Strip benchmark isolation tag: 'Acme [qwen3.5]' → 'Acme'."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip() or s

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

ORCHESTRATOR_INSTRUCTIONS = """\
You are a research orchestrator for a B2B sales intelligence system.

Your job is ONLY to plan what research is still needed. You do NOT write reports.

WORKFLOW:
1. Use search_kb to read all existing documents for the subject \
(search by subject name, type=osint, type=finding, type=research).
2. Use get_client to load the client profile and see last_activity dates.
3. Count how many type=finding documents exist. Check each of the 7 required \
sections for coverage gaps.
4. For each gap, call enqueue_task ONCE with a specific targeted search query or URL.
5. When done, stop. Do not call enqueue_task for information already covered \
by recent findings.

WHAT COUNTS AS COVERAGE:
- A type=osint document alone is NOT sufficient — it is a starting point only.
- Coverage requires type=finding documents: at minimum 5 findings spread across \
the 7 sections below.
- If fewer than 5 type=finding documents exist, ALWAYS enqueue research tasks — \
do not skip.
- Sections to cover: Overview, Leadership, Financial, Products & Services, \
Recent News, Market Position, Sales Intelligence.
- A section is only "covered" if at least one type=finding document addresses it \
AND was fetched within the last 30 days.

RULES:
- Only skip enqueuing if ALL 7 sections have recent findings (within 30 days) AND \
total finding count is ≥ 10. Otherwise always enqueue.
- Never enqueue duplicate tasks. One task per specific gap.
- task_type must be one of: web_search, fetch_url, profile_lookup
- For web_search, pass the search string in the "query" argument (not nested in payload).
  Good: query="IBM CEO Arvind Krishna 2026"  Bad: query="IBM"
- Enqueue at least 5 tasks, one per section gap, unless coverage is genuinely complete.
- Prioritise: recent news (priority 7), financial signals (priority 8), \
leadership gaps (priority 6), general overview (priority 5).

OUTPUT (write this as your final text after all enqueue_task calls):
{"gaps": ["list of gaps found"], "existing_coverage": {"section": "status"}, "tasks_spawned": N}
"""


# ---------------------------------------------------------------------------
# Tool factory — search_kb + get_client + enqueue_task
# ---------------------------------------------------------------------------

def build_orchestrator_tools(
    org_id: int,
    agent_run_id: Optional[int],
    orchestrate_task_id: int,
    default_subject: str = "",
    default_subject_type: str = "company",
    brain_override: str = "",
    model_override: str = "",
) -> list[Tool]:
    """Return the three tools available to the OrchestratorAgent."""
    from agents.events import emit as _emit

    standard = {t.name: t for t in build_tools(org_id, agent_run_id)}

    # Benchmark isolation: when subject carries a [tag] (e.g. "Acme [qwen3.5]"),
    # scope all KB lookups to that exact labeled client so cloud model orchestrators
    # only see their own findings, not those from other model runs.
    is_isolated = "[" in default_subject and "]" in default_subject
    if is_isolated:
        _orig_search = standard["search_kb"]

        async def _scoped_search_kb(query: str, type: str = "", client: str = "", top_k: int = 10) -> dict:
            # If the labeled client doesn't exist yet, return empty rather than
            # falling back to all-document search (which would expose other model runs).
            labeled_client = await _db.get_client(org_id, default_subject)
            if not labeled_client:
                return {"results": [], "count": 0}
            return await _orig_search.fn(query=query, type=type, client=default_subject, top_k=top_k)

        standard["search_kb"] = Tool(
            name=_orig_search.name,
            description=_orig_search.description,
            parameters=_orig_search.parameters,
            fn=_scoped_search_kb,
        )

    async def _enqueue_task(
        task_type: str,
        query: str = "",         # for web_search — the search string
        url: str = "",           # for fetch_url — the URL to fetch
        name: str = "",          # for profile_lookup — person name
        priority: int = 5,
        subject: str = "",       # LLMs frequently omit this since it's in context
        subject_type: str = "",  # same
        payload=None,            # legacy/nested form — merged if provided
        **_extra,                # absorb any unexpected kwargs gracefully
    ) -> dict:
        # Issue #2: when benchmark-isolated, always use the labeled subject regardless
        # of what the LLM passed — prevents tasks landing under the unlabeled client.
        _subject = default_subject if is_isolated else (subject.strip() or default_subject)
        _subject_type = subject_type.strip() or default_subject_type
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 5
        # Normalise legacy payload — can be None, empty string, or a dict.
        if payload is None or payload == "":
            payload = {}
        elif isinstance(payload, str):
            try:
                payload = json.loads(payload.strip()) if payload.strip() else {}
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # Flat top-level args take precedence over nested payload keys
        if query:
            payload["query"] = query
        if url:
            payload["url"] = url
        if name:
            payload["name"] = name
        if _extra:
            logger.debug("enqueue_task: absorbed unexpected kwargs: %s", list(_extra.keys()))
        # Propagate brain/model overrides into child task payloads so workers use
        # the same model as the orchestrator that spawned them.
        enriched_payload = dict(payload)
        if brain_override:
            enriched_payload["_brain_override"] = brain_override
        if model_override:
            enriched_payload["_model_override"] = model_override
        task_id = await _db.enqueue_research_task(
            org_id=org_id,
            subject_type=_subject_type,
            subject=_subject,
            task_type=task_type,
            payload=enriched_payload,
            depth=1,
            parent_task_id=orchestrate_task_id,
            priority=priority,
            agent_run_id=agent_run_id,
        )
        logger.info(
            "Orchestrator enqueued %s task %d for subject=%r (brain=%s model=%s)",
            task_type, task_id, _subject, brain_override or "ollama", model_override or "default",
        )
        await _emit({
            "type": "task_enqueued",
            "subject": _subject,
            "task_type": task_type,
            "query": enriched_payload.get("query") or enriched_payload.get("url") or enriched_payload.get("name") or "",
            "task_id": task_id,
            "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return {"task_id": task_id, "task_type": task_type, "subject": _subject}

    enqueue_tool = Tool(
        name="enqueue_task",
        description=(
            "Enqueue a targeted research task to fill a specific knowledge gap. "
            "Only emit tasks for information that is genuinely missing or stale (>14 days). "
            "Do not re-research what already exists and is current."
        ),
        parameters={
            "type": "object",
            "properties": {
                "subject_type": {
                    "type": "string",
                    "description": "company|person|topic|url",
                },
                "subject": {
                    "type": "string",
                    "description": "The entity being researched (company name or person name)",
                },
                "task_type": {
                    "type": "string",
                    "description": "web_search|fetch_url|profile_lookup",
                },
                "query": {
                    "type": "string",
                    "description": "Required for task_type=web_search. The specific search string, e.g. 'Trumpf CEO 2026 leadership'",
                },
                "url": {
                    "type": "string",
                    "description": "Required for task_type=fetch_url. The URL to fetch.",
                },
                "name": {
                    "type": "string",
                    "description": "Required for task_type=profile_lookup. The person's full name.",
                },
                "priority": {
                    "type": "integer",
                    "description": "1 (low) to 9 (high), default 5",
                },
            },
            "required": ["subject_type", "subject", "task_type"],
        },
        fn=_enqueue_task,
    )

    return [
        standard["search_kb"],
        standard["get_client"],
        enqueue_tool,
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_orchestrator(
    task_id: int,
    org_id: int,
    subject: str,
    subject_type: str,
    payload: dict,
    brain_override: str = "",
    model_override: str = "",
) -> dict:
    """Run the OrchestratorAgent for a given subject.

    Called by research_runner when task_type == 'orchestrate'.
    Creates an agent_runs row, runs the Agent loop (think=True, 32k ctx),
    parses the JSON summary from the output, and returns a result dict.

    The orchestrator emits child tasks via the enqueue_task tool during its run —
    NOT via a new_tasks return value. research_runner should pass [] as new_tasks.
    """
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    run_id = await _db.create_agent_run(
        org_id=org_id,
        agent_type="orchestrator",
        task=f"Research orchestration: {_display_subject(subject)}",
        trigger_type="research_queue",
    )
    await _db.update_agent_run(run_id, "running")

    # Record which agent_run is handling this task
    if _db._pool:
        try:
            async with _db._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE research_tasks SET assigned_agent_run_id = $1 WHERE id = $2",
                    run_id, task_id,
                )
        except Exception as exc:
            logger.warning("Could not set assigned_agent_run_id: %s", exc)

    try:
        brain = _load_brain(orchestrator=True, brain_override=brain_override, model_override=model_override)
        logger.info(
            "Orchestrator brain=%s model=%s subject=%r",
            brain_override or "ollama", model_override or "default", subject,
        )
        tools = build_orchestrator_tools(
            org_id, run_id, task_id,
            default_subject=subject,
            default_subject_type=subject_type,
            brain_override=brain_override,
            model_override=model_override,
        )
        agent = Agent(
            name="orchestrator",
            brain=brain,
            tools=tools,
            org_id=org_id,
            run_id=run_id,
            instructions=ORCHESTRATOR_INSTRUCTIONS,
        )

        angles_hint = payload.get("angles", "")
        is_isolated = "[" in subject and "]" in subject
        task_text = (
            f"Research subject: {_display_subject(subject)} (type: {subject_type})\n"
            + (f"Client name in KB (use exactly for get_client): {subject}\n" if is_isolated else "")
            + (f"Requested angles: {angles_hint}\n" if angles_hint else "")
            + "Identify coverage gaps and enqueue targeted research tasks."
        )

        result = await agent.run(task_text)

        await _db.update_agent_run(
            run_id,
            "done",
            tool_calls=result["tool_calls"],
            output={"text": result["output"], "iterations": result["iterations"]},
        )

        # Parse the JSON summary from the output (best-effort)
        output_text = result.get("output", "")
        m = re.search(r"\{.*\}", output_text, re.DOTALL)
        parsed: dict = {}
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                pass

        return {
            "agent_run_id": run_id,
            "gaps": parsed.get("gaps", []),
            "existing_coverage": parsed.get("existing_coverage", {}),
            "tasks_spawned": parsed.get("tasks_spawned", 0),
            "raw_output": output_text,
        }

    except Exception as exc:
        logger.error("Orchestrator failed for %s: %s", subject, exc)
        await _db.update_agent_run(run_id, "failed", error=str(exc))
        return {"agent_run_id": run_id, "error": str(exc)}
