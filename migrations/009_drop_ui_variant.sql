-- 009: drop the UI A/B theme column now that Carbon is the only look.

ALTER TABLE users DROP COLUMN IF EXISTS ui_variant;
