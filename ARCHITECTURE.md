# Buzzowl — Architecture & Instructions

> **Setup & quickstart:** see the [README](README.md). This document covers system
> design, the database schema, and agent patterns — not installation.

> ⚠️ **Deprecation note (2026-06):** the **Obsidian vault has been removed.** PostgreSQL is
> the single source of truth and the web UI renders markdown directly from
> `documents.content`. Vault references throughout this document are retained as historical
> design context only — there are no file-based vault writes in the codebase, and the
> `## Obsidian Vault` section below is marked REMOVED.

## Vision

A **research and knowledge sharing platform for sales teams**. Captures knowledge from meetings (audio → transcript → summary), enriches it through autonomous agent research, and makes it searchable and actionable — all visualised through an Obsidian vault and accessible to any MCP-compatible agent.

The system is built incrementally. Every layer is independently useful before the next is added.

---

## Guiding Principles

- **Incremental** — each step ships something working. No big-bang scaffolding.
- **Cloud-first for LLM** — all agent reasoning and summaries route through OpenRouter (any hosted model). Claude API is an opt-in alternative. Audio processing (Whisper, diarization) runs on-device.
- **Open** — the knowledge base exposes itself via MCP so any compatible agent (Claude Code, OpenClaw, custom) can read and write to it.
- **Proactive** — agents run on heartbeat schedules and event hooks, not just when triggered by a user.
- **Traceable** — every piece of agent-written content is linked to the agent run that created it.
- **Never break the vault** — Obsidian remains the human-readable layer. The database is the machine-readable index. They stay in sync.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Obsidian vault                           │
│   AGENTS.md ← symlink → CLAUDE.md  (agent operating manual)    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ read/write .md
┌──────────────────────────▼──────────────────────────────────────┐
│                   Buzzowl API  (FastAPI)                │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Auth layer │  │ Knowledge API│  │     MCP Server         │ │
│  │  orgs,      │  │  documents,  │  │  tools + resources     │ │
│  │  users,     │  │  clients,    │  │  exposed to any MCP    │ │
│  │  sessions   │  │  contacts,   │  │  client (Claude Code,  │ │
│  └─────────────┘  │  search      │  │  OpenClaw, external)   │ │
│                   └──────────────┘  └────────────────────────┘ │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                   Agent Runtime                           │  │
│  │                                                           │  │
│  │  ┌─────────────┐   ┌──────────────────────────────────┐  │  │
│  │  │  Heartbeat  │   │         Agent Loop               │  │  │
│  │  │  Scheduler  │──▶│  1. Observe  (read KB + triggers)│  │  │
│  │  │  (APSched.) │   │  2. Plan     (LLM reasoning)     │  │  │
│  │  └─────────────┘   │  3. Act      (call tools)        │  │  │
│  │                    │  4. Reflect  (evaluate results)  │  │  │
│  │  ┌─────────────┐   │  5. Write    (persist to KB)     │  │  │
│  │  │  Event      │──▶│  → repeat until done             │  │  │
│  │  │  Hooks      │   └──────────────────────────────────┘  │  │
│  │  │  post-export│                    │                     │  │
│  │  │  new entity │         ┌──────────▼──────────┐         │  │
│  │  └─────────────┘         │       Tools          │         │  │
│  │                          │  search_kb           │         │  │
│  │  ┌─────────────┐         │  write_document      │         │  │
│  │  │   Brain     │         │  get_client          │         │  │
│  │  │  Ollama     │         │  update_client       │         │  │
│  │  │  or Claude  │         │  web_search          │         │  │
│  │  │  API        │         │  fetch_page          │         │  │
│  │  └─────────────┘         │  run_osint           │         │  │
│  │                          │  [+ any MCP tool]    │         │  │
│  └──────────────────────────┴──────────────────────┘         │  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
        ┌──────────────────┼───────────────────┐
        │                  │                   │
┌───────▼──────┐  ┌────────▼───────┐  ┌────────▼────────┐
│  PostgreSQL  │  │ External MCP   │  │  MCP clients    │
│  + pgvector  │  │ tool servers   │  │  Claude Code,   │
│              │  │ web search,    │  │  OpenClaw,      │
│  orgs        │  │ LinkedIn,      │  │  any agent      │
│  users       │  │ Companies      │  │  that speaks    │
│  clients     │  │ House, etc.    │  │  MCP            │
│  contacts    │  └────────────────┘  └─────────────────┘
│  documents   │
│  doc_links   │
│  agent_runs  │
│  heartbeats  │
└──────────────┘
```

---

## Database Schema

### Design decisions

- **Document-oriented** — `documents` is the universal content table. Meetings, research notes, uploaded PDFs, and agent-written summaries are all rows with a `type` field. No new tables for new content types.
- **JSONB metadata** — flexible attributes on every entity. Add new fields (deal stage, LinkedIn URL, OSINT data) without schema migrations.
- **Multi-tenancy via `org_id`** — every table carries `org_id`. Nothing crosses org boundaries.
- **`visibility` on documents only** — clients and contacts are always shared within an org. Only notes/documents can be private.
- **Agent attribution** — every document records `source` (human | agent | import) and `agent_run_id`.
- **vector(768)** — `nomic-embed-text` embedding model. HNSW-indexable, purpose-built for semantic search.

### Full DDL

```sql
-- Top-level tenancy
CREATE TABLE orgs (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Users belong to one org
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    email         TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',  -- admin | member
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(org_id, username)
);

-- User sessions (token-based auth)
CREATE TABLE user_sessions (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- Client entities
CREATE TABLE clients (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}',
    -- metadata carries: industry, status, website, employees, hq,
    --                   deal_stage, deal_value, assigned_to, etc.
    session_count INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    fts_doc       TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata->>'industry', '')), 'B') ||
        setweight(to_tsvector('english', coalesce(metadata->>'notes', '')), 'C')
    ) STORED,
    embedding     vector(768),
    UNIQUE(org_id, name)
);

-- Contact entities
CREATE TABLE contacts (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    client_id     BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}',
    -- metadata carries: role, email, phone, linkedin, influence, notes, etc.
    session_count INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    fts_doc       TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(
            coalesce(metadata->>'role', '') || ' ' ||
            coalesce(metadata->>'company', ''), ''
        )), 'B') ||
        setweight(to_tsvector('english', coalesce(metadata->>'notes', '')), 'C')
    ) STORED,
    embedding     vector(768),
    UNIQUE(org_id, name)
);

-- Universal document store
CREATE TABLE documents (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    doc_id       TEXT NOT NULL,       -- session_id, file hash, or slug
    type         TEXT NOT NULL,       -- meeting | research | upload | note | osint | summary
    title        TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    metadata     JSONB NOT NULL DEFAULT '{}',
    -- metadata carries type-specific fields:
    --   meeting:  {date, duration_s, speakers, language, transcript_path, audio_path}
    --   research: {source_url, author, published_date}
    --   upload:   {filename, file_size, pages, mime_type}
    --   note:     {tags}
    --   osint:    {sources, confidence}
    visibility   TEXT NOT NULL DEFAULT 'shared',  -- shared | private
    source       TEXT NOT NULL DEFAULT 'human',   -- human | agent | import
    agent_run_id BIGINT,              -- FK set after agent_runs is created
    created_by   BIGINT REFERENCES users(id),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    fts_doc      TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED,
    embedding    vector(768),
    UNIQUE(org_id, doc_id)
);

-- Many-to-many: documents ↔ clients, documents ↔ contacts
CREATE TABLE document_links (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,   -- client | contact
    entity_id   BIGINT NOT NULL,
    PRIMARY KEY (document_id, entity_type, entity_id)
);

-- Agent run log
CREATE TABLE agent_runs (
    id            BIGSERIAL PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    triggered_by  BIGINT REFERENCES users(id),  -- null = heartbeat / event hook
    trigger_type  TEXT NOT NULL,  -- manual | heartbeat | event_hook
    agent_type    TEXT NOT NULL,  -- research | osint | org | custom
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    task          TEXT NOT NULL,
    tool_calls    JSONB NOT NULL DEFAULT '[]',  -- full audit trail
    output        JSONB NOT NULL DEFAULT '{}',
    error         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at  TIMESTAMPTZ
);

-- FK from documents to agent_runs (deferred to avoid circular DDL)
ALTER TABLE documents
    ADD CONSTRAINT fk_documents_agent_run
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL;

-- Per-org heartbeat schedule
CREATE TABLE heartbeats (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    agent_type  TEXT NOT NULL,
    cron_expr   TEXT NOT NULL,   -- standard cron: "0 8 * * *"
    task        TEXT NOT NULL,   -- natural language task description
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ
);
```

### Indexes

```sql
-- GIN full-text
CREATE INDEX idx_clients_fts   ON clients   USING GIN(fts_doc);
CREATE INDEX idx_contacts_fts  ON contacts  USING GIN(fts_doc);
CREATE INDEX idx_documents_fts ON documents USING GIN(fts_doc);

-- HNSW vector (768 dims — within pgvector's 2000-dim HNSW limit)
CREATE INDEX idx_clients_vec   ON clients   USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX idx_contacts_vec  ON contacts  USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX idx_documents_vec ON documents USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);

-- Org scoping (most queries filter by org_id)
CREATE INDEX idx_clients_org   ON clients(org_id);
CREATE INDEX idx_contacts_org  ON contacts(org_id);
CREATE INDEX idx_documents_org ON documents(org_id, type);
CREATE INDEX idx_agent_runs_org ON agent_runs(org_id, status);

-- Array + JSONB
CREATE INDEX idx_documents_type ON documents(org_id, type);
CREATE INDEX idx_clients_meta   ON clients USING GIN(metadata);
CREATE INDEX idx_contacts_meta  ON contacts USING GIN(metadata);
```

---

## Auth Layer

Token-based session auth. Passwords hashed with bcrypt (passlib).

```
POST /api/auth/register   → create org + first admin user, return token
POST /api/auth/login      → return session token
POST /api/auth/logout     → invalidate token
GET  /api/auth/me         → current user + org
POST /api/auth/users      → invite a user to org (admin only)
```

Every other endpoint requires a valid token in the `Authorization: Bearer <token>` header. A FastAPI `Depends(current_user)` injects `user` and `org_id` into every handler automatically.

---

## Knowledge API

All endpoints are org-scoped — the current user's `org_id` is injected automatically.

```
# Documents
POST   /api/documents              → create (type, title, content, metadata, visibility, client_links)
GET    /api/documents/{id}         → get single document
PATCH  /api/documents/{id}         → update title / content / metadata
DELETE /api/documents/{id}         → soft delete

# Clients
GET    /api/clients                → list (paginated, sorted by session_count)
POST   /api/clients                → create
GET    /api/clients/{name}         → get with linked documents
PATCH  /api/clients/{name}         → update metadata
GET    /api/clients/{name}/docs    → all documents linked to this client

# Contacts
GET    /api/contacts               → list
POST   /api/contacts               → create
GET    /api/contacts/{name}        → get with linked documents
PATCH  /api/contacts/{name}        → update metadata

# Search
GET    /api/search?q=&type=&client= → hybrid search (vector 0.6 + FTS 0.4)

# Sessions (current meeting pipeline)
GET    /api/sessions               → list meeting documents
GET    /api/sessions?company=X     → filter by client
```

---

## Agent Architecture (OpenClaw-inspired loop)

### Core pattern

```python
class Agent:
    name:         str           # "research" | "osint" | "org" | custom
    instructions: str           # loaded from vault AGENTS.md section for this agent
    tools:        list[Tool]    # subset of the tool registry
    brain:        Brain         # OpenRouter or Claude API — swappable
    org_id:       int
    run_id:       int           # agent_runs.id for this invocation

    async def run(self, task: str, context: dict) -> AgentResult:
        self.observe(context)           # load relevant KB context
        while not done:
            plan   = self.brain.think(task, self.memory, self.tools)
            result = await self.act(plan.tool_calls)
            done   = self.reflect(result)
        self.write(result)              # persist to documents + agent_runs
        return result
```

### Tool registry

All tools are also exposed as MCP tools — one definition, two consumers.

| Tool | Description |
|---|---|
| `search_kb(query, type?, client?)` | Hybrid search across documents/clients/contacts |
| `get_client(name)` | Full client profile + linked documents |
| `write_document(type, title, content, client_links, metadata)` | Persist to DB + vault |
| `update_client_metadata(name, patch)` | Patch JSONB metadata fields |
| `web_search(query, n_results)` | Web search via Brave/DuckDuckGo MCP |
| `fetch_page(url)` | Scrape and extract text from a URL |
| `list_clients()` | All clients for the org with activity stats |
| `get_agent_context(client_name)` | Pull all documents for a client into context |

### Brain — swappable LLM backend

```python
class OpenRouterBrain:  # default — routes to any hosted model via OpenRouter
    model: str = "qwen/qwen3.5"

class ClaudeBrain:      # opt-in, requires API key in config.yaml
    model: str = "claude-sonnet-4-6"
```

Switched via `config.yaml`:
```yaml
agent_brain: openrouter      # openrouter | claude
agent_model: qwen/qwen3.5    # model name for chosen backend
openrouter_api_key: ""       # required for openrouter brain
anthropic_api_key: ""        # required if agent_brain: claude
```

### Agent types

| Agent | Trigger | Task |
|---|---|---|
| `research` | Manual / post-export hook | Given a client, gather and structure existing knowledge |
| `osint` | New client created / weekly heartbeat | Web search, news, company info → write research doc |
| `org` | Weekly heartbeat | Deduplicate contacts, link orphaned documents, suggest tags |
| `meeting-prep` | Manual | Pull all docs for a client, generate a pre-meeting brief |
| `enrichment` | Post-export event hook | Extract entities from new session, enrich client/contact profiles |

### Pi agent service

All agent types run on Pi (TypeScript, port 8001). `_get_service_url()` in `routers/agents.py` is a one-liner returning `agent_service_url_pi`. Camofox is wired to Pi for all browser fetching.

Hermes was retired in Phase 29 after benchmarks showed Pi producing 12,506–22,544 chars vs Hermes 869 chars across all task types (research, osint, monitor, product_research, product_deep_research, pain_point_research, match_monitor). See `data/benchmarks/phase28-osint-benchmark-findings.md`.

When adding a new agent type: add a prompt to `PROMPTS` in `agent_service_ts/src/agent.ts`, add the type to `AGENT_TOOL_ALLOWLIST`, and add any needed tools. No routing config changes required.

---

## Heartbeats + Proactivity

### Time-driven (cron heartbeats, stored in `heartbeats` table)

```
"0 8 * * 1-5"   research   "Check all active clients. For any not updated in 14 days, search for recent news."
"0 9 * * 1"     osint      "Run OSINT on all clients created this week."
"0 6 * * 0"     org        "Deduplicate contacts. Link unlinked documents. Flag stale entries."
"0 7 * * 1"     research   "Generate weekly account health summaries for all active prospects."
```

### Event-driven (hooks wired into the API)

```
POST /api/export completes     → trigger enrichment agent on extracted entities
POST /api/clients (new)        → trigger osint agent for initial company research
POST /api/contacts (new)       → trigger contact research (LinkedIn, company lookup)
GET  /api/search with 0 results → flag query for manual research queue
```

---

## MCP Layer

### This system as MCP server

Exposes the knowledge base to any MCP-compatible client (Claude Code, OpenClaw, custom agents).

```
Tools:     search_kb, write_document, get_client, list_clients,
           get_session, update_client_metadata, trigger_agent

Resources: clients://{org}/{name}
           documents://{org}/{id}
           vault://{path}

Prompts:   research_client     → pulls all docs for client, asks LLM to summarise
           meeting_prep        → pre-meeting brief for a client
           weekly_brief        → account health summary
           osint_report        → structured OSINT findings
```

Implementation: Python `mcp` SDK, runs as a subprocess alongside FastAPI or as a separate `mcp_server.py`.

### Internal agents consuming external MCP tools

```yaml
# config.yaml
mcp_servers:
  web_search:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: ""
  browser:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-puppeteer"]
  # future:
  # linkedin: ...
  # companies_house: ...
```

Agent tools that call `web_search` or `fetch_page` are wired to these MCP servers transparently.

---

## Obsidian Vault — REMOVED (2026-06)

> This layer was removed. The app never read it back; PostgreSQL is the single store and
> the web UI renders `documents.content`. The section below is kept for historical context.

The vault was originally the human-readable layer. Structure per org:

```
north-info/                    ← org vault root (vault_path in config.yaml)
├── CLAUDE.md                  ← LLM operating manual (auto-generated, maintained)
├── AGENTS.md → CLAUDE.md      ← symlink for OpenClaw / non-Claude agents
├── index.md                   ← master catalog (auto-updated on export)
├── log.md                     ← append-only session log
├── _templates/                ← reference templates
├── raw/                       ← immutable meeting transcripts (never edit)
├── clients/                   ← one page per company
├── contacts/                  ← one page per person
├── research/                  ← agent-written research docs
├── osint/                     ← agent-written OSINT reports
└── briefs/                    ← meeting prep and weekly summaries
```

Every file written by an agent includes frontmatter `source: agent` and `agent_run_id:` for traceability.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Transcription (live) | faster-whisper |
| Transcription (post) | WhisperX + wav2vec2 alignment |
| Speaker diarization | pyannote.audio 3.1 |
| Embeddings | Ollama `nomic-embed-text` (768 dims) — local only, embeddings are never sent to cloud |
| AI summary / agents | OpenRouter (any hosted model, default) or Claude API (opt-in) |
| Web backend | FastAPI + WebSocket |
| Database | PostgreSQL 16 + pgvector |
| MCP server | Python `mcp` SDK |
| MCP tool servers | Brave Search MCP, Puppeteer MCP (external) |
| Frontend | Vanilla JS + marked.js |
| Audio capture | Web Audio API (16 kHz PCM) |
| Vault / notes | Obsidian-compatible Markdown |
| Scheduling | APScheduler |
| Auth | passlib[bcrypt] + token sessions |
| Containerisation | Docker Compose (DB only; app runs natively) |

---

## Build Order

Each phase is independently shippable. Do not start the next phase until the current one is working and tested.

### Phase 1 — Schema + Auth
- Rebuild DB schema: orgs, users, user_sessions, clients, contacts, documents, doc_links, agent_runs, heartbeats
- Auth endpoints: register, login, logout, me
- FastAPI `current_user` dependency (token middleware)
- Switch embed model to `nomic-embed-text`, rebuild `db.py`

### Phase 2 — Knowledge API
- CRUD endpoints for documents, clients, contacts (all org-scoped)
- Re-wire `/api/export` to write `documents` rows instead of `sessions`
- Hybrid search updated for new schema
- Obsidian vault sync updated

### Phase 3 — MCP Server
- `mcp_server.py` exposing tools + resources
- Wire to FastAPI knowledge API internally
- `AGENTS.md` symlink in vault
- Test with Claude Code as MCP client

### Phase 4 — Agent Runtime
- `agents/base.py` — Agent base class (observe → plan → act → reflect → write loop)
- `agents/brain.py` — OllamaBrain + ClaudeBrain (swappable)
- `agents/tools.py` — Tool registry (search_kb, write_document, web_search, fetch_page, get_client)
- `agents/runner.py` — async run, logs to `agent_runs`

### Phase 5 — First Agent + Heartbeats
- `agents/enrichment.py` — post-export hook: enrich entities from new session
- `agents/research.py` — given a client, gather and structure knowledge
- APScheduler wired to `heartbeats` table
- Event hooks in `/api/export`
- Agent status API: `POST /api/agents/run`, `GET /api/agents/tasks/{id}`

### Phase 6 — OSINT Agent
- `agents/osint.py` — web search + fetch + structured extraction
- Wire Brave Search MCP (or DuckDuckGo fallback)
- Write findings as `type=osint` documents linked to client
- Weekly heartbeat

### Phase 7 — Org Agent + UI
- `agents/org.py` — deduplication, linking, tagging
- Agent activity feed in UI (what did agents do this week?)
- Private/shared toggle on document creation
- Multi-user login screen

---

## Configuration (`config.yaml`)

```yaml
# Transcription
model: large-v2
live_model: base
language: en
compute_type: int8
hf_token: ""

# Vault
vault_path: "/path/to/north-info"

# AI
ollama_model: llama3.2          # for summaries
agent_brain: ollama             # ollama | claude
agent_model: qwen3.5            # model for agent reasoning
anthropic_api_key: ""           # optional, for claude brain

# Embeddings
embed_model: nomic-embed-text   # pull with: ollama pull nomic-embed-text
embed_dim: 768

# Database
db_url: "postgresql://whisper:whisper@localhost:5432/whisper"

# MCP tool servers
mcp_servers:
  web_search:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: ""
```

---

## Development Rules

1. **Build incrementally.** Finish one phase before starting the next.
2. **Ask before adding anything not explicitly requested.**
3. **Never edit files in `raw/`.** Transcripts are immutable.
4. **Never overwrite `## Facts` or `## Mentions` on vault pages.** Append only.
5. **Every agent-written document must carry `source: agent` and `agent_run_id`.**
6. **DB indexing is always best-effort.** Export/save must succeed even if DB is offline.
7. **Graceful degradation everywhere.** OpenRouter unreachable → skip summary. DB offline → skip index. Agent fails → log error, continue.
8. **One embedding model, fixed forever.** `nomic-embed-text` at 768 dims. Changing models requires a schema rebuild.
9. **Always link back to sources.** Every agent-written document — finding, summary, OSINT report, research brief — must include a `## Sources` section listing every URL the content was drawn from. No source URL, no claim. Summaries must backlink to the individual finding documents they synthesised. This applies at every level: individual findings link to their URL; aggregated summaries link to their findings; vault files carry `source_url` in frontmatter. A claim with no traceable source is treated as unverified and must be marked `(unconfirmed)`.
