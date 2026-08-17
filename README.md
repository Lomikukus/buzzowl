# Buzzowl

An agentic research and knowledge platform for sales teams. Buzzowl turns
meetings into structured, searchable client knowledge, keeps that knowledge fresh
through autonomous research agents, and tells each rep who to contact next and why.

The core loop:

1. **Capture** — meeting audio becomes a diarized transcript and an AI summary;
   clients, contacts, and commitments are extracted into a knowledge base.
2. **Enrich** — autonomous agents research each client (web search, news and OSINT
   sources, company signals) and write source-linked findings back into the same store.
3. **Act** — the platform matches products to client pain points, ranks a
   next-best-action queue ("who to contact today and why"), and drafts outreach
   emails grounded in the stored knowledge.

Everything lives in PostgreSQL as documents with hybrid (vector + full-text) search
on top, is rendered by the web UI, and is exposed via MCP so external agents can
read and write the same knowledge base.

## Features

- **Meeting capture** — live transcription plus a high-quality post-pass with
  speaker diarization; sessions become reviewable knowledge documents
- **Autonomous client research** — scheduled agent runs (heartbeats) per client;
  every agent-written document carries its sources
- **Signal monitoring** — news and press pages are watched for changes; relevant
  signals can escalate into deeper research automatically
- **Product–client matching** — two-stage research + reasoning that connects your
  product catalog to client pain points
- **Next-best-action queue** — a daily ranked list per rep, with deterministic
  scoring and LLM-written reasoning on top
- **Outreach drafts and rep digests** — email drafts and per-rep summaries for
  admin review (or automatic sending via SMTP)
- **Hybrid search** — pgvector + full-text search across sessions, clients,
  contacts, and research
- **MCP server** — the knowledge base is usable as a tool server by any
  MCP-compatible agent
- **Multi-tenant** — organizations, users, roles, and invite keys built in
- **CRM essentials** — deals with stage history, a per-client activity
  timeline, recurring reminders, a pipeline board, CSV import/export
- **Shared clients** — two reps (in one deployment or on separate installs)
  share a client and research it together; research, findings, signals and the
  profile sync, contacts/notes/mails/deals stay private. Across installs the
  sync runs end-to-end encrypted over Matrix — see `docs/federation.md`
- **Hosted plans** — light (bring your own LLM key, encrypted at rest) or
  premium (platform providers, metered against a monthly budget)

## Quickstart (Docker)

Requirements: Docker with Compose v2.

```bash
git clone <repo-url> buzzowl
cd buzzowl

cp .env.example .env
# Generate the internal service token and put it in .env:
openssl rand -hex 32        # -> AGENT_SERVICE_TOKEN=<paste>
# Add at least one LLM credential (see "Bring your own LLM" below), e.g.:
# OPENROUTE=<your OpenRouter API key>

docker compose up -d
# open http://localhost:8000/login
```

First-run bootstrap (empty database) works one of two ways:

- **Env-based admin** — set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` before
  the first start; the server creates that admin account on boot.
- **Registration key** — without admin env vars, the server logs a one-time
  registration key on first start (`docker compose logs server`). Enter it on the
  login page to create the first admin account.

After logging in, invite further users from the Settings panel (admins can create
member/admin accounts and invite keys).

## Bring your own LLM

All chat/completion calls go through a single provider layer configured by the
`llm:` block in `config.yaml`. Two adapter kinds cover every provider:

- `openai-compat` — any OpenAI-compatible endpoint: OpenRouter, OpenAI, Ollama,
  LM Studio, vLLM, LiteLLM, ...
- `anthropic` — the Anthropic API

```yaml
llm:
  providers:
    openrouter:
      kind: openai-compat
      base_url: https://openrouter.ai/api/v1
      api_key_env: OPENROUTER_API_KEY
    anthropic:
      kind: anthropic
      api_key_env: ANTHROPIC_API_KEY
    ollama:
      kind: openai-compat
      base_url: http://localhost:11434/v1
      api_key: local          # local servers need a non-empty dummy key
  roles:
    default:  {provider: openrouter, model: <model-id>}
    chat:     {provider: openrouter, model: <model-id>}
    # research / pipeline / match / summary / triage ... — per-task routing
```

Options, in increasing order of setup effort:

- **OpenRouter (recommended default)** — one API key, any hosted model. There is a
  connect button in the web UI Settings that walks through linking an OpenRouter
  key; alternatively set the key in `.env`.
- **Anthropic / OpenAI API keys** — point a provider at `kind: anthropic` or an
  OpenAI-compatible `base_url` and supply the key via env.
- **Fully local** — run Ollama or LM Studio on the host and point `base_url` at it.
  No data leaves your machine, at the cost of model quality/speed.
- **Subscription logins (disabled by default)** — the agent service contains OAuth
  flows for ChatGPT-Codex and GitHub Copilot subscriptions, gated behind
  `llm_oauth_gray_flows: false` in `config.yaml`. Neither vendor officially
  sanctions these flows for third-party apps; review the providers' terms of
  service before enabling. Anthropic explicitly blocks Claude subscription use by
  third-party apps, so a Claude subscription login is never offered — use the
  Anthropic API key instead.

Embeddings are configured separately (`embed_backend` / `embed_url` /
`embed_model`) and also accept any OpenAI-compatible endpoint or local Ollama.
Changing the embedding model changes the vector space — re-embed with
`python scripts/backfill_embeddings.py --all` afterwards.

## Architecture overview

Docker Compose runs the platform as a set of services:

| Service | Role |
|---|---|
| `server` | FastAPI app — web UI, knowledge API, pipeline, scheduler, MCP server |
| `db` | PostgreSQL 16 + pgvector — the single store (documents, clients, contacts, runs) |
| `searxng` | Self-hosted metasearch — primary web search for agents |
| `agent-pi` | TypeScript agent runtime — executes research/OSINT/match/chat agent tasks |
| `camofox` + `browser-service` | Hardened Firefox + Playwright service — browser-based fetching for agents |
| `pgweb` | DB browser UI for local debugging (host port 5433, `--profile debug`) |
| `cloudflared` | Tunnel for exposing an instance publicly (`--profile tunnel`) |

Design in one paragraph: the schema is document-oriented — meetings, research,
notes, and signals are all rows in a universal `documents` table with a `type`
field and JSONB metadata; every table carries an `org_id` for tenant isolation;
every agent-written document records its agent run and a `## Sources` section.
Agents follow an observe → plan → act → reflect → write loop and run on heartbeat
schedules with change-detection so quiet clients cost nothing.

Full system design, schema DDL, and agent patterns: see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Transcription

The Docker deployment does **not** transcribe audio by default — it ingests text:

- **App mode (default, `transcription_mode: app`)** — a native macOS companion app
  records and transcribes locally on Apple Silicon, then posts finished transcripts
  to `POST /api/transcript/ingest` (authenticated with a per-user token from
  Settings). The server does no Whisper inference.
- **Transcribe profile (optional)** — an optional Compose profile that runs Whisper
  inside Docker for server-side file transcription, useful when no Mac is involved.
- **Local mode (`transcription_mode: local`)** — run `python server.py` directly on
  a Mac: browser mic → faster-whisper live transcription, then a WhisperX post-pass
  with speaker diarization (requires a HuggingFace token for the pyannote models,
  set as `hf_token` or the `HFTOKEN` env var).

There is also a CLI for batch work:

```bash
python transcribe.py path/to/audio.mp4 --language en
# flags: --model large-v2, --no-diarize, --no-summary
```

## Configuration

Two places:

- **`config.yaml`** — models and providers (`llm:` block, embeddings), agent
  scheduling and throttling, next-best-action weights, SMTP for digests,
  `public_url` for outbound links. Every key is commented in the file itself.
- **`.env`** (from `.env.example`) — secrets and per-deployment values:
  `AGENT_SERVICE_TOKEN` (required, shared secret between server and agent
  service), LLM/API keys, optional `ADMIN_USERNAME`/`ADMIN_PASSWORD` bootstrap,
  optional `HFTOKEN`, SMTP and Telegram credentials.

Precedence: environment variables override `config.yaml` where both exist.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the server against a local Docker Postgres
docker compose up -d db
python server.py

# tests (CI runs the fast suite)
pytest tests/ -m "not slow and not ollama"
```

Conventions:

- Development happens on `test-branch`; `main` is the release branch (CI runs on
  both, deployment templates trigger from `main` only — see
  `docs/examples/deploy.yml.example`).
- The schema is document-oriented: new content types are new `type` values on the
  `documents` table, never new tables. Attributes go into JSONB `metadata`.
- Graceful degradation everywhere: LLM offline → skip summary; DB index fails →
  promotion still succeeds; agent fails → log and continue.

## License

Buzzowl is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0) — see [LICENSE](LICENSE).

In short: you may use, modify, and self-host Buzzowl freely. If you modify it and offer it to others over a network (e.g. as a hosted service), you must make your modified source available under the same license. Commercial licensing for closed hosted offerings is available on request.
