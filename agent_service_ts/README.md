# Agent Service — TypeScript + Pi (Candidate A)

Phase 12.5 framework evaluation candidate. Runs in Docker — do not install packages on the host.

## HTTP Contract

All requests require `Authorization: Bearer <agent_service_token>` (from `config.yaml`).

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
  "task": "research Bosch — focus on recent executive changes",
  "org_id": 1,
  "subject": "Bosch",
  "subject_type": "company | person | topic",
  "brain": "ollama | openrouter | claude",
  "model": "qwen3.5",
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
  "subject": "Bosch",
  "output": { "findings_saved": 12, "signals_extracted": 3 },
  "error": null
}
```

## Build & Run

```bash
# From repo root:
docker compose --profile bench up agent-pi

# Check health:
curl http://localhost:8001/health
```

## Environment (injected by docker-compose.yml)

- `DATABASE_URL` — PostgreSQL connection string
- `OLLAMA_URL` — `http://host.docker.internal:11434` (reaches macOS host Ollama)
- `OPENROUTER_API_KEY` — injected from `HERMES` in `.env` (OpenRouter key for benchmark runs)
- `DEFAULT_BRAIN` — `openrouter` (primary) or `ollama` (fallback)
- `DEFAULT_MODEL` — `minimax/minimax-m2.7` (primary); override per-run via `POST /runs` body
- `AGENT_SERVICE_PORT` — 8001

## Models

**Primary (OpenRouter):** `minimax/minimax-m2.7` — set `brain: openrouter` in `POST /runs`.
API key comes from `HERMES` in `.env`, passed into the container as `OPENROUTER_API_KEY`.

**Fallback (Ollama):** set `brain: ollama`, `model: qwen3.5` in `POST /runs`.
Reaches the macOS host Ollama via `OLLAMA_URL`.

## Implementation notes

- `@earendil-works/pi-agent-core` + `@earendil-works/pi-ai` — Pi agent engine
- `pi-ollama` extension required for reliable tool_call streaming with local Ollama
- OpenRouter: native first-class support — pass `base_url: "https://openrouter.ai/api/v1"` + `OPENROUTER_API_KEY`
- Fastify wrapper: ~80 lines mapping `POST /runs` → Pi agent run → callback
- Tool registry: TypeScript async functions calling PostgreSQL via `pg` npm package
- Vault writes: Node.js `fs` + `gray-matter` to `/vault` (mounted from `./north-info`)
