# Agent Memory Model — Buzzowl Deep Research Engine

Two separate questions answered here:

1. **What does each agent know?** (mission awareness)
2. **How is the LLM model loaded and when is context freed?** (memory lifecycle)

---

## Part 1 — What Each Agent Knows

The research engine uses a deliberate **three-tier architecture**. Each tier has a different scope of knowledge.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         KNOWLEDGE SCOPE                                  │
│                                                                          │
│  TIER 1 — ORCHESTRATOR                                                   │
│  ┌─────────────────────────────────────────────────────────────────┐     │
│  │  Knows: everything already in the KB for this subject           │     │
│  │  • All existing documents, OSINT reports, findings              │     │
│  │  • Client profile and metadata                                  │     │
│  │  • What is missing, stale (>14 days), or shallow                │     │
│  │  • The overarching goal: "produce a complete profile of Bosch"  │     │
│  │                                                                 │     │
│  │  Does NOT know: what workers are doing, real-time results       │     │
│  └─────────────────────────────────────────────────────────────────┘     │
│                              │ spawns                                    │
│                              ▼                                           │
│  TIER 2 — WORKERS (agnostic)                                             │
│  ┌─────────────────────────────────────────────────────────────────┐     │
│  │  Knows: subject + subject_type + their one task                 │     │
│  │  • subject = "Bosch"                                            │     │
│  │  • task_type = "web_search"                                     │     │
│  │  • payload = { query: "Bosch CEO LinkedIn 2026" }               │     │
│  │                                                                 │     │
│  │  Does NOT know:                                                 │     │
│  │  • Why this task was created (what gap it fills)                │     │
│  │  • What other workers are doing in parallel                     │     │
│  │  • What has already been found                                  │     │
│  │  • What the final output will look like                         │     │
│  └─────────────────────────────────────────────────────────────────┘     │
│                              │ findings written to DB                    │
│                              ▼                                           │
│  TIER 3 — AGGREGATORS                                                    │
│  ┌─────────────────────────────────────────────────────────────────┐     │
│  │  Knows: all findings for this subject that scored ≥ 3           │     │
│  │  • Reads documents WHERE type='finding' AND subject='Bosch'     │     │
│  │  • Ranks by relevance score                                     │     │
│  │  • The final goal: write a 7-section synthesis                  │     │
│  │                                                                 │     │
│  │  Does NOT know: individual task payloads or worker decisions    │     │
│  └─────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why workers are intentionally agnostic

| If workers knew everything | With agnostic workers |
|---|---|
| Must read shared state before starting | Start immediately, no coordination |
| Risk of duplicate searches (worker A and B both think they should search LinkedIn) | DB deduplication handles conflicts at write time (`ON CONFLICT` + sha256 doc_id) |
| Slower — serialised reads slow parallel execution | N workers claim tasks in parallel with zero contention |
| Complex to test | Simple, deterministic, testable in isolation |

The orchestrator is the one strategic thinker. It reads everything once, plans all gaps, then releases workers to execute independently. The aggregator reads everything once at the end and synthesises. The workers in between are intentionally blind — they do one thing well.

---

## Part 2 — Model Loading and Context Lifecycle

### The key distinction

Two completely separate things often get conflated:

```
┌───────────────────────────────────┐   ┌──────────────────────────────────┐
│    MODEL in Ollama memory         │   │  CONTEXT per request             │
│                                   │   │                                  │
│  • Loaded once on first request   │   │  • Built fresh for each call     │
│  • Stays loaded for keepalive     │   │  • Sent in the HTTP request body │
│    window (default: 5 min)        │   │  • Processed by the model        │
│  • Shared across ALL workers      │   │  • Returned as response          │
│  • Ollama manages this            │   │  • Freed after response returns  │
│  • ~4-8 GB RAM for qwen3.5        │   │  • Python GC handles this        │
└───────────────────────────────────┘   └──────────────────────────────────┘
          persistent                              ephemeral
     (during active use)                     (one request only)
```

### Ollama as a shared inference daemon

```
┌────────────────────────────────────────────────────────────────────────┐
│                         OLLAMA DAEMON                                   │
│                     (separate OS process)                               │
│                                                                         │
│  ┌──────────────────────────────────────────────────┐                  │
│  │              MODEL IN RAM                         │                  │
│  │   qwen3.5 (~4-8 GB)                              │                  │
│  │                                                  │                  │
│  │   Loaded on: first request                       │                  │
│  │   Kept until: 5 min of inactivity               │                  │
│  │   (with 4 workers running → always loaded)       │                  │
│  └──────────────────────────────────────────────────┘                  │
│                                                                         │
│  ← HTTP /api/chat ──  Worker 1  (web_search for Bosch CEO)             │
│  ← HTTP /api/chat ──  Worker 2  (fetch_url reuters.com/bosch)          │
│  ← HTTP /api/chat ──  Worker 3  (analyze page content)                 │
│  ← HTTP /api/chat ──  Worker 4  (web_search for Bosch revenue)         │
│                                                                         │
│  Requests are queued and processed sequentially inside Ollama          │
│  (one inference at a time on CPU — no GPU parallelism here)            │
└────────────────────────────────────────────────────────────────────────┘
```

---

### Worker agent — context lifecycle (think=False)

Workers make a **single HTTP call** and discard everything after.

```
WORKER claims task: { subject: "Bosch", task: fetch_url, url: "reuters.com/bosch-q1" }
│
├─ Step 1: Fetch the page
│   Python RAM: page_content = "Bosch Q1 2026 revenue €21.4bn..." (≤ 1000 chars)
│
├─ Step 2: Build prompt string
│   Python RAM: prompt = system_prompt + page_content + extraction_instructions
│   (lives in memory as a plain Python string, ~2-4 KB)
│
├─ Step 3: POST /api/chat to Ollama
│   │  Request body: { messages: [{role, content}], think: false, num_ctx: 16384 }
│   │
│   │  ┌─ INSIDE OLLAMA (separate process) ──────────────────────────────────┐
│   │  │  Model allocated a 16k token context window for this request        │
│   │  │  Prompt tokenised → processed → response generated                  │
│   │  │  Context window freed when response is complete                     │
│   │  └──────────────────────────────────────────────────────────────────── ┘
│   │
│   └─ Response: { content: '{"relevance": 5, "facts": [...]}' }
│
├─ Step 4: Parse response, write finding to DB
│   DB row: { type: "finding", source_url: "...", content: "...", relevance_score: 5 }
│   Vault file written if score ≥ 4
│
└─ Step 5: Mark task complete, return
    Python RAM: page_content, prompt, response → all garbage collected ♻️
    Context window in Ollama: freed at end of step 3 ♻️
    Model in Ollama: STILL LOADED (keepalive timer resets on each request)
```

**Memory at peak for one worker:**
- Python: ~2-4 KB (prompt string + response)
- Ollama: 16k token context window (~16-64 MB transient) + model weight (~4-8 GB persistent)

---

### Orchestrator — context lifecycle (think=True, 32k ctx)

The orchestrator uses the **Agent loop** (`base.py`) — multiple turns within one run. Context grows across turns.

```
ORCHESTRATOR starts: { subject: "Bosch", task: orchestrate }
│
├─ Step 1: Observe — load KB context
│   search_kb("Bosch") → list of existing documents
│   get_client("Bosch") → client profile
│   Python RAM: self.memory = [
│     { role: "system", content: ORCHESTRATOR_INSTRUCTIONS },
│     { role: "user",   content: all existing KB docs for Bosch (up to 32k tokens) }
│   ]
│
├─ Step 2: Turn 1 — POST /api/chat (think=True, num_ctx=32768)
│   │
│   │  ┌─ INSIDE OLLAMA ────────────────────────────────────────────────────┐
│   │  │  32k context window allocated                                       │
│   │  │                                                                     │
│   │  │  <think> (hidden reasoning — NOT returned to Python)               │
│   │  │    "Existing OSINT has CEO Volkmar Denner but no CFO..."           │
│   │  │    "Revenue from 2024, may be stale..."                            │
│   │  │    "No LinkedIn activity found..."                                 │
│   │  │    → I should call enqueue_task for: CFO search, 2026 earnings,   │
│   │  │       Bosch LinkedIn company page, recent news                     │
│   │  │  </think>                                                          │
│   │  │                                                                     │
│   │  │  Output: tool_call { name: "enqueue_task", args: { ... } }        │
│   │  │  Context window: STILL ALLOCATED (turn not complete)               │
│   │  └────────────────────────────────────────────────────────────────── ┘
│   │
│   Response: tool_call → Python executes enqueue_task → writes to DB
│   self.memory grows: + assistant message + tool_result appended
│
├─ Step 3: Turn 2 — POST /api/chat (same memory, now longer)
│   self.memory now contains: system + user context + assistant turn 1 + tool result
│   Full conversation sent again (standard LLM stateless pattern)
│   │
│   │  ┌─ INSIDE OLLAMA ────────────────────────────────────────────────────┐
│   │  │  32k context window re-allocated for this request                  │
│   │  │  Previous context is gone — this is a FRESH allocation             │
│   │  │  But Python sent the full history in the request body              │
│   │  │  → Ollama "remembers" via the messages array, not via persistence  │
│   │  └────────────────────────────────────────────────────────────────── ┘
│
├─ ... (up to MAX_ITERATIONS = 10 turns)
│
└─ Final: orchestrator writes agent_run row to DB, returns
    Python RAM: self.memory (the full conversation) → garbage collected ♻️
    Ollama: model still loaded, context freed after each request ♻️
```

**Memory at peak for orchestrator:**
- Python: ~50-200 KB (`self.memory` list with full conversation history)
- Ollama: 32k context window (~64-128 MB transient) + model weight (~4-8 GB persistent)

---

### Full system memory map — 4 workers + 1 orchestrator running

```
MACHINE RAM (example: 32 GB M-series Mac)
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Ollama daemon                                           ~6 GB    │   │
│  │  ┌──────────────────────────────────────┐                        │   │
│  │  │  qwen3.5 model weights (persistent)  │  ~4-8 GB               │   │
│  │  └──────────────────────────────────────┘                        │   │
│  │  ┌──────────────────────────────────────┐                        │   │
│  │  │  Active context window               │  ~64-128 MB transient  │   │
│  │  │  (one at a time — CPU serial)        │  (freed per request)   │   │
│  │  └──────────────────────────────────────┘                        │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI server (python server.py)                      ~300 MB   │   │
│  │                                                                   │   │
│  │  ┌──────────────────────────────────────────────────────────┐    │   │
│  │  │  4 worker asyncio tasks (tiny — just await DB + HTTP)    │    │   │
│  │  │  Each: ~2-4 KB context string while active              │    │   │
│  │  └──────────────────────────────────────────────────────────┘    │   │
│  │                                                                   │   │
│  │  ┌──────────────────────────────────────────────────────────┐    │   │
│  │  │  Orchestrator (if running)                               │    │   │
│  │  │  self.memory: ~50-200 KB conversation history           │    │   │
│  │  └──────────────────────────────────────────────────────────┘    │   │
│  │                                                                   │   │
│  │  asyncpg connection pool: ~20 MB                                  │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  PostgreSQL (Docker)                                    ~200 MB   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  SearXNG (Docker)                                       ~100 MB   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  Remaining free RAM: ~25 GB                                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### Is context preserved between agent runs?

**No.** The short answer:

```
Run 1: Research Bosch             Run 2: Research SAP (next day)
  Orchestrator reads KB ──────►     Orchestrator reads KB ──────►
  Builds self.memory               Builds self.memory
  Makes LLM calls                  Makes LLM calls (no memory of Run 1)
  Writes to DB ──────────────────► Reads DB (finds Run 1's results!)
  self.memory discarded ♻️         self.memory discarded ♻️
```

**What IS preserved:** the findings in PostgreSQL and the vault files. That's the system's long-term memory. The agent's `self.memory` is ephemeral working memory — it only lives for the duration of one run.

**What is NOT preserved:** the LLM's internal reasoning, the conversation history, intermediate thoughts.

The DB is the brain. The LLM is the reasoning engine that reads and writes to it.

---

### The context window is NOT a cache

A common misconception:

```
WRONG mental model:
  Agent sends prompt → Ollama stores conversation → next agent picks up where it left off

CORRECT mental model:
  Agent sends full conversation in request body → Ollama processes → returns response → forgets
  Next request must re-send everything needed (or read it from the DB first)

This is standard stateless LLM API behavior.
Ollama is a stateless inference server.
State lives in the DB and vault, not in the model.
```

---

### Ollama keepalive — when is the model actually unloaded?

```
Timeline with 4 workers running research on Bosch:

t=0:00   First request → Ollama loads qwen3.5 (~30s first load)
t=0:30   Worker 1 response returned, context freed
t=0:31   Worker 2 sends request (model already loaded, ~0s)
t=1:15   Worker 3 sends request
t=2:40   Worker 4 sends request
...
t=45:00  All tasks complete, no new requests
t=50:00  Keepalive expires → qwen3.5 unloaded from RAM ♻️
t=50:01  Next request → Ollama loads qwen3.5 again (~30s)

With active research running, the model stays warm the whole time.
Cost of a cold load is paid once per session, not per agent.
```

---

### Summary table

| | Orchestrator | Worker | Aggregator |
|---|---|---|---|
| **Knows the mission** | Yes — reads all KB context | Subject name only | Yes — reads all findings |
| **Context per run** | Growing multi-turn memory (self.memory) | Single-request prompt | Single-request prompt |
| **Context preserved after run** | No — discarded | No — discarded | No — discarded |
| **LLM calls per task** | 2-10 (multi-turn loop) | 1 (single call) | 1 (single call) |
| **think=True** | Yes (open-ended planning) | No (structured extraction) | No (structured synthesis) |
| **Context window** | 32k tokens | 16k tokens | 16k tokens |
| **Peak Python RAM** | ~50-200 KB | ~2-4 KB | ~50-100 KB |
| **Ollama context (transient)** | ~64-128 MB | ~32-64 MB | ~32-64 MB |
| **Long-term memory** | PostgreSQL + vault | PostgreSQL + vault | PostgreSQL + vault |
