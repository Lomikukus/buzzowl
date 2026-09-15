# Plan: Verlässliche Prospect-Intelligence auf dem ChatGPT-Abo (Runde 2)

## Kontext

Buzzowl (`<repo>`) läuft seit Runde 1 vollständig auf Konrads ChatGPT-Abo.
Der Durchlauf mit OBI/Schwarz IT/Duravit zeigt, dass die Kette prinzipiell funktioniert, aber vier
Dinge fehlen, die Konrad benannt hat: (1) die Stellensuche soll bei **jedem** Kunden sofort und
parallel zur Recherche starten und die Karriereseite selbstständig finden; (2) der finale Account
Intelligence Brief soll erst geschrieben werden, wenn **alle** Informationen da sind; (3) News-Artikel
zu Kunden sollen zuverlässig gefunden werden; (4) ein **selbstverbessernder Agent**, der sich
Best Practices zum Navigieren jeder Kunden-Website merkt. Übergeordnetes Ziel: verlässliche,
belegbare Informationen je Prospect, passend zu den Produkten gematcht — alles auf dem aktuellen Abo.

### Konrads Entscheidungen (2026-09-13)
- **Testfirmen:** Miele, Trumpf, DATEV, Vorwerk — plus OBI, Schwarz IT, Duravit AG als Vergleich.
- **Selbstlernen:** Navigations-Playbook je Website **und** übergreifende Lehren — letztere nur mit
  Freigabe-Schleife (vorgeschlagen → von Konrad freigegeben → erst dann in Aufgabentexte).
- **Bericht-Gate:** auf OSINT, Recherche, Stellen, News warten; nach 25 Minuten trotzdem schreiben,
  sichtbar als „partial — fehlt: …" kennzeichnen, und einmal nachziehen, wenn der fehlende Teil kommt.
  *Mein Zuschnitt dazu:* Die 25 Minuten zählen ab dem Moment, in dem die Pi-Läufe des Kunden
  **tatsächlich starten** (agent-pi hat 2 Slots; bei vier gleichzeitig angelegten Firmen warten die
  hinteren in der Warteschlange und bekämen sonst falsche Teil-Berichte). Absolute Obergrenze
  90 Minuten ab Anlage.

### Ist-Befunde (drei Erkundungsagenten, verifiziert mit file:line)
- **Kein Sammelpunkt.** `agents.py:821` löst `_brief_then_match` nach **jeder** fertigen Recherche
  (OSINT *oder* Research) aus → zwei parallele Briefs pro Neukunde (gleiche `doc_id`, der zweite
  überschreibt), Brief entsteht **vor** dem Stellenscan (`agents.py:1459` vs `:1469`), Match-Gate
  läuft zweimal. Die ganze Folgekette hängt an einem einzigen HTTP-Callback; der Watcher
  (`agents.py:371`) gleicht nur die Zeile ab, stößt nichts an. Kein Orchestrierungszustand am Kunden.
- **Stellen** (`pipeline.py:1519-1978`): rein Python, textbasiertes LLM (Abo-tauglich). Karriere-URL
  wird via SearXNG + LLM-Auswahl gefunden, dann Sitemap → Seitentext → Listing-Links. Läuft nur
  wöchentlich (Mo 04:00) und seriell *nach* der Recherche. Sitemap-Stufe verlangt ≥3 Treffer;
  Fehlschläge werden nicht vermerkt (Rotation verhungert); Junior-Rollen rutschen durch
  (Duravit: Ausbildung/Praktikum/Buchhaltung); Brief-Prompt hat keine Hiring-Sektion.
- **News**: nur Prompt-Anweisung. SearXNG hat `bing_news`/`google_news` aktiviert, aber
  `search.ts:11` verdrahtet `categories=general` fest, kein `time_range`, `publishedDate` wird
  verworfen; Python `_searxng_results` ebenso. Quellensuche (`pipeline.py:1040`) = Stichwortfilter
  → Duravit bekam einen türkischen Windows-11-Artikel, OBI/Schwarz IT 0 Quellen. `market_news`
  hat keinen Prompt/Allowlist in `agent.ts` → fällt auf `research` zurück → Modell schreibt nichts
  → Läufe scheitern (`runner.ts:191`).
- **Selbstlernen**: existiert nicht. Bausteine vorhanden: `documents.type` ist freier Text
  (kein CHECK), `db.index_document` ist ein Upsert auf `(org_id, doc_id)`, `update_client`
  (PATCH `/api/internal/clients/{name}`) ist ein generischer JSONB-Merge, Tool-Aufrufe jedes Laufs
  liegen in `agent_runs.tool_calls` (auf 200 Zeichen gekürzt; Retention verdichtet nach 14 Tagen).
- **Nebenbefunde (echte Bugs):** `internal.py:68/:169` `cache_clear(org_id)` vor der Zuweisung →
  `create_client`/`create_contact` über die interne API werfen 500 → Kontaktextraktion tot.
  Drei Aufgabentexte verlangen ein nicht existierendes Tool `fetch_youtube_transcript`
  (`agents.py:115/126/170`). `search.ts:109` reicht `wait_ms` nicht an den Browser-Service weiter.
  Sitemap-Guard `pipeline.py:1743` (String-Konkatenation statt `or`). Drei `_call_brain_sync`-
  Aufrufe ohne Overlay-Warmup (`pipeline.py:1607/1805/1396`), `_auto_generate_brief` ebenso.
- **Randbedingungen:** Abo-Brücke ist textonly → Python-Seite nur `llm.acomplete(...,
  org_id=…)` (wärmt den Overlay selbst); nie durch `agents/runner.py`. Jeder Lauf über
  `resolve_run_target`. Keine neuen Tabellen; neue Inhalte = neuer `documents.type` + JSONB.
  Metadaten-Merge ist **flach** (`metadata || patch`) → verschachtelte Zustände brauchen
  `jsonb_set`. Server ist ein Prozess (`uvicorn.run`, keine Worker) → DB-CAS + In-Prozess-Locks reichen.

## Arbeitspakete

### WP0 — Bug-Sweep (zuerst mergen, konfliktarm)
- `routers/internal.py:67-68, :168-169`: `cache_clear(org_id)` hinter die Zuweisung. Test in
  `tests/test_internal.py` (POST clients/contacts → 200 mit gemocktem `upsert_*`/`embed_text`).
- `routers/agents.py:115, :126, :170`: `fetch_youtube_transcript`-Sätze entfernen
  („fetch_page auf die Videoseite").
- `routers/pipeline.py:1743`: Guard `status==200 and ("xml" in ct or "<urlset" in head or
  "<sitemapindex" in head)`, `head = r.text[:300]`.
- `agent_service_ts/src/search.ts:109`: `wait_ms` an Browser-Service durchreichen (3500).
- `pipeline.py:1607, :1805, :1396` und `knowledge.py:1468`: `run_in_executor(_call_brain_sync)`
  → `await llm.acomplete(prompt, role="research", timeout=180, org_id=org_id)`.
- `tests/test_llm_subscription.py`: neuer Wächter `test_no_bare_brain_call_outside_knowledge`
  (`_call_brain_sync(` nur in `routers/knowledge.py`); Scan-Globs um `intake.py`, `playbook.py`
  erweitern.

### WP1 — Such-Plumbing (klein, Basis für WP2/WP3)
- `pipeline._searxng_results(query, limit=10, *, categories=None, time_range=None,
  language=None)` — Parameter nur setzen, wenn angegeben (bestehende Mocks unberührt);
  `publishedDate`, `engine`, `content` durchreichen.
- `search.ts`: `SearchResult.publishedDate?`; `searxngSearch(query, n, {category, timeRange,
  language})` statt fest verdrahtetem `&language=en&categories=general`; DDG-Fallback ignoriert opts.
- `tools.ts:127-144` `web_search`: optionale `category` ('general'|'news'), `time_range`
  ('day'|'week'|'month'|'year'), `language`; Ergebniszeile mit `(published: YYYY-MM-DD)`;
  Beschreibung: „für aktuelle News category='news', time_range='month'".
- `_NEWS_SIGNAL_SCORING_HINT` (`pipeline.py:1147`): ein Satz, der genau das anweist.
- Test `tests/test_news_scan.py::test_searxng_results_forwards_params` (httpx-Patch wie
  `_patch_httpx` in `tests/test_source_monitor.py:95`). TS: `tsc`-Build.

### WP2 — Stellen beim Intake, Karriereseite selbstständig, gehärtet (bleibt Python/textonly)
`routers/pipeline.py` (Jobs-Block):
- `_JUNIOR_TITLE_RE` (ausbildung|azubi|praktikum|werkstudent|duales studium|dh-studium|trainee|
  intern(ship)|bachelor|master thesis|abschlussarbeit|ferienjob|minijob|aushilfe|student) und
  `_IT_MGMT_WORD_RE` (Wortgrenzen-Fassung von `_IT_MGMT_TITLE_KEYS` :1721 + cio|cto|ciso|devops|
  sap|erp|entwickler|architect). `_filter_positions(positions)`: Junior raus, außer IT/Mgmt-Wort;
  Titel dedupen; Cap 20.
- `_harvest_links(html, base_url, keys, own_domain)` — verallgemeinertes `_career_listing_links` :1666.
- `_careers_candidates(org_id, client, pb)` in dieser Reihenfolge: Playbook-URL (≤60 Tage frisch →
  allein), `metadata.careers_url`, Homepage-Harvest (`_CAREERS_KEYS` :1525, DE/EN: karriere,
  jobs, stellen, stellenangebote, careers, join), Sitemap-Probe der Website (`_sitemap_job_urls`
  :1727, ≥1 Treffer), SearXNG (die drei bestehenden Anfragen).
- `_discover_careers_url(org_id, client, pb=None) -> (url, tier)`: LLM-Auswahl via `llm.acomplete`,
  akzeptiert nur eigene Domain oder `_ATS_HOSTS` :1639.
- `_scan_client_jobs(org_id, client, careers_url="", *, run_id=None)`: Sitemap-Schwelle ≥3 → ≥1;
  `_filter_positions` nach jedem `_extract_jobs`; **Fehlschlag wird vermerkt** statt still
  zurückzukehren (:1878): vorhandenes Jobs-Dokument → `db.update_document(...)` mit
  `last_attempt/last_error/attempts` (bumpt `updated_at` → Rotation :1957 verhungert nicht mehr);
  sonst leeres Dokument mit denselben Feldern; **nie** einen guten Scan mit leer überschreiben.
  `agent_run_id=run_id` an beide `index_document`-Aufrufe (:1918, :1930). Danach
  `playbook.record(org_id, domain, {"careers": {url, tier, last_success_at|last_failure_at, error}},
  run_id)`; `needs_js=True`, wenn Plain-GET <500 Zeichen, Browser-Service aber Stellen lieferte
  (bis WP4 gemergt ist: `try: import playbook … except ImportError`).
- Jobs-Dokument-Metadaten zusätzlich: `tier` (playbook|metadata|homepage|sitemap|searxng),
  `last_attempt`, `last_error`, `attempts`, `filtered_out`.

`routers/knowledge.py`:
- `_BRIEF_PROMPT` (:920): neue Sektion `## Hiring Signals` nach Strategic Intelligence (offene
  IT/Mgmt-Rollen zuerst, was das Muster bedeutet, Karriereseite zitieren; sonst „(no data yet)").
  Active-Signals-Zeile: „je Signal: Typ, Datum, Schlagzeile, Beleg, Quelle".
- `_build_brief_context` (:1334): strukturierter `[JOBS]`-Block aus dem Jobs-Dokument (Source =
  careers_url, bis 12 Positionen mit Team/Ort, inferred_needs, last_scanned); `[SIGNAL]`-Kopf mit
  `Published:`; `type='jobs'` in der generischen Schleife überspringen.

Tests `tests/test_jobs_scan.py` (Helfer aus `test_source_monitor.py`; `pipeline.llm.acomplete`,
`_fetch_page_raw`, `_sitemap_job_urls`, `pipeline.playbook` gepatcht): Playbook-URL überspringt
Discovery; LLM-Auswahl außerhalb der Domain wird verworfen; Junior-Filter (Ausbildung
Fachinformatiker bleibt, Praktikum Marketing fällt); ein Sitemap-Treffer reicht; Fehlschlag stempelt
statt zu überschreiben; Sitemap-Guard; `test_brief_context_has_jobs_block`,
`test_brief_prompt_has_hiring_section`.

### WP3 — News als Pipeline, Quellensuche, Marktscan, Match-Kontext
`routers/pipeline.py`, neuer Abschnitt „Client news scan" (nach `_maybe_escalate_match` :1205):
- `_NEWS_SKIP_HOSTS` (linkedin, xing, facebook, instagram, youtube, twitter/x, wikipedia, kununu,
  glassdoor, indeed, stepstone, pinterest, tiktok). `_NEWS_SCORE_PROMPT`: JSON-Liste
  `[{i, relevance 1-5, signal_type, headline ≤90, why ≤160}]`, Namensvettern = 1.
- `_norm_news_url(url)` (Host klein, www weg, utm_*/fbclid/gclid, Fragment, Slash);
  `_parse_published(r)` (publishedDate ISO → Datum; sonst Datum aus der URL; sonst None);
  `_existing_signal_urls(org_id, client_id)`.
- `_news_candidates(org_id, client)`: `("{ascii}", news, month)`, `("{ascii} {industry}", news,
  month)` falls Branche, `("site:{domain}", general, month)` falls Domain, Fallback
  `("{simple} news", general, month)` bei <3; Skip-Hosts; Dedupe; nur ≤90 Tage alt (eigene Domain
  undatiert → URL-Datum, sonst weg); Cap 15.
- `_client_news_scan(org_id, client, *, run_id=None, max_write=8) -> {found, scored, written,
  max_relevance, error}`: Kandidaten minus vorhandene URLs → **ein** `llm.acomplete` → relevance ≥2
  → `index_document(doc_id=f"news-{cid}-{sha1(norm)[:10]}", doc_type="signal", …, metadata=
  {source_url, published_at, signal_type, relevance_score, subject, from_news_scan: true, query,
  service: "python"}, agent_run_id=run_id)` + `link_document`; Inhalt mit `Published:`/`Source:`/
  `## Sources`. SearXNG-Ausfall: einmal nach 20 s wiederholen, dann `{"error": …}` (Teil scheitert
  terminal, nichts geschrieben). Danach `playbook.record(... {"news": {good_queries,
  last_success_at}})`.
- `_market_news_scan(org_id, industry, focus, *, run_id=None)`: `("{industry} regulation OR
  compliance OR Regulierung", news, week)`, `("{industry} market news Branche", news, week)`;
  Dedupe gegen `scope='market'`-Signale der letzten 30 Tage (Signatur von `db.list_signals`
  :2355 prüfen — sonst direkt abfragen); `doc_id=f"market-news-{sha1(norm)[:10]}"`, nicht
  kundenverknüpft. `_apply_market_signals` :1337 bleibt.
- `_run_market_monitor` (:1435): inneres `_fire` (Pi `market_news`) → `_market_news_scan`, Lauf-Zeile
  `agent_type="market_news", trigger_type="heartbeat"` weiter anlegen und done/failed setzen.
  `_MARKET_NEWS_TASK` (:71) löschen. `agent.ts` unverändert (Typ wird nicht mehr gefeuert).
- `_monitor_client` (:1226): wenn „news search" sich geändert hat (:1244) → `_client_news_scan`
  für **jeden** Kunden (ein Textaufruf, kein Pi-Slot) vor der Fokus-/Autonomie-Logik;
  `_fire_news_research` für Fokus-Kunden bleibt. Summary + `news_written`.
- `knowledge.py`: `POST /api/clients/{name}/news/scan` neben `/sources/check` (:751).
- `_discover_client_sources` (:1040) **neu, weiterhin ohne LLM**: `_NEWSROOM_PATHS`
  (/news, /newsroom, /presse, /press, /pressemitteilungen, /aktuelles, /media, /unternehmen/presse,
  /company/news, /en/news, /de/presse, /investor-relations, /investors);
  `_probe_newsroom_paths(website)` (GET, 12 s, `_SOURCE_UA`; akzeptiert 200 + ≥500 Zeichen +
  (≥3 Datumsmuster oder Titel/h1 mit `_SOURCE_KEYWORDS`)). Reihenfolge: bestehende
  Nutzerquellen → Probe-Treffer → Homepage-Harvest → SearXNG `site:{domain} news OR presse OR
  newsroom`. **Nur eigene Domain** wird akzeptiert. Die zwei Fremd-Namensanfragen (:1062) fallen
  weg. Caps wie heute. `playbook.record({"newsroom": {urls, last_success_at}})`. Ohne Website:
  unverändert zurück, `sources_discovered_at` stempeln (7-Tage-Retry :1208 bleibt).
- `routers/agents.py` `_handle_pain_point_callback` (:1255-1343): drei Abfragen statt einer —
  findings/research/osint neueste 15 (1200 Zeichen, `Source:`), **Signale** neueste 10
  (kundenverknüpft, relevance ≥2, inkl. market-applied; Kopf `[{signal_type} · {published_at} ·
  relevance n]`, `Source:`), alle `from_jobs`-Findings komplett. `_MATCH_SYNTHESIS_TEMPLATE`
  (:183): Block `{news_signals}` „RECENT NEWS SIGNALS (dated, each with source URL — cite by
  date)" zwischen Pain-Point-Summary und Hiring. `agent.ts:189-227` match_synthesis: ein Satz
  „datierte News-Signale und offene Rollen als Belege werten und mit URL zitieren".

Tests `tests/test_news_scan.py`: URL-Normalisierung; undatiert-fremd fällt, eigene-Domain-URL-Datum
bleibt; bekannte Signal-URLs werden nicht neu bewertet; deterministische doc_ids + Metadaten +
`agent_run_id`; SearXNG down → Fehler, nichts geschrieben; Marktscan `scope=market`, unverknüpft;
Quellensuche nur eigene Domain (`TestDiscoverSources` patcht `_probe_newsroom_paths`/
`_harvest_links`, sonst echtes HTTP!); `test_match_context_includes_signals_and_jobs_needs`.

### WP4 — Site-Playbook + freigegebene Lehren (neu `playbook.py`, `routers/lessons.py`)
**Daten (keine Migration):**
- `documents.type='site_playbook'`, `doc_id=f"site-playbook-{domain}"`, org-weit, unverknüpft
  (Vorbild `nba_queue`), `source='agent'`, `agent_run_id` = letzter schreibender Lauf. Inhalt =
  gerendertes Markdown inkl. `## Sources` (Lauf-IDs + gelernte URLs). Metadaten:
  `{domain, website, careers:{url, tier, last_success_at, last_failure_at, error},
  newsroom:{urls[], last_success_at}, news:{good_queries[], last_success_at}, needs_js,
  cookie_wall, blocked_urls:[{url, kind: 403|4xx|fetch_error|no_content|binary, at}],
  good_queries[], failed_queries[], notes[], sources_of_truth:[run_ids], updated_at, version:1}`.
  Grenzen: blocked_urls ≤20 (neueste), Queries ≤15, notes ≤8, sources_of_truth ≤10.
- `documents.type='agent_lessons'`, `doc_id="agent-lessons-org"`: `lessons:[{id:"l-<sha1[:8]>",
  text, scope: jobs|news|research|all, status: proposed|approved|rejected, evidence:[domains],
  proposed_at, decided_at, decided_by, proposed_by_run}]`; ≤20 proposed, ≤100 entschieden.

**`playbook.py`:** `domain_of(client)` (nutzt `pipeline._client_domain`), `load(org_id, domain)`,
`record(org_id, domain, patch, *, run_id=None, website="")` (Lock je Domain; deterministischer
Merge: Skalare überschreiben, Dicts eine Ebene tief, Listen Union + Grenze; `index_document`;
optional `site_playbook_summary` ≤300 Zeichen in die Kunden-Metadaten für `get_client`),
`render_markdown`, `render_block(pb)` („## Site playbook (learned)", ≤1200 Zeichen, leere Felder
weg), `resolve_client_exact(org_id, subject)` (**exakte** Namensgleichheit — `db.get_client` ist
fuzzy!), `enrich_task(org_id, subject, task, agent_type) -> (task, needs_js)` (Playbook-Block +
Lehren-Block je Scope), `classify_tool_calls(tool_calls, domain)` (rein: blocked_urls aus den
Sentinels `Error: HTTP …`/`Error fetching page`/`(no readable content)`/`(skipped binary`;
`needs_js` bei ≥2 eigenen Domain-Fetches ohne Inhalt; good_queries = Suche, der binnen 3 Aufrufen
ein erfolgreicher Fetch folgt; failed_queries bei „(no results"), `reflect_on_run(org_id,
db_run_id)` (idempotent über `output.reflected`; immer deterministisch klassifizieren; **ein**
`llm.acomplete` für ≤4 Notizen nur bei ≥8 Tool-Aufrufen; läuft **zur Callback-Zeit**, weil Retention
`tool_calls` nach 14 Tagen verdichtet). Lehren: `lessons_load`, `lessons_propose(org_id, run_id)`
(wöchentlich; Eingabe alle Playbooks + gescheiterte Läufe der Woche; ein Textaufruf → ≤5
`{text, scope, evidence}`; Dedupe per Token-Jaccard ≥0.6), `lessons_decide(org_id, id, decision,
user_id)`, `lessons_block(lessons, scope)` („## Learned rules (approved)", ≤8 Zeilen),
`scope_for(agent_type)`.

**Schreiber:** Callback in `agents.py` (nach `_persist_tool_calls`): für research/osint/
pain_point_research **auch bei failed** `create_task(playbook.reflect_on_run(...))`.
`_clean_tool_call` (:304) `[:200]` → `[:600]`; `tools.ts:46` 300 → 600. Python-Schreiber aus WP2/WP3.

**Leser:** `_fire_agent_service` (:253): für research/osint/pain_point_research
`task, needs_js = await playbook.enrich_task(...)`, `payload["use_browser_fetch"] = needs_js`
(`runner.ts:174` liest es). Direkt-Poster `agents.py` `_maybe_trigger_pain_point_research` und
`match.py` `_fire_pain_point_research`: ebenso. match_synthesis und Event-Fetch: nicht.
Python-Prompts (`_discover_careers_url`-Auswahl, `_JOBS_EXTRACT_PROMPT`, `_NEWS_SCORE_PROMPT`)
bekommen `{rules}` = `lessons_block` ihres Scopes.

**`routers/lessons.py`:** `GET /api/agents/lessons`, `POST /api/agents/lessons/{id}/decision
{approve|reject}` (Admin, 403 sonst — Muster `org_settings`), `POST /api/agents/lessons/review`
(Admin, jetzt vorschlagen). `server.py`: Router einbinden. **Nie** automatisch freigeben.
Heartbeat `lessons_review` `0 7 * * 1` (`late_additions` :2640, `_HB_NAMES` :100, Zweig in
`_run_heartbeat_job` vor dem `else` :2616). `static/agents.html`: Panel `lessons-panel` + Reiter
(Switch :1510), `loadLessons()` nach Vorbild `loadAutonomy()` (:1522): Vorschläge mit
Approve/Reject, Freigegebene mit Scope-Badge, Abgelehnte eingeklappt.

Tests `tests/test_playbook.py`: Merge + Grenzen; render_block; `classify_tool_calls` mit Fixture;
Reflexion idempotent; `enrich_task` hängt Blöcke an und setzt needs_js, nur bei exaktem Namen;
`_fire_agent_service` setzt `use_browser_fetch` (Capture-Muster `test_llm_subscription.py:246`);
Lehren: Dedupe, nur approved injiziert, Nicht-Admin 403, nie auto-approved.

### WP5 — Intake-Orchestrator mit Sammelpunkt (neu `intake.py`)
**Zustand** `clients.metadata.intake`:
```json
{"version":1,"trigger":"create|trigger_research|internal","started_at":"…","deadline_at":null,
 "attempt":1,
 "parts":{"osint":{"status":"queued","run_id":12,"started_at":null,"done_at":null,"error":null},
          "research":{…},"jobs":{…},"news":{…}},
 "brief":{"status":"waiting","written_at":null,"missing":[],"refreshed_at":null,"error":null}}
```
Teil-Status `queued|running|done|failed`; Brief-Status `waiting|writing|partial|written|
refreshed|failed`. **Zeitfenster:** `deadline_at` = jetzt + 25 min, gesetzt sobald der Watcher einen
Pi-Teil erstmals `running` sieht; absolute Obergrenze `started_at` + 90 min.

**`db.py`** (neben `update_client_metadata` :2667): `set_client_intake_path(org_id, name, path,
value)` via `jsonb_set(metadata, $3::text[], $4::jsonb, true)` (atomar je Teil — der flache
`||`-Merge würde parallele Teile gegenseitig überschreiben); `cas_client_intake_brief(org_id, name,
from_states, to_state)` (`WHERE …->'brief'->>'status' = ANY($3) RETURNING`; None = Rennen
verloren); `list_clients_with_open_intake()` (alle Orgs, Brief in waiting|writing|partial).
`start` schreibt das ganze Objekt einmal, damit alle Pfade existieren.

**`intake.py`:** `PARTS = ("osint","research","jobs","news")`; `start(org_id, name, *, trigger,
osint_run_id, research_run_id)` (Zustand schreiben; Lauf-Zeilen `jobs_scan`/`news_scan`
anlegen; `create_task` für `_trigger_osint`/`_trigger_research` (mit run_id) und
`_run_python_part("jobs"|"news")`); `note_run_started(org_id, name, db_run_id)` (Watcher-Hook:
queued→running, `deadline_at` setzen falls leer); `part_done(org_id, name, part, status, *,
run_id, error)` (ignorieren ohne aktiven Intake, bei fremder run_id, oder wenn schon terminal —
**idempotent**); `_run_python_part` (jobs → `_scan_client_jobs`, news → `_client_news_scan`,
Lauf-Zeile pflegen, dann `part_done`); `_maybe_finish` (alle terminal → finish; Deadline vorbei →
finish mit `missing` = Teile ≠ done, gescheiterte als „jobs (failed: …)"; Brief partial **und** ein Teil wurde seit `written_at` `done` **und** **alle** Teile terminal **und** noch nicht refreshed → finish(refresh); *Stand nach Review 2026-09-14: die Nachbesserung wartet auf den letzten Teil, sonst gäbe es eine Regeneration pro Nachzügler. Scheitert der letzte Teil nur, wird der Brief ohne LLM-Aufruf als `written` geschlossen. Ein manueller `POST /brief` schließt einen offenen Intake ebenfalls (`written`, `missing=[]`), damit der Sammelpunkt ihn nicht überschreibt*); `_finish` (CAS
waiting|partial → writing; `_auto_generate_brief(org_id, name, partial_missing=missing)`;
Erfolg → refreshed|partial|written; Fehlschlag → vorheriger Status, `attempt+1`, Sweeper
wiederholt bis 3, dann failed; danach `_maybe_trigger_pain_point_research` — bei refresh nur,
wenn noch kein match_report); `sweep()` alle 60 s (Deadline vorbei → `_maybe_finish`; Obergrenze
→ erzwingen; `writing` >10 min → zurück auf waiting; Teile queued/running, deren Lauf-Zeile
done/failed ist → `part_done` = **Rettung bei verlorenem Callback**); `summary(meta)`.
Config `intake_deadline_min: 25`, `intake_absolute_cap_min: 90`. `_INTAKE_LLM_SEM =
asyncio.Semaphore(2)` um die Textaufrufe (Jobs/News/Brief), damit das Abo-Rate-Limit neben zwei
laufenden Agenten hält.

**`knowledge.py`:** `create_client` (:284-300): nach den vorab angelegten Lauf-Zeilen
`await intake.start(...)` statt der zwei `create_task`; `_discover_sources_for_new_client` bleibt.
`trigger_client_research` (:571): ebenso. `_auto_generate_brief(org_id, name, *,
partial_missing=None)` (:1458): bei partial vorn `> **Partial brief — missing: jobs (failed: no
careers page), news (still running)**. It refreshes automatically when the missing parts arrive.`,
Metadaten `{"partial": [...], "partial_at"}`; sonst `{"partial": []}`; gleiche `doc_id` → Refresh
überschreibt. `GET /api/clients/{name}/brief` (:1428) liefert `partial`. Neu `GET
/api/clients/{name}/intake` → `intake.summary` (kein Cache).
**`internal.py` (:61):** Lauf-Zeilen anlegen wie `knowledge` :289-296 → `intake.start(trigger=
"internal")` → `_discover_sources_for_new_client` (Import in der Funktion); `skip_research` beachten.
**`agents.py`:** Callback (:820-822) meldet research/osint **auch bei failed** an
`_brief_then_match(org_id, subject, part=agent_type, status=final_status, run_id=db_run_id)`;
`_brief_then_match` (:1455): bei aktivem Intake nur `intake.part_done(...)`; sonst Legacy-Pfad
(Brief → Jobs-Scan nur wenn kein Jobs-Dokument oder `last_scanned` >7 Tage → Match-Gate) — der
Source-Monitor-Pfad (OSINT nach News-Änderung → Brief-Refresh) bleibt so erhalten. Watcher
(:437-456): bei `running` → `intake.note_run_started`; bei terminal → `intake.part_done`
(idempotent mit dem Callback). **`pipeline.py`:** `intake.sweep` als Intervall-Job (60 s,
`coalesce`, `max_instances=1`) direkt nach dem `outreach_worker` (:2700-2706) registrieren.
**`static/client.html`:** `#intakeStrip` direkt unter `#tab-overview` (:387, **außerhalb** der
Sektionen, die `updateOverviewEmptyState` versteckt) mit vier Chips (○ queued · ⟳ running · ✓ done
· ✗ failed) + Brief-Chip („waiting · deadline 12:34" / „partial — missing: news" / „written" /
„refreshed"); `loadIntake()` + `startIntakePolling()` alle 5 s solange aktiv (Muster :1651-1661);
bei Übergängen `loadJobs()/loadSignals()/loadBrief()/loadMatchSummary()`; `runResearchNow` (:858)
startet das Polling; Brief-Kopf zeigt „Partial — missing: …" aus `data.partial`. Manuelles
`POST /brief` (:1492) bleibt immer erlaubt und setzt `written`, `missing=[]`.

Tests `tests/test_intake.py`: `summary`; `missing` listet failed+pending; `part_done` ignoriert
fremde run_id, ist idempotent; **vier parallele `part_done` → `_auto_generate_brief` genau einmal**
(CAS-Mock liefert beim zweiten None); Deadline → partial, später genau ein Refresh; Sweep rettet
verlorenen Callback; Brief-Fehler → waiting; Callback bei failed meldet den Teil; Watcher terminal →
`part_done`; `create_client`/`internal_create_client` starten Intake (+ Quellensuche); `/intake`.

### WP6 — Doku + Konfig
`docs/agents.md`: `jobs_scan`, `news_scan`, `market_news` (Python), `lessons_review`; `site_playbook`,
`agent_lessons`, `clients.metadata.intake`. `ARCHITECTURE.md`: Absatz zum Sammelpunkt.
`config.yaml`: `intake_deadline_min`, `intake_absolute_cap_min`, `news_scan_max_write`.

### WP7 — Testfahrt (Gate; nach dem Merge aller Pakete, auf Konrads Test-Instanz)
`scripts/intake_smoke.sh` (bash + curl + psql), Opus-Agent führt aus und berichtet Pass/Fail je Punkt:
1. Voraussetzungen: Stack healthy, agent-pi `max_concurrent=2`, ≥1 Fokus-Produkt; Snapshot der
   Bestandskunden (Intake-Zustand, Dokumentzähler je Typ).
2. Anlegen von Miele, Trumpf, DATEV, Vorwerk über `POST /api/internal/clients` mit dem
   Service-Token (30 s Abstand) — im **echten** Serverprozess, damit die Hintergrund-Tasks dort leben.
3. Beobachten alle 30 s über `GET /api/clients/{name}/intake`, bis alle Briefs written|refreshed und
   `match_status=done` (bis 3 h; erwartete Dauer ≈ 1,5–2 h wegen der zwei Pi-Slots).
4. Je Kunde: `careers_url` gesetzt, Jobs-Dokument mit Positionen ohne Junior-Titel (außer IT),
   `tier` gesetzt; ≥3 `signal`-Dokumente `from_news_scan` mit `published_at` ≤90 Tage und
   http-`source_url`; genau **ein** Brief heute, `partial` leer (oder Intake `refreshed`), Brief mit
   `## Hiring Signals`; `match_report` zitiert ≥1 Signal-URL und ≥1 Finding-URL und nennt eine
   offene Rolle; `site-playbook-{domain}` mit `careers.tier` und `newsroom.urls`; je Kunde genau
   je ein jobs_scan/news_scan/osint/research/pain_point_research/match_synthesis (keine Dubletten).
5. Zweiter Lauf: `POST /jobs/scan` + `/news/scan` bei Miele → `tier=playbook`, Discovery
   übersprungen (Server-Log), `blocked_urls` unverändert oder gewachsen.
6. Bestandskunden: `trigger-research` bei OBI → Intake läuft; ein Heartbeat-OSINT (`focus_osint`
   run-now) → Legacy-Pfad, Brief refresht, **kein** Intake-Objekt.
7. Lehren: `POST /api/agents/lessons/review` → ≤5 Vorschläge, alle `proposed`; eine freigeben →
   der nächste Research-Task (agent-pi `GET /runs/:id`) enthält `## Learned rules (approved)`.
8. Negativ: SearXNG stoppen, „Vorwerk Test" anlegen → News-Teil failed, Brief partial nach
   Deadline, kein Absturz; SearXNG starten, `/news/scan` → Teil done → Brief genau einmal refreshed.
Telegram-Benachrichtigungen bei Laufende sind Bestandsverhalten und bleiben.

## Nebenläufigkeit (2 Pi-Slots)
| Arbeit | Wo | Slot | Dauer |
|---|---|---|---|
| osint, research | Pi (FIFO) | ja | 6–15 min, parallel je Kunde |
| jobs scan | Python, 1–3 Textaufrufe | nein | 30–120 s |
| news scan | Python, 3–4 SearXNG + 1 Textaufruf | nein | 20–60 s |
| brief | Python, 1 Textaufruf | nein | 30–90 s |
| pain_point → match_synthesis | Pi | ja | 10–15 + 3–5 min |
Ein Neukunde: Jobs/News nach ~2 min, Brief nach ~13 min, Match-Bericht nach ~30–35 min. Vier
gleichzeitig: Pi-Warteschlange ≈ 50 min Recherchephase, alle Match-Berichte ≈ 1,5–2 h; die
25-Minuten-Uhr startet erst, wenn die Läufe wirklich laufen → keine falschen Teil-Berichte.

## Topologie, Besetzung, Merge-Reihenfolge
Worktrees nach Konvention `buzzowl-wt-*`: `wt-bugs` (WP0), `wt-search` (WP1), `wt-jobs` (WP2),
`wt-news` (WP3), `wt-intake` (WP5), `wt-playbook` (WP4). **Merge:** WP0 → WP1 → WP2 → WP3 → WP5 →
WP4 → WP6. WP2/WP3 berühren disjunkte Regionen von `pipeline.py`. WP5 und WP4 ändern beide den
Callback-Block und `_fire_agent_service` in `agents.py`: WP5 zuerst (ändert die Dispatch-Zeile),
WP4 danach rebasen. Hotspots (`pipeline.py`, `agents.py`, `knowledge.py`, `tools.ts`/`search.ts`,
`client.html`): Reviewer lehnen jedes Paket ab, das fremde Regionen reformatiert.

| Rolle | Modell | Einsatz |
|---|---|---|
| Manager/Plan/Eskalationen | Fable (diese Sitzung) | verteilt Pakete, entscheidet, fragt Konrad an den Freigabepunkten |
| Entwickler je WP | Sonnet | WP0–WP6 in isolierten Worktrees |
| Eskalation | Opus | übernimmt ein WP, wenn Sonnet feststeckt |
| Review je WP | Opus | adversarial vor jedem Merge: Wächter-Tests grün, keine Reformatierung, Abo-Regeln (textonly, `resolve_run_target`, org_id + Overlay) |
| Testfahrt WP7 | Opus | auf der Test-Instanz, Pass/Fail-Bericht |

Freigabepunkte für Konrad: (1) Screenshot Intake-Leiste + partial-Brief vor WP5-Merge,
(2) Lehren-Panel vor WP4-Merge, (3) Abschlussbericht der Testfahrt. Nichts wird gepusht.

## Risiken → Gegenmaßnahmen
Teil-Status-Überschreiben (flacher Merge) → `jsonb_set` je Pfad, andere Schreiber fassen `intake`
nie an. Doppelter Brief/Match → CAS + bestehende 7-Tage-/Läuft-schon-Gates. Verlorener Callback,
toter Watcher (Neustart) → Sweeper gleicht Teile mit `agent_runs` ab; Obergrenze 90 min. Heartbeat-
OSINT bei aktivem Intake → `part_done` prüft `run_id`. Fuzzy `get_client` → `resolve_client_exact`.
Playbook-Schreibrennen → Lock je Domain. Namensvettern in News → Scoring 1 = nicht geschrieben.
Domain-lose Kunden ohne Quellen → Website-Auflösung zuerst; News-Scan deckt Fremdberichte ab.
`TestDiscoverSources` mit echtem HTTP → Probe/Harvest als eigene, patchbare Funktionen. Abo-Rate-
Limit → `_INTAKE_LLM_SEM`, Retry/Backoff in `llm.py`, Pi-FIFO. Reflexionskosten → deterministisch
immer, LLM nur ab 8 Tool-Aufrufen, idempotent.

## Verifikation (gesamt)
Je Paket: die genannten Tests + CI-Subset `pytest -q --ignore=tests/test_search_integration.py
--ignore=tests/test_db.py` (Stand 856 grün) + `tsc` für TS + Opus-Review; nach jedem Merge Suite
auf `main`. WP7 ist das Gate mit Pass/Fail je Punkt; Fails gehen zurück ans besitzende Paket.
