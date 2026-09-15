# WP7b — Targeted second test drive (after round-2b fixes)

Instance: `<test-instance>`, server image `15743d4`, deployed main `3ffeabd`, org 1, admin id 1.
Drive window: **2026-09-14 05:22:31Z → 05:58:16Z** (36 min; hard stop 07:52Z never approached).
Evidence: `wp7b_evidence/*.txt`. No token was printed, echoed or written to any file (verified with a grep sweep).
Session row created (id 3) and deleted at the end (`DELETE 1`). Festo and all rescans left in place.

---

## 1. Pass / Fail

| # | Check | Result | Evidence (one line) |
|---|---|---|---|
| 1 | Prerequisites: 6 containers healthy, camofox `browserRunning:true`, baseline snapshot of the 4 clients | **Pass** | `01_prereq_snapshot.txt`: all 6 up+healthy; `{"ok":true,"engine":"camoufox","browserConnected":true,"browserRunning":true,"consecutiveFailures":0}`; baseline in `01b_baseline_docs.txt` (Trumpf/DATEV/Vorwerk: no careers_url, 0 positions, `last_error="no careers page found"`) |
| 2 | Jobs rescans: own-domain/ATS careers URL for Trumpf/DATEV/Vorwerk, ≥5 positions, Miele tier `playbook` with no discovery, sane tiers, own-domain-only `blocked_urls` | **Fail (2 of 4 clients)** | `02_jobs_scans.txt`: DATEV ✓ (Workday ATS, 10 pos), Miele ✓ (tier `playbook`, 20 pos, zero discovery calls), Trumpf ✗ (correct careers URL but **0 positions**), Vorwerk ✗ (`no careers page found`). `title_source` present on every position. No Impressum/consent page was ever accepted. **The `path-probe` tier never issued a single request in any scan** — see D11 |
| 3 | News rescans: `written ≥ 3` for ≥3 of 4, fresh dated http signals, right company, degraded search named | **Fail (1 of 4 reached ≥3)** | `08_news_scans.txt`: Trumpf 4, Miele 2, DATEV 1, Vorwerk 0 (vs. first drive 1/0/1/0). All 7 written signals are ≤12 d old, https `source_url`, correct company, `from_news_scan:true`. Namesakes (THW-Kiel "Trumpf", Friedrich Vorwerk SE, the village of Vorwerk, lawyer "Miele") were fetched, scored and **correctly rejected**. `unresponsive` engines named in all 4 responses — not silent. No scan tripped `error`, because `degraded` requires *zero* candidates and candidates were always present (by design, `_client_news_scan`). `engine` is **not** persisted on the signal doc — see D14 |
| 4 | Fresh full intake for Festo: all parts `done`, brief with `## Hiring Signals`, match report citing signal + finding URLs, one run per part, playbook written | **Pass with 2 defects** | `07_festo.txt`/`11_festo_poll.txt`: created 05:31:27, all 4 parts `done`, brief `written` 05:41:06 (9 min 39 s), `percent:100`, `missing:[]`, `metadata.partial:[]`. Jobs `done`, 20 positions, tier `searxng`. Brief has `## Hiring Signals` with real roles. Match report cites 11 URLs incl. a careers URL and 4 `press.festo.com` URLs. Exactly one server run per part (212–215) + the known agent-pi mirror rows. Defects: **news part `done` with 0 signals and no warning**, and the **match report was stored as `type='research'`, not `match_report`** (D15) |
| 5 | Fetch tiers: count per tier, ≥1 camofox win on a 403/503 site, no fetch > ~30 s | **Partial** | `18_fetchlog_final.txt`: 8 entries — `none` 4, `browser` 2, `http` 1, `camofox` 1. The camofox win is `datev.wd3.myworkdayjobs.com/.../login` (907 chars), an ATS host, **not** Trumpf/Miele: camofox was never attempted against Trumpf's 503-ing own domain. Longest single fetch ≈28 s (Trumpf careers via browser-service, 05:24:03 → 05:24:31); nothing over 30 s. The ring log only covers `_fetch_rendered_tier` callers — the ~60 plain GETs of this drive (sitemaps, newsroom probes, posting pages) are invisible in it |
| 6 | Logs: tracebacks, `No API key`, 429/rate-limit, sweeper warnings, camofox launch failures | **Pass** | `21_log_summary.txt` over 36 min of `server`+`agent-pi` (2 055 lines): **0** tracebacks, **0** `No API key`, **0** bridge rate-limit/quota, **0** sweeper warnings, **0** agent-pi lines at level ≥ 40. **0** camofox launch failures (vs. 571 in the first drive); 68 tabs created, all successful. Distinct non-routine lines in §4 |

Cleanup (point 7): session row deleted, Festo (id 9) and all rescans kept, all 6 containers healthy at the end (`23_final_state.txt`).

---

## 2. Per-client before / after

| Client | careers_url before | careers_url after (tier) | positions before → after | news signals written 1st drive → now | newsroom tier fired | camofox used |
|---|---|---|---|---|---|---|
| **Miele** (4) | `karriere.miele.de/search` | same, **tier `playbook`** (zero discovery HTTP, zero SearXNG) | 19 → **20** | 0 → **2** | no (no newsroom URLs) | no |
| **Trumpf** (5) | — (`no careers page found`) | `www.trumpf.com/de_INT/karriere/stellenangebote/` (tier `searxng`) | 0 → **0** (`no positions found on careers page`; browser tier returned 1 377 chars = SPA shell) | 1 → **4** | no | no — the only camofox call for trumpf.com was the bare-domain 400 |
| **DATEV** (6) | — | `datev.wd3.myworkdayjobs.com/de-DE/Datev_Careers` (jobs doc `searxng`, playbook `listing-link`) | 0 → **10** | 1 → **1** | **yes** (both playbook URLs fetched — one of them is DATEV's *Impressum*, D13) | **yes** (1 win, the Workday `/login` page) |
| **Vorwerk** (7) | — | — (still `no careers page found`) | 0 → **0** | 0 → **0** (4 candidates, all namesakes, max_relevance 0) | no | no |
| **Festo** (9, new) | n/a | `jobs.festo.com/viewalljobs/?locale=de_DE` (tier `searxng`, `discovered_tier: searxng`) | n/a → **20** | n/a → **0** (5 candidates, max_relevance 0) | probed 13 own paths; `/presse` + `/press` returned 200 but failed the content test, so no `newsroom.urls` | no (server side); Pi used it 67× |

Playbook end state (`23_final_state.txt`): `careers.candidate_url` (pi-run tier) is **empty on every playbook** — that path was never exercised, see D12. `blocked_urls` grew only for Festo (+2, own-domain `sitemap.xml` `no_content`, from the Pi run) — the scanner-side `blocked_urls` path (D7's fix) never fired because the path-probe never ran.

---

## 3. Festo timeline (2026-09-14 UTC)

| Time | Event |
|---|---|
| 05:31:27 | `POST /api/internal/clients` → id 9; runs 212 osint, 213 research, 214 jobs_scan, 215 news_scan created; intake `active`, `deadline_at` 05:56:32 |
| 05:31:27–28 | Newsroom discovery probes 13 own-domain paths (`/news` 404, `/presse` 200, `/press` 200, …). One bare-domain fetch fails: browser-service 500 + camofox 400 `Invalid URL: festo.com` |
| 05:31:46 | **news** `done` — found 5, scored 5, **written 0**, max_relevance 0, `error:null`, 1 unresponsive engine |
| 05:31:48 | Jobs discovery: SearXNG query (no path-probe requests at all) → `jobs.festo.com` |
| 05:32:33 | 8 posting pages fetched for real titles (`title_source: page`); the other 12 stay slug-derived |
| 05:32:47 | **jobs** `done` — 20 positions, 6 `jobs-need-9-*` findings, `site-playbook-festo.com` created |
| 05:36:14 | **osint** `done` (run 212) |
| 05:40:17 | **research** `done` (run 213); playbook gains notes, 15 good_queries, 13 failed_queries, `needs_js:true`, 2 blocked_urls |
| 05:40:55 | Brief `writing`; `_maybe_trigger_pain_point_research` fires run 218 |
| 05:41:06 | Brief **`written`**, `percent:100`, `missing:[]`, `partial:[]` — 9 min 39 s end to end |
| 05:49:27 | pain_point_research `done` → match_synthesis runs 220/221 |
| 05:50:40 | Match document written (doc id 473, title `Match: Festo — 2026-09-14 [Pi]`) — but with `type='research'` |
| 05:50:59 | match_synthesis `done`; `_handle_match_synthesis_callback: match_status=done` |

---

## 4. Distinct non-routine log lines (redacted)

| Count | Line | Where / meaning |
|---|---|---|
| 4 | `{"level":"error","msg":"tab create failed","error":"Invalid URL: trumpf.com"}` (also `datev.de`, `vorwerk.de`, `festo.com`), each followed by `internal error` + `res status:400` | camofox. Exactly one per scan that reached the homepage-harvest tier. **Root cause of D11** |
| 4 | `POST http://browser-service:3000/fetch "HTTP/1.1 500 Internal Server Error"` | server. Same four bare-domain calls, one tier earlier |
| 3 | `{"level":"warn","msg":"orphan page reaper closed leaked pages","reaped":1}` | camofox. Housekeeping, one leaked page per sweep |
| 6 | `{"level":"warn","msg":"ariaSnapshot failed, retrying"}` / `ariaSnapshot retry failed, returning empty refs` / `getAriaSnapshot failed` — `locator.ariaSnapshot: Timeout 5000ms exceeded` | camofox, 05:35–05:36, during a Pi run. Snapshot degraded to empty refs; the run still completed |
| 1 | `GET https://www.trumpf.com/index.php?id=21&type=1533906435 "HTTP/1.1 503"` | server. trumpf.com's sitemap redirect target; bot-protection |
| 1 | `GET https://www.yahoo.com/lifestyle/articles/... "HTTP/1.1 429 Too Many Requests"` | server. One external article fetch during Festo's news scan; not a bridge/LLM rate limit |
| ~25 | `WARNING:searx.engines.{qwant,yahoo,duckduckgo,wikipedia}: ...CaptchaException / ParserError / engine timeout`, 1× `ERROR:searx: call to ResultContainer.add_unresponsive_engine after ResultContainer.close` | searxng container. Environment; surfaced to callers as `unresponsive` |
| 1 | `HTTP/1.1" 401` on `GET /api/agents/fetch-log` | my own deliberate unauthenticated probe — the admin gate works |

No tracebacks, no `No API key for provider`, no bridge 429/quota, no intake-sweeper warnings, no camofox launch failures.

---

## 5. Round-2b verdict on D1–D10

| Defect | Status |
|---|---|
| D1 own-domain path-probe tier | **Code shipped but dead at runtime** — see D11. Trumpf/DATEV got careers URLs from SearXNG/listing-link, not path-probe; Vorwerk still gets nothing |
| D2 Pi-run careers URL → playbook | **Not exercised** — `candidate_url` empty on all 7 playbooks; see D12 |
| D3 news "no news" vs. "backend broken" | **Fixed as specified** — `unresponsive` is reported on every response; `error`/`warning` fire only when candidates are zero, which never happened. Yield up from 2 to 7 signals |
| D4 false "Partial" banner | Not deployed (WP10). Still visible on the first drive's briefs (`22_wp10_notes.txt`) |
| D5 second scan downgrades tier | **Fixed** — Miele's second scan reports tier `playbook` and its playbook keeps `metadata` instead of being overwritten. Side effect: `discovered_tier` is only ever written for a genuine discovery, so Miele/OBI/Duravit have none |
| D6 camofox non-functional | **Fixed** — 0 launch failures, `browserRunning:true`, 68 tabs created, 1 server-side camofox win |
| D7 scanners contribute `blocked_urls` | **Code shipped, not exercised** — the only producer is the path-probe (dead, D11); own-domain 403s from the newsroom probe are still not recorded |
| D8 fake `queued` parts | Not deployed (WP10). Still reproducible on Schwarz IT |
| D9 fuzzy client lookup | Not deployed (WP10). Still reproducible: `/api/clients/Vorwerk%20Test/intake` and `/Miele%20AG/intake` return Vorwerk's / Miele's intake |
| D10 truncated slug titles | **Partially fixed** — `title_source` is now on every position, but only the first 8 postings get a `page` title; the rest keep truncated slugs (`Business Process Manager Central Lo`, `DMZ Operations Specia`, `Global Contact Center Subject Matte`) |

---

## 6. Remaining defects

**D11 (blocker for D1/D7) — the whole `path-probe` tier is a no-op whenever `metadata.website` has no scheme.**
*`routers/pipeline.py:_careers_candidates` (the `website = (meta.get("website") or "").strip()` line, ~3477) → `_careers_probe_urls` (3166) and `_fetch_page_raw` (3077).*
`_careers_probe_urls` starts with `p = urlparse(website); if not p.netloc: return []`. `urlparse("trumpf.com")` yields `netloc=''` (the domain lands in `path`), so the function returns an empty list and **not one probe is ever issued**. All four clients created via `POST /api/internal/clients` store `metadata.website` as a bare domain (`trumpf.com`, `datev.de`, `vorwerk.de`, `miele.de`, `festo.com`) — `internal.py:internal_create_client` stores `metadata` verbatim. Proof: zero `GET https://www.trumpf.com/karriere`-style lines anywhere in `03b_jobs_ts.txt` / `15_festo_scan_log.txt`, and camofox logging `Invalid URL: trumpf.com` / `datev.de` / `vorwerk.de` / `festo.com` (`19_camofox_log.txt`) — the same bare string is also handed to `_fetch_page_raw`, which burns a browser-service 500 + a camofox 400 on every scan and files a `tier:"none", chars:0` entry in the fetch log.
The news side already gets this right (`routers/pipeline.py:1128`, `:1176`: `base = website if website.startswith("http") else f"https://{website}"`) — which is why Festo's 13 newsroom probes did run. Fix: normalise once in `_careers_candidates` (or on write in `internal_create_client`), the same way `_probe_newsroom_paths` does. This single line is the reason Vorwerk still has no careers page, Trumpf had to fall through to SearXNG, and D7's scanner `blocked_urls` never fires.

**D12 — a Pi run's careers/ATS URL is still not picked up when the careers site lives on a `jobs.<domain>` subdomain.**
*`playbook.py:classify_tool_calls` (~530), `_CAREERS_CANDIDATE_PATH_RE` at `playbook.py:430`.*
The careers-candidate test is `is_ats or (own_domain and _CAREERS_CANDIDATE_PATH_RE.search(urlparse(url).path))` — it matches on the **path only**. Festo's research run cleanly fetched `https://jobs.festo.com/job/Bangalore%2C-Karnataka-System-Engineer-Kubernetes-Platform-Engi/1409093733/`: own-domain ✓, but the path is `/job/...` (no "jobs"/"karriere"), so no candidate was recorded and `careers.candidate_url` stayed empty on every playbook in the org. The host carries the signal here — `jobs.`/`karriere.`/`careers.<domain>` are exactly the subdomains `_careers_probe_urls` already probes, so fold the host into the same check.

**D13 — an Impressum URL sits in `playbook.newsroom.urls` and is re-fetched on every news scan.**
*`routers/pipeline.py:_discover_sources_for_new_client` (~1303) / `_probe_newsroom_paths` acceptance test (~1121).*
`site-playbook-datev.de.newsroom.urls` holds `.../presse?utm_...` **and** `.../ueber-datev/impressum?utm_...`. The own-newsroom tier dutifully fetched both at 05:34:21 (4 requests once redirects are counted, `10_news_log.txt`) and DATEV still ended with 1 signal. The `has_keyword or has_dates` gate passed an Impressum page; an Impressum/legal blacklist, or requiring `has_dates` for a page whose heading has no news keyword, would drop it.

**D14 — `engine` is dropped when a news signal is persisted.**
*`routers/pipeline.py:_client_news_scan`, the `index_document(..., metadata={...})` call at ~2422.*
Candidates carry `"engine": "newsroom"` (set at ~2091) and the searxng engine name, but the stored metadata is only `{source_url, published_at, signal_type, relevance_score, subject, from_news_scan, query, service}`. `09b_signal_meta.txt` confirms it for all 7 new signals. There is no way to tell after the fact which tier produced a signal, which is exactly what the own-newsroom tier was added to make visible.

**D15 — the match report can be written with `type='research'`, so it is invisible to `type='match_report'` consumers.**
*agent-pi's document-type choice in the `match_synthesis` run (server side: `routers/agents.py` `_handle_match_synthesis_callback` accepts whatever type the run wrote).*
Festo's match synthesis (runs 220/221) produced doc id 473, `doc_id='pi-research-5e402ed6'`, title `Match: Festo — 2026-09-14 [Pi]`, content a full product-fit report — stored as `type='research'`. Every earlier match report on this instance (`pi-match_report-*`, ids 349/361/362/396) is `type='match_report'`, so `SELECT ... WHERE type='match_report'` returns nothing for Festo even though `match_status=done` was logged. Same agent-pi build, so this is non-deterministic; the callback should coerce the type rather than trust the run.

**D16 (minor) — Trumpf's careers URL is cached to `clients.metadata.careers_url` even though the scan found 0 positions.**
*`routers/pipeline.py:_scan_client_jobs` / `_stamp_failure`.*
`found:false`, `error:"no positions found on careers page"`, yet `clients.metadata.careers_url` and `playbook.careers.url` both now hold `https://www.trumpf.com/de_INT/karriere/stellenangebote/`. The next scan will therefore take the `metadata` branch, skip discovery entirely and fail the same way forever. The URL is genuinely the right page — the browser tier only returned 1 377 chars (SPA shell), so this is really "the renderer could not see the listing", which is worth distinguishing from "this URL is good".

**D17 (minor) — `title_source: page` titles carry site boilerplate and un-decoded HTML entities.**
*`routers/pipeline.py:_extract_jobs` / the posting-page title path (D10's fix).*
`Product Architect Embedded Software (m/w/d) (Gütersloh) › Miele Gruppe`, `System Engineer Kubernetes-Platform Engi Job Details`, `Endpoint &amp; OT Client Platform Engineer Job Details`. Strip the trailing site/`Job Details` suffix and run `html.unescape`.

**D18 (minor) — a non-IT junior posting still survives the junior filter.**
DATEV's 10 positions include `Werkstudent Homebase Product, Delivery & Process mit Schwerpunkt Kommunikation & Organisation` (`filtered_out: 6`). The two `Duales Studium Informatik / Wirtschaftsinformatik` entries on Festo and the `Ausbildung Fachinformatiker` on DATEV are legitimate IT-override survivors; the Kommunikation & Organisation one is not.

**D19 (minor) — `/api/agents/fetch-log` covers only `_fetch_rendered_tier` callers.**
8 entries for a drive that made ~60 outbound fetches. Sitemap crawls, newsroom probes and posting-page title fetches never appear, so "which tier won" cannot actually be answered for most of the traffic. The admin gate itself is correct (401 without a token).

---

## 7. What clearly improved

- camofox is genuinely alive: 0 launch failures (was 571 in 100 min), 68 tabs, 1 server-side win on a Workday ATS page that plain GET and browser-service both returned thin.
- Jobs coverage 1/4 → 3/5 clients with a real careers URL and ≥10 positions (Miele, DATEV, Festo).
- News yield 2 → 7 signals across the same four clients, every one fresh, dated, https, and about the right company; the namesake filter rejected 6 wrong-company articles that were fetched and scored.
- D5's tier precedence works: Miele's rescan reported tier `playbook`, made zero discovery requests and did not downgrade its recorded tier.
- The WP8 review blockers held: no Impressum, consent wall or generic page was ever accepted as a careers URL.
- A full cold intake (Festo) completed in 9 min 39 s with all four parts `done`, a clean brief and a complete match report — the fastest and most complete intake observed on this instance.
