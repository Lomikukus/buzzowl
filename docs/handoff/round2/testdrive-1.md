# WP7 — Round-2 Test Drive, Buzzowl local test instance

Instance: `<test-instance>`, commit `079511a`, org 1 (`test-suite`), admin `jeff` (id 1).
Drive window: **2026-09-14 01:23:03Z → 03:05Z** (1 h 42 min; hard stop 04:23Z not reached).
Evidence: `wp7_evidence/*.txt` in the same directory. All tokens redacted/never printed.

---

## 1. Pass / Fail

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Prerequisites: 6 containers healthy, `AGENT_MAX_CONCURRENT=2`, baseline snapshot | **Pass** | `01_prereq_snapshot.txt`: all 6 up+healthy, agent-pi env `AGENT_MAX_CONCURRENT=2`, 3 clients (OBI/Schwarz IT/Duravit AG), no `metadata.intake` on any, 152 docs, last run id 142 |
| 2 | Create 4 clients via `POST /api/internal/clients`, 30 s apart, 200 + intake with 4 `queued` parts + `brief.status=waiting` | **Pass (with caveat)** | `02_create_clients.txt`: 4× HTTP 200 (ids 4-7). `03_intake_initial.txt`: intake object present, `brief.status=waiting`, `deadline_at=null`. Caveat: the jobs/news parts are dispatched synchronously and were already terminal at first poll (~70 s), so "four `queued` parts" was only observable for the two Pi parts |
| 3 | Poll to completion; log transitions; `deadline_at` set only at `running`; jobs/news finish early; ≤1 refresh | **Pass** | `04_intake_transitions.txt`: all 4 briefs `written` by 01:56:16Z. `deadline_at` stayed `null` while osint/research were `queued` and was stamped exactly at first `running` (Trumpf 01:26:14→dl 01:51:14, DATEV 01:34:39→dl 01:59:39, Vorwerk 01:43:55→dl 02:08:55). jobs/news all terminal within 2-54 s. No client went `partial`, so no refresh was due and none happened |
| 4 | Per-client artifact verification | **Fail** (5 of 8 sub-checks fail) | See table §2 below |
| 5 | 2nd run: jobs tier `playbook`, discovery skipped, `blocked_urls` unchanged/grown | **Fail** (partial) | `09_point5_second_run.txt`: discovery **was** skipped (zero SearXNG careers queries, zero homepage fetch — only `karriere.miele.de/sitemap.xml`), `blocked_urls` unchanged (0). But `tier = "metadata"`, not `playbook`, and the playbook's own `careers.tier` was **overwritten** `searxng` → `metadata` |
| 6a | `POST /api/clients/OBI/trigger-research` → fresh intake, attempt 1, parts queued | **Pass** | `11_point6_existing.txt`: OBI had no `intake` before; after: `trigger="trigger_research"`, `attempt=1`, osint/research `queued`, `deadline_at=null`, brief `waiting` |
| 6b | Heartbeat OSINT run-now for a focus client → legacy path, no intake object | **Not testable / partial** | No focus client exists on the instance (`metadata.is_focus` null for all 7); marking one focus was blocked by the sandbox permission classifier, so I did not change it. Heartbeat 3 (`osint`) run-now: run 183 `done` at 02:04:22 with **0 child runs** (0 focus + 0 stale clients). Negative half confirmed: Schwarz IT and Duravit AG still had **no** `intake` object afterwards; "brief refreshed" could not be exercised |
| 7 | Lessons: ≤5 proposals all `proposed`, list, approve one, injected into next research task | **Pass** | `10_point7_lessons.txt`: 3 proposals, all `proposed`; `GET /api/agents/lessons` groups them; approved `l-eddb1f01` (scope `research`) → `approved`. `15_task_duravit_research.txt` (Pi mirror row of run 187): task ends with `## Learned rules (approved)` + the approved text; both `proposed` lessons absent (grep count 0). Same confirmed on OBI run 180 |
| 8 | Negative: SearXNG down → news part `failed` (SearXNG named), brief written, no crash; restart → news signals written | **Fail** (only the second half) | `19_point8_negative.txt`: with searxng stopped, "Vorwerk Test" (id 8) got `news: failed, error "searxng unreachable"` ✓, `jobs: failed` (same as the healthy run), osint/research completed, brief `written` at 02:50:47 naming both missing parts ✓, no crash, no traceback ✓. After `docker compose start searxng`, `POST /news/scan` returned `{found:0, scored:0, written:0}` → **no news signals written** (SearXNG returns no dated candidates, see D3). Brief was `written` not `partial`, so the "exactly one refresh" branch was N/A. Client deleted afterwards (`DELETE /api/clients?name=Vorwerk%20Test` → ok) |
| 9 | Log watch: tracebacks, "No API key for provider", bridge rate limits, sweeper warnings | **Pass** | `22_point9_errors.txt` / `23_point9_targeted.txt`: over the full 100 min of `server`+`agent-pi` logs — 0 tracebacks, 0 "No API key for provider", 0 rate-limit/429/quota errors, 0 intake-sweeper warnings, 0 agent-pi log lines at level ≥ 40. Only 3 distinct non-routine lines (§3) |
| 10 | Cleanup | **Pass** | Session row deleted (`DELETE 1`), poller stopped, "Vorwerk Test" removed, 4 new clients kept, all 6 containers healthy |

### §2 — Point 4 sub-checks (per client)

| Sub-check | Miele | Trumpf | DATEV | Vorwerk | Verdict |
|---|---|---|---|---|---|
| `metadata.careers_url` set, own domain / ATS | `https://karriere.miele.de/search` ✓ | — | — | — | **Fail** (1/4) |
| `jobs` doc with positions, no junior titles | 19-20 positions, `filtered_out=1`; only "Werkstudent … Field Data Analys" survives, legitimately (IT/data override) ✓ | 0, `last_error="no careers page found"` | 0, same | 0, same | **Fail** (1/4) |
| `metadata.tier` set | jobs doc `tier=searxng` (1st) / `metadata` (2nd) ✓ | `""` | `""` | `""` | **Fail** (1/4); note `clients.metadata.tier` is **never** written — `tier` only lives on the jobs doc and the playbook |
| ≥3 `signal` docs with `from_news_scan=true`, ≤90 d, http url | 0 | 1 (2026-09-01, http ✓) | 1 (2026-09-11, http ✓) | 0 | **Fail** (0/4) |
| exactly ONE brief doc today, contains `## Hiring Signals` | 1 ✓, `## Hiring Signals` ✓ | 1 ✓ ✓ | 1 ✓ ✓ | 1 ✓ ✓ | **Pass** |
| brief `metadata.partial` empty (or intake `refreshed`) | `[]` ✓ | `["jobs (failed: …)"]`, intake `written` | same | same | **Fail** (1/4) — see D4 |
| `match_report` cites ≥1 signal URL + ≥1 finding URL + names an open role | findings ✓, signal ✗, role ✓ | findings ✓, signal ✓ (manilatimes), role ✗ | findings ✓, signal ✓ (borncity), role ✗ | findings ✓, signal ✗, role ✗ | **Fail** (0/4 complete) |
| `site_playbook` `site-playbook-{domain}` with `careers.tier` (+ `newsroom.urls` if found) | doc ✓, tier ✓, newsroom 0 | doc ✓, tier `""`, newsroom 0, 11 `blocked_urls` | doc ✓, tier `""`, 2 newsroom urls | doc ✓, tier `""` | **Partial** — doc always written, `careers.tier` only where the scan succeeded |
| exactly one run each of jobs_scan/news_scan/osint/research/pain_point_research/match_synthesis | 1 each ✓ | 1 each ✓ | 1 each ✓ | 1 each ✓ | **Pass** — every Pi-backed part additionally has a `trigger_type='external_service'` mirror row inserted by agent-pi (`agent_service_ts/src/db.ts:218`); this is by design and already filtered in `db.py:1337`, not a duplicate |

---

## §3 — Distinct error lines observed (redacted)

| Count | Line | Where |
|---|---|---|
| 5 | `INFO:httpx: HTTP Request: POST http://browser-service:3000/fetch "HTTP/1.1 500 Internal Server Error"` | server; body: `{"error":"TimeoutError: page.goto: Timeout 30000ms exceeded"}` (reproduced by hand against `www.trumpf.com/de_DE/karriere/`) |
| 1 | `INFO:whisper.db: Embeddings not configured (no API key for the 'openai' backend at <url>) — vector search disabled, full-text search still works` | server startup — environment, not a regression |
| 1 | `INFO:wk.server: Rate limiting: default 300/minute (enabled=True), 15 of 301 routes exempt` | informational |
| 571 / 373 / 200 | camofox: `camoufox launch attempt failed` / `internal error` / `background browser warm retry failed`, all `ENOENT: no such file or directory, open '/root/.cache/camoufox/properties.json'` | camofox container (not in the server/agent-pi stream) |

No tracebacks, no `No API key for provider`, no ChatGPT-bridge rate-limit/429/quota errors, no intake-sweeper warnings.

---

## §4 — Timeline (all times 2026-09-14 UTC)

| Client | created | 1st Pi part `running` (= `deadline_at` stamped) | jobs | news | brief doc | pain_point | match_report |
|---|---|---|---|---|---|---|---|
| Miele (4) | 01:23:03 | 01:23:08 → dl 01:48:08 | 01:23:57 **done** (20 pos) | 01:23:10 done (0 written) | 01:27:18 `written` | 01:27:18→02:01:25 | 02:17:58 |
| Trumpf (5) | 01:23:33 | 01:26:14 → dl 01:51:14 | 01:23:41 **failed** | 01:23:39 done (1 written) | 01:40:00 `written`, missing=jobs | 01:40:00→02:05:14 | 02:19:25 |
| DATEV (6) | 01:24:03 | 01:34:39 → dl 01:59:39 | 01:24:05 **failed** | 01:24:10 done (1 written) | 01:44:35 `written`, missing=jobs | 01:44:35→02:08:52 | 02:27:18 |
| Vorwerk (7) | 01:24:33 | 01:43:55 → dl 02:08:55 | 01:24:35 **failed** | 01:24:36 done (0 written) | 01:56:16 `written`, missing=jobs | 01:56:16→02:14:15 | 02:28:43 |
| OBI (re-trigger) | 02:01:16 | ~02:01:20 | 02:01:51 done | 02:01:37 done | `written` | — | — |
| Duravit AG (re-trigger) | 02:05:47 | ~02:05:50 | 02:06:11 done | 02:05:49 done | ~02:34:40 `written` | none fired | — |
| Vorwerk Test (8, searxng down) | 02:44:28 | 02:44:34 → dl 03:09:34 | 02:44:28 **failed** | 02:44:48 **failed** (searxng) | 02:50:47 `written`, missing=jobs+news | 02:50:47→02:55:45 | 02:57:06 — client deleted after |

agent-pi FIFO with 2 slots behaved exactly as documented: Trumpf waited 2 min 41 s, DATEV 10 min 36 s, Vorwerk 19 min 22 s before their first slot, and `deadline_at` was stamped only then. Nothing hit the 25-min deadline or the 90-min cap.

---

## §5 — Defects (with owning package)

**D1 — jobs discovery has no own-domain path-probe tier; 3 of 4 clients got no careers page.** *(jobs — `routers/pipeline.py:_careers_candidates` / `_discover_careers_url`)*
The cascade is playbook → `metadata.careers_url` → homepage-link harvest → sitemap probe → SearXNG. On this instance the homepage fetch returns 403 (miele.de) / 503 (trumpf.com) and the browser fallback is dead (D6), the root sitemap yields nothing, and SearXNG `site:` queries return 0 results (D3) — so the whole cascade collapses in 2-8 s with `"no careers page found"`. A cheap tier that probes well-known own-domain paths (`/karriere`, `/career(s)`, `/jobs`, `/de/karriere`, `karriere.<domain>`, `jobs.<domain>`) would have found `trumpf.com/de_DE/karriere/`, `datev.de/…/karriere`, `vorwerk.de/…/karriere`. Evidence that the pages exist: the Pi agents themselves cited `trumpf.wd3.myworkdayjobs.com/TRUMPF_Graduates_and_Professionals` and `miele.wd3.myworkdayjobs.com/…` in their match reports.

**D2 — a careers/ATS URL discovered by a Pi run is never fed back to the playbook.** *(playbook — `playbook.py:reflect_on_run` / `_classify_tool_calls`)*
`reflect_on_run` harvests `blocked_urls`, `good_queries`, `failed_queries` and free-text notes from a run's tool-call log (Trumpf's playbook gained 11 `blocked_urls` this way) but does not harvest a careers/ATS URL into `careers.url`. Trumpf's playbook therefore still has `careers.tier=""` even though the same org's `match_synthesis` run cited Trumpf's Workday careers site minutes later, so the next `jobs_scan` will fail identically.

**D3 — news scan cannot distinguish "no news" from "search backend is broken".** *(news — `routers/pipeline.py:_client_news_scan` / `_news_candidates`)*
`found` was 2/1/1/0 per client and 0 on the post-restart rescan. Probing SearXNG directly (`08_searxng_probe.txt`): almost every engine is suspended — brave "too many requests", duckduckgo timeout, karmasearch/mojeek/qwant "access denied", startpage CAPTCHA, yahoo parsing error — and the news results that do come back carry **no `publishedDate`**, so `_news_candidates`' 90-day freshness filter drops them all. The scan still reports `error: null` and the intake part goes `done`, so the brief closes as complete with zero news. It should treat `unresponsive_engines` / an all-undated result set as a degraded condition and surface it (part `failed` or `error` set), the way the outage path already does.

**D4 — a brief whose only missing part *failed* is permanently stamped "Partial".** *(intake — `intake.py:_finish` → `routers/knowledge.py:_auto_generate_brief(partial_missing=…)`)*
For Trumpf/DATEV/Vorwerk the intake closes as `brief.status = "written"` (correct, per `docs/agents.md`: a part that only fails never triggers a refresh), but `_finish` passes the same `missing` list as `partial_missing`, so the document gets `metadata.partial = ["jobs (failed: …)"]` and the banner *"Partial brief — missing: jobs (failed: no careers page found). It refreshes automatically when the missing parts arrive."* — which is false: nothing will ever refresh it. `20_partial_banner_inconsistency.txt`. Fix: only pass `partial_missing` for parts that are still open, and render failed parts as a "could not be collected" note instead.

**D5 — the second jobs scan never reaches the playbook tier and downgrades the recorded tier.** *(jobs/playbook — `routers/pipeline.py:_scan_client_jobs`)*
```python
url  = (careers_url or meta.get("careers_url") or "").strip()
tier = "metadata" if url else ""
if not url:
    url, tier = await _discover_careers_url(org_id, client, pb)
```
Once run 1 has written `clients.metadata.careers_url`, every later scan short-circuits here, labels the tier `metadata`, and `_record_playbook` writes that back — Miele's playbook `careers.tier` went `searxng` → `metadata`. The `playbook` tier is therefore unobservable after the first successful scan, even though the freshness short-circuit in `_careers_candidates` exists precisely for it. (Behaviourally the intent held: discovery really was skipped, zero SearXNG/homepage calls.)

**D6 — camofox is non-functional and the hardened-fetch fallback is silently degraded; camofox still reports healthy.** *(infrastructure / browser-service)*
camofox cannot launch a browser at all: `ENOENT … '/root/.cache/camoufox/properties.json'`, 571 failed launch attempts in 100 min, every request answered 500 — yet `docker compose ps` shows it `healthy`. `browser-service` answers `POST /fetch` with a 30 s `page.goto` timeout on bot-protected sites. Every fetch therefore falls back to plain httpx, which the target sites answer with 403/503. This is the common root cause behind D1 and most of the empty jobs/news results; it is an environment defect, but the healthcheck hiding it is a real one.

**D7 — the Python jobs/news scanners never contribute `blocked_urls`.** *(playbook — `playbook.py:record` vs `reflect_on_run`)*
`blocked_urls` is only ever produced by `reflect_on_run` from a Pi tool-call log (line ~449-499). The Python newsroom probe hit ~12 own-domain 403s on `www.miele.de/{news,newsroom,presse,press,…}` in one pass and `site-playbook-miele.de.blocked_urls` is still `[]`, so the next scan repeats all 12 requests. `playbook.record()` should accept blocked/failed own-domain URLs from the scanners.

**D8 (cosmetic) — `GET /api/clients/{name}/intake` on a client that never had an intake reports four `queued` parts.** *(intake — `intake.py:summary`)*
`summary()` fills missing parts with `_empty_part()` (`status: "queued"`), so OBI, before its re-trigger, returned `osint/research/jobs/news = queued`, `brief = waiting`, `percent = 0`. Only `active: false` distinguishes it; the client-page intake strip has to rely on that flag alone.

**D9 (minor) — `{name}` client lookup is fuzzy.** `GET /api/clients/Vorwerk%20Test/intake` returned **Vorwerk**'s intake verbatim while no "Vorwerk Test" client existed (`db.get_client` falls back to a fuzzy match). Harmless for the UI, misleading for scripted checks.

**D10 (observation) — position titles come from URL slugs and are truncated mid-word.** `"IT Solution Architect Customer Serv"`, `"Werkstudent (w/m/d) Field Data Analys"`, `"PMO und Managementassistenz IT (w/m/d)"` — the sitemap-derived titles are cut at the slug length. *(jobs — `routers/pipeline.py:_sitemap_job_urls` / `_extract_jobs`)*

---

## §6 — What worked well

- Intake state machine: correct `queued → running → done/failed` ordering, `deadline_at` stamped only at first real `running`, single-flight brief CAS, no double briefs, no stuck `writing`, sweeper ran every 60 s without a single warning.
- Exactly one server-side run per part per client across 7 client intakes; the agent-pi FIFO (2 slots) held and was visible in the timing.
- Failure isolation: a failed jobs part never blocked the brief, never crashed the server, and was named in `brief.missing`.
- Playbook capture from Pi runs: notes, `good_queries`, `failed_queries`, `blocked_urls` all populated and plausible.
- Lessons loop end-to-end: proposal → human approval → injection as `## Learned rules (approved)` in the very next research task, with `proposed` lessons correctly excluded.
- SearXNG outage handling: clean `"searxng unreachable"` on the news part, brief still produced, zero tracebacks.
