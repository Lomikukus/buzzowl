# Troubleshooting

Start here:

```bash
docker compose ps                       # which containers are actually running
docker compose logs --tail=100 server   # the server says what it refused to do
curl -fsS http://localhost:8000/api/health
```

## Symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| The first `docker compose up -d` sits on `camofox` for minutes | Compose is building the browser image from its upstream repo (~2.5 GB, first start only) | let it finish; next time build it up front with `./scripts/build-browser.sh`, or skip it: `docker compose up -d db searxng server agent-pi` |
| The `camofox` build fails on `curl … camoufox-135.0.1-beta.24-lin.<arch>.zip` | the build arch does not match the host | x86_64 hosts need **both** `CAMOFOX_ARCH=x86_64` and `CAMOFOX_BUILD_ARCH=x86_64` in `.env`, then `docker compose build camofox` |
| The `camofox` build fails cloning `github.com/jo-inc/camofox-browser.git` | no network/proxy access to GitHub from the Docker builder | build it on a connected host, or start without it: `docker compose up -d db searxng server agent-pi` |
| Server log: *"agent_service_token not set — internal APIs disabled (401)"* | `AGENT_SERVICE_TOKEN` missing in `.env` | `./scripts/init-env.sh`, `docker compose up -d` |
| `agent-pi` log: *"agent-pi is fail-closed: all requests will 401 until AGENT_SERVICE_TOKEN is set…"*, every agent run 401s | same missing `.env` value — `agent-pi` is fail-closed too | `./scripts/init-env.sh`, `docker compose up -d`. For local dev only, `ALLOW_INSECURE_INTERNAL=1` serves it unauthenticated |
| Login page asks for a registration key you do not have | no admin exists yet | `docker compose logs server \| grep -B2 -A3 "FIRST RUN"` — the key is printed there (plain `tail` can miss it once request logging pushes the banner out of range). Or set `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` and restart |
| Port 8000 already in use | something else is on it | stop it, or map another host port in `docker-compose.yml` (`"8010:8000"`) |
| Everything starts, but nothing an agent does works | no usable LLM credential | `curl -s localhost:8000/api/llm/status`, then Settings → LLM providers |
| Chat/summary works, agents fail | `agent-pi` cannot reach a provider or the DB | `docker compose logs --tail=100 agent-pi` |
| Agent runs stay `queued` forever | the agent container is down or its token differs | `docker compose ps agent-pi`; the token in `.env` must match on both containers |
| Research finds nothing, every page fetch fails | SearXNG or the browser stack is down | `docker compose logs searxng`; page fetching degrades to plain HTTP when `camofox` is missing — JS-heavy sites then return little |
| Search returns nothing sensible | embeddings missing or in the wrong vector space | see *Embeddings* below |
| Server log: *"Embeddings not configured (no API key) — vector search disabled"* | expected on a fresh install with no embeddings key | nothing is broken: search still works (full-text only). To enable vector search set `OPENROUTER_API_KEY` (or `EMBED_API_KEY`) in `.env` — see *Embeddings* below |
| Server log: *"Embedding failed (openai @ …): 401"* | a key **is** set but the provider rejected it | check `OPENROUTER_API_KEY`/`EMBED_API_KEY` in `.env`; the warning repeats at most once every 15 min while it keeps failing |
| Telegram bot does not react to `/start <code>` | bot token missing, or the link code expired (15 min) | `curl -s localhost:8000/api/notifications/status`; generate a fresh link in Settings → Notifications |
| Containers get OOM-killed, the box swaps | less than ~8 GB RAM free | run without `camofox`/`browser-service`, or raise Docker's memory limit |
| `no space left on device` | old images and build cache | `docker system prune -a` (this does **not** touch the `buzzowl_pgdata` volume) |

## The server will not start

```bash
docker compose logs --tail=200 server
```

Read the last block before the exit — the server prints what it is missing.
Common ones:

- **Database not reachable** — `docker compose ps db` must show `healthy`. On a
  fresh volume Postgres needs a few seconds; the server retries.
- **A migration failed** — the file name and the SQL error are in the log. The
  transaction rolled back, so the database is consistent; fix the cause (usually
  a hand-edited schema) and restart. See [upgrading.md](upgrading.md).
- **Port conflict** — `Bind for 0.0.0.0:8000 failed`.

## Agents do nothing

1. Is the container up? `docker compose ps agent-pi` and
   `curl -fsS http://127.0.0.1:8001/health`.
2. Does it have work? The Agents page shows runs; in SQL:

   ```bash
   docker compose exec -T db psql -U whisper -d whisper -c \
     "SELECT id, agent_type, status, trigger_type, created_at
        FROM agent_runs ORDER BY id DESC LIMIT 10;"
   ```

3. Failed runs carry the error: add `, error` to that query, or
   `docker compose logs --tail=200 agent-pi`.
4. Nothing scheduled at all? Heartbeats only run for clients that qualify — mark
   a client as focus (★) or trigger a run by hand from the client page.
5. Autonomy level 0 means agents observe but never act on their own — Settings →
   Agent Autonomy.

## Brief shows "partial", with missing parts listed

The account brief was written before every intake part (osint, research,
jobs, news) finished: the 25-minute collection deadline (`intake_deadline_min`)
or the 90-minute absolute cap (`intake_absolute_cap_min`) passed first. This
is not an error. It is regenerated automatically, once, when the missing
part(s) have landed and nothing is still open; a part that only fails closes
the brief out without a regeneration. `GET /api/clients/{name}/intake` shows
which part is still open and why.

To skip the wait, regenerate now:

```bash
curl -s -X POST http://localhost:8000/api/clients/Acme/brief \
  -H "Authorization: Bearer $TOKEN"
```

This manual endpoint always runs immediately and closes an open intake, so
the automatic refresh will not overwrite what you generated by hand.

## No careers page found

The jobs scan tried, in order, the client's site playbook, `metadata.careers_url`,
a homepage link crawl, a sitemap probe, and SearXNG as a last resort, and none
of those returned a page on the client's own domain or a known ATS host. Set
the URL by hand and re-scan:

```bash
curl -s -X PATCH http://localhost:8000/api/internal/clients/Acme \
  -H "Authorization: Bearer $AGENT_SERVICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org_id": 1, "patch": {"careers_url": "https://acme.example/careers"}}'

curl -s -X POST http://localhost:8000/api/clients/Acme/jobs/scan \
  -H "Authorization: Bearer $TOKEN"
```

## LLM problems

```bash
curl -s http://localhost:8000/api/llm/status | python3 -m json.tool
```

Each role shows its provider and whether the endpoint answered.

- **401/403 from the provider** — wrong or expired key. Keys come from `.env`
  (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`); per-org keys set
  in the UI live encrypted in the database and need `BUZZOWL_SECRET_KEY`.
- **A role points at a provider you do not have** — edit the `llm.roles` block in
  `config.yaml` (or keep personal choices in an untracked `config.local.yaml`).
- **Local Ollama/LM Studio from inside Docker** — use
  `http://host.docker.internal:11434/v1` as `base_url`, not `localhost`, and set a
  dummy `api_key: local`.
- **Timeouts on big models** — a slow model can exceed the agent watchdog; use a
  faster model for the `research`/`triage` roles.

### 401 "Internal APIs disabled" / "agent_service_token is not configured"

This is not a provider key problem — it is the shared secret between `server`
and `agent-pi`, checked before either one ever reaches an LLM provider.

- **Why it happens.** Both containers are fail-closed: with no
  `AGENT_SERVICE_TOKEN`, every internal endpoint (and, on `agent-pi`, every
  request except `/health`) answers 401 rather than running unauthenticated.
  A mismatch between the two containers' values 401s the same way as a
  missing one — `server` calls `agent-pi` with its own token, and `agent-pi`
  rejects anything that is not an exact match.
- **Precedence.** The token can also live in `config.yaml`'s top-level
  `agent_service_token`, on both `server` and `agent-pi` — if set there, it
  wins over `AGENT_SERVICE_TOKEN` in `.env`/the environment, which is only the
  fallback. The untracked `config.local.yaml` overlay wins over `config.yaml`
  the same way it does for the rest of the config — but only if it is
  actually mounted: a token set only in `config.local.yaml` on the server side
  has no effect on `agent-pi` unless that file is *also* bind-mounted into the
  `agent-pi` service — uncomment this line in `docker-compose.override.yml` or
  directly in `docker-compose.yml` (both services have the commented-out mount
  ready), and do it for BOTH services.
- **Fix.** Set `AGENT_SERVICE_TOKEN` in `.env` (`./scripts/init-env.sh` does
  this for you) — the *same* value is used by both `server` and `agent-pi`,
  since both read it from the one `.env`. Then apply it with
  `docker compose up -d`. **`docker compose restart` does NOT re-read `.env`**
  — the containers keep running with whatever value they already loaded, so a
  restart after editing `.env` looks like nothing changed.
- **`ALLOW_INSECURE_INTERNAL=1`** disables the check on both `server` and
  `agent-pi` — every internal API and agent-pi endpoint then serves
  unauthenticated requests. Local dev only, never on an instance anyone else
  can reach; it is not a substitute for setting the token. Careful: this is a
  plain environment variable, so a stray `export ALLOW_INSECURE_INTERNAL=1`
  left in your host shell also takes effect — Docker Compose lets a real
  shell variable override the same name in `.env`.
- **Rotating the token? Set `BUZZOWL_SECRET_KEY` first.** Per-org LLM keys
  (Settings › LLM) are encrypted at rest, and the encryption key falls back to
  `AGENT_SERVICE_TOKEN` whenever `BUZZOWL_SECRET_KEY` is unset. Rotating the
  token without an explicit `BUZZOWL_SECRET_KEY` orphans every stored key —
  they stop decrypting and have to be re-entered by hand, they cannot be
  recovered. Set `BUZZOWL_SECRET_KEY` (`openssl rand -hex 32`) once, on first
  install, before storing any org key, and never change it.

## Embeddings

Embeddings are optional. Without them search runs full-text only — everything
still works, results are just less fuzzy.

**No key set.** The server says so once at startup and then stays quiet:

```
Embeddings not configured (no API key for the 'openai' backend at
https://openrouter.ai/api) — vector search disabled, full-text search still
works; set OPENROUTER_API_KEY (or EMBED_API_KEY) to enable.
```

No request is made to the provider in this state, so `/api/health` reports
`"embeddings": false` without any error in the log, and `embed_stats.skipped`
counts the calls that were never attempted. This is the expected first-run
state, not a fault. Local backends (`embed_backend: ollama`, or an
OpenAI-compatible server on `localhost`/`host.docker.internal`/a compose
service name) need no key, so they are never treated as unconfigured and are
always called.

**Key set but rejected** (`401`/`403`) **, or the provider unreachable.** That
is a real failure and gets a warning:

```
Embedding failed (openai @ https://openrouter.ai/api): 401 … — vector search
degraded to full-text only; further identical warnings suppressed for 15 min
```

The warning is logged in full the first time and then at most once every 15
minutes while the same error persists (suppressed repeats go to `DEBUG`), so a
bad key cannot flood the log via the once-a-minute health probe. A *different*
error warns again immediately. `embed_stats.fail` and `last_error` in
`/api/health` keep counting every attempt regardless of what is logged.

**Dimension mismatch.** The dimension is fixed at boot (`embed_dim`, default
768). If you change the embedding model, old vectors stay in the old space and
hybrid search quietly gets worse. The server warns on a mismatch at startup.

```bash
docker compose exec -T db psql -U whisper -d whisper -c \
  "SELECT count(*) total, count(embedding) with_vector FROM documents;"
python scripts/backfill_embeddings.py       # re-embed after a model change
```

## Database access

```bash
docker compose exec db psql -U whisper -d whisper       # interactive SQL
docker compose --profile debug up -d pgweb              # browser UI (no auth — local only)
```

The Postgres port is deliberately not published to the host. For host development
uncomment the `ports` block of the `db` service.

## Outreach mail

Nothing sends unless outreach is enabled for the org *and* an item is approved —
that is by design. See [outreach.md](outreach.md) for the state machine and the
guardrails (daily cap, quiet hours, kill switch).

## Still stuck

Open an issue with: what you ran, what you expected, the last 50 log lines of the
failing container, your OS/architecture and RAM, and how you run it (Compose or
`python server.py`). The install-problem issue form asks for exactly this.
