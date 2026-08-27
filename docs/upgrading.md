# Upgrading

Schema changes apply themselves. The normal upgrade is three commands.

```bash
cd /path/to/buzzowl
docker compose exec -T db pg_dump -U whisper -Fc whisper > "pre-upgrade-$(date +%F).dump"   # 30 s, do it
git pull
docker compose up -d --build
docker compose logs -f server        # watch the migrations run, then Ctrl+C
```

## What happens on start

`db.py` reconciles the schema before the API accepts traffic:

1. **Empty database** → `schema.sql` is applied in full (it stamps schema version 1).
2. **Database from before the migration runner** → a one-time baseline reconcile,
   then it is stamped as version 1.
3. **Every start** → each `migrations/NNN_*.sql` newer than the recorded version is
   applied, *one transaction per file*, and the version is recorded.

A failing migration rolls back that file and the server stops with the error in
the log — the database is never left half-migrated.

## Which version am I on?

```bash
docker compose exec -T db psql -U whisper -d whisper -c \
  "SELECT max(version) AS schema_version FROM schema_version;"
ls migrations/          # the highest NNN_ prefix is what the code expects
git log --oneline -1    # the build you are running
```

## After the upgrade

```bash
curl -fsS http://localhost:8000/api/health           # server up
docker compose ps                                    # every container "running"
docker compose logs --since=5m server | grep -i "error\|migration"
```

Then log in and check one client page, one agent run, and Settings → LLM
providers (a new release may add a role you have not configured).

## Rolling back

Migrations are forward-only — there are no down-scripts. To go back:

```bash
git checkout <previous-tag-or-commit>
docker compose up -d --build
# only if the new version had already migrated the schema:
docker compose exec -T db psql -U whisper -d postgres -c "DROP DATABASE whisper;"
docker compose exec -T db psql -U whisper -d postgres -c "CREATE DATABASE whisper;"
docker compose exec -T db pg_restore -U whisper -d whisper --no-owner < pre-upgrade-2026-08-18.dump
```

That is the reason for the dump in the first step. See
[backup-restore.md](backup-restore.md).

## Staying on a release instead of `main`

```bash
git fetch --tags
git checkout v0.1.0
docker compose up -d --build
```

Read the release notes before moving to the next tag: anything that needs manual
action (a new required env var, a re-embedding run) is called out there.

## Before you rotate `AGENT_SERVICE_TOKEN`

Check that `.env` has its own `BUZZOWL_SECRET_KEY`:

```bash
grep -c '^BUZZOWL_SECRET_KEY=.\+' .env      # 1 = safe to rotate, 0 = read on
```

With no `BUZZOWL_SECRET_KEY`, per-org LLM keys and the Matrix federation access
token are encrypted with a key derived from `AGENT_SERVICE_TOKEN`. Rotating the
token then orphans all of them permanently — Settings shows *needs reconnection*
and each org admin has to enter the key again. Fix it before rotating: set
`BUZZOWL_SECRET_KEY` to the **current** `AGENT_SERVICE_TOKEN` value, restart, and
only then generate a new token.

## Upgrades that need more than a rebuild

- **A new required environment variable** — the server refuses to start and says
  which one. Add it to `.env`, then `docker compose up -d`.
- **A changed embedding model or dimension** — existing vectors are in the old
  space and search quality degrades silently. Re-embed with
  `scripts/backfill_embeddings.py` after changing `embed_model`/`embed_dim`.
- **The agent service** — `docker compose up -d --build agent-pi` rebuilds it;
  the TypeScript image is not versioned separately.
- **The browser image** — `./scripts/build-browser.sh` is a one-time build. Rerun
  it only when the release notes say the browser version changed.
