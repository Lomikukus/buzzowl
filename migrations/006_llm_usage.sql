-- 006: LLM usage metering per org (Phase 6a — plans light|premium, monthly budgets).
-- One row per completed LLM call (Python llm.py or the Pi agent service).
CREATE TABLE IF NOT EXISTS llm_usage_events (
    id                BIGSERIAL PRIMARY KEY,
    org_id            BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id           BIGINT REFERENCES users(id) ON DELETE SET NULL,
    surface           TEXT,                          -- chat | research | pipeline | summary | triage | agent:<type> | embed
    role              TEXT,                          -- llm role that was resolved
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12,6),                 -- NULL when the model has no known price
    source            TEXT NOT NULL DEFAULT 'python', -- python | pi
    agent_run_id      BIGINT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_org_time ON llm_usage_events (org_id, created_at DESC);
