-- 005: shared clients — collaboration between orgs (Phase 6a).
--
-- Two (or more) orgs share ONE client: each keeps its own clients row (org
-- isolation stays absolute), the rows are linked through a share group. Company
-- knowledge (research/osint/finding/signal documents, profile fields, monitored
-- sources) is replicated to every member org with provenance; personal data
-- (contacts, notes, meetings, outreach, deals, tasks) never leaves an org.
--
-- Replication is driven by an OUTBOX filled by triggers — that catches writes from
-- the Python server AND from the Pi agent service (which inserts documents
-- directly) — and drained by a worker through a Transport (in-DB today, Matrix
-- later). Replicated rows carry source='shared' and are never re-enqueued; the
-- worker additionally sets the session flag buzzowl.sync='on' which the triggers
-- honour, so applying a change can never echo back.

CREATE TABLE IF NOT EXISTS shared_clients (
    id               BIGSERIAL PRIMARY KEY,
    key              UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),  -- stable across instances
    name             TEXT NOT NULL,                                    -- client name at creation
    created_by_org   BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    created_by_user  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    monitor_org_id   BIGINT REFERENCES orgs(id) ON DELETE SET NULL,   -- who runs heartbeat monitoring
    scope            JSONB NOT NULL DEFAULT '{"doc_types": ["research", "osint", "finding", "signal"],
                                             "profile": true, "sources": true, "contacts": false}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'active',                  -- active | closed
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT shared_clients_status_chk CHECK (status IN ('active', 'closed'))
);

CREATE TABLE IF NOT EXISTS shared_client_members (
    shared_client_id BIGINT NOT NULL REFERENCES shared_clients(id) ON DELETE CASCADE,
    org_id           BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    client_id        BIGINT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    role             TEXT NOT NULL DEFAULT 'member',                  -- owner | member
    joined_by        BIGINT REFERENCES users(id) ON DELETE SET NULL,
    joined_at        TIMESTAMPTZ DEFAULT NOW(),
    left_at          TIMESTAMPTZ,
    PRIMARY KEY (shared_client_id, org_id)
);
-- a client row belongs to at most one ACTIVE share group
CREATE UNIQUE INDEX IF NOT EXISTS idx_scm_client_active ON shared_client_members (client_id) WHERE left_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_scm_org ON shared_client_members (org_id) WHERE left_at IS NULL;

CREATE TABLE IF NOT EXISTS share_invites (
    id               BIGSERIAL PRIMARY KEY,
    shared_client_id BIGINT NOT NULL REFERENCES shared_clients(id) ON DELETE CASCADE,
    from_org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    from_user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    to_org_id        BIGINT REFERENCES orgs(id) ON DELETE CASCADE,   -- resolved from to_user / to_email
    to_user_id       BIGINT REFERENCES users(id) ON DELETE CASCADE,
    to_email         TEXT,
    message          TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',                 -- pending | accepted | declined | revoked
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    responded_at     TIMESTAMPTZ,
    CONSTRAINT share_invites_status_chk CHECK (status IN ('pending', 'accepted', 'declined', 'revoked'))
);
CREATE INDEX IF NOT EXISTS idx_share_invites_to ON share_invites (to_org_id, status);
CREATE INDEX IF NOT EXISTS idx_share_invites_from ON share_invites (from_org_id, status);

-- Sync log: one row per change that must reach the other members.
CREATE TABLE IF NOT EXISTS share_outbox (
    id               BIGSERIAL PRIMARY KEY,
    shared_client_id BIGINT NOT NULL REFERENCES shared_clients(id) ON DELETE CASCADE,
    origin_org_id    BIGINT NOT NULL,
    kind             TEXT NOT NULL,          -- document | document_delete | profile | full_sync
    ref_id           BIGINT,                 -- documents.id for document; clients.id for profile
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    processed_at     TIMESTAMPTZ,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_outbox_pending ON share_outbox (id) WHERE processed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Triggers: detect shareable changes and enqueue them.
-- ---------------------------------------------------------------------------

-- Is this session applying a sync (must not echo)?
CREATE OR REPLACE FUNCTION buzzowl_sync_applying() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('buzzowl.sync', true), '') = 'on'
$$;

-- document_links: a document got linked to a client → if that client is in an
-- active share group and the type is in scope, enqueue the document.
CREATE OR REPLACE FUNCTION buzzowl_share_on_doc_link() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    d   RECORD;
    m   RECORD;
BEGIN
    IF NEW.entity_type <> 'client' OR buzzowl_sync_applying() THEN
        RETURN NEW;
    END IF;
    SELECT id, org_id, type, source INTO d FROM documents WHERE id = NEW.document_id;
    IF NOT FOUND OR d.source = 'shared' THEN
        RETURN NEW;
    END IF;
    FOR m IN
        SELECT sc.id AS shared_client_id, sc.scope
          FROM shared_client_members scm
          JOIN shared_clients sc ON sc.id = scm.shared_client_id
         WHERE scm.client_id = NEW.entity_id AND scm.left_at IS NULL AND sc.status = 'active'
    LOOP
        IF (m.scope->'doc_types') ? d.type THEN
            INSERT INTO share_outbox (shared_client_id, origin_org_id, kind, ref_id, payload)
            VALUES (m.shared_client_id, d.org_id, 'document', d.id, jsonb_build_object('client_id', NEW.entity_id));
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_share_on_doc_link ON document_links;
CREATE TRIGGER trg_share_on_doc_link AFTER INSERT ON document_links
    FOR EACH ROW EXECUTE FUNCTION buzzowl_share_on_doc_link();

-- documents: content/title/metadata changed on a document linked to a shared client.
CREATE OR REPLACE FUNCTION buzzowl_share_on_doc_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    m RECORD;
BEGIN
    IF buzzowl_sync_applying() OR NEW.source = 'shared' THEN
        RETURN NEW;
    END IF;
    IF NEW.title IS NOT DISTINCT FROM OLD.title AND NEW.content IS NOT DISTINCT FROM OLD.content
       AND NEW.metadata IS NOT DISTINCT FROM OLD.metadata AND NEW.type IS NOT DISTINCT FROM OLD.type THEN
        RETURN NEW;
    END IF;
    FOR m IN
        SELECT DISTINCT sc.id AS shared_client_id, sc.scope, dl.entity_id AS client_id
          FROM document_links dl
          JOIN shared_client_members scm ON scm.client_id = dl.entity_id AND scm.left_at IS NULL
          JOIN shared_clients sc ON sc.id = scm.shared_client_id AND sc.status = 'active'
         WHERE dl.document_id = NEW.id AND dl.entity_type = 'client'
    LOOP
        IF (m.scope->'doc_types') ? NEW.type THEN
            INSERT INTO share_outbox (shared_client_id, origin_org_id, kind, ref_id, payload)
            VALUES (m.shared_client_id, NEW.org_id, 'document', NEW.id, jsonb_build_object('client_id', m.client_id));
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_share_on_doc_update ON documents;
CREATE TRIGGER trg_share_on_doc_update AFTER UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION buzzowl_share_on_doc_update();

-- documents: a shared-scope document was deleted → tell members to drop their copy.
CREATE OR REPLACE FUNCTION buzzowl_share_on_doc_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    m RECORD;
BEGIN
    IF buzzowl_sync_applying() OR OLD.source = 'shared' THEN
        RETURN OLD;
    END IF;
    FOR m IN
        SELECT DISTINCT sc.id AS shared_client_id
          FROM document_links dl
          JOIN shared_client_members scm ON scm.client_id = dl.entity_id AND scm.left_at IS NULL
          JOIN shared_clients sc ON sc.id = scm.shared_client_id AND sc.status = 'active'
         WHERE dl.document_id = OLD.id AND dl.entity_type = 'client'
    LOOP
        INSERT INTO share_outbox (shared_client_id, origin_org_id, kind, ref_id, payload)
        VALUES (m.shared_client_id, OLD.org_id, 'document_delete', OLD.id,
                jsonb_build_object('doc_id', OLD.doc_id));
    END LOOP;
    RETURN OLD;
END $$;
DROP TRIGGER IF EXISTS trg_share_on_doc_delete ON documents;
CREATE TRIGGER trg_share_on_doc_delete BEFORE DELETE ON documents
    FOR EACH ROW EXECUTE FUNCTION buzzowl_share_on_doc_delete();

-- clients: profile fields / monitored sources changed on a shared client.
-- Only the whitelisted keys are ever replicated (see sharing.PROFILE_KEYS); the
-- trigger just detects a change and enqueues — the worker picks the keys.
CREATE OR REPLACE FUNCTION buzzowl_share_on_client_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    m RECORD;
BEGIN
    IF buzzowl_sync_applying() OR NEW.metadata IS NOT DISTINCT FROM OLD.metadata THEN
        RETURN NEW;
    END IF;
    FOR m IN
        SELECT sc.id AS shared_client_id
          FROM shared_client_members scm
          JOIN shared_clients sc ON sc.id = scm.shared_client_id AND sc.status = 'active'
         WHERE scm.client_id = NEW.id AND scm.left_at IS NULL
    LOOP
        INSERT INTO share_outbox (shared_client_id, origin_org_id, kind, ref_id, payload)
        VALUES (m.shared_client_id, NEW.org_id, 'profile', NEW.id, '{}'::jsonb);
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_share_on_client_update ON clients;
CREATE TRIGGER trg_share_on_client_update AFTER UPDATE OF metadata ON clients
    FOR EACH ROW EXECUTE FUNCTION buzzowl_share_on_client_update();
