# Contributing to Buzzowl

Buzzowl is maintained by one person in short, focused phases. Right now, outside
feedback and real users teach us more than code contributions — but bug fixes,
docs corrections, and small, well-scoped PRs are welcome too. See "What would
help most right now" below before opening a big PR.

## Running it locally

### Docker Compose (quickest way to see the whole app)

```bash
git clone https://github.com/Lomikukus/buzzowl
cd buzzowl
cp .env.example .env
openssl rand -hex 32        # -> AGENT_SERVICE_TOKEN=<paste into .env>
# add at least one LLM credential, e.g. OPENROUTER_API_KEY=<key> in .env

docker compose up -d
# open http://localhost:8000/login
```

### Python venv (for working on server code)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt      # or requirements-ci.txt for a lighter,
                                      # no-WhisperX/torch install for server-only work

docker compose up -d db              # Postgres + pgvector only
python server.py
```

The agent runtime (`agent_service_ts/`) is a separate Node/TypeScript service:

```bash
cd agent_service_ts
npm install
npm run build   # or: npm run dev
```

## Running the tests

Fast suite (no external services, what CI runs):

```bash
python -m pytest -q -p no:cacheprovider \
  --ignore=tests/test_search_integration.py \
  --ignore=tests/test_db.py
```

Markers: `slow` (loads WhisperX models) and `ollama` (needs a local Ollama) are
skipped in the fast suite by default — use `-m "not slow and not ollama"` if
you're running the full `tests/` path directly instead of the ignore flags above.

Two files need a live Postgres and are excluded from the command above:
`tests/test_search_integration.py` and `tests/test_db.py`. To run them:

```bash
docker compose up -d db
bash scripts/db_init.sh       # or let the setup_test_db fixture create whisper_test
pytest tests/test_db.py tests/test_search_integration.py -v
```

Some integration tests that call a real LLM are skipped unless you set
`RUN_LLM_INTEGRATION=1`.

## Project conventions

- **Migrations are raw SQL.** `schema.sql` is the v1 baseline; every change
  after that is a new file in `migrations/`, named `NNN_short_name.sql`
  (see `migrations/README.md`). Don't edit `schema.sql` or an already-applied
  migration — add the next numbered file. Migrations run automatically on
  server start and must be idempotent (`IF NOT EXISTS`, `ON CONFLICT DO
  NOTHING`, guarded `UPDATE`s) and additive — no `DROP TABLE`/`DELETE` without
  an explicit plan.
- **The schema is document-oriented.** The `documents` table is universal —
  meetings, research, notes, and signals are all rows with a `type` field and
  JSONB `metadata`. A new content type is a new `type` value, never a new
  table.
- **Graceful degradation everywhere.** LLM offline → skip the summary/step, do
  not fail the request. DB index/write fails on a best-effort path → log and
  continue, don't block the primary operation (e.g. promotion must still
  succeed even if indexing fails).
- **Agent-written documents must cite sources.** Every document an agent
  writes needs a `## Sources` section listing the URLs it drew from. A claim
  with no traceable source is marked `(unconfirmed)`.
- **Multi-tenancy is not optional.** Every table carries `org_id`; new queries
  must be scoped to it — nothing should cross an org boundary.

## Proposing a change

- Keep PRs small and focused on one topic — easier to review, easier to
  revert if something's wrong.
- Add or update tests for new behavior. If you touch schema, add a migration
  file rather than editing `schema.sql`.
- Open an issue first for anything that changes behavior broadly (new
  dependency, new service, schema changes affecting multiple features) so we
  can agree on the approach before you invest the time.
- Commit messages: short, imperative, and specific about the "what" — look at
  `git log --oneline -20` for the house style, e.g. `Telegram notifications
  per person: link your own chat, choose what you get`, `Multi-tenant
  hardening: org-scoped live feeds, one worker pool for all orgs`. A subject
  line plus a colon-delimited detail is the common pattern; keep the subject
  under ~70 characters.

## What would help most right now

- **Install reports on hardware other than Apple Silicon** — does the Docker
  stack come up cleanly on Linux/x86, what broke, what had to change.
- **Bug reports with logs** — `docker compose logs <service>` output plus
  what you expected. See the bug report issue template.
- **Docs corrections** — anything in `README.md`, `ARCHITECTURE.md`, or
  `docs/` that's out of date or unclear.
- **LLM provider compatibility reports** — which OpenAI-compatible endpoints
  (OpenRouter, local Ollama/LM Studio, other providers) you tried and how it
  went.

Thank you for taking the time to try Buzzowl and report back.
