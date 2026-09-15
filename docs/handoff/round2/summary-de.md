# Buzzowl Runde 2 – Zusammenfassung der Arbeit (13.–15. September 2026)

Stand: lokaler `main` = GitHub-Branch `test/round2` (`1fd7463`), 1278 Tests grün, TypeScript sauber, auf der Test-Instanz deployed. `main` auf GitHub ist unverändert.

## 1. Auftrag

Deutsche Unternehmen testen; Stellensuche bei jedem Kunden sofort und parallel zur Recherche, Karriereseite selbstständig finden; finaler Report erst, wenn alle Informationen da sind; ein selbstverbessernder Agent mit Best Practices je Kunden-Website; verlässliche, belegbare Informationen je Prospect, gematcht auf die Produkte; News zu Kunden; alles auf dem ChatGPT-Abo.

Deine Entscheidungen: Testfirmen Miele, Trumpf, DATEV, Vorwerk (plus Festo als frischer Intake); Playbook je Website plus übergreifende Lehren mit Freigabeschleife; Report-Gate: warten, nach 25 Minuten trotzdem schreiben und kennzeichnen, einmal nachziehen; alle 29 Produkte, kein Fokus.

## 2. Arbeitsweise

Fable (diese Sitzung) als Manager und Planer. Je Arbeitspaket ein Sonnet-Entwickler in einem eigenen Git-Worktree, vor jedem Merge ein adversarialer Opus-Review mit eigenen Messungen (Mock-HTTP, In-Memory-Datenbank, Zustandsautomaten-Simulation). 14 Arbeitspakete, 40 Review-Runden, mehrere Pakete brauchten zwei bis vier Runden. Drei Testfahrten auf deiner Instanz durch Opus-Agenten mit Pass/Fail-Bericht. Nichts wurde ohne deine Anweisung gepusht.

## 3. Was gebaut wurde

### Intake-Sammelpunkt (WP5, WP10)
- Neuer oder neu angestoßener Kunde startet vier Teile parallel: OSINT und Recherche auf agent-pi, Stellen und News als Python-Textaufrufe.
- Zustand in `clients.metadata.intake`, atomar je Teil (`jsonb_set`), Brief-Übergang per Compare-and-Set: genau ein Brief, auch bei vier gleichzeitigen Callbacks.
- 25-Minuten-Uhr startet erst, wenn ein Pi-Teil wirklich läuft (zwei Slots, Warteschlange); 90 Minuten absolute Obergrenze; Sweeper alle 60 Sekunden als Rettung bei verlorenem Callback.
- Partial-Brief mit einmaliger Nachbesserung, wenn alle Teile da sind; ein gescheiterter Teil wird als „Not collected" genannt, nie als falsches „refreshes automatically". Manueller Brief schließt den Intake.
- Intake-Leiste auf der Kundenseite, `GET /api/clients/{name}/intake`, exakte Kundennamen auf den Skript-Endpunkten.

### Stellen und Karriereseite (WP2, WP8, WP12, WP13)
- Kaskade: Playbook, Pi-Run-Kandidat, bekannte URL, Homepage-Links, Pfad-Probe auf der eigenen Domain (`/karriere`, `karriere.<domain>` usw.), Sitemap, zuletzt SearXNG. Nur eigene Domain (inklusive erkannter Umleitungs-Domain wie vorwerk.de auf vorwerk.com) oder bekannte Bewerber-Systeme werden akzeptiert.
- Junior-Filter (Ausbildung, Praktikum, Werkstudent, Bachelorand …) mit IT/Management-Ausnahme; Erkennung reiner Studierenden-Boards; Titel von der Stellenseite nachgeladen und bereinigt.
- Fehlschläge werden vermerkt statt verschwiegen; kein guter Scan wird mit leer überschrieben; 7-Tage-Sperre für zuletzt erfolglose URLs.
- Brief bekommt eine Sektion „Hiring Signals", der Match-Bericht bekommt offene Rollen und datierte News als Belege.

### News (WP1, WP3, WP9, WP12, WP13)
- SearXNG-Anbindung mit News-Kategorie und Zeitraum, `publishedDate` wird durchgereicht.
- Python-Pipeline je Kunde: Kandidaten, ein Bewertungsaufruf, Signale mit Datum, Quelle, Relevanz; Marktscan ohne Pi-Slot.
- Eigene Newsroom-Stufe (funktioniert auch bei gesperrten Suchmaschinen), Datumsgewinnung von der Artikelseite, ehrliche Fehlermeldung, wenn die Mehrheit der Engines gesperrt ist, statt stillem „fertig".
- Quellensuche nur auf der eigenen Domain, Impressum und Co. gefiltert.

### Selbstverbessernder Agent (WP4)
- `site_playbook` je Website: Karriere- und Newsroom-URLs, gute Suchanfragen, gesperrte URLs, Navigationsnotizen, Domain-Aliase. Jeder Recherche-Lauf liest es (Aufgabentext plus Browser-Hinweis) und schreibt es nach dem Lauf fort; Python-Scanner speisen es ebenfalls.
- `agent_lessons`: wöchentlich vorgeschlagene übergreifende Lehren, nie automatisch freigegeben; Admin-Freigabe über API und Reiter „Lessons" auf der Agents-Seite; nur freigegebene Lehren landen in Aufgabentexten.

### Abruf-Infrastruktur (WP0, WP11, Infra)
- Bug-Sweep: interne API warf 500, totes Tool in drei Aufgabentexten, Sitemap-Guard, `wait_ms`, alle Brain-Aufrufe über die Abo-Brücke.
- Python-Abrufe fallen bei 403/503 oder leeren Seiten auf browser-service und dann Camoufox zurück; Abruf-Log mit Stufe je URL unter `GET /api/agents/fetch-log`.
- Camoufox war auf der Test-Instanz zwei Wochen defekt (halb entpacktes Image, Healthcheck trotzdem grün). Neu gebaut, Healthcheck prüft jetzt den Browserstart, Build-Skript verweigert unvollständige Images, Troubleshooting-Eintrag. Pin auf Upstream v1.16.0 / Camoufox 152.0.4-beta.28 angehoben und verifiziert.
- SearXNG-Image aktualisiert; die Suchmaschinen sperren die Instanz trotzdem regelmäßig per CAPTCHA.

### Doku (WP6)
`docs/agents.md` (Laufarten, Dokumenttypen, Intake-Lebenszyklus, Playbooks und Lehren), `ARCHITECTURE.md` (Sammelpunkt), `docs/troubleshooting.md` (Partial-Brief, keine Karriereseite, halb gebautes Browser-Image), `config.yaml` (`intake_deadline_min`, `intake_absolute_cap_min`).

## 4. Ergebnisse der drei Testfahrten

| Kunde | Karriereseite | Stellen | News-Signale |
|---|---|---|---|
| Miele | karriere.miele.de | 20 | 0–2 |
| Festo | jobs.festo.com | 18 | 0 |
| DATEV | Workday Careers | 8 | 1 |
| Trumpf | Workday, noch das Studierenden-Board | 5 | 2–4 |
| Vorwerk | career.vorwerk.de gefunden, keine Stellen extrahiert | 0 | 0–1 |

Durchgängig bestanden: ein Brief je Kunde, keine doppelten Läufe, kein Absturz, kein Rate-Limit am Abo, Lehren-Schleife von Vorschlag bis Injektion, Fehlerisolation (ein gescheiterter Teil blockiert nie den Brief).

## 5. Offen

1. **Vorwerk**: Karriere-Host gefunden, aber das Portal liefert keine Stellenliste an den Extraktor. Manueller Eintrag oder eine weitere Stufe für JS-Jobportale.
2. **Trumpf**: hängt am Studierenden-Board; wechselt auf das Professionals-Board, sobald ein Pi-Lauf es abruft und das Playbook den Kandidaten liefert.
3. **News-Menge** hängt an der Suchinfrastruktur (CAPTCHA-Sperren). Robust wären ein zweiter SearXNG mit anderer IP oder ein News-API-Schlüssel.
4. **Python-Anreicherungsschleife** braucht Tool-Calling und läuft daher nicht über das Abo (bekannt seit Runde 1).
5. **Embeddings** brauchen weiterhin einen API-Schlüssel (Volltextsuche funktioniert).
6. Worktree `buzzowl-wt-spine` mit 18 Zeilen alter Konfig-Experimente, unangetastet.

## 6. Nachweise

Testfahrt-Berichte: `wp7_report.md`, `wp7b_report.md`, `wp7c_report.md` (Sitzungs-Scratchpad, mit Rohbelegen). Plan: `~/.claude/plans/mutable-greeting-clock.md`. Gedächtnisnotiz: `buzzowl-round2-state.md`.
