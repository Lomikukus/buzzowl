"""
retention.py — nightly pruning of operational telemetry.

Why
---
Two tables grow forever on a busy instance and nothing ever read them back
after a few weeks:

  agent_runs.tool_calls  one JSONB blob per agent run holding every tool call
                         it made — including fetched page content. By far the
                         biggest single contributor per research run.
  prompt_log             one row per chat/search prompt (capped at 4000 chars),
                         written on every message.

What this is NOT
----------------
Knowledge is never pruned. `documents`, `clients`, `contacts`, `deals`,
meetings, research — none of it is touched here, at any age. Only telemetry.

Model
-----
Two stages for agent_runs, because the row and its payload have very different
half-lives:

  1. after `tool_call_payload_days` (default 14) the heavy per-call payload is
     stripped but the row stays — status, timings, task, output and the
     documents pointing at it keep working for the dashboards and stats. The
     array keeps one (emptied) entry per call so call *counts* stay correct.
  2. after `agent_runs_days` (default 90) the row itself goes. The two FKs into
     agent_runs (documents.agent_run_id, research_tasks.assigned_agent_run_id)
     are ON DELETE SET NULL, so nothing cascades into knowledge.

prompt_log is a single stage (`prompt_log_days`, default 180 — the evaluation
endpoints look back up to 365 days, so this window is deliberately generous).

Everything is fail-safe: each step is independent, a failure is logged and the
remaining steps still run, and the job never propagates an exception into
APScheduler. Turn the whole thing off with `retention.enabled: false`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("wk.retention")

# Defaults — every one of these is overridable from the config.yaml `retention` block.
DEFAULTS = {
    "enabled": True,
    "cron": "20 3 * * *",          # quiet hour: after the nightly dump, before the 04:00+ heartbeats
    "tool_call_payload_days": 14,  # stage 1: strip agent_runs.tool_calls payloads
    "agent_runs_days": 90,         # stage 2: delete the agent_runs row
    "prompt_log_days": 180,        # prompt_log rows (evaluation looks back <= 365d)
    "batch_size": 2000,            # rows per statement; the job loops until done or capped
}

# Hard cap on batches per table per run — bounds the work a single night can do
# so a first run on a years-old database can't hold the table for an hour. The
# remainder is picked up the next night.
MAX_BATCHES = 50

# Below this the window is treated as a misconfiguration and the step is
# skipped: nobody wants a typo'd `0` to wipe today's telemetry.
MIN_DAYS = 1


def get_settings(config: dict) -> dict:
    """Merge the config.yaml `retention` block over DEFAULTS, coercing types."""
    raw = config.get("retention") or {}
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
    out["cron"] = str(raw.get("cron") or DEFAULTS["cron"])
    for key in ("tool_call_payload_days", "agent_runs_days", "prompt_log_days", "batch_size"):
        try:
            out[key] = int(raw.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    out["batch_size"] = max(1, min(out["batch_size"], 50_000))
    return out


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def run_retention() -> dict:
    """Prune operational telemetry once. Returns per-table counts.

    Never raises: a broken step is logged and the others still run, so one bad
    table can't stop the rest of the prune (or the scheduler).
    """
    import context

    db = context.db_module
    if not context.DB_AVAILABLE or db is None or getattr(db, "_pool", None) is None:
        return {"skipped": "db unavailable"}

    settings = get_settings(context.config)
    if not settings["enabled"]:
        return {"skipped": "disabled"}

    batch = settings["batch_size"]
    stats: dict = {"tool_calls_compacted": 0, "agent_runs_deleted": 0, "prompt_log_deleted": 0}
    errors: list[str] = []

    # (label, days, coroutine factory). Deleting the oldest runs first means the
    # payload strip has fewer rows left to walk.
    steps = [
        ("agent_runs_deleted", settings["agent_runs_days"],
         lambda c: db.delete_agent_runs_before(c, batch, MAX_BATCHES)),
        ("tool_calls_compacted", settings["tool_call_payload_days"],
         lambda c: db.compact_agent_run_tool_calls(c, batch, MAX_BATCHES)),
        ("prompt_log_deleted", settings["prompt_log_days"],
         lambda c: db.delete_prompt_log_before(c, max(batch, 5000), MAX_BATCHES)),
    ]

    for label, days, call in steps:
        if days < MIN_DAYS:
            logger.warning("retention: %s window is %s days (< %s) — step skipped",
                           label, days, MIN_DAYS)
            continue
        try:
            stats[label] = await call(_cutoff(days))
        except Exception as exc:            # one bad step must not stop the others
            errors.append(f"{label}: {exc}")
            logger.warning("retention step %s failed (non-fatal): %s", label, exc)

    if errors:
        stats["errors"] = errors
    total = sum(v for v in stats.values() if isinstance(v, int))
    line = ("retention: %d agent_runs deleted (>%dd), %d tool_call payloads stripped (>%dd), "
            "%d prompt_log rows deleted (>%dd)")
    args = (stats["agent_runs_deleted"], settings["agent_runs_days"],
            stats["tool_calls_compacted"], settings["tool_call_payload_days"],
            stats["prompt_log_deleted"], settings["prompt_log_days"])
    if total or errors:
        logger.info(line, *args)
    else:
        logger.debug(line, *args)          # nothing to do — stay quiet
    return stats
