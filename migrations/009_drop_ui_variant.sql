-- 009: drop the UI A/B theme column now that Carbon is the only look.
-- One-way: once this applies, rolling the APP back to a pre-WP3 build
-- breaks auth queries (old code selects the now-dropped column). Accepted
-- cost — schema.sql still creates ui_variant for a fresh install, so this
-- migration always has a column to drop either way.

ALTER TABLE users DROP COLUMN IF EXISTS ui_variant;
