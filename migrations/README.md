# migrations/

Ordered SQL migrations for the Buzzowl schema.

## How it works

- **`schema.sql` (repo root) IS version 1.** A fresh database gets `schema.sql`
  applied once (by `docker-entrypoint.sh`, `scripts/db_init.sh`, or
  `db.init_db()`'s fresh-install path) and is stamped `version = 1` in the
  `schema_version` table.
- **Every change after v1 is a file in this directory**, applied automatically
  by `db.init_db()` on server start, in order, inside one transaction per file.
  After a file is applied, its version is stamped into `schema_version`, so it
  never runs again.
- Databases that predate the version system (no `schema_version` table) get a
  one-time "baseline reconcile" on first boot: the historical runtime DDL that
  used to live in `db.py` is replayed (all `IF NOT EXISTS`, safe), then the DB
  is stamped `version = 1` and migrations proceed from there.

## Conventions

1. **File name: `NNN_short_name.sql`** — a zero-padded integer version prefix,
   an underscore, a snake_case description, `.sql`. Examples:
   `002_add_products_sku.sql`, `003_user_tasks_reminder_index.sql`.
   Versions start at `002` (`001`/v1 is `schema.sql` itself). Files that do
   not match the pattern (like this README) are ignored by the runner.
2. **Versions are unique and applied in numeric order.** Never reuse or
   renumber a version that may already be applied somewhere (dev, prod,
   whisper_test). The runner refuses to start on duplicate version prefixes.
3. **Write idempotent SQL** — `CREATE TABLE IF NOT EXISTS`,
   `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
   `ON CONFLICT DO NOTHING`, guarded `UPDATE`s. The version stamp normally
   prevents re-runs, but idempotency makes recovery from a half-failed boot or
   a manually-applied file painless.
4. **One concern per file.** Small, reviewable, revertable steps.
5. **No data-destructive statements** (`DROP TABLE`, `DELETE`) without an
   explicit plan; prefer additive changes (document-oriented schema + JSONB
   metadata means most features need no migration at all).
6. **Also fold the end state into `schema.sql`?** No — `schema.sql` stays the
   v1 snapshot. A fresh DB gets v1 + all migrations, which by construction
   equals every legacy DB's state. If the migration chain ever grows unwieldy,
   a deliberate "squash to v2 baseline" can be done as its own task.

## Graceful degradation

The runner in `db.init_db()` is best-effort like the rest of the DB layer:
DB down → the server boots without a pool as before; a failing migration is
logged loudly and the server keeps running on the existing schema.
