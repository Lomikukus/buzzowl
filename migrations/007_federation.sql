-- 007: Matrix federation transport for shared clients (Phase 5b).
--
-- One Matrix bot identity per org, one encrypted invite-only room per partner
-- pair. Share groups can have REMOTE members (a partner instance) next to the
-- local member orgs; the sharing outbox fans out to remote members through
-- federation_outbox, and events received from partners land in
-- federation_inbox before they are applied. Nothing here carries plaintext
-- keys: access tokens are encrypted with plans.encrypt_secret.

CREATE TABLE IF NOT EXISTS federation_identities (
    org_id           BIGINT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    homeserver_url   TEXT NOT NULL,
    mxid             TEXT NOT NULL UNIQUE,
    device_id        TEXT,
    access_token_enc TEXT,
    ed25519          TEXT,                       -- our device fingerprint (shown to partners)
    display_name     TEXT,                       -- org name announced in hello
    status           TEXT NOT NULL DEFAULT 'configured',   -- configured | online | error | disabled
    last_error       TEXT,
    last_sync_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS federation_partners (
    id               BIGSERIAL PRIMARY KEY,
    org_id           BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    partner_mxid     TEXT NOT NULL,
    partner_name     TEXT,                       -- from their hello
    room_id          TEXT,
    direction        TEXT NOT NULL DEFAULT 'outgoing',     -- outgoing (we invited) | incoming (they invited)
    status           TEXT NOT NULL DEFAULT 'pending',      -- pending | active | reverify | blocked | left
    pinned_device_id TEXT,
    pinned_ed25519   TEXT,
    seen_device_id   TEXT,                       -- newest device we observed (for the verify screen)
    seen_ed25519     TEXT,
    verified_at      TIMESTAMPTZ,
    verified_by      BIGINT REFERENCES users(id) ON DELETE SET NULL,
    last_event_at    TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (org_id, partner_mxid),
    CONSTRAINT federation_partners_status_chk CHECK (status IN ('pending', 'active', 'reverify', 'blocked', 'left'))
);
CREATE INDEX IF NOT EXISTS idx_fed_partners_room ON federation_partners (room_id);

-- Events we still have to send (one row per partner per change).
CREATE TABLE IF NOT EXISTS federation_outbox (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    partner_id   BIGINT NOT NULL REFERENCES federation_partners(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,                  -- hello | share_invite | share_accept | share_decline | share_leave |
                                                 -- document | document_delete | profile | monitor
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    sent_at      TIMESTAMPTZ,
    event_id     TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_fed_outbox_pending ON federation_outbox (org_id, id) WHERE sent_at IS NULL;

-- Events received from partners (replay-safe on event_id); applied when the
-- partner is verified.
CREATE TABLE IF NOT EXISTS federation_inbox (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    partner_id   BIGINT REFERENCES federation_partners(id) ON DELETE SET NULL,
    room_id      TEXT,
    event_id     TEXT UNIQUE,
    sender       TEXT,
    sender_key   TEXT,
    verified     BOOLEAN NOT NULL DEFAULT FALSE,   -- decrypted from the pinned/verified device
    kind         TEXT NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at  TIMESTAMPTZ DEFAULT NOW(),
    applied_at   TIMESTAMPTZ,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_fed_inbox_pending ON federation_inbox (org_id, id) WHERE applied_at IS NULL;

-- Remote members of a share group (a partner instance).
CREATE TABLE IF NOT EXISTS shared_client_remote_members (
    shared_client_id BIGINT NOT NULL REFERENCES shared_clients(id) ON DELETE CASCADE,
    partner_id       BIGINT NOT NULL REFERENCES federation_partners(id) ON DELETE CASCADE,
    role             TEXT NOT NULL DEFAULT 'member',
    joined_at        TIMESTAMPTZ DEFAULT NOW(),
    left_at          TIMESTAMPTZ,
    PRIMARY KEY (shared_client_id, partner_id)
);

-- Monitoring may sit on a remote member.
ALTER TABLE shared_clients ADD COLUMN IF NOT EXISTS monitor_partner_id BIGINT REFERENCES federation_partners(id) ON DELETE SET NULL;

-- Invites across instances: from a partner (incoming) or to a partner (outgoing);
-- from_org_id becomes optional for incoming remote invites.
ALTER TABLE share_invites ALTER COLUMN from_org_id DROP NOT NULL;
ALTER TABLE share_invites ADD COLUMN IF NOT EXISTS from_partner_id  BIGINT REFERENCES federation_partners(id) ON DELETE CASCADE;
ALTER TABLE share_invites ADD COLUMN IF NOT EXISTS to_partner_id    BIGINT REFERENCES federation_partners(id) ON DELETE CASCADE;
ALTER TABLE share_invites ADD COLUMN IF NOT EXISTS remote_group_key UUID;
ALTER TABLE share_invites ADD COLUMN IF NOT EXISTS remote_invite_id BIGINT;   -- the sender's invite id (echoed in accept)
