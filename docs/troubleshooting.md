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
| `docker compose up` fails on `camofox`: image not found | the browser image is neither pulled nor built by Compose | `./scripts/build-browser.sh`, or start without it: `docker compose up -d db searxng server agent-pi` |
| Server log: *"agent_service_token not set — internal APIs disabled (401)"* | `AGENT_SERVICE_TOKEN` missing in `.env` | `openssl rand -hex 32` → `.env`, same value everywhere, `docker compose up -d` |
| `agent-pi` log: *"AGENT_SERVICE_TOKEN not set — API disabled (401)"*, every agent run 401s | same missing `.env` value — `agent-pi` is fail-closed too | `openssl rand -hex 32` → `.env`, `docker compose up -d`. For local dev only, `ALLOW_INSECURE_INTERNAL=1` serves it unauthenticated |
| Login page asks for a registration key you do not have | no admin exists yet | `docker compose logs server \| grep "FIRST RUN"` — the key is printed there. Or set `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` and restart |
| Port 8000 already in use | something else is on it | stop it, or map another host port in `docker-compose.yml` (`"8010:8000"`) |
| Everything starts, but nothing an agent does works | no usable LLM credential | `curl -s localhost:8000/api/llm/status`, then Settings → LLM providers |
| Chat/summary works, agents fail | `agent-pi` cannot reach a provider or the DB | `docker compose logs --tail=100 agent-pi` |
| Agent runs stay `queued` forever | the agent container is down or its token differs | `docker compose ps agent-pi`; the token in `.env` must match on both containers |
| Research finds nothing, every page fetch fails | SearXNG or the browser stack is down | `docker compose logs searxng`; page fetching degrades to plain HTTP when `camofox` is missing — JS-heavy sites then return little |
| Search returns nothing sensible | embeddings missing or in the wrong vector space | see *Embeddings* below |
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

## Embeddings

The dimension is fixed at boot (`embed_dim`, default 768). If you change the
embedding model, old vectors stay in the old space and hybrid search quietly gets
worse. The server warns on a mismatch at startup.

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
