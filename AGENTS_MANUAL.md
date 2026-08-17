# Buzzowl — Agent Operations Manual

Everything you need to run, test, and observe the research agents.

---

## Overview — two agent systems

| System | Where it runs | How to trigger | Live view |
|---|---|---|---|
| **Existing Python agent** | Host (inside `server.py`) | Dashboard at `:8000/agents` | `/agents` page WebSocket |
| **Candidate A — Pi** (TypeScript) | Docker container, port **8001** | `curl` or `watch_run.py` | `watch_run.py` or `docker logs` |
| **Candidate B — Hermes** (Python) | Docker container, port **8002** | `curl` or `watch_run.py` | `watch_run.py` or `docker logs` |

Only run one benchmark candidate at a time (Pi or Hermes, not both).

---

## Prerequisites

```bash
# Docker must be running
docker ps

# Core services (DB + SearXNG) must be up
docker compose up -d

# Verify
curl http://localhost:5432  # postgres (will error but proves port is open)
curl http://localhost:8080  # SearXNG → should return HTML
```

---

## Starting the agents

### Candidate A — Pi (TypeScript, port 8001)

```bash
# Start
docker compose --profile bench up agent-pi -d

# Verify
curl http://localhost:8001/health
# → {"status":"ok","candidate":"pi-typescript","model":"deepseek/deepseek-v4-flash"}

# Stop
docker compose --profile bench stop agent-pi
```

### Candidate B — Hermes (Python, port 8002)

```bash
# Start
docker compose --profile bench up agent-hermes -d

# Verify
curl http://localhost:8002/health
# → {"status":"ok","service":"hermes","model":"deepseek/deepseek-v4-flash"}

# Stop
docker compose --profile bench stop agent-hermes
```

---

## Triggering a research run

### Option A — watch_run.py (trigger + watch in one command)

```bash
# Pi
python scripts/watch_run.py --trigger \
  --task "Research Siemens — 2025 financials, AI strategy, and leadership" \
  --subject Siemens \
  --port 8001

# Hermes
python scripts/watch_run.py --trigger \
  --task "Research Siemens — 2025 financials, AI strategy, and leadership" \
  --subject Siemens \
  --port 8002
```

The script prints every tool call live as it happens:

```
[RUNNING]  Siemens
  22:03:28  🗄  search_kb    siemens 2025 financials
             → (no results)
  22:03:29  🔍 web_search   Siemens AG 2025 annual revenue profit
             → Siemens AG Reports Strong Q4 2025 Results...
  22:03:31  📄 fetch_page   https://www.siemens.com/...
             → Siemens generated revenue of €75.9 billion in fiscal...
  22:03:34  💾 write_document  [finding] Siemens 2025 financial results

[DONE]
Output:
  iterations: 12
  tool_calls_count: 38
  findings_saved: 6
```

### Option B — curl only

```bash
# 1. Trigger
curl -X POST http://localhost:8001/runs \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Research Siemens — 2025 financials and AI strategy",
    "org_id": 1,
    "subject": "Siemens",
    "brain": "openrouter",
    "model": "deepseek/deepseek-v4-flash"
  }'
# → {"run_id": 216, "status": "queued"}

# 2. Poll status + see tool calls so far
curl http://localhost:8001/runs/216 | python3 -m json.tool

# 3. Watch via a poll loop
watch -n 2 "curl -s http://localhost:8001/runs/216 | python3 -c \
  \"import sys,json; d=json.load(sys.stdin); \
  print(d['status'], len(d.get('tool_calls',[])), 'calls'); \
  [print(' ', t['tool'], '-', str(t.get('args',''))[:60]) \
   for t in d.get('tool_calls',[])[-5:]]\""
```

### watch_run.py — all options

```bash
# Watch an existing run (already triggered)
python scripts/watch_run.py 216
python scripts/watch_run.py 216 --port 8002   # Hermes

# Custom poll interval (default: 2s)
python scripts/watch_run.py 216 --interval 1

# Trigger with Ollama instead of OpenRouter
python scripts/watch_run.py --trigger \
  --task "Research Bosch" \
  --subject Bosch \
  --brain ollama \
  --model qwen3.5 \
  --port 8001

# Full options
python scripts/watch_run.py --help
```

---

## Tool call icons

| Icon | Tool | What it does |
|---|---|---|
| 🗄 | `search_kb` | Searches PostgreSQL knowledge base (vector + FTS) |
| 👥 | `search_clients` | Fuzzy-searches client names |
| 👤 | `get_client` | Pulls full client profile + linked documents |
| 🔍 | `web_search` | SearXNG → DuckDuckGo fallback |
| 📄 | `fetch_page` | Fetches + strips a URL to plain text |
| 💾 | `write_document` | Saves finding/report to DB + vault |

---

## Checking output

### Vault files (Obsidian)

Research reports appear in `north-info/research/{subject}/overview.md`.

```bash
# List all research in vault
ls north-info/research/

# Read a report
cat north-info/research/siemens/overview.md
```

### PostgreSQL

```bash
# Open pgweb in browser
open http://localhost:5433

# Or query directly
docker exec buzzowl_db psql -U whisper -d whisper \
  -c "SELECT type, title, created_at FROM documents ORDER BY created_at DESC LIMIT 20;"

# All documents from a specific run
docker exec buzzowl_db psql -U whisper -d whisper \
  -c "SELECT type, title FROM documents WHERE agent_run_id = 216;"

# Agent run history
docker exec buzzowl_db psql -U whisper -d whisper \
  -c "SELECT id, agent_type, status, task, created_at FROM agent_runs ORDER BY id DESC LIMIT 10;"
```

### List all runs via API

```bash
curl http://localhost:8001/runs | python3 -m json.tool   # Pi
curl http://localhost:8002/runs | python3 -m json.tool   # Hermes
```

---

## Raw Docker logs

When you need the full HTTP-level trace (every OpenRouter call, every SearXNG request):

```bash
docker logs buzzowl_agent_pi    -f   # Pi
docker logs whisper_agent_hermes -f  # Hermes

# Last 50 lines
docker logs buzzowl_agent_pi --tail=50
```

---

## Cancelling a run

```bash
# Via API
curl -X POST http://localhost:8001/runs/216/cancel

# Via watch_run.py: just hit Ctrl+C — the run keeps running in Docker,
# but you stop watching. To also cancel the run:
curl -X POST http://localhost:8001/runs/216/cancel
```

---

## Switching between Pi and Hermes

Only one candidate should be running at a time during benchmarking.

```bash
# Stop Pi, start Hermes
docker compose --profile bench stop agent-pi
docker compose --profile bench up agent-hermes -d

# Switch config.yaml to point at Hermes (used by the main server)
# Change: agent_service_url: "http://localhost:8002"

# Switch back
docker compose --profile bench stop agent-hermes
docker compose --profile bench up agent-pi -d
# Change: agent_service_url: "http://localhost:8001"
```

---

## Full quick-start sequence

```bash
# 1. Start infrastructure
docker compose up -d

# 2. Start one candidate
docker compose --profile bench up agent-pi -d

# 3. Confirm it's healthy
curl http://localhost:8001/health

# 4. Trigger a run and watch it live
python scripts/watch_run.py --trigger \
  --task "Research Bosch — 2025 restructuring, financials, and strategy" \
  --subject Bosch \
  --port 8001

# 5. When done, find the report
cat north-info/research/bosch/overview.md

# 6. Stop the candidate
docker compose --profile bench stop agent-pi
```

---

## Where things are saved

| Output | Location | Always written? |
|---|---|---|
| Agent run record | `agent_runs` table in PostgreSQL | Yes |
| Individual findings | `documents` table, `type=finding` | Yes (if LLM calls write_document) |
| Final research report | `documents` table, `type=research` | Yes (auto-saved from final LLM response if not called explicitly) |
| Vault file | `north-info/research/{subject}/overview.md` | Yes, on final report only |

If the DB is offline when a run fires, the run will fail at the first DB write. If SearXNG is offline, web search falls back to DuckDuckGo automatically.
