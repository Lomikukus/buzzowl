"""
agents/research_runner.py — Dispatch loop for Phase 7 Deep Research Engine.

run_research_workers(org_id, n_workers) starts N concurrent asyncio tasks.
Each worker: claim_task → dispatch → complete_task → maybe trigger aggregator.

Task type → handler mapping:
  orchestrate     → run_orchestrator     (uses Agent loop)
  web_search      → run_link_collector   (direct Ollama)
  profile_lookup  → run_link_collector   (direct Ollama, with person-specific query)
  fetch_url       → run_page_reader      (direct Ollama)
  analyze         → run_content_analyzer (direct Ollama)
  aggregate       → run_company_aggregator or run_person_aggregator
  summarise       → stub (future)

Workers idle (asyncio.sleep(2)) when no tasks are available.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from agents.events import emit as _emit
from agents import research_log as _rlog

import db as _db
from agents.orchestrator import run_orchestrator
from agents.delegator import run_delegator_orchestrator
from agents.workers import (
    run_company_aggregator,
    run_content_analyzer,
    run_link_collector,
    run_page_reader,
    run_person_aggregator,
)

logger = logging.getLogger(__name__)

_PAUSED = False

# Post-queue org-hygiene trigger state
_last_task_completed = 0   # incremented after every completed task
_org_triggered_after = 0   # value of _last_task_completed when org was last triggered


def pause_workers() -> None:
    global _PAUSED
    _PAUSED = True


def resume_workers() -> None:
    global _PAUSED
    _PAUSED = False


async def _reset_stale_running_tasks(org_id: int) -> int:
    """On startup: reset tasks stuck in 'running' from a previous server session to 'pending'."""
    if not _db._pool:
        return 0
    async with _db._pool.acquire() as conn:
        count = await conn.fetchval(
            "WITH updated AS (UPDATE research_tasks SET status='pending' WHERE org_id=$1 AND status='running' RETURNING id) SELECT COUNT(*) FROM updated",
            org_id,
        )
    if (count or 0) > 0:
        logger.info("Reset %d stale 'running' research tasks to 'pending' on startup", count)
    return count or 0


async def _maybe_trigger_org(org_id: int) -> None:
    """Fire org-hygiene agent when the research queue has fully drained after work was done."""
    global _org_triggered_after
    cfg = _load_config()
    if not cfg.get("org_agent_enabled", True):
        return
    if _last_task_completed <= _org_triggered_after:
        return
    if not _db._pool:
        return
    try:
        async with _db._pool.acquire(timeout=5) as conn:
            # Only block on pending tasks — running tasks from this session will
            # complete and increment _last_task_completed before going idle again.
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM research_tasks WHERE org_id=$1 AND status='pending'",
                org_id,
            )
    except Exception:
        return  # pool busy — will retry on next idle tick
    if (pending or 0) > 0:
        return

    _org_triggered_after = _last_task_completed
    logger.info("Research queue drained — triggering post-research org hygiene")
    try:
        run_id = await _db.create_agent_run(
            org_id=org_id, agent_type="org",
            task="Post-research org hygiene: link new findings to clients, deduplicate contacts.",
            trigger_type="heartbeat", triggered_by=None,
        )
        from agents.runner import run_agent
        asyncio.create_task(run_agent(
            run_id, org_id, "org",
            "Post-research org hygiene: link new findings to clients, deduplicate contacts.",
        ))
        asyncio.create_task(_emit({
            "type": "org_sweep_triggered",
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as exc:
        logger.warning("Post-research org sweep failed to start: %s", exc)


def _load_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    try:
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_runner_config() -> tuple[str, int, int, int, Optional[Path], str]:
    """Return (model, num_ctx, interest_threshold, max_depth, vault_path, brain_type)."""
    cfg = _load_config()
    # research_brain/research_model take priority over agent_brain/agent_model for research tasks
    brain_type = cfg.get("research_brain") or cfg.get("agent_brain", "ollama")
    model = cfg.get("research_model") or cfg.get("agent_model", "llama3.2")
    num_ctx = int(cfg.get("agent_num_ctx", 16384))
    threshold = int(cfg.get("research_interest_threshold", 4))
    max_depth = int(cfg.get("research_max_depth", 3))
    vp = cfg.get("vault_path", "")
    vault_path = Path(vp) if vp else None
    return model, num_ctx, threshold, max_depth, vault_path, brain_type


def _inject_brain_overrides(child_tasks: list[dict], brain: str, model: str) -> None:
    """Propagate brain/model overrides into child task payloads so the entire cascade uses the same model."""
    if not brain or brain == "ollama":
        return
    for t in child_tasks:
        p = t.setdefault("payload", {})
        p["_brain_override"] = brain
        p["_model_override"] = model


async def _dispatch(
    task: dict,
    model: str,
    num_ctx: int,
    threshold: int,
    vault_path: Optional[Path],
    max_depth: int,
    today: str,
    brain_type: str = "ollama",
) -> tuple[dict, list[dict]]:
    """Route a task to the appropriate handler. Returns (result_dict, child_tasks)."""
    task_type = task["task_type"]
    org_id = task["org_id"]
    subject = task["subject"]
    subject_type = task["subject_type"]
    payload = task.get("payload") or {}
    depth = task.get("depth", 0)
    agent_run_id = task.get("assigned_agent_run_id")

    # Per-task brain/model overrides (set at enqueue time via trigger payload)
    brain_override = payload.get("_brain_override", "") or (brain_type if brain_type != "ollama" else "")
    model_override = payload.get("_model_override", "")

    # Resolved brain/model for this task — used for workers and child task propagation
    effective_brain = brain_override or "ollama"
    effective_model = model_override or model

    if task_type == "orchestrate":
        effective_brain = brain_override or brain_type
        effective_model = model_override or model
        # Delegator handles all brains now (Ollama, OpenRouter, Claude) — no tool-calling needed
        # Agent-loop orchestrator kept for future use but delegator is the primary path
        use_delegator = True

        _rlog.info("runner", subject,
            f"Orchestrate START — brain={effective_brain!r} model={effective_model!r} mode=delegator")

        if use_delegator:
            result = await run_delegator_orchestrator(
                task_id=task["id"],
                org_id=org_id,
                subject=subject,
                subject_type=subject_type,
                payload=payload,
                brain_override=brain_override,
                model_override=effective_model,
            )
        else:
            try:
                result = await run_orchestrator(
                    task_id=task["id"],
                    org_id=org_id,
                    subject=subject,
                    subject_type=subject_type,
                    payload=payload,
                    brain_override=brain_override,
                    model_override=model_override,
                )
            except Exception as orch_exc:
                _rlog.warn("runner", subject,
                    f"Agent-loop orchestrator failed ({orch_exc}) — falling back to delegator",
                    detail=str(orch_exc))
                logger.warning(
                    "Orchestrator failed for %r with brain=%r (%s) — falling back to delegator",
                    subject, brain_override, orch_exc,
                )
                result = await run_delegator_orchestrator(
                    task_id=task["id"],
                    org_id=org_id,
                    subject=subject,
                    subject_type=subject_type,
                    payload=payload,
                    brain_override=brain_override,
                    model_override=effective_model,
                )

        tasks_spawned = result.get("tasks_spawned", 0) if isinstance(result, dict) else "?"
        _rlog.info("runner", subject,
            f"Orchestrate DONE — tasks_spawned={tasks_spawned}",
            detail=str(result.get("raw_output", ""))[:300] if isinstance(result, dict) else None)
        if tasks_spawned == 0:
            _rlog.warn("runner", subject,
                "⚠️  Orchestrator spawned 0 tasks — aggregator will fire immediately with no findings")
        return result, []

    elif task_type in ("web_search", "profile_lookup"):
        if task_type == "profile_lookup" and "query" not in payload:
            name = payload.get("name", subject)
            company = payload.get("company", "")
            angle = payload.get("angle", "linkedin profile background")
            payload = {"query": f'"{name}" {company} {angle}'.strip()}
        result, child_tasks = await run_link_collector(
            task_id=task["id"],
            org_id=org_id,
            subject=subject,
            subject_type=subject_type,
            payload=payload,
            depth=depth,
            max_depth=max_depth,
        )
        _inject_brain_overrides(child_tasks, effective_brain, effective_model)
        return result, child_tasks

    elif task_type == "fetch_url":
        result, child_tasks = await run_page_reader(
            task_id=task["id"],
            org_id=org_id,
            subject=subject,
            subject_type=subject_type,
            payload=payload,
            depth=depth,
            max_depth=max_depth,
            model=effective_model,
            num_ctx=num_ctx,
            interest_threshold=threshold,
            brain=effective_brain,
        )
        _inject_brain_overrides(child_tasks, effective_brain, effective_model)
        return result, child_tasks

    elif task_type == "analyze":
        result, child_tasks = await run_content_analyzer(
            task_id=task["id"],
            org_id=org_id,
            subject=subject,
            subject_type=subject_type,
            payload=payload,
            depth=depth,
            max_depth=max_depth,
            model=effective_model,
            num_ctx=num_ctx,
            interest_threshold=threshold,
            vault_path=vault_path,
            today=today,
            agent_run_id=agent_run_id,
            brain=effective_brain,
        )
        _inject_brain_overrides(child_tasks, effective_brain, effective_model)
        return result, child_tasks

    elif task_type == "aggregate":
        if subject_type == "industry":
            from agents.workers import run_industry_aggregator
            result = await run_industry_aggregator(
                task_id=task["id"],
                org_id=org_id,
                subject=subject,
                payload=payload,
                model=effective_model,
                num_ctx=num_ctx,
                vault_path=vault_path,
                today=today,
                brain=effective_brain,
            )
        elif subject_type == "company":
            result = await run_company_aggregator(
                task_id=task["id"],
                org_id=org_id,
                subject=subject,
                payload=payload,
                model=effective_model,
                num_ctx=num_ctx,
                vault_path=vault_path,
                today=today,
                brain=effective_brain,
            )
        else:
            result = await run_person_aggregator(
                task_id=task["id"],
                org_id=org_id,
                subject=subject,
                payload=payload,
                model=effective_model,
                num_ctx=num_ctx,
                vault_path=vault_path,
                today=today,
                brain=effective_brain,
            )
        return result, []

    else:
        logger.warning("Unknown task_type %r for task %d — skipping", task_type, task["id"])
        return {"error": f"unknown task_type: {task_type}"}, []


async def _mark_failed(task_id: int) -> None:
    """Best-effort: mark a task failed after a worker crash."""
    if not _db._pool:
        return
    try:
        async with _db._pool.acquire() as conn:
            await conn.execute(
                "UPDATE research_tasks SET status='failed', completed_at=NOW() WHERE id=$1",
                task_id,
            )
    except Exception:
        pass


async def _emit_spawned(parent_task: dict, org_id: int) -> None:
    """Fetch newly-spawned child tasks from DB and broadcast them to dashboard clients."""
    try:
        children = await _db.list_research_tasks(org_id, subject=parent_task["subject"])
        new_children = [
            t for t in children
            if t.get("parent_task_id") == parent_task["id"] and t["status"] == "pending"
        ]
        if new_children:
            await _emit({
                "type": "tasks_spawned",
                "parent_task_id": parent_task["id"],
                "subject": parent_task["subject"],
                "new_tasks": new_children,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as exc:
        logger.debug("Could not emit tasks_spawned: %s", exc)


async def _aggregate_in_flight(org_id: int, subject: str) -> bool:
    """Return True if an aggregate task for this subject is already pending or running."""
    if not _db._pool:
        return False
    async with _db._pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM research_tasks
            WHERE org_id = $1 AND subject = $2
              AND task_type = 'aggregate'
              AND status IN ('pending', 'running')
            """,
            org_id, subject,
        )
        return (count or 0) > 0


async def _worker_loop(org_id: int, worker_id: int) -> None:
    """Single worker: claims tasks, dispatches, marks complete. Runs indefinitely."""
    global _last_task_completed
    logger.info("Research worker %d started for org %d", worker_id, org_id)
    task: Optional[dict] = None

    while True:
        try:
            if _PAUSED:
                await asyncio.sleep(2)
                continue

            task = await _db.claim_research_task(org_id)
            if task is None:
                # Worker 0 owns the post-queue org trigger check
                if worker_id == 0:
                    try:
                        await _maybe_trigger_org(org_id)
                    except Exception as exc:
                        logger.warning("_maybe_trigger_org error: %s", exc)
                await asyncio.sleep(2)
                continue

            model, num_ctx, threshold, max_depth, vault_path, brain_type = _load_runner_config()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            claimed_at = datetime.now(timezone.utc)
            logger.info(
                "Worker %d claimed task %d: type=%s subject=%r depth=%d",
                worker_id, task["id"], task["task_type"], task["subject"], task.get("depth", 0),
            )

            asyncio.create_task(_emit({
                "type": "task_claimed",
                "task_id": task["id"],
                "task_type": task["task_type"],
                "subject": task["subject"],
                "subject_type": task["subject_type"],
                "depth": task.get("depth", 0),
                "worker_id": worker_id,
                "ts": claimed_at.isoformat(),
            }))

            result, new_tasks = await _dispatch(
                task, model, num_ctx, threshold, vault_path, max_depth, today, brain_type
            )

            elapsed = (datetime.now(timezone.utc) - claimed_at).total_seconds()

            should_aggregate = await _db.complete_research_task(
                task_id=task["id"],
                result=result,
                new_tasks=new_tasks,
                max_depth=max_depth,
            )
            _last_task_completed += 1

            asyncio.create_task(_emit({
                "type": "task_completed",
                "task_id": task["id"],
                "task_type": task["task_type"],
                "subject": task["subject"],
                "depth": task.get("depth", 0),
                "elapsed_s": round(elapsed, 1),
                "child_count": len(new_tasks) if new_tasks else 0,
                "ts": datetime.now(timezone.utc).isoformat(),
            }))

            if new_tasks:
                asyncio.create_task(_emit_spawned(task, org_id))

            if should_aggregate and task["task_type"] != "aggregate":
                _rlog.info("runner", task["subject"],
                    f"Queue drained after task #{task['id']} ({task['task_type']}) — checking if aggregator needed")
                agg_already_queued = await _aggregate_in_flight(org_id, task["subject"])
                if agg_already_queued:
                    _rlog.info("runner", task["subject"], "Aggregator already in flight — skipping")
                    logger.debug(
                        "Subject %r: aggregate already in flight, skipping auto-trigger",
                        task["subject"],
                    )
                else:
                    logger.info(
                        "Subject %r task queue drained — enqueuing aggregator",
                        task["subject"],
                    )
                    _rlog.info("runner", task["subject"],
                        f"▶ Enqueuing aggregator (triggered by task #{task['id']} {task['task_type']})")
                    task_payload = task.get("payload") or {}
                    agg_payload: dict = {"triggered_by_task_id": task["id"]}
                    if task_payload.get("_brain_override"):
                        agg_payload["_brain_override"] = task_payload["_brain_override"]
                    if task_payload.get("_model_override"):
                        agg_payload["_model_override"] = task_payload["_model_override"]
                    agg_id = await _db.enqueue_research_task(
                        org_id=org_id,
                        subject_type=task["subject_type"],
                        subject=task["subject"],
                        task_type="aggregate",
                        payload=agg_payload,
                        depth=task.get("depth", 0),
                        priority=9,
                        agent_run_id=task.get("assigned_agent_run_id"),
                    )
                    asyncio.create_task(_emit({
                        "type": "aggregator_triggered",
                        "subject": task["subject"],
                        "subject_type": task["subject_type"],
                        "task_id": agg_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }))

            task = None

        except asyncio.CancelledError:
            logger.info("Research worker %d cancelled", worker_id)
            raise
        except Exception as exc:
            logger.error("Research worker %d error: %s", worker_id, exc, exc_info=True)
            if task is not None:
                asyncio.create_task(_emit({
                    "type": "task_failed",
                    "task_id": task["id"],
                    "task_type": task["task_type"],
                    "subject": task["subject"],
                    "error": str(exc)[:200],
                    "ts": datetime.now(timezone.utc).isoformat(),
                }))
                await _mark_failed(task["id"])
                task = None
            await asyncio.sleep(5)


def run_research_workers(org_id: int, n_workers: int = 4) -> None:
    """Start n_workers concurrent worker coroutines. Fire-and-forget (no await).
    Also resets stale 'running' tasks from previous server sessions."""
    asyncio.create_task(_reset_stale_running_tasks(org_id))
    for i in range(n_workers):
        asyncio.create_task(_worker_loop(org_id, worker_id=i))
    logger.info("Research workers started: %d workers for org %d", n_workers, org_id)
