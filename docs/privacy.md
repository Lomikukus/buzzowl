# Privacy and data protection (for operators)

You run this instance, so you are the data controller. This page says exactly
what Buzzowl stores, what leaves your machine, and what you have to decide. It is
practical guidance, **not legal advice** — for a formal assessment ask a lawyer.

## What is stored, and where

Everything is in your PostgreSQL database. There is no vendor backend, no
telemetry, no phone-home.

| Data | Table | Personal data? |
|---|---|---|
| Companies you track | `clients` | usually not |
| People at those companies: name, role, email, phone, notes | `contacts` | **yes** |
| Meetings: transcripts, summaries, commitments | `documents` (`type='meeting'`) | **yes** — and often other people's words |
| Agent research and OSINT findings, incl. facts about named individuals | `documents` (`research`, `osint`, `finding`, `signal`) | **often yes** |
| Outreach mails and their state | `documents` (`type='outreach'`) | **yes** |
| Contact history, tasks, deals | `contact_log`, `user_tasks`, `deals`, `deal_events` | **yes** |
| Your own users | `users` (password hashes, settings) | **yes** |
| Every agent run, its task and its sources | `agent_runs` | indirectly |
| Prompt/usage logs for evaluation | `prompt_log`, `llm_usage_events` | prompts can contain personal data |

## What leaves your server

| Goes to | What exactly | How to avoid it |
|---|---|---|
| Your LLM provider (OpenRouter, OpenAI, Anthropic …) | the prompt: client profile, research snippets, meeting text, mail drafts — whatever the task needs | run a local model (Ollama/LM Studio) via `base_url`; then nothing leaves |
| SearXNG → the search engines it queries | search terms, i.e. company and person names | self-host SearXNG (the default) and pick engines deliberately |
| Websites the agent fetches | a normal page request from your IP (the browser container) | disable the browser stack; it still fetches over plain HTTP |
| Your SMTP/IMAP provider | outreach mails and replies | your own mail server |
| Telegram (only if configured) | notification texts and research reports you asked for | leave `TELEGRAMBOT` empty |
| A sharing partner (only if you share a client) | that client's profile, research, findings, signals — never contacts, notes, meetings, mails, deals, tasks | do not share, or share fewer clients |

Nothing else is transmitted. The database port is not published, and the browser
and agent containers listen on loopback only.

## The GDPR picture for an EU operator

- **Controller**: you (or your employer). Your LLM provider, mail provider and
  hosting provider are **processors** — you need a data processing agreement
  (DPA) with each. All major LLM providers offer one; check whether the plan you
  are on includes it, and whether your prompts are used for training (turn that
  off).
- **Legal basis** for researching business contacts is normally **legitimate
  interest** (Art. 6(1)(f)) for B2B sales. Do the balancing test and write it
  down. Special categories (health, political opinion, …) must never be collected
  — instruct your agents accordingly and delete such findings when you see them.
- **Art. 14 information duty**: the people you research did not give you their
  data. When you first contact them, tell them where the data came from — your
  first outreach mail is the natural place for one sentence and a link to your
  privacy notice.
- **Data subject rights**: access, rectification, erasure, objection. There is no
  self-service portal for this — you answer manually. See below.
- **Records of processing** (Art. 30): keep a short entry describing this system,
  its purposes, categories of people, recipients (your providers) and retention.
- **Transfers outside the EU/EEA**: most LLM providers process in the US. That
  needs a transfer mechanism (usually the provider's SCCs, or their EU region if
  offered). A local model avoids the question entirely.
- **Recording meetings**: transcripts of a call include everyone on it. In
  Germany and most of the EU you need everyone's consent before recording — ask
  at the start of the call, and note the consent.

## Answering a data subject request

Find everything about a person:

```bash
docker compose exec -T db psql -U whisper -d whisper -c "
  SELECT id, name, email, company FROM contacts WHERE name ILIKE '%Doe%' OR email ILIKE '%doe%';"

docker compose exec -T db psql -U whisper -d whisper -c "
  SELECT id, type, title, created_at FROM documents
   WHERE content ILIKE '%Jane Doe%' ORDER BY created_at DESC;"
```

Delete a contact and their traces (adapt to what you found; run it in a
transaction and check the counts before committing):

```sql
BEGIN;
DELETE FROM contact_log WHERE contact_id = <id>;
DELETE FROM contacts     WHERE id = <id>;
-- documents that are *about* the person, not just mentioning them:
DELETE FROM documents WHERE id IN (<ids you reviewed>);
COMMIT;
```

Deleting a client cascades to its documents and contacts through the UI. What it
does **not** do: remove the person from an already-sent mail, from your backups
(they age out), or from a sharing partner's instance — tell the partner, that is
the Art. 19 duty.

## Retention

Operational telemetry expires on its own: a nightly job prunes `prompt_log`
(180 days by default) and `agent_runs` (90 days, with the tool-call payloads
stripped after 14) — windows and the off switch are the `retention` block in
`config.yaml`, described in [backup-restore.md](backup-restore.md).

Knowledge never expires on its own. Decide a retention period for it (e.g.
meetings 24 months, research 12 months), tighten `retention.prompt_log_days` if
3 months is your policy, and enforce the rest with a cron job:

```sql
DELETE FROM documents WHERE type = 'research' AND created_at < now() - interval '12 months';
```

## Zero-third-party setup

Fully local, nothing leaves the machine: point every `llm.roles` entry at a local
Ollama or LM Studio (`base_url: http://host.docker.internal:11434/v1`,
`api_key: local`), set the embedding backend to the same local server, keep
SearXNG self-hosted, and leave SMTP/IMAP and Telegram unconfigured. Research
quality drops with a small local model — that is the trade-off.

## Checklist before you point it at real people

- [ ] DPA with your LLM provider, training on your data switched off
- [ ] Legitimate-interest balancing test written down
- [ ] Privacy notice published, and linked from your first outreach mail
- [ ] Retention periods decided and automated
- [ ] Access restricted: strong admin password, TLS in front, DB port closed
- [ ] `BUZZOWL_SECRET_KEY` and backups stored safely
- [ ] Consent process for recording meetings
- [ ] You know how to answer an erasure request (above)
