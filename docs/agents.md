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
| `jobs_scan` | scans a client's careers page, extracts open positions, infers hiring-driven needs | singleton `jobs` document, `finding` documents |
| `news_scan` | scores fresh SearXNG news candidates for one client with a single LLM call | `signal` documents |
| `market_news` | industry/market news sweep, not tied to one client yet | `signal` documents (`metadata.scope: "market"`) |
| `lessons_review` | weekly pass over site playbooks and failed runs, proposes cross-site navigation rules | `agent_lessons` document (entries `status: proposed`) |

`jobs_scan`, `news_scan` and `market_news` never touch the `agent-pi` container:
they run in the FastAPI process itself, each making a single text-only
`llm.acomplete(..., role="research")` call. They do not occupy one of
agent-pi's two run slots, and they are not in `agent_service_ts`'s
`PROMPTS`/`AGENT_TOOL_ALLOWLIST` (there is nothing for Pi to run). `market_news`
used to be a Pi run; it was rewritten as a Python scan for the same reason
`jobs_scan`/`news_scan` never were Pi runs to begin with: scoring a list of
search results against one prompt does not need a tool-calling agent loop.
`lessons_review` is heartbeat-only (see Tuning below) and never has a
per-client subject.

### Jobs and news data

`jobs_scan` writes one singleton document per client, `doc_id=jobs-{client_id}`,
`type=jobs`. Its metadata carries `positions`, `inferred_needs`,
`needs_mapped`, `careers_url`, `last_scanned`, and two round-2 additions:
`tier` (which discovery step found the page: `playbook`, `metadata`,
`homepage`, `sitemap`, `searxng`, or `listing-link`) and `filtered_out` (how
many extracted positions `_filter_positions` dropped as
apprentice/intern/student/thesis titles, unless the title also carried an
IT or management word). A failed scan never overwrites a prior good scan's
`positions`/`careers_url`; it only stamps `last_attempt`, `last_error` and
bumps `attempts` on the same document. The account brief renders this
document as its own `[JOBS]` context block and its prompt has a
`## Hiring Signals` section asking specifically for it.

`news_scan` writes each relevant article as its own `type=signal` document,
`doc_id=news-{client_id}-{sha1(url)[:10]}`, client-linked. Its metadata
carries `from_news_scan: true`, `published_at` (the article's date, required
and within the last 90 days), `relevance_score` (1-5, from the scoring LLM
call), and `source_url`. `market_news` writes the same shape but unlinked,
tagged `metadata.scope: "market"`, later mapped onto specific clients by a
separate apply step. Both are distinct from the pre-existing `osint`-written
signals only by `from_news_scan`/`service: "python"` in their metadata; they
render into the brief and into match synthesis exactly like any other signal.

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

## Client intake (a new or re-triggered client's first sweep)

Creating a client (`POST /api/clients`, `POST /api/internal/clients`) or
re-triggering research (`POST /api/clients/{name}/trigger-research`) starts
`intake.start()`, which fires four parts at once instead of loose background
tasks: `osint` and `research` on `agent-pi`, and `jobs`/`news` as direct
Python calls. State lives at `clients.metadata.intake` (never read or written
through the ordinary shallow-merge metadata patch, since two parts finishing
in the same second would otherwise clobber each other's nested state).

Each part is tracked as `{status, run_id, started_at, done_at, error}` with
`status` one of `queued`, `running`, `done`, `failed`. The account brief
(`GET`/`POST /api/clients/{name}/brief`) is written exactly once, from
everything collected so far, when the collection point closes rather than
racing off whichever part happens to finish first. Its status lives at
`intake.brief.status`:

| Status | Meaning |
|---|---|
| `waiting` | collecting; brief not started |
| `writing` | one caller won the write race (a CAS on `brief.status`) and is generating it |
| `partial` | the deadline or absolute cap passed with parts still open; a brief was written from what had arrived, missing parts named in the text |
| `written` | every part reached `done`/`failed` and the brief reflects all of them |
| `refreshed` | a `partial` brief was regenerated once more parts finished |
| `failed` | brief generation failed 3 attempts in a row |

**Why the 25-minute clock starts at `running`, not at client creation.**
`agent-pi` enforces a 2-slot FIFO (`AGENT_MAX_CONCURRENT`, default 2, compose
env), so a newly created client's `osint`/`research` runs can sit `queued`
behind other clients' work for a while. `intake.note_run_started()` fires the
first time `agent-pi` reports one of those parts as actually `running` (past
its FIFO slot, not merely dispatched), and only then stamps
`intake.deadline_at = now + intake_deadline_min` (`config.yaml`, default 25).
Starting the clock at row creation would hand a merely queued client a bogus
partial brief before its research had even begun.

**One-time refresh.** A `partial` brief is regenerated automatically,
exactly once, when a part finishes successfully after it was written and no
part is still open. A late part that only fails does not trigger a
regeneration: the brief is closed out as `written` with the failed part named
in `intake.brief.missing`. After the refresh (`brief.refreshed_at` set),
nothing reopens it automatically again.

**Absolute cap.** `intake_absolute_cap_min` (`config.yaml`, default 90)
counts from `intake.started_at` and force-finishes the intake regardless of
the deadline, e.g. when a part never leaves `queued` at all.

**The sweeper** (`intake.sweep()`, APScheduler job `intake_sweeper`, every
60s) is the lost-callback rescue: for every client with an open intake it (1)
reverts a `writing` brief stuck for more than 10 minutes (the process died
mid-write) back to `waiting`; (2) reconciles a part whose `agent_runs` row
already finished while `intake.parts` still shows it `queued`/`running`,
i.e. the HTTP callback to the server never arrived or was lost; and (3)
otherwise re-checks whether the deadline, the absolute cap, or the one-time
refresh condition has now been met.

`GET /api/clients/{name}/intake` returns the live state for the client
page's intake strip (a chip per part plus a brief-status chip). None of this
gates the manual regenerate button: `POST /api/clients/{name}/brief` always
runs immediately, intake open or not, and a manual brief closes an open
intake (`written`, `missing` empty) so the collection point never overwrites
it later.

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

## Site playbooks and cross-site lessons

Two document types close the loop between what an agent learns on one run
and what the next run starts already knowing:

**`site_playbook`** (`doc_id=site-playbook-{domain}`, one per website, not
client-linked) records what agents have learned navigating a specific site:
its careers URL and discovery tier, newsroom URLs, search queries that did or
didn't pay off, blocked/failed URLs (own-domain only, so a 403 on a
competitor's page never contaminates the client's own playbook), and whether
the site needs a JS-rendering fetch. It is written two ways: directly, by the
jobs and news scanners after every attempt (`playbook.record()`); and from
the `agent-pi` callback handler, after every `research`, `osint` or
`pain_point_research` run, success or failure (`playbook.reflect_on_run()`),
which classifies the run's own tool-call log deterministically and, for runs
with 8 or more tool calls, makes one extra LLM call for free-text navigation
notes. Before firing a `research`, `osint` or `pain_point_research` run,
`playbook.enrich_task()` appends the site's playbook (if any) and any
approved lessons to the task text and returns whether the run should ask for
a JS-rendering fetch. This only fires on an exact client-name match: a
fuzzy or placeholder subject (e.g. the market monitor's `"org"`) gets its
task back unchanged, so one client's playbook can never leak into another
client's run.

**`agent_lessons`** (`doc_id=agent-lessons-org`, one per org) holds general,
cross-site navigation lessons, not site-specific facts (those live in each
site's own playbook). They are proposed weekly by an LLM pass over every
`site_playbook` plus that week's failed runs (the `lessons_review` heartbeat,
Monday 07:00, or on demand). Each lesson carries `status: proposed | approved
| rejected` and a `scope` (`jobs`, `news`, `research`, or `all`). **A lesson
is never used until a human approves it**: nothing in `playbook.py` sets
`status: approved` on its own.

- `GET /api/agents/lessons`: list, grouped by status (any org member).
- `POST /api/agents/lessons/{id}/decision` with `{"decision": "approve"|"reject"}` (admin only).
- `POST /api/agents/lessons/review`: run the weekly proposal pass immediately (admin only).

Only `approved` lessons whose scope matches the agent type (or is `all`) are
ever injected, as a `## Learned rules (approved)` block capped at 8 lines.
The Agents page (`/agents`) has a Lessons tab for the approve/reject queue.

## Tuning

`config.yaml`:

- `llm.roles.research` / `.triage` — model per role; a faster model here changes
  cost and latency the most.
- `heartbeat_stale_days`, `heartbeat_max_nonfocus_per_run`, `news_change_detection`
  — how much automatic work happens at all.
- `match_escalation_min_relevance` — how strong a signal must be before a match
  re-run is triggered.
- `intake_deadline_min` (default 25) and `intake_absolute_cap_min` (default 90):
  how long a new client's intake collects before writing a partial brief, and
  the hard stop after that.
- `AGENT_MAX_CONCURRENT` (compose env) — parallel runs; raise only with RAM to spare.

Slow or expensive runs are almost always a model choice, not the harness — see
[troubleshooting.md](troubleshooting.md).
