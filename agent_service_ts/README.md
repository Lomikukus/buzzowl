# Agent service (TypeScript, Pi runtime)

The agent runtime: it receives a task, plans, calls tools (search, page fetch,
knowledge read/write), and writes source-linked documents back into PostgreSQL.
Built on [`@earendil-works/pi-ai`](https://www.npmjs.com/package/@earendil-works/pi-ai);
runs as the `agent-pi` container — do not install its packages on the host.

## HTTP Contract

All requests except `GET /health` require `Authorization: Bearer <AGENT_SERVICE_TOKEN>`.
Auth is **fail-closed**: with no `AGENT_SERVICE_TOKEN` in the environment every
endpoint answers `401` — same posture as the main server's internal APIs. Set
`ALLOW_INSECURE_INTERNAL=1` to serve unauthenticated for local dev only.

```
POST   /runs              → enqueue a run → {run_id, status: "queued"}
GET    /runs/{id}         → poll status + output
POST   /runs/{id}/cancel  → cancel a running job
GET    /runs              → list recent runs (?org_id=)
GET    /health            → liveness check
```

### POST /runs — request body

```json
{
  "agent_type": "research | osint | enrichment | org | system",
  "task": "research Acme Corp — focus on recent executive changes",
  "org_id": 1,
  "subject": "Acme Corp",
  "subject_type": "company | person | topic",
  "provider_name": "openrouter",
  "model": "deepseek/deepseek-v4-flash",
  "max_queries": 20,
  "callback_url": "http://host.docker.internal:8000/api/agents/callback"
}
```

### Callback (POST to callback_url on completion)

```json
{
  "run_id": "abc123",
  "status": "done | failed",
  "agent_type": "research",
  "subject": "Acme Corp",
  "output": { "findings_saved": 12, "signals_extracted": 3 },
  "error": null
}
```

## Build & run

```bash
# From the repo root — part of the default stack:
docker compose up -d --build agent-pi

curl http://localhost:8001/health
docker compose logs -f agent-pi
```

Local TypeScript work: `npm install && npm run build` inside this directory; the
container is the only supported runtime.

## Environment (injected by docker-compose.yml)

- `DATABASE_URL` — PostgreSQL connection string
- `OLLAMA_URL` — `http://host.docker.internal:11434` (reaches macOS host Ollama)
- `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — provider keys from `.env`
- `DEFAULT_BRAIN` — `openrouter` unless overridden
- `DEFAULT_MODEL` — `deepseek/deepseek-v4-flash`; override per run in the `POST /runs` body
- `SEARXNG_URL`, `CAMOFOX_URL`, `BROWSER_SERVICE_URL` — tool backends (page fetch degrades to plain HTTP when the browser stack is down)
- `AGENT_SERVICE_TOKEN` — shared secret with the main server (required; without it every route returns 401)
- `ALLOW_INSECURE_INTERNAL` — `1` disables the token check (local dev backdoor, never in production)
- `BUZZOWL_SECRET_KEY` — decrypts per-org LLM keys stored in the database
- `AGENT_SERVICE_PORT` — 8001, `AGENT_MAX_CONCURRENT` — parallel runs (default 2)

## Models

Providers are resolved from the `llm:` block in the mounted `config.yaml` — the
server never sends API keys over HTTP, it sends `{provider_name, model}` and the
container resolves the key locally (env var or, for a hosted org, the encrypted
per-org key from the database). Any OpenAI-compatible endpoint works, including a
local Ollama/LM Studio via `base_url`.

## Implementation notes

- `@earendil-works/pi-ai` — the agent engine (planning loop, tool calls, streaming)
- Fastify wrapper mapping `POST /runs` → agent run → callback to the main server
- Tool registry: TypeScript async functions talking to PostgreSQL via `pg`
- Everything the agent writes goes into the `documents` table with its `agent_run_id`
  and a `## Sources` section — there is no file-based vault
