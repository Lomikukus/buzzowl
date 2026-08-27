-- 008: age indexes for the nightly retention prune (retention.py).
--
-- The prune filters operational telemetry by age across ALL orgs
-- (`created_at < cutoff`), so the existing org-scoped indexes
-- (idx_agent_runs_org_trigger_created, idx_prompt_log_org) cannot serve it —
-- without these two a nightly prune is a sequential scan per batch.
-- Additive and idempotent; no data is touched here.

CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_prompt_log_created ON prompt_log (created_at);
