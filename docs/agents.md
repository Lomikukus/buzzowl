# Agents — what they do, how to run them, how to watch them

An agent run is: a task goes to the agent service, the agent plans, calls tools
(web search, page fetch, knowledge read/write), and finally writes a document
back into PostgreSQL with a `## Sources` section. Everything an agent produced is
traceable to the run that produced it (`documents.agent_run_id`).

The runtime is the `agent-pi` container (TypeScript, built on the Pi engine). The
server never sends API keys to it — it sends `{provider_name, model}` and the
container resolves the key locally.

## Agent types

| Type | Does what | Writes |
|---|---|---|
| `research` | deep research on a company: web search, page fetching, synthesis | `research` document |
| `osint` | news, press and signal sweep for a client | `osint` document, `signal` documents |
| `enrichment` | fills gaps on a client or contact profile | profile updates, `finding` |
| `orchestrate` | looks at what is known and decides what (if anything) to do next | `note` + follow-up runs |
| `monitor` | daily sweep over monitored news/press pages, escalates changes | `signal`, follow-up runs |
| `pain_point_research` | stage 1 of product matching: what hurts at this client | `finding` |
| `match_synthesis` | stage 2: which of your products fit those pain points | `match_report` |
| `product_research` | maps your own product catalogue from your website | product entries |

## Triggering a run

**From the UI** — the client page ("Research", "OSINT"), the Match page, the
Agents page. This is the normal way.

**Automatically** — heartbeats. Focus clients (★) are researched on a schedule;
non-focus clients only trickle in when they go stale (`heartbeat_stale_days`,
`heartbeat_max_nonfocus_per_run` in `config.yaml`). A news fingerprint skips
clients whose news picture has not changed, so quiet clients cost nothing.

**By an agent** — with autonomy on, the `orchestrate` agent may trigger further
runs itself (see below).

**By hand**, for debugging:

```bash
curl -s -X POST http://localhost:8000/api/agents/run \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"agent_type":"research","subject":"Acme Corp","task":"Research Acme Corp — focus on recent leadership changes"}'
```

## Autonomy levels

Per organisation, in Settings → Agent Autonomy (`orgs.settings.autonomy_level`):

| Level | Meaning |
|---|---|
| **0** | off — exactly the pre-autonomy behaviour. Nothing decides on its own. |
| **1** | observe — every decision is made *and logged*, but never acted on. |
| **2** | act — may trigger research / OSINT / match runs on its own. |
| **3** | + outreach — may additionally *draft* outreach. Sending always needs a human. |

Every decision, including the skips, is written to `agent_runs` as
`agent_type='autonomy_review'` — the Agents page has an Autonomy tab showing what
the agent chose *not* to do and why. Budgets (`max_autonomous_runs_per_day`), a
per-client cooldown and a kill switch live in the same settings block. If the LLM
call fails, the deterministic fallback runs, so level ≥ 1 is never worse than
level 0.

## Watching a run

- **Agents page** (`/agents`) — live tool calls over WebSocket, run history,
  autonomy audit.
- **Logs**: `docker compose logs -f agent-pi`
- **SQL**:

```bash
docker compose exec -T db psql -U whisper -d whisper -c "
  SELECT id, agent_type, status, trigger_type, created_at
    FROM agent_runs ORDER BY id DESC LIMIT 10;"

docker compose exec -T db psql -U whisper -d whisper -c "
  SELECT id, type, title FROM documents WHERE agent_run_id = 123;"
```

Cancel a run: the Agents page, or

```bash
curl -s -X POST http://localhost:8000/api/research/cancel \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"run_id":123}'
```

## Tools an agent has

Read: `search_kb`, `get_client`, `search_clients`, `list_clients`,
`get_recent_findings`, `get_contact_log`, `get_nba_queue`, `get_deals`,
`get_client_timeline`. Act: `web_search` (SearXNG), `fetch_page` (hardened
browser, falls back to plain HTTP), `write_document`, `update_client`,
`create_task`, `find_people`, `trigger_run`, `update_deal_stage` (level ≥ 2, open
stages only), `draft_outreach` (level 3, draft only).

Write tools are gated server-side, not by the prompt: an agent cannot approve its
own outreach, cannot close a deal, and cannot cross organisation boundaries.

## Rules every agent follows

- Every written document carries `source: agent`, its `agent_run_id`, and a
  `## Sources` section listing every URL used. A claim without a traceable source
  is marked `(unconfirmed)`.
- New content types are new `type` values on `documents` — agents never create
  tables.
- Failure is survivable: a failed run is logged and the next one continues.

## Tuning

`config.yaml`:

- `llm.roles.research` / `.triage` — model per role; a faster model here changes
  cost and latency the most.
- `heartbeat_stale_days`, `heartbeat_max_nonfocus_per_run`, `news_change_detection`
  — how much automatic work happens at all.
- `match_escalation_min_relevance` — how strong a signal must be before a match
  re-run is triggered.
- `AGENT_MAX_CONCURRENT` (compose env) — parallel runs; raise only with RAM to spare.

Slow or expensive runs are almost always a model choice, not the harness — see
[troubleshooting.md](troubleshooting.md).
