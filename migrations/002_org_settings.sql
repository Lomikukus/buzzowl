-- 002: per-org settings (autonomy level, budgets, kill switch, ...).
-- Also the prerequisite for per-org configuration in the hosted offering.
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Autonomous decisions are agent_runs rows (agent_type = 'autonomy_review',
-- trigger_type = 'autonomous'); this index keeps the daily budget count and the
-- audit log cheap.
CREATE INDEX IF NOT EXISTS idx_agent_runs_org_trigger_created
    ON agent_runs (org_id, trigger_type, created_at DESC);
