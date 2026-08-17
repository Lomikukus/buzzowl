-- 003: supervised outreach.
-- Per-user settings (display name / reply-to / signature for outreach identity;
-- org SMTP stays the single transport).
ALTER TABLE users ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Outreach state machine lives in documents.metadata (type = 'outreach').
-- These expression indexes keep the approval queue and the send worker's
-- "approved → send" scan cheap.
CREATE INDEX IF NOT EXISTS idx_documents_outreach_state
    ON documents (org_id, (metadata->>'state'))
    WHERE type = 'outreach';
CREATE INDEX IF NOT EXISTS idx_documents_outreach_message_id
    ON documents ((metadata->>'message_id'))
    WHERE type = 'outreach' AND metadata ? 'message_id';
