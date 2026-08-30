-- schema.sql — the single source of truth for the Buzzowl schema (v1).
--
-- A fresh database gets exactly this file (docker-entrypoint.sh, scripts/db_init.sh,
-- or db.init_db()'s fresh-install path). Anything beyond v1 lives in migrations/
-- as ordered NNN_name.sql files applied by db.init_db() — see migrations/README.md.
-- Do NOT add ad-hoc CREATE TABLE / ALTER TABLE calls to db.py.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE orgs (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    email         TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    -- UI A/B: opt-in front-end theme variant ('classic' | 'carbon').
    -- Kept as the LAST column: it was added via ALTER TABLE on live DBs,
    -- so this position matches existing databases exactly.
    ui_variant    TEXT NOT NULL DEFAULT 'classic',
    UNIQUE(org_id, username)
);

CREATE TABLE user_sessions (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE clients (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}',
    session_count INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    fts_doc       TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata->>'industry', '')), 'B') ||
        setweight(to_tsvector('english', coalesce(metadata->>'notes', '')), 'C')
    ) STORED,
    embedding     vector(768),
    UNIQUE(org_id, name)
);

CREATE TABLE contacts (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    client_id     BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}',
    session_count INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    fts_doc       TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(
            coalesce(metadata->>'role', '') || ' ' ||
            coalesce(metadata->>'company', ''), ''
        )), 'B') ||
        setweight(to_tsvector('english', coalesce(metadata->>'notes', '')), 'C')
    ) STORED,
    embedding     vector(768),
    UNIQUE(org_id, name)
);

CREATE TABLE documents (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    doc_id       TEXT NOT NULL,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    metadata     JSONB NOT NULL DEFAULT '{}',
    visibility   TEXT NOT NULL DEFAULT 'shared',
    source       TEXT NOT NULL DEFAULT 'human',
    agent_run_id BIGINT,
    created_by   BIGINT REFERENCES users(id),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    fts_doc      TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED,
    embedding    vector(768),
    UNIQUE(org_id, doc_id)
);

CREATE TABLE document_links (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id   BIGINT NOT NULL,
    PRIMARY KEY (document_id, entity_type, entity_id)
);

CREATE TABLE agent_runs (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    triggered_by  BIGINT REFERENCES users(id),
    trigger_type  TEXT NOT NULL,
    agent_type    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    task          TEXT NOT NULL,
    tool_calls    JSONB NOT NULL DEFAULT '[]',
    output        JSONB NOT NULL DEFAULT '{}',
    error         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at  TIMESTAMPTZ
);

ALTER TABLE documents
    ADD CONSTRAINT fk_documents_agent_run
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL;

CREATE TABLE heartbeats (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    agent_type  TEXT NOT NULL,
    cron_expr   TEXT NOT NULL,
    task        TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ
);

-- GIN full-text indexes
CREATE INDEX idx_clients_fts   ON clients   USING GIN(fts_doc);
CREATE INDEX idx_contacts_fts  ON contacts  USING GIN(fts_doc);
CREATE INDEX idx_documents_fts ON documents USING GIN(fts_doc);

-- HNSW vector indexes
CREATE INDEX idx_clients_vec   ON clients   USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX idx_contacts_vec  ON contacts  USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX idx_documents_vec ON documents USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);

-- Org scoping indexes
CREATE INDEX idx_clients_org    ON clients(org_id);
CREATE INDEX idx_contacts_org   ON contacts(org_id);
CREATE INDEX idx_documents_org  ON documents(org_id, type);
CREATE INDEX idx_agent_runs_org ON agent_runs(org_id, status);
CREATE INDEX idx_documents_type ON documents(org_id, type);
CREATE INDEX idx_clients_meta   ON clients  USING GIN(metadata);
CREATE INDEX idx_contacts_meta  ON contacts USING GIN(metadata);

-- Trigram indexes for fuzzy name/title matching
CREATE INDEX idx_clients_trgm   ON clients   USING GIN(name gin_trgm_ops);
CREATE INDEX idx_contacts_trgm  ON contacts  USING GIN(name gin_trgm_ops);
CREATE INDEX idx_documents_trgm ON documents USING GIN(title gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Phase 7: Deep Research Engine
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS research_tasks (
    id                    BIGSERIAL PRIMARY KEY,
    org_id                BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    subject_type          TEXT NOT NULL,           -- company|person|topic|url
    subject               TEXT NOT NULL,
    task_type             TEXT NOT NULL,           -- web_search|fetch_url|profile_lookup|analyze|aggregate|orchestrate
    payload               JSONB NOT NULL DEFAULT '{}',
    depth                 INT NOT NULL DEFAULT 0,
    parent_task_id        BIGINT REFERENCES research_tasks(id) ON DELETE SET NULL,
    status                TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
    priority              INT NOT NULL DEFAULT 5,
    assigned_agent_run_id BIGINT REFERENCES agent_runs(id) ON DELETE SET NULL,
    result                JSONB,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    completed_at          TIMESTAMPTZ
);

-- claim_research_task hot path: org+status+priority+id
CREATE INDEX IF NOT EXISTS idx_research_tasks_claim   ON research_tasks(org_id, status, priority DESC, id ASC);
-- aggregation check: "are there pending tasks for this subject?"
CREATE INDEX IF NOT EXISTS idx_research_tasks_subject ON research_tasks(org_id, subject, status);
CREATE INDEX IF NOT EXISTS idx_research_tasks_parent  ON research_tasks(parent_task_id);

-- Phase 9.5: Chat sessions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS chat_sessions (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    title       TEXT NOT NULL DEFAULT 'New conversation',
    messages    JSONB NOT NULL DEFAULT '[]',
    client_name TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_org ON chat_sessions(org_id, updated_at DESC);

-- research_findings view: finding documents joined to their entity links
CREATE OR REPLACE VIEW research_findings AS
    SELECT d.id, d.org_id, d.doc_id, d.title, d.content, d.metadata,
           d.source, d.agent_run_id, d.created_at, d.updated_at,
           dl.entity_type, dl.entity_id
    FROM documents d
    LEFT JOIN document_links dl ON dl.document_id = d.id
    WHERE d.type = 'finding';

-- Phase: Seller Product Intelligence
CREATE TABLE IF NOT EXISTS seller_companies (
    id               BIGSERIAL PRIMARY KEY,
    org_id           BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    website_url      TEXT,
    industry         TEXT,
    research_status  TEXT NOT NULL DEFAULT 'pending',
    -- pending | researching | products_found | deep_researching | deep_research_done | verified
    research_doc_id  BIGINT REFERENCES documents(id) ON DELETE SET NULL,
    metadata         JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(org_id)
);

CREATE TABLE IF NOT EXISTS products (
    id                BIGSERIAL PRIMARY KEY,
    org_id            BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    seller_company_id BIGINT NOT NULL REFERENCES seller_companies(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    category          TEXT,
    description       TEXT,
    key_features      JSONB NOT NULL DEFAULT '[]',
    pricing_info      TEXT,
    target_customer   TEXT,
    is_focus          BOOLEAN NOT NULL DEFAULT FALSE,
    priority          INTEGER NOT NULL DEFAULT 0,
    is_favorite       BOOLEAN NOT NULL DEFAULT FALSE,
    is_shared         BOOLEAN NOT NULL DEFAULT FALSE,
    status            TEXT NOT NULL DEFAULT 'draft',
    source_doc_id     BIGINT REFERENCES documents(id) ON DELETE SET NULL,
    metadata          JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    -- Added via ALTER TABLE on live DBs — kept last so column order matches.
    website_url       TEXT
);

CREATE INDEX IF NOT EXISTS idx_seller_companies_org ON seller_companies(org_id);
CREATE INDEX IF NOT EXISTS idx_products_org_company ON products(org_id, seller_company_id);
CREATE INDEX IF NOT EXISTS idx_products_org_status  ON products(org_id, status);
CREATE INDEX IF NOT EXISTS idx_products_focus       ON products(org_id, is_focus) WHERE is_focus = TRUE;
CREATE INDEX IF NOT EXISTS idx_products_shared      ON products(org_id, is_shared) WHERE is_shared = TRUE;

-- Phase 18: invite-key whitelist
CREATE TABLE IF NOT EXISTS invitations (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    invite_key  TEXT NOT NULL UNIQUE,
    email       TEXT,
    role        TEXT NOT NULL DEFAULT 'member',
    created_by  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    used_by     BIGINT REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_invitations_org ON invitations(org_id, used_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_invitations_key ON invitations(invite_key);

-- System-level registration keys (operator-issued, required to create a new org)
CREATE TABLE IF NOT EXISTS registration_keys (
    id          BIGSERIAL PRIMARY KEY,
    reg_key     TEXT NOT NULL UNIQUE,
    label       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,
    used_at     TIMESTAMPTZ,
    used_by_org BIGINT REFERENCES orgs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_reg_keys_key ON registration_keys(reg_key);

-- Thesis evaluation: manual research timing (Session 88)
CREATE TABLE IF NOT EXISTS research_sessions (
    id              BIGSERIAL PRIMARY KEY,
    org_id          BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id         BIGINT REFERENCES users(id) ON DELETE SET NULL,
    client_name     TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    method          TEXT NOT NULL DEFAULT 'manual',
    sources_checked INTEGER,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_sessions_org ON research_sessions(org_id, started_at);

-- Usage analytics: what users ask the system (Session 88)
CREATE TABLE IF NOT EXISTS prompt_log (
    id         BIGSERIAL PRIMARY KEY,
    org_id     BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
    surface    TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    context    JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prompt_log_org ON prompt_log(org_id, created_at);

-- Per-contact outreach log (Session 92): the durable record behind the Home
-- "Last contacted" panel. One row per person a rep exported a mail to — with
-- the actual email sent, plus replied / follow-up tracking.
CREATE TABLE IF NOT EXISTS contact_log (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
    client_name   TEXT NOT NULL,
    contact_name  TEXT,
    contact_email TEXT,
    subject       TEXT,
    body          TEXT,
    sent_at       TIMESTAMPTZ DEFAULT NOW(),
    replied       BOOLEAN NOT NULL DEFAULT false,
    follow_up     BOOLEAN NOT NULL DEFAULT false,
    source_doc_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_contact_log_user ON contact_log(org_id, user_id, sent_at DESC);

-- User to-do / follow-up tasks (Deploy 2): rep-created reminders with a due
-- date. Its own table (not a documents row) because it needs indexable
-- due_date/status columns for the reminder query and NBA feed, and carries no
-- knowledge/embedding value — same reasoning as contact_log above.
CREATE TABLE IF NOT EXISTS user_tasks (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id      BIGINT REFERENCES users(id) ON DELETE SET NULL,
    client_name  TEXT,
    title        TEXT NOT NULL,
    notes        TEXT,
    due_date     DATE,
    priority     INTEGER NOT NULL DEFAULT 5,
    status       TEXT NOT NULL DEFAULT 'open',   -- open | done
    source       TEXT NOT NULL DEFAULT 'manual', -- manual | follow_up | chat
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_tasks_open ON user_tasks(org_id, user_id, due_date) WHERE status='open';

-- ---------------------------------------------------------------------------
-- Schema version bookkeeping
-- ---------------------------------------------------------------------------
-- This file IS version 1. Changes beyond v1 are ordered migration files in
-- migrations/NNN_name.sql, applied by db.init_db() which stamps each applied
-- version here. See migrations/README.md.

CREATE TABLE IF NOT EXISTS schema_version (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
