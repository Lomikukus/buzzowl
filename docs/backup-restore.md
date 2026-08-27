# Backup and restore

Everything that matters lives in PostgreSQL. The rest is configuration you can
recreate — except two secrets that make old data unreadable if you lose them.

## What to back up

| What | Where | Why |
|---|---|---|
| The database | Docker volume `buzzowl_pgdata` (service `db`) | clients, contacts, documents, deals, agent runs, embeddings |
| `.env` | repo root | `BUZZOWL_SECRET_KEY` decrypts per-org LLM keys, `AGENT_SERVICE_TOKEN` |
| `config.yaml` (+ `config.local.yaml`) | repo root | providers, weights, schedules |
| `data/` | repo root, mounted into the server | uploads and the federation store (device keys) |

**Losing `BUZZOWL_SECRET_KEY` means every per-org LLM key stored in the database
stays encrypted forever** — they have to be entered again. Keep it in a password
manager, not only on the server.

> **Set `BUZZOWL_SECRET_KEY` explicitly on the first install** (`openssl rand -hex 32`).
> When it is empty, the encryption key is derived from `AGENT_SERVICE_TOKEN`
> instead — so rotating that token, which is otherwise routine hygiene, orphans
> every stored per-org LLM key and the Matrix federation access token. Nothing
> recovers them: Settings shows the key as *needs reconnection* and an admin has
> to enter it again. Rotating `AGENT_SERVICE_TOKEN` is safe **only** once
> `BUZZOWL_SECRET_KEY` is set to its own separate value.

Not worth backing up: Docker images, the SearXNG container, `camofox`, anything
under `node_modules/`, and the Postgres volume of a test instance.

## Nightly dump

```bash
# from the repo directory
docker compose exec -T db pg_dump -U whisper -Fc whisper > "backup-$(date +%F).dump"
```

`-Fc` is the custom format: compressed, and `pg_restore` can read it selectively.
A schema-only dump for comparing versions:

```bash
docker compose exec -T db pg_dump -U whisper --schema-only whisper > schema-$(date +%F).sql
```

Cron example (2:30 every night, keeps 14 days, on the machine running Compose):

```cron
30 2 * * * cd /srv/buzzowl && docker compose exec -T db pg_dump -U whisper -Fc whisper > /var/backups/buzzowl/$(date +\%F).dump && find /var/backups/buzzowl -name '*.dump' -mtime +14 -delete
```

Copy the dumps off the machine (rsync, restic, S3 — whatever you already trust)
and back up `.env`, `config.yaml` and `data/` in the same run:

```bash
tar czf "config-$(date +%F).tgz" .env config.yaml config.local.yaml data/
```

## Restore

Into an empty instance (the normal disaster case):

```bash
docker compose up -d db                       # only the database
docker compose exec -T db psql -U whisper -d postgres -c "DROP DATABASE IF EXISTS whisper;"
docker compose exec -T db psql -U whisper -d postgres -c "CREATE DATABASE whisper;"
docker compose exec -T db pg_restore -U whisper -d whisper --no-owner < backup-2026-08-18.dump
docker compose up -d                          # the rest of the stack
```

The server applies any newer migrations on start, so a dump from an older version
comes back up on a newer build — see [upgrading.md](upgrading.md).

## Verify a dump actually restores

Do this once after you set up backups, and again whenever you change the setup.
It restores into a scratch database and counts rows — the real instance is not
touched:

```bash
docker compose exec -T db psql -U whisper -d postgres -c "DROP DATABASE IF EXISTS restore_check;"
docker compose exec -T db psql -U whisper -d postgres -c "CREATE DATABASE restore_check;"
docker compose exec -T db pg_restore -U whisper -d restore_check --no-owner < backup-2026-08-18.dump

docker compose exec -T db psql -U whisper -d restore_check -c "
  SELECT 'orgs' t, count(*) FROM orgs
  UNION ALL SELECT 'users',     count(*) FROM users
  UNION ALL SELECT 'clients',   count(*) FROM clients
  UNION ALL SELECT 'contacts',  count(*) FROM contacts
  UNION ALL SELECT 'documents', count(*) FROM documents
  UNION ALL SELECT 'deals',     count(*) FROM deals
  UNION ALL SELECT 'embeddings not null',
         count(*) FROM documents WHERE embedding IS NOT NULL;"

docker compose exec -T db psql -U whisper -d postgres -c "DROP DATABASE restore_check;"
```

If `embeddings not null` is zero but `documents` is not, the dump is fine but the
`vector` extension was missing during restore — check that the image really is
`pgvector/pgvector:pg16`.

## Disk usage and retention

Almost all growth is the `buzzowl_pgdata` volume, and on a busy instance most of
it is *telemetry*, not knowledge. Two tables are responsible:

| Table | What it holds | Why it grows |
|---|---|---|
| `agent_runs` | one row per agent run; `tool_calls` is a JSONB blob of every tool call it made | research runs store fetched page content in there |
| `prompt_log` | one row per chat/search prompt (max 4000 chars) | written on every message, read only by the evaluation pages |

A nightly job (`retention.py`, 03:20 by default) prunes them. It never touches
knowledge — `documents`, `clients`, `contacts`, `deals` and meetings are kept
forever regardless of age. Defaults in `config.yaml`:

```yaml
retention:
  enabled: true
  cron: "20 3 * * *"
  tool_call_payload_days: 14    # strip agent_runs.tool_calls payloads (row stays)
  agent_runs_days: 90           # delete the agent_runs row
  prompt_log_days: 180          # evaluation looks back up to 365 days
  batch_size: 2000
```

`agent_runs` is pruned in two stages. After 14 days the heavy per-call payload is
stripped but the row survives, so run status, timings and "which documents did
this run write" keep working — the array keeps one emptied entry per call, so
call counts on the agents dashboard stay correct. After 90 days the row goes;
documents that referenced it keep their content and simply lose the
`agent_run_id` pointer (the foreign key is `ON DELETE SET NULL`).

Turn it off with `retention.enabled: false`, or widen any window — set
`prompt_log_days: 365` to keep the full evaluation range. Each run logs one line:

```bash
docker compose logs server | grep retention
```

Where the space actually went, largest tables first:

```bash
docker compose exec -T db psql -U whisper -d whisper -c "
  SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
    FROM pg_catalog.pg_statio_user_tables
   ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
```

Deleted rows free space for reuse but do not shrink the files — that needs
`VACUUM FULL` (takes an exclusive lock; run it once, off hours, not on a
schedule). Docker images and build caches are usually the *other* half of a full
disk: `docker system prune` before you go looking at the database.

## Moving to another machine

1. Dump the database and tar the config files (above).
2. Clone the repo on the new host, copy `.env`, `config.yaml`, `data/` in.
3. `docker compose up -d db`, restore the dump, then `docker compose up -d`.
4. Build the browser image once: `./scripts/build-browser.sh`.
5. Log in and check Settings → LLM providers: the keys come from `.env`, per-org
   keys from the database (they need the same `BUZZOWL_SECRET_KEY`).
