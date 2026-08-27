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

## Moving to another machine

1. Dump the database and tar the config files (above).
2. Clone the repo on the new host, copy `.env`, `config.yaml`, `data/` in.
3. `docker compose up -d db`, restore the dump, then `docker compose up -d`.
4. The browser image is rebuilt by that `up` if the new host does not have it —
   `./scripts/build-browser.sh` beforehand keeps the restart short.
5. Log in and check Settings → LLM providers: the keys come from `.env`, per-org
   keys from the database (they need the same `BUZZOWL_SECRET_KEY`).
