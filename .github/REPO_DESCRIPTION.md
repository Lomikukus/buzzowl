# GitHub repository metadata for `buzzowl`

Copy these into the GitHub "Create repository" form / repo settings.

## Description (≤ 350 chars, shown under the repo name)

```
Self-hostable, agentic research & knowledge platform for sales teams. Turns meetings into client knowledge, keeps it fresh with autonomous research agents, and tells reps who to contact next and why. Bring your own LLM (Anthropic, OpenAI, OpenRouter, or local Ollama/LM Studio). AGPL-3.0.
```

## Short one-liner (for the "About" sidebar / social preview)

```
The agentic sales research platform you can run yourself.
```

## Topics (tags)

```
sales, crm, ai-agents, agentic, llm, self-hosted, open-source, research, osint,
knowledge-base, postgresql, pgvector, fastapi, docker, ollama, openrouter,
bring-your-own-llm, sales-intelligence, meeting-transcription
```

## Website

Leave empty until the first public release (then the docs/landing URL).

## Settings recommended for the private phase

- Visibility: **Private** (flip to Public with the first working release)
- Default branch: `main`
- Merge strategy: squash merge (keeps history clean until go-public)
- Branch protection on `main`: require PR + passing CI once CI exists
- License detected automatically from `LICENSE` (AGPL-3.0)

## Suggested first release notes (v0.1.0)

Buzzowl v0.1.0 is the first self-hostable cut.

- **One command to run**: `docker compose up` starts the full stack (PostgreSQL +
  pgvector, API server, agent runtime, search, browser fetch). First run
  bootstraps your organization and admin user from `.env`.
- **Bring your own LLM**: one config block covers Anthropic, OpenAI, OpenRouter,
  and any OpenAI-compatible local server (Ollama, LM Studio). One-click
  "Connect OpenRouter" login — no key copy-paste.
- **Autonomous research**: agents research clients from the web and OSINT
  sources on a schedule and write source-linked findings into the knowledge base.
- **Product–client matching** and a daily **next-best-action queue** for reps.
- **Outreach drafts** with product/pain-point context, handed to your mail client.
- Licensed **AGPL-3.0**.

Known limits in v0.1.0: single organization per install; outreach ends at a
ready-to-send draft (no sending); in-Docker mic transcription is an optional
build variant without speaker diarization.
