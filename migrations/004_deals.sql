-- 004: CRM deals + activity timeline prerequisites + reminder upgrades.
--
-- deals is a real table (not a documents type): it is a typed business object
-- like clients/contacts/products — pipeline totals need SUM(value) by stage,
-- stages need constrained transitions with history, and FK integrity to
-- clients matters. The documents rule covers content, not operational objects.

CREATE TABLE IF NOT EXISTS deals (
    id              BIGSERIAL PRIMARY KEY,
    org_id          BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    client_id       BIGINT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'lead',
    value           NUMERIC(14,2),
    currency        TEXT NOT NULL DEFAULT 'EUR',
    probability     INTEGER,                       -- 0-100, NULL = derive from stage
    expected_close  DATE,
    owner_user_id   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open | won | lost
    closed_at       TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by      BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT deals_status_chk CHECK (status IN ('open', 'won', 'lost')),
    CONSTRAINT deals_probability_chk CHECK (probability IS NULL OR (probability BETWEEN 0 AND 100))
);
CREATE INDEX IF NOT EXISTS idx_deals_org_status_stage ON deals (org_id, status, stage);
CREATE INDEX IF NOT EXISTS idx_deals_client ON deals (client_id);
CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals (org_id, owner_user_id) WHERE status = 'open';

-- Append-only stage/status history: who moved what, when, from where.
CREATE TABLE IF NOT EXISTS deal_events (
    id                BIGSERIAL PRIMARY KEY,
    org_id            BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    deal_id           BIGINT NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,               -- created | stage | status | value | note
    from_value        TEXT,
    to_value          TEXT,
    note              TEXT,
    actor_user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    actor_agent_run_id BIGINT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_deal_events_deal ON deal_events (deal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deal_events_org_time ON deal_events (org_id, created_at DESC);

-- Reminders: recurrence, snooze, deal link.
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS recurrence   TEXT;          -- daily | weekly | monthly | NULL
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS snooze_until TIMESTAMPTZ;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS deal_id      BIGINT REFERENCES deals(id) ON DELETE SET NULL;
