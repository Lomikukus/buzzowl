# Handoff: Round 2 (intake, jobs, news, playbook, Camofox) — 2026-09-13 to 2026-09-15

Read this first when picking up Buzzowl development. It records what a separate
Claude session (the thesis session) changed, how it was verified, what is open,
and two new work items Konrad asked for. Branch: `test/round2` on GitHub
(`main` on GitHub is untouched). The checkout in `~/Youtube/buzzowl` is on
`test/round2`; local `main` tracks `origin/main` and must not receive this work
until Konrad merges it deliberately (`git merge --ff-only test/round2` on `main`).

## 1. State in one paragraph

Buzzowl runs entirely on Konrad's ChatGPT subscription (`openai-codex` through
the agent-pi bridge; Python-side LLM calls are text-only `llm.acomplete(...,
org_id=...)`; every Pi run goes through `routers/agents.resolve_run_target`).
Round 2 added an intake collection point (one Account Intelligence Brief per
client after osint/research/jobs/news all land), autonomous careers-page
discovery, a news pipeline with dated and sourced signals, per-site navigation
playbooks plus human-approved cross-site lessons, a Camofox fallback for the
Python fetchers, and the docs for all of it. 1278 tests pass
(`pytest -q --ignore=tests/test_search_integration.py --ignore=tests/test_db.py`),
`npx tsc --noEmit -p agent_service_ts/tsconfig.json` is clean.

## 2. What changed, by area (with the files to read)

| Area | Files | What to know |
|---|---|---|
| Intake collection point | `intake.py`, `db.py` (`set_client_intake_path`, `cas_client_intake_brief`, `list_clients_with_open_intake`), `routers/knowledge.py` (`create_client`, `trigger_client_research`, `_auto_generate_brief`, `_rewrite_brief_close_out`, `GET /api/clients/{name}/intake`), `routers/internal.py`, `routers/agents.py` (callback + watcher), `static/client.html` (intake strip) | State in `clients.metadata.intake`, written per path with `jsonb_set`; brief transition by CAS (exactly one brief under concurrent callbacks). Deadline 25 min from the first Pi part seen `running` (agent-pi has 2 slots, queued runs must not start the clock); absolute cap 90 min; sweeper every 60 s rescues lost callbacks. `partial` = written early with parts still open, refreshed exactly once when the last open part lands; failed parts are named as "Not collected", never as "refreshes automatically". A manual `POST /brief` closes an open intake. Scripted endpoints (`/intake`, `POST /brief`, `/jobs/scan`, `/news/scan`) require the exact client name. |
| Jobs / careers page | `routers/pipeline.py` jobs block (`_scan_client_jobs`, `_careers_candidates`, `_discover_careers_url`, `_careers_probe_urls`, `_probe_careers_paths`, `_filter_positions`, `_fetch_posting_title`, `_resolve_site_domain`, `_site_base`) | Discovery cascade: fresh playbook URL → Pi-run candidate → argument → `metadata.careers_url` → homepage harvest → own-domain path probe (`/karriere`, `karriere.<domain>` …) → sitemap → SearXNG last. Only the client's own domain (including a bounded redirect alias stored in `metadata.canonical_domain`) or an anchored ATS host is accepted; `_ATS_HOSTS` has no generic `jobs.` entry on purpose. Junior titles filtered unless IT/management; junior-only boards (≥70 % junior of ≥8 raw titles) are not cached. A URL that yields 0 positions is skipped for 7 days (`careers.last_tried_url/at`); `metadata.careers_url` is only cached with ≥1 position. Signature `_scan_client_jobs(org_id, client, careers_url="", *, run_id=None) -> dict`. |
| News | `routers/pipeline.py` news block (`_searxng_query`, `_news_candidates`, `_newsroom_candidates`, `_extract_date_near`, `_probe_published_date`, `_client_news_scan`, `_market_news_scan`, `_discover_client_sources`), `routers/knowledge.py` `/news/scan`, `routers/agents.py` match context | SearXNG `categories=news`, `time_range`; own-newsroom tier harvests dated items from the client's newsroom pages (container-bounded date extraction); undated results get a page-header date probe (≤5, concurrent); degraded search (majority of engines suspended, 0 written) is reported as `error` so the intake part fails and the brief names it; `warning` when something was still written. Signals carry `published_at`, `source_url`, `relevance_score`, `engine`, `from_news_scan`. `market_news` is Python now (no Pi slot). |
| Playbooks + lessons | `playbook.py`, `routers/lessons.py`, `routers/agents.py` (`enrich_task`, `reflect_on_run` scheduling in callback and watcher), `static/agents.html` (Lessons tab), heartbeat `lessons_review` (`0 7 * * 1`) | `documents.type='site_playbook'`, `doc_id=site-playbook-{domain}`: careers/newsroom URLs, good/failed queries, own-domain `blocked_urls`, `needs_js`, notes, `aliases`, `careers.candidate_urls` (from Pi runs, professional Workday boards ranked first), `junior_board_urls`. Reflection resolves the domain via `resolve_client_exact` (exact name) and rejects hosts belonging to another client. `documents.type='agent_lessons'`: proposed weekly, never auto-approved, admin decides (`POST /api/agents/lessons/{id}/decision`), only approved lessons are injected as `## Learned rules (approved)` into research/osint/pain-point tasks and the jobs/news prompts. |
| Fetch tiers | `routers/pipeline.py` (`_fetch_page_text`, `_fetch_rendered_tier`, `_fetch_page_camofox`, `_FETCH_TIER_LOG`), `routers/agents.py` `GET /api/agents/fetch-log` (admin) | plain GET → browser-service → Camofox (`POST /tabs {url,userId,sessionKey}` → `GET /tabs/{id}/snapshot` → `DELETE`), ≤29 s per Camofox call; Camofox snapshot links are rebuilt as `<a href>` so `_harvest_links` works on JS shells. The ring log (200 entries) shows which tier won per URL. |
| Camofox container | `docker-compose.yml` (camofox service), `scripts/build-browser.sh`, `docs/troubleshooting.md` | Pinned to upstream `jo-inc/camofox-browser` v1.16.0 (`79d425b`) with Camoufox 152.0.4-beta.28. The upstream Dockerfile unzips with `\|\| true`, so a truncated archive still builds and the wrapper answers `/health` while every launch fails; the healthcheck now requires `"browserRunning":true` and the build script verifies `libxul.so`/`properties.json`. `BROWSER_IDLE_TIMEOUT_MS` must be a large number, never 0 (older upstream treated 0 as "shut down now"). |
| Search plumbing | `routers/pipeline.py` `_searxng_results`, `agent_service_ts/src/search.ts`, `tools.ts` (`web_search` category/time_range/language) | `publishedDate` is passed through; only `bing news` answered on the test instance and it returns no date, hence the page-date probe. |
| Docs | `docs/agents.md`, `ARCHITECTURE.md`, `docs/troubleshooting.md`, `config.yaml` (`intake_deadline_min`, `intake_absolute_cap_min`) | Read `docs/agents.md` "Client intake" and "Site playbooks and cross-site lessons". |

Guard tests that must stay green: `tests/test_llm_subscription.py` (no hardcoded
deployment brain, every LLM call carries `org_id`, no bare `_call_brain_sync(`
outside `routers/knowledge.py`, `provider_for_brain(` only inside the resolver).

## 3. How it was verified

Three test drives on Konrad's local test instance (a sibling clone of this repo
running docker compose) with Miele, Trumpf, DATEV, Vorwerk and Festo. Reports
with raw evidence: `docs/handoff/round2/testdrive-1.md`, `-2.md`, `-3.md`. The
plan that the packages were built against: `docs/handoff/round2/plan.md`.
Every package had an adversarial review (measured with mocked HTTP and
in-memory stores) before it was merged.

Results: orchestration, one-brief guarantee, lessons loop and stability passed
in all drives (no tracebacks, no duplicate runs, no subscription rate limits).
Careers pages: Miele 20 positions, Festo 18, DATEV 8, Trumpf 5 (a Workday
student board; the professional board `TRUMPF_Graduates_and_Professionals`
should win once a Pi run fetches it), Vorwerk 0 (`career.vorwerk.de` is found,
its JS portal yields no list). News: 0–4 signals per client because the search
engines CAPTCHA-block the instance's SearXNG; the code now says so instead of
reporting "no news".

## 4. Open items (ordered)

1. **CAPTCHA: let the user solve it (Konrad, 2026-09-15).** Today a degraded
   search only produces an error/warning. Wanted: when a fetch or search hits a
   CAPTCHA, keep the Camofox tab open, tell the user (client page banner plus
   Telegram via `notify_user`), let them solve it in the browser, then resume.
   Facts: camofox ships an interactive mode and a VNC plugin
   (`/app/camofox.config.json` in the container: `"interactive": {"mode": "off"}`,
   `"plugins": {"vnc": {"enabled": false, "resolution": "1920x1080"}}`); Buzzowl
   does not mount that file yet. CAPTCHA detection points: `_fetch_page_camofox`
   (snapshot text: "captcha", "Sind Sie ein Mensch", "unusual traffic",
   "Suspended: CAPTCHA" in SearXNG `unresponsive_engines`), `_client_news_scan`
   (`unresponsive`), agent-pi `search.ts` `fetchPageCamofox`. SearXNG's own
   engine CAPTCHAs (Google, Startpage) cannot be solved by the user because the
   request leaves SearXNG, so the fallback must be a Camofox-driven search on the
   engine's site with the tab exposed. Design first, then build; keep the
   session key per tab and never store what the user types.
2. **Tool calling for the Python enrichment loop on the subscription (Konrad,
   2026-09-15).** `llm.py` raises `LLMError("provider kind 'pi' is text-only (no
   tool calling)")` for `llm.chat`/`llm.achat` with tools (`llm.py:717-720`), so
   `agents/orchestrator.run_orchestrator` and `agents/brain.OpenAICompatibleBrain`
   cannot run on the subscription. agent-pi already has an `enrichment` run
   type with tools (`agent_service_ts/src/agent.ts:671`) and tool calling works
   there on `openai-codex`. Recommended: route `routers/pipeline._trigger_enrichment`
   to agent-pi via `resolve_run_target(org_id, "", "")` like the other run types
   instead of the in-process loop, and keep the Python loop only for providers
   with `kind != "pi"`. Alternative: extend the bridge (`agent_service_ts`
   `/complete`) to a `/chat` endpoint that accepts tool definitions and returns
   tool calls; more work, same result.
3. Vorwerk (`career.vorwerk.de`, JS portal): either a manual `careers_url` or a
   listing tier that renders the portal through Camofox and reads the job cards.
4. Trumpf: student board cached; expect the professional board after the next
   research run (playbook `careers.candidate_urls`).
5. News volume: a second SearXNG with another IP or a news API key.
6. Embeddings still need an API key (full-text search works without).
7. Worktree `~/Youtube/buzzowl-wt-spine` holds 18 lines of stale, uncommitted
   config experiments from an earlier round; untouched.

## 5. Conventions the thesis session followed (and one it did not)

- Nothing was pushed until Konrad asked; then `main` went to `test/round2`.
- Sonnet developers per package in git worktrees, an Opus adversarial review
  before every merge, small commits.
- This repo's rule is "no Claude co-author trailer". The round-2 commits were
  first written with one (the thesis session's harness required it) and the
  trailers were stripped from the whole `origin/main..test/round2` range on
  2026-09-15 at Konrad's request (history rewrite, force-pushed to the test
  branch only). Authorship is Konrad Firley <konrad@codexperiment.de>.
- The local test instance was updated by fetching from the local repo, not by
  pulling from GitHub; the hosted server keeps the commit → GitHub → pull →
  rebuild flow.
- Never commit `.env` or `config.local.yaml`; never print secrets; the
  employer is never named.
