# WP7c — Third test drive (after WP10 + WP12)

Instance: `<test-instance>`, deployed main `a6f52a6`, org 1, admin id 1.
Server + agent-pi restarted 2026-09-14 **07:41Z**; everything below is post-deploy.
Drive window: **08:22Z → 08:47Z** (25 min; hard stop 09:52Z never approached).
Evidence: `wp7c_evidence/*.txt`. No token printed, echoed or written to any evidence file (redaction sweep run on the one psql INSERT that touched it).

## 0. Important: the "cut-off" prior attempt was NOT cut off before doing anything

A prior WP7c attempt ran **07:42Z–08:05Z** and completed steps 2, 3, 4 and part of 5 — it just never wrote a report. Its evidence is preserved in `00_server_prior_drive.txt` (1 118 lines) and is in several ways **more informative than my own re-run**, because it was the drive that actually exercised careers *discovery*; by the time I re-scanned, the playbooks were warm and every client short-circuited to tier `playbook`. Both drives are reported below.

Per instructions I left its leftover `user_sessions` row **id 4** (created 07:42:52Z, expires 13:42Z) **alone**. My own row **id 5** was created 08:23:04Z and deleted at the end. Note for Konrad: row 4 is a live 6 h admin token that nothing will clean up.

---

## 1. Pass / Fail (steps 1–6)

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Prereqs: 6 healthy, camofox `browserRunning:true`, baseline snapshot | **Pass** | `25_final_state.txt`: all 6 up+healthy; camofox `{"ok":true,"engine":"camoufox","browserConnected":true,"browserRunning":true,"activeTabs":0,"consecutiveFailures":0}`. Baselines in `01_baseline_clients.txt`, `01b_baseline_docs.txt`, `01c_pb_summary.txt`, `01d_baseline_signals.txt` |
| 2 | Jobs scans: Trumpf + Vorwerk get a real careers page, ≥5 positions each, cleaned titles, DATEV/Festo unchanged-or-better | **Fail (2 of 4)** | `02_jobs_scans.txt`, `04_jobs_scan_logs.txt`. Festo 18 ✓, DATEV 8 ✓, **Trumpf 3** ✗ (and all 3 are student roles — see D22), **Vorwerk 0** ✗ (`no careers page found`). `title_source: page` on **every** position of all three working clients (20/20 fetched) |
| 3 | News scans: `written ≥3` for ≥3 of 4, `engine` in metadata, no Impressum, explicit `error` if degraded | **Fail** | `05_news_scans.txt`: Trumpf 0, Vorwerk 1, Festo 0, Miele 0. Prior drive (07:48–07:52, before the engines got suspended): Miele 1, rest 0. `engine` ✓ on both news-scan signals (D14 fixed). No Impressum fetched ✓ (D13 fixed). **No `error` despite 0 written** — D3 residual, see D20 |
| 4 | WP10: `present:false`, exact-name 404s, brief `partial`/`failed_parts`, no false "Partial" | **Pass (1 caveat)** | `07_wp10_checks.txt`, `08_briefs.txt`, `12_vorwerk_brief_after.txt`. All 7 name/intake assertions correct. Regenerated Vorwerk brief: `partial:[]`, `failed_parts:[]`, no "Partial", no "refreshes automatically" ✓. Caveat: no `Not collected:` note either, and the regen drops `metadata.generated_at` — D23 |
| 5 | D15: match reports written after the deploy are typed `match_report` | **Pass** | Doc **530** `pi-match_report-3917340b` "Match: Festo — 2026-09-14", **`type=match_report`**, run 233, 08:05:33Z. The pre-deploy doc 473 (05:50Z) is still `type=research`, exactly as WP7b described |
| 6 | Logs since deploy: tracebacks, `No API key`, 429, sweeper warnings, camofox `Invalid URL`/launch failures | **Pass** | `23_log_summary.txt` over 1 401 server + 945 agent-pi + 370 camofox lines: **0** tracebacks, **0** `No API key`, **0** bridge 429/quota, **0** sweeper warnings, **0** agent-pi lines at level ≥40, **0 camofox `Invalid URL`**, **0** camofox launch failures, **0** camofox error/warn lines at all. 38 tabs created, 38 closed |

Cleanup (step 7): session row 5 deleted (`DELETE 1`); row 4 left alone; all scans/briefs left in place; 6 containers healthy at the end.

## 1b. Per-fix verdict

| Fix | Verdict |
|---|---|
| **D11** bare-domain `metadata.website` reaches the path-probe | **Fixed.** Vorwerk's scan issued 12 own-domain probes (`vorwerk.de/karriere`, `/karriere/`, `/de/karriere`, `/de-de/karriere`, `/de_DE/karriere/`, `/careers`, `/career`, `/en/careers`, `/jobs`, + `jobs./karriere./careers.vorwerk.de`). Camofox logged **zero** `Invalid URL: …` (was 4/drive). Trumpf never probed only because it short-circuits on a cached URL |
| **D12** `jobs.<domain>` → `careers.candidate_url` | **Fixed.** `site-playbook-festo.com.careers.candidate_url = https://jobs.festo.com/viewalljobs/?locale=de_DE` after Festo's post-deploy research run. Still empty on the other 6 playbooks (no new research run) |
| **D13** Impressum dropped from `newsroom.urls` | **Fixed at fetch time.** DATEV's news scan fetched only `.../presse`; **0** Impressum fetches in the whole post-deploy log. Residual: the stale entry is still *stored* in `site-playbook-datev.de` (D24) |
| **D14** `engine` on signals | **Fixed for the news-scan writer** (Miele `duckduckgo`, Vorwerk `bing news`). Absent on the 7 signals the pain-point/research path wrote (D25) |
| **D15** match report typed | **Fixed** (doc 530) |
| **D16** 0-position URL not cached, 7-day skip | **Not exercised — see D21.** `last_tried_url` is empty on all 7 playbooks. Trumpf's stale `de_INT/karriere/stellenangebote/` 200'd again, the scan followed a listing link off it to a Workday board, found positions, and so took the *success* branch — `careers.url` and `clients.metadata.careers_url` were **overwritten** with the Workday URL. The skip mechanism never ran |
| **D17/D18** cleaner titles, 20 fetched, `\bsap\b` | **Mostly fixed.** All 20 posting pages fetched (was 8); `Job Details` suffix and `&amp;` gone (`Endpoint & OT Client Platform Engineer`). Residuals: localized suffix `IT Business Application Specialist CRM Detalhes da vaga`; `Duales Studium` now correctly filtered on Festo (2 filtered out) but Trumpf's student roles survive (D22) |
| **D19** `/api/agents/fetch-log` covers probes + title fetches | **Fixed.** 67 entries for this drive (was 8), including all 12 Vorwerk path-probes and all 20 Festo posting-page fetches |
| **WP10** `present:false`, exact names, no false Partial | **Fixed** (see step 4 row) |

---

## 2. Per-client before / after

"WP7b" = end of the second drive. "Cut-off drive" = 07:42–08:05Z (careers *discovery* happened here). "WP7c" = my re-run 08:25–08:31Z.

| Client | careers_url + tier — WP7b | careers_url + tier — after cut-off drive | positions WP7b → cut-off → WP7c | news written (cut-off / WP7c) | camofox used |
|---|---|---|---|---|---|
| **Trumpf** (5) | `www.trumpf.com/de_INT/karriere/stellenangebote/` (`searxng`), 0 positions, error "no positions found" | **`trumpf.wd3.myworkdayjobs.com/de-DE/TRUMPF_Students`** (response tier `metadata` = where the scan started; playbook records the effective tier `listing-link`) | 0 → 5 → **3** (filtered_out 12) | 0 / **0** (found 1, scored 1) | no |
| **Vorwerk** (7) | — (`no careers page found`) | — (still `no careers page found`); 12 path-probes fired, 2 `blocked_urls` recorded | 0 → 0 → **0** | 0 / **1** (`bing news`, `engine` ✓) | no |
| **Festo** (9) | `jobs.festo.com/viewalljobs/?locale=de_DE` (`searxng`) | same (`searxng`), now also `candidate_url` | 20 → 18 → **18** (filtered_out 2) | 0 / **0** (found 5, scored 5) | no |
| **DATEV** (6) | `datev.wd3.myworkdayjobs.com/de-DE/Datev_Careers` (`listing-link`) | same | 10 → 8 → **8** (filtered_out 7) | 1 (07:5x) / n/a | no |
| **Miele** (4) | `karriere.miele.de/search` (`playbook`) | same | 20 → 20 → 20 | **1** (`duckduckgo`, `engine` ✓) / **0** | no |

All four WP7c jobs scans reported tier `playbook` (except Vorwerk, no tier) — D5 tier precedence holds and no scan downgraded a recorded tier.

## 3. Fetch-tier counts (`06_fetchlog_after_jobs.json`, 67 entries, jobs scans only)

| Tier | Count | Notes |
|---|---|---|
| `http` | 61 | 12 Vorwerk path-probes, 4 sitemap probes, 20 Festo posting pages, 2 ATS landing pages, rest redirect targets |
| `browser` | 4 | Trumpf + DATEV Workday boards (browser-service, ~4 kB each) |
| `none` | 2 | Two Vorwerk subdomain probes that DNS-failed |
| `camofox` | **0** | browser-service sufficed everywhere this drive; camofox itself served 38 tabs for agent-pi with 0 failures |

Longest single fetch ≈ 2 s; nothing near 30 s. One duplicate fetch observed (`Barcelona-Senior-Java-Backend-Developer` fetched twice in the same Festo scan).

## 4. Distinct errors (all sources, since 07:41Z)

| Count | Line | Meaning |
|---|---|---|
| 7 | `HTTP/1.1 404` on `…/sitemap.xml` / `sitemap_index.xml` (Workday hosts, trumpf.com) | expected — ATS hosts have no sitemap |
| 3 | `HTTP/1.1 429` on `hdblog.it`, `yahoo.com` (×2) | external article sites; **not** a bridge/LLM rate limit |
| 2 | `HTTP/1.1 403` | external article fetches |
| ~125 | `WARNING:searx.engines.{duckduckgo,yahoo,google,mojeek,brave,bing}` CaptchaException / parsing error / access denied | searxng container; surfaced to callers as `unresponsive`. **This is why step 3 failed** — 7 of the news engines were CAPTCHA'd or suspended |
| 1 | `jobs discovery: path-probe found a careers page at https://www.vorwerk-group.com/de` | **misleading** — that URL is then correctly discarded (D26) |
| 0 | tracebacks, `No API key`, sweeper warnings, camofox `Invalid URL`, camofox launch failures | clean |

---

## 5. Remaining defects

**D20 (step-3 blocker, carried over from D3) — a news scan that writes nothing still returns `error: null`.**
*`routers/pipeline.py:_client_news_scan`.*
All four WP7c scans returned `{"ok":true, …, "written":0 or 1, "error":null}` while listing 6–7 `unresponsive` engines (`brave`, `brave.news`, `duckduckgo`, `google`, `google cse`, `mojeek`, `yahoo` — mostly `Suspended: …`). `degraded`/`error` still requires *zero* candidates, and one surviving engine (`bing news`) always produced 1–5, so the caller cannot distinguish "nothing newsworthy happened" from "7 of 8 search backends are CAPTCHA-blocked". Suggest raising `error`/`warning` when `unresponsive` covers the majority of configured engines, regardless of candidate count.

**D21 (D16 unverified, and the reason it did not fire) — the D16 skip sits behind two branches that never reach it.**
*`routers/pipeline.py:_scan_client_jobs` (~4083, the `pb_url`/`arg_url`/`meta_url` precedence ladder) vs. `_careers_candidates` (~3566, where `last_tried_url` is actually consulted).*
`_careers_candidates` — the only reader of `careers.last_tried_url` — is called **only** in the `else:` branch, after a fresh playbook URL, an argument URL *and* a cached `metadata.careers_url` have all been ruled out. Trumpf's stale `metadata.careers_url` took the `meta_url` branch, so the skip could not apply even in principle. It then didn't matter, because that stale page 200'd and the scan followed a listing link off it to `trumpf.wd3.myworkdayjobs.com/de-DE/TRUMPF_Students`, extracted positions there, and took the success branch — writing the *effective* URL back over both `careers.url` and `clients.metadata.careers_url`. Net effect for the drive is fine, but `last_tried_url`/`last_tried_at` are empty on all 7 playbooks and the 7-day skip is still unproven on this instance. A client whose cached URL yields 0 positions *and* has no listing link will stamp `last_tried_url` and then still re-enter via the same `meta_url` branch next scan, because nothing clears `clients.metadata.careers_url`.

**D22 (new, jobs quality) — Trumpf is now permanently locked onto a student-only Workday board.**
*`routers/pipeline.py:_filter_positions` (junior filter + IT override) and the listing-link tier in `_scan_client_jobs`.*
The listing-link tier picked `…/de-DE/TRUMPF_Students` — TRUMPF's *student* board, not its main careers board — and, because it yielded positions, it is now cached in `clients.metadata.careers_url` **and** `site-playbook-trumpf.com.careers.url` with `last_success_at` fresh, so every future scan short-circuits to it (tier `playbook`). The 3 surviving positions are `Masterarbeit: Automatische Klassifikation … (SS27)`, `Masterarbeit: Optimierung der Defect-Klassifikation … (SS27)` and `Praktikum Industrial Engineering mit KI-Fokus (SS27)` — 12 of 15 were filtered out; the 3 that survived are theses and an internship that the IT/AI keyword override rescued. Two fixes needed: (a) `Masterarbeit`/`Praktikum`/`Werkstudent` should not be rescuable by the IT override; (b) a board whose URL or title says *Students/Studenten/Schüler* should not be accepted as the careers URL when a general board exists.

**D23 (new, WP10) — `POST /brief` clears the banner but writes no `Not collected` note and loses `generated_at`.**
*`routers/knowledge.py:_rewrite_brief_close_out` (~1686) / `_NOT_COLLECTED_PREFIX` (1572); `intake.py:564`.*
Regenerating Vorwerk's brief (08:30Z, doc 262) correctly removed the false `> **Partial brief — missing: …** It refreshes automatically…` banner and set `partial:[]`, `failed_parts:[]`. But Vorwerk's jobs part is genuinely `status:"failed", error:"no careers page found"` in its intake, and the new brief carries **no** `> **Not collected:**` line — so the honest replacement promised by WP10 never appears on an ad-hoc regeneration (the note is only produced on `intake._maybe_finish`'s partial→all_terminal close-out path). Separately the regen wipes `metadata.generated_at`, and `GET …/brief` then falls back to the *original* `2026-09-14 01:56:16` for a document whose `updated_at` is `08:31:45` — the UI will show a 7-hour-old timestamp on a brand-new brief.

**D24 (minor, D13 residual) — the Impressum URL is filtered on read but never removed from the stored playbook.**
*`routers/pipeline.py:_is_legal_url` (941), called at 1340 / 2035 / 2041.*
`site-playbook-datev.de.newsroom.urls` still holds `…/ueber-datev/impressum?utm_…` (written 01:24Z, `last_success_at` never refreshed by a news scan). The fetch-time filter means it costs nothing today, but the playbook is now permanently wrong, and any future consumer that reads `newsroom.urls` without going through `_is_legal_url` inherits the bug. A one-time prune on `playbook.record` would close it.

**D25 (minor, D14 residual) — `engine` is stamped only by the news-scan writer.**
The 7 Festo signals written 08:02–08:03Z by the pain-point/research path (`signal_type` `pain_point`/`opportunity`/`risk`, sources `press.festo.com`) carry neither `from_news_scan` nor `engine`. Post-deploy coverage is therefore 2 of 9 signals. If `engine` is meant to answer "which tier produced this signal", the research-side signal writer needs the same field (value `pi-run`/`newsroom` as appropriate).

**D26 (cosmetic) — the path-probe logs "found a careers page" for a URL it is about to discard.**
*`routers/pipeline.py:3400` (and the twin at 3509) vs. the own-domain filter in `_discover_careers_url` (~3705).*
Vorwerk's `/de-de/karriere` 302s to `https://www.vorwerk-group.com/de` — a corporate **homepage on a different registrable domain**. `_probe_one`'s acceptance test only checks length ≥500, not-an-SPA-shell, and keyword-or-≥3-job-links, so it accepts and logs `path-probe found a careers page at https://www.vorwerk-group.com/de`. The WP8 guard then does its job — `candidates = [c for c in candidates if _own_or_ats(c["url"], domain)]` drops it and the scan correctly reports `no careers page found` — but the log line reads like a success and will mislead anyone debugging Vorwerk. Reject an off-domain redirect target (or a bare `/`, `/de`, `/en` path) inside `_probe_one`.

**D27 (root cause for Vorwerk) — the client's live domain differs from `metadata.website`, and nothing adopts the redirect target.**
*`routers/pipeline.py:_client_domain` (~944) / `_own_or_ats`.*
`vorwerk.de` 302s **everything** to `www.vorwerk.com/de/de`; `karriere.vorwerk.de` and `careers.vorwerk.de` do not resolve (both now recorded as `blocked_urls`, so D7 is working). Because `_client_domain` returns `vorwerk.de`, every probe result on `vorwerk.com` — including any genuine careers page there — is off-domain and discarded, which is why 12 probes, a homepage harvest and a sitemap probe all end in `no careers page found` for the third drive running. Vorwerk cannot be fixed by more probing; it needs the scanner to treat a stable homepage redirect (`vorwerk.de` → `vorwerk.com`) as an alias of the client's own domain, or `metadata.website` corrected to `vorwerk.com`.

---

## 6. What clearly improved

- **D11 is properly dead-and-buried:** 12 real own-domain probes on a bare-domain client, and zero `Invalid URL` lines out of camofox (was 4 per drive).
- **Observability is finally usable:** the fetch log went from 8 to 67 entries and now shows probes, sitemaps and posting-page title fetches, so "which tier won" is answerable for the whole drive.
- **Title quality:** 20/20 posting pages fetched (was 8/20), `title_source: page` on every position, HTML entities decoded and `Job Details` stripped.
- **WP10 name handling is exactly right:** `Miele AG` → 404 on `/intake`, `/jobs/scan`, `/news/scan`, `/brief`; `miele` and `MIELE` → 200; `Vorwerk Test` → 404; `Schwarz IT` → `present:false, parts:{}`.
- **Runtime is quiet:** 0 tracebacks, 0 `No API key`, 0 sweeper warnings, 0 camofox errors across 2 716 log lines.
