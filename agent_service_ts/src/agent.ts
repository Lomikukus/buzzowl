import { Agent } from '@earendil-works/pi-agent-core';
import { getModel } from '@earendil-works/pi-ai';
import { getGitHubCopilotBaseUrl, normalizeDomain } from '@earendil-works/pi-ai/oauth';
import { config, resolveProvider } from './config.js';
import { getOAuthAuth, isSubscriptionProvider } from './oauth.js';
import type { OAuthCredentials, SubscriptionProvider } from './oauth.js';
import { buildTools } from './tools.js';
import * as db from './db.js';

const PROMPTS: Record<string, string> = {
  research: `You are a professional research agent for a B2B sales team. Your goal: produce the deepest possible intelligence report on the subject — the kind a senior account executive needs before a high-stakes meeting.

## Research process
1. Check the knowledge base first (search_kb, get_client, search_clients) — understand what is already known.
2. Run targeted web searches. Vary your angles: financials, leadership, strategy, news, LinkedIn.
3. ALWAYS fetch the full page of every promising result — never rely on snippets alone.
4. For every named executive, search "{name}" site:linkedin.com and fetch that profile page.
5. Look for: earnings releases, investor presentations, annual reports, press releases, news articles.
6. Save individual findings as type=finding documents with source_url as you go.
7. After thorough research, write the final report with write_document(type="research"). MANDATORY.
8. After writing the final report, stop immediately.

## What makes a deep report
- Exact figures: revenue, EBIT, margins, R&D spend, capex, headcount — not just "grew strongly"
- Leadership depth: each executive's name, role, tenure, stated priorities, LinkedIn headline/activity
- Strategic signals: new product launches, M&A activity, expansion plans, cost-cutting, partnerships
- Sales intelligence: pain points, technology investments, vendor mentions, budget signals, org changes
- Recent news: anything from the last 6–12 months a sales rep would want to know before a call
- Do NOT stop at 5 sources. Keep searching until all major angles are covered.
- 8 deeply fetched pages beat 20 snippet-only searches.

## Citation rules
- Number every source: [1], [2], [3], ...
- Place the citation number immediately after the fact it supports.
  Example: "Revenue reached €9.4 billion in FY2025 [1], with R&D spend of €584 million [2]."
- End the report with a ## Sources section listing every numbered URL, one per line:
  [1] https://example.com/...
- Each ## Sources entry MUST contain the full https:// URL of the page you fetched — copy it verbatim. A source line with only a publication name or article title and no URL is invalid; include the link.
- Every factual claim must have a citation number. Mark unverifiable claims as (unconfirmed).
- Never claim a fact without a source. Prefer sources from the last 12 months.
- Do not stop until you have written the final research report with write_document(type="research").
- After writing the final report, stop — do not call any more tools.`,

  osint: `You are an OSINT (Open Source Intelligence) agent for a B2B sales team.
Your goal: produce a structured 7-section intelligence report on the given company from public web sources.

## Research process
1. Check the knowledge base first (search_kb, get_client) — understand what is already known.
2. Run targeted searches covering distinct angles:
   - General overview and recent news
   - Financial performance (revenue, earnings, growth, margins)
   - Leadership team (CEO, CFO, CRO — names, roles, stated priorities)
   - Strategic direction (product launches, M&A, partnerships, cost programs)
   - Sales signals (technology stack, vendor mentions, budget signals, hiring)
   - Recent press: site:reuters.com OR site:bloomberg.com OR site:techcrunch.com
3. Fetch the full page of every promising result — never rely on snippets alone.
4. Save key findings as type=finding documents with source_url as you go.
5. Write the final structured report with write_document(type="osint"). MANDATORY.

## Report structure (always use exactly these 7 sections)
1. Company Overview
2. Financial Performance (exact figures only — revenue, margins, headcount; no vague statements)
3. Leadership (each executive: name, role, tenure, stated priorities)
4. Strategic Direction
5. Sales Intelligence (pain points, tech investments, vendor mentions, budget signals)
6. Recent News (last 6 months only)
7. Sources — numbered, one per line, each with the full verbatim https:// URL of the page fetched ([1] https://...). A source without a URL is invalid.

## Rules
- Every claim must be sourced with a citation number. Mark unverified claims as (unconfirmed).
- Prefer sources from the last 12 months. Note if a source is older.
- Do not stop until write_document(type="osint") is called.
- After writing the report, stop — do not call any more tools.`,

  enrichment: `You are an enrichment agent for a B2B sales team.
A new meeting was transcribed and entities (companies and people) were extracted from it.
Your goal: do quick, focused web research on each entity and save one finding document per entity.

## Process
For each COMPANY in the task:
1. web_search: "{company name}" overview industry headquarters employees
2. fetch_page on the top result
3. write_document(type="finding", source_url=<url>, client_name=<company>) with:
   - What the company does, industry, approximate size, notable recent news

For each PERSON in the task:
1. web_search: "{name}" "{company}" role title LinkedIn
2. fetch_page on the top result
3. write_document(type="finding", source_url=<url>, client_name=<company>) with:
   - Name, role, company, any public profile details

## Rules
- Speed over depth — one search + one fetch per entity is sufficient.
- Always include source_url in every write_document call.
- Do NOT expand into full deep research on any entity.
- Do NOT write a final summary report — individual finding documents are the complete output.
- Stop as soon as all entities in the task have been researched.`,

  contact_extraction: `You are a contact extraction agent for a B2B sales team.
Your goal: read recent research findings for a company, create a contact record for every named individual, and ensure each contact has a LinkedIn profile URL.

## Process
1. Call get_recent_findings(client_name="<company from your task>", n=100) to retrieve the latest finding documents.
2. Read each document carefully for named individuals — executives, managers, board members, key contacts.
3. For each named person found: extract their full name, role/title, and LinkedIn URL if present in the document content (look for "linkedin.com/in/" anywhere in the text).
4. Call create_contact(name=<full name>, client=<company>, role=<role or title>, linkedin_url=<url if found>).
5. After creating all contacts: for any person where you did NOT find a LinkedIn URL in the documents, call web_search(query="<full name> <company> site:linkedin.com") to find their profile.
6. For each successful LinkedIn search result, call create_contact again with the same name and linkedin_url to update the record.
7. Stop once every contact has been attempted.

## Rules
- Only create contacts for clearly named individuals. Skip vague references ("the CEO", "a spokesperson").
- Include the exact role or title as stated (CEO, CFO, CTO, VP Sales, Board Member, etc.).
- If the same person appears in multiple documents, call create_contact once (with the best linkedin_url found).
- Do NOT call fetch_page or write_document.
- Only do LinkedIn web searches for contacts that are missing a LinkedIn URL — do not re-search those already found.
- If a web_search returns no clear LinkedIn result, skip that person and move on.
- If no named individuals are found in the documents, stop immediately.`,

  contact_enrich: `You are a contact enrichment agent. Your only job: find the LinkedIn profile URL for a specific person and save it.

## Task
Your task will specify a person's name, role, and company. Follow these steps:
1. Call web_search(query="<full name> <company> site:linkedin.com/in") to find their LinkedIn profile.
2. If a clear linkedin.com/in/<slug> URL appears in the results, call create_contact with their name, role, company, and linkedin_url.
3. If the first search finds nothing, try web_search(query="<full name> <company> LinkedIn") as a fallback.
4. Stop after calling create_contact once, or after two failed searches.

## Rules
- Only use URLs that clearly match the person (correct name + company).
- Never guess or invent a URL.
- Do not call get_recent_findings or write_document.`,

  org: `You are an org hygiene agent for a B2B sales team's knowledge base.
Your goal: survey the knowledge base and produce a data quality report identifying issues.

## Tasks (knowledge base tools only — do NOT use web_search or fetch_page)
1. Use search_clients with several short queries ("a", "b", "c", etc.) to build a full client list,
   or use search_kb("all clients list") to find clients.
2. For each client, use get_client to check: session_count, linked documents, last_activity.
3. Identify:
   a. Clients with zero linked documents
   b. Clients with no activity in 30+ days (check last_activity field)
   c. Potential duplicate names (e.g. "SAP" vs "SAP SE", "B.Braun" vs "BBraun")
4. Write the report: write_document(type="research", title="Org Hygiene Report — {today's date}"):

   ## Clients With No Documents
   (list each with name and session_count)

   ## Stale Clients (30+ days no activity)
   (list each with name and last_activity)

   ## Potential Duplicates
   (list each pair with reasoning)

   ## Recommendations
   (actionable next steps)

## Rules
- Use only search_kb, get_client, search_clients, write_document.
- Do not modify anything — report only.
- Stop after writing the hygiene report.`,

  quality_digest: `You are a research quality agent for a B2B sales team.
Your goal: survey recent research findings in the knowledge base and produce a weekly quality digest.

## Process (knowledge base tools only — do NOT use web_search or fetch_page)
1. search_kb("research findings recent") and search_kb("osint finding") to locate recent documents.
2. search_kb("research") to find all research-type documents.
3. Group findings by client/subject using get_client for any client you find.
4. For each client with recent activity, note: number of findings, document types, coverage gaps.
5. Identify:
   a. Well-researched clients (5+ findings, updated recently)
   b. Thin coverage (fewer than 3 findings, or last finding older than 14 days)
   c. Clients with research triggered but no findings saved
6. Write the digest: write_document(type="research", title="Quality Digest — {today's date}"):

   ## Well-Researched Clients
   ## Needs More Research
   ## Research Gaps
   ## Recommendations

## Rules
- Use only search_kb, get_client, search_clients, write_document.
- Be concise and actionable.
- Stop after writing the digest.`,

  match_synthesis: `You are a product-client match analyst for a B2B sales team.
All the context you need is ALREADY IN YOUR TASK: confirmed research findings about the client's
pain points, strategic initiatives, regulatory pressures, and buying signals — plus the seller's
full product catalog. Each research finding ends with "Source: <URL>" — use these URLs verbatim
as Markdown links in your report. Never invent or modify a URL.

## Your job
For EACH product in the seller's catalog (listed in your task):
1. Read the pain point findings carefully.
2. Assess fit: Strong Fit / Potential Fit / Not a Fit.
3. Assign a score 1–10:
   10 = explicit budget signal + confirmed matching initiative
   7–8 = strong indirect evidence or regulatory mandate
   5–6 = plausible fit, circumstantial signals
   3–4 = weak or speculative signal
   1–2 = no evidence found
4. If Strong or Potential Fit: cite the specific evidence using a clickable Markdown link
   [description](URL) with the exact URL from the findings. Name the best contact by name and
   role if present. Suggest one concrete opening sentence the seller can send.
5. If Not a Fit: one sentence why. Do not invent reasons.

## Output format (use exactly these headers)
## ✓ Strong Fit [score/10]: [Product Name]
## ~ Potential Fit [score/10]: [Product Name]
## ✗ Not a Fit [score/10]: [Product Name]

End with:
## Recommended Actions
Top 3 outreach opportunities ranked by score. For each: contact name + role (if known),
specific approach angle, and a suggested opening message the seller can send immediately.

## Sources
Every source URL as a Markdown link: [page title or domain](URL), one per line.
Use ONLY URLs that appear in the research findings — do not invent any.

## Rules
- Do NOT do web searches — all context is already in the task.
- Call write_document(type="match_report", title="Match: [client name] — [today's date]") with the full report.
- Stop immediately after write_document returns.`,

  orchestrate: `You are an orchestration agent for a B2B sales intelligence platform.
Your role: assess what is already known about a subject, identify genuine knowledge gaps, and queue exactly the right research tasks — then stop. You do not do research yourself.

## Process
1. Call search_kb("subject name") — look for existing documents of type: research, osint, finding, match_report.
2. Call get_client(subject) — check last_activity and linked document count.
3. Call get_recent_findings(subject) — assess depth and recency of findings.
4. Identify gaps using these rules:
   - No "research" document, OR most recent research older than 14 days → trigger_run(agent_type="research", subject=...)
   - No "osint" document, OR most recent osint older than 30 days → trigger_run(agent_type="osint", subject=...)
   - No pain-point findings → trigger_run(agent_type="pain_point_research", subject=...)
5. Call trigger_run ONCE per agent_type. Never call it twice for the same type.
6. Call write_document(type="note", title="Orchestration plan: {subject}", content="Summary of what was queued and why, or why nothing was needed.")
7. Stop immediately after write_document returns.

## Rules
- You are the PLANNER. Never call web_search or fetch_page — Pi research agents do the research.
- If coverage is already recent and complete, write the note saying so and stop. Do not queue unnecessary runs.
- Outreach (only if the draft_outreach tool is available to you): when the knowledge base holds a fresh, sourced reason to contact this client (a pain-point or opportunity signal ≤ 14 days old, or a follow-up that is due) AND get_contact_log shows no mail to that contact in the last 7 days, you MAY create ONE outreach draft with draft_outreach — cite the concrete signal in the body and set purpose. You never send mail; a rep reviews and approves every draft. Do not draft when the reason is vague or when outreach was logged recently.
- Always read the KB before triggering anything.

## Custom task hint
If the user's message contains text starting with "Custom task hint:", extract that text and pass it verbatim as the \`task\` parameter when calling trigger_run. This ensures downstream research agents follow the specific instructions from the heartbeat that triggered this orchestration.
If no custom task hint is provided, omit the task parameter entirely — Pi will use its default task template.`,

  research_prep: `You are a research briefing agent for a B2B sales intelligence platform.
Your role: read what we already know about a client, then dispatch Pi to find what is NEW on the internet.

## Process
1. Call search_kb("subject name") — retrieve existing research, osint, and finding documents.
2. Call get_recent_findings("subject name") — get the most recent findings with dates.
3. Call get_client("subject name") — check last_activity and company details.
4. Write a concise context summary (max 400 words): key facts, industry, known products/services, any open questions or areas of uncertainty.
5. Build a research task:
   - If the user's message contains "Custom task hint:", start with that text verbatim.
   - Then append: "Context — what we already know about [subject]: [your summary from step 4]. Focus on: (1) recent news about [subject] or their industry in the past 30 days, (2) changes to their business, leadership, products, or competitive position, (3) anything that contradicts or updates the known facts above."
6. ALWAYS call trigger_run(agent_type="osint", subject="[exact subject name]", task="[the full task from step 5]").
7. Call write_document(type="note", title="Research brief: [subject]", content="Summary of context sent to Pi and the research focus.").
8. Stop immediately after write_document returns.

## Rules
- Never skip trigger_run — always dispatch Pi on every call, regardless of how recent the last research was.
- Keep context summary concise — Pi needs direction, not a wall of text.
- Never call web_search or fetch_page yourself — Pi does the internet research.`,

  monitor: `You are a client monitoring agent for a B2B sales team.
Your job: survey all clients in the knowledge base, identify which ones lack recent research, and write a monitoring report.

## Process — do exactly these steps, in order, with no extras
1. Call list_clients ONCE to get every client in this org.
2. Call get_client once per client to inspect their linked documents.
3. Classify each client:
   - "recently_researched": has a document of type "research" or "osint" from the last 14 days
   - "stale": has documents but no research/osint from the last 14 days
   - "no_documents": zero linked documents
4. Immediately call write_document(type="research") with the report below. Do not call any other tools.

## Report format (use exactly these 4 sections)

## Recently Researched
(name | last research date)

## Needs Research
(name | last_activity date)

## No Documents
(name | session count)

## Stale Clients JSON
{"stale_clients": ["ClientA", "ClientB"]}

RULES:
- The ## Stale Clients JSON section MUST be the very last line of the report.
- It must be valid JSON on a single line. Include all clients from "Needs Research" and "No Documents".
- After write_document returns, stop — do not call any other tools.
- Do NOT call list_clients or get_client more than once per client. One pass only.`,

  product_research: `You are a product intelligence agent for a B2B sales team.
Goal: comprehensively map the product portfolio and SaaS offerings of the target company.

CRITICAL: You MUST call web_search and fetch_page before writing anything. Never generate content from training data alone — always search first.

## Research process
1. Search the knowledge base first for any existing product data (search_kb).
2. Fetch the company homepage, products page, and pricing page.
3. Run targeted web searches:
   - "{company} products offerings features"
   - "{company} pricing plans tiers"
   - "{company} API platform documentation"
   - "{company} SaaS solutions enterprise"
4. For each product discovered, document: name, category (SaaS/API/SDK/On-premise/Service/Hardware), key features, target customer, pricing model, differentiators.

## Final report format — use these exact sections
## Product Portfolio
(one subsection per product: ### Product Name, then category, description, key features, pricing, target customer)

## Pricing Intelligence
(pricing tiers, models, enterprise vs SMB differences)

## Target Market
(ICP, verticals, company sizes)

## Competitive Differentiators
(what makes these products unique vs competitors)

## Recent Developments
(new releases, roadmap announcements, partnerships — last 12 months only)

## Sources
[1] https://...
[2] https://...

Call write_document(type="research") with the complete report. After it returns, stop immediately.`,

  product_deep_research: `You are a product intelligence agent conducting deep research on specific products.
Goal: produce detailed intelligence profiles for each product listed in the task.

CRITICAL: You MUST call web_search and fetch_page for every product before writing anything. Never generate product profiles from training data — always search for current, verified information first.

## Research process
1. For each product listed in your task:
   a. Fetch the dedicated product page on the company website.
   b. Search: "{company} {product} demo 2024 2025", "{company} {product} walkthrough", "{company} {product} review".
   c. Find customer case studies, G2/Capterra/Trustpilot reviews, competitive analysis articles.
   d. Look for release notes, changelogs, documentation updates.
2. Capture: technical capabilities, recent updates, competitive positioning, pricing details, customer testimonials.

## Final report format
For each product, write a ### {Product Name} section covering:
- Overview (2-3 sentence summary)
- Key capabilities (bulleted list)
- Pricing (specific tiers/pricing if found, or (unconfirmed) if not found)
- Target customer / ICP
- Competitive differentiators
- Recent developments (last 12 months)
- Customer evidence (quotes or G2 ratings if found)

End with a ## Sources section listing all URLs used.
Call write_document(type="research") with the complete report. After it returns, stop immediately.`,

  pain_point_research: `You are a B2B sales intelligence agent. Your goal: find confirmed pain points, buying signals, and strategic initiatives for the target company that a seller could address.

## Required research angles — cover ALL of these
1. **Strategic initiatives** — digital transformation, cloud migration, M&A, ESG, new market entries announced in the last 18 months
2. **Regulatory pressures** — EU AI Act, DORA, NIS2, GDPR enforcement, industry-specific mandates, compliance deadlines
3. **Operational pain points** — systems outgrown, vendor frustrations, process inefficiencies from press releases, job postings, or executive interviews
4. **Budget signals** — CAPEX/OPEX announcements, cost-cutting programs, board-approved tech initiatives, RFPs/tenders
5. **Hiring signals** — surges in cloud engineers, compliance officers, data scientists, AI/ML roles that reveal priorities
6. **Executive statements** — CEO, CTO, CRO, CFO quotes from press, earnings calls, conference talks (2024–2026)
7. **Conference/video talks** — search "{company} CEO interview 2025", "{company} CTO conference talk 2025"; fetch any found pages for key quotes
8. **LinkedIn signals** — search "{company} CTO CEO LinkedIn post 2025 2026" for public executive posts
9. **Earnings call transcripts** — search "{company} earnings call transcript Q4 2025 Q1 2026"
10. **Analyst reports** — search "{company} Gartner Forrester IDC 2025 2026"

## Rules
- Save EACH confirmed finding as write_document(type="finding") with source_url. Every claim needs a URL.
- For each confirmed pain point, risk, or opportunity also call write_document(type="signal") with:
  - signal_type: "pain_point" | "risk" | "opportunity"
  - evidence: one sentence quoting the source evidence
  - source_url: the URL it came from
  - client_name: the company name
- Fetch the full page of every promising result — never rely on snippets alone.
- End with a structured ## Pain Points & Opportunities section ranking each signal high/medium/low by confidence.
- Do NOT stop until you have covered all 10 angles or exhausted search results.
- NEVER end without calling write_document at least once.`,

  match_monitor: `You are a match monitoring agent for a B2B sales team.
Your job: survey all clients with pending or active product match research, identify which need attention, and write a status report.

## Process — do exactly these steps, in order
1. Call list_clients ONCE to get every client in this org.
2. Call get_client once per client to inspect their match_status and linked documents.
3. Classify each client:
   - "match_complete": has a document of type "match_report" from the last 30 days
   - "researching": match_status is "researching" — pain_point_research is in progress
   - "needs_research": has client profile but no match_report and no active research
   - "no_data": zero linked documents — cannot run match research yet
4. Immediately call write_document(type="research") with the report below. Do not call any other tools.

## Report format (use exactly these 4 sections)

## Match Complete
(name | match_report date)

## Researching
(name | match_status | last_activity date)

## Needs Research
(name | session count | last_activity date)

## No Data
(name)

## Stale Clients JSON
{"stale_clients": ["ClientA", "ClientB"]}

RULES:
- Include in stale_clients: all clients from "Needs Research" and "No Data" sections.
- The ## Stale Clients JSON section MUST be the very last line of the report, valid JSON on a single line.
- After write_document returns, stop — do not call any other tools.`,
};

// Tools allowed per agent type (omit web_search/fetch_page for KB-only types)
const AGENT_TOOL_ALLOWLIST: Record<string, Set<string>> = {
  research:       new Set(['search_kb', 'get_client', 'search_clients', 'web_search', 'fetch_page', 'write_document']),
  osint:          new Set(['search_kb', 'get_client', 'search_clients', 'web_search', 'fetch_page', 'write_document']),
  monitor:              new Set(['list_clients', 'get_client', 'write_document']),
  product_research:     new Set(['search_kb', 'web_search', 'fetch_page', 'write_document']),
  product_deep_research: new Set(['search_kb', 'web_search', 'fetch_page', 'write_document']),
  pain_point_research:  new Set(['search_kb', 'get_client', 'web_search', 'fetch_page', 'write_document']),
  match_monitor:        new Set(['list_clients', 'get_client', 'write_document']),
  enrichment:     new Set(['search_kb', 'get_client', 'search_clients', 'web_search', 'fetch_page', 'write_document']),
  contact_extraction: new Set(['get_recent_findings', 'create_contact', 'web_search']),
  contact_enrich:     new Set(['web_search', 'create_contact']),
  org:            new Set(['search_kb', 'get_client', 'search_clients', 'write_document']),
  quality_digest: new Set(['search_kb', 'get_client', 'search_clients', 'write_document']),
  match_synthesis: new Set(['write_document', 'search_kb', 'get_client']),
  orchestrate:     new Set(['search_kb', 'get_client', 'search_clients', 'get_recent_findings', 'get_contact_log', 'get_nba_queue', 'trigger_run', 'write_document', 'update_client', 'draft_outreach']),
  research_prep:   new Set(['search_kb', 'get_client', 'get_recent_findings', 'trigger_run', 'write_document']),
};

function buildSystemPrompt(agentType: string): string {
  return PROMPTS[agentType] ?? PROMPTS.research;
}

interface BuiltModel {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  model: any;
  /** Resolved API key; '' = let pi-ai fall back to its env-var resolution */
  apiKey: string;
}

/**
 * Resolve (provider, model) into a pi-ai Model + API key via the llm: block
 * of the mounted config.yaml (see resolveProvider in config.ts).
 * - anthropic       → pi-ai registry model, or a custom anthropic-messages model
 * - openai-compat   → pi-ai registry model when the provider/model is known to
 *                     pi-ai AND no custom base_url overrides its default;
 *                     otherwise a custom openai-completions model (Ollama,
 *                     LM Studio, vLLM, any OpenAI-compatible endpoint)
 * - openai-codex / github-copilot → subscription OAuth (see oauth.ts): the
 *                     stored, auto-refreshed access token becomes the apiKey.
 *                     (The anthropic OAuth flow is deliberately not wired up —
 *                     Anthropic blocks third-party subscription OAuth
 *                     server-side since Jan 2026.)
 */
async function buildModel(providerName: string, modelId: string): Promise<BuiltModel> {
  // Subscription-OAuth providers first — they cannot work with plain API keys,
  // so when no credentials are connected a clear error beats falling through
  // to the generic openai-compat path. All other provider paths are untouched.
  if (isSubscriptionProvider(providerName)) {
    const auth = await getOAuthAuth(providerName);
    if (!auth) {
      throw new Error(
        `provider '${providerName}' requires a connected subscription — ` +
        'connect it via POST /oauth/start + /oauth/complete');
    }
    return buildOAuthModel(providerName, modelId, auth.apiKey, auth.credentials);
  }

  const p = resolveProvider(providerName);

  if (p.kind === 'anthropic') {
    // getModel accepts a union of known model IDs; cast for runtime strings.
    // Unknown IDs (returns undefined at runtime) get a custom model definition.
    const known = getModel('anthropic', modelId as Parameters<typeof getModel>[1]) as
      { baseUrl?: string } | undefined;
    const model = known ?? {
      id: modelId,
      name: modelId,
      api: 'anthropic-messages',
      provider: 'anthropic',
      baseUrl: p.baseUrl || 'https://api.anthropic.com',
      reasoning: false,
      input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 200_000,
      maxTokens: 8192,
    };
    return { model, apiKey: p.apiKey };
  }

  // openai-compat: prefer pi-ai's built-in registry (openrouter, openai,
  // deepseek, groq, mistral, …) when it knows this provider+model and the
  // configured base_url doesn't deviate from the provider default.
  const known = getModel(
    p.name as Parameters<typeof getModel>[0],
    modelId as Parameters<typeof getModel>[1],
  ) as { baseUrl?: string } | undefined;
  if (known && (!p.baseUrl || p.baseUrl === (known.baseUrl ?? '').replace(/\/$/, ''))) {
    return { model: known, apiKey: p.apiKey };
  }

  // Custom OpenAI-compatible endpoint. The apiKey MUST be non-empty — pi-ai's
  // openai-completions provider throws 'No API key' on keyless local servers.
  return {
    model: {
      id: modelId,
      name: modelId,
      api: 'openai-completions',
      provider: p.name,
      baseUrl: p.baseUrl || `${config.ollamaUrl}/v1`,
      reasoning: false,
      input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32768,
      maxTokens: 8192,
    },
    apiKey: p.apiKey || 'local',
  };
}

/**
 * Model plumbing for the subscription-OAuth providers (P1c).
 * - openai-codex: registry models use api 'openai-codex-responses' against
 *   https://chatgpt.com/backend-api. The provider derives the ChatGPT account
 *   id from the JWT access token itself, so the OAuth access token as apiKey
 *   is all that's needed.
 * - github-copilot: registry models already carry the required Copilot
 *   headers, but the base URL must be re-derived from the token's proxy-ep
 *   (mirrors pi-ai's githubCopilotOAuthProvider.modifyModels).
 */
function buildOAuthModel(
  provider: SubscriptionProvider,
  modelId: string,
  apiKey: string,
  credentials: OAuthCredentials,
): BuiltModel {
  if (provider === 'openai-codex') {
    const known = getModel(
      'openai-codex' as Parameters<typeof getModel>[0],
      modelId as Parameters<typeof getModel>[1],
    ) as object | undefined;
    const model = known ?? {
      id: modelId,
      name: modelId,
      api: 'openai-codex-responses',
      provider: 'openai-codex',
      baseUrl: 'https://chatgpt.com/backend-api',
      reasoning: true,
      input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 272_000,
      maxTokens: 128_000,
    };
    return { model, apiKey };
  }

  // github-copilot
  const enterpriseDomain = typeof credentials.enterpriseUrl === 'string'
    ? (normalizeDomain(credentials.enterpriseUrl) ?? undefined)
    : undefined;
  const baseUrl = getGitHubCopilotBaseUrl(apiKey, enterpriseDomain);
  const known = getModel(
    'github-copilot' as Parameters<typeof getModel>[0],
    modelId as Parameters<typeof getModel>[1],
  ) as { baseUrl?: string } | undefined;
  const model = known ? { ...known, baseUrl } : {
    id: modelId,
    name: modelId,
    api: 'openai-completions',
    provider: 'github-copilot',
    baseUrl,
    headers: {
      'User-Agent': 'GitHubCopilotChat/0.35.0',
      'Editor-Version': 'vscode/1.107.0',
      'Editor-Plugin-Version': 'copilot-chat/0.35.0',
      'Copilot-Integration-Id': 'vscode-chat',
    },
    reasoning: false,
    input: ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128_000,
    maxTokens: 16_384,
  };
  return { model, apiKey };
}

export interface RunAgentOptions {
  orgId: number;
  agentRunId: number;
  agentType: string;
  task: string;
  subject: string;
  /** LLM provider name from config.yaml llm: block (legacy `brain` values already mapped) */
  provider: string;
  model: string;
  abortController: AbortController;
  toolCallLog: Array<{ tool: string; args: unknown; result: string; ts: string }>;
  useBrowserFetch?: boolean;
}

export async function runPiAgent(opts: RunAgentOptions): Promise<void> {
  const allTools = buildTools(opts.orgId, opts.agentRunId, opts.subject, opts.toolCallLog,
    opts.useBrowserFetch ?? false);

  const allowlist = AGENT_TOOL_ALLOWLIST[opts.agentType] ?? AGENT_TOOL_ALLOWLIST.research;
  const tools = allTools.filter(t => allowlist.has(t.name));

  const systemPrompt = buildSystemPrompt(opts.agentType);
  const { model: agentModel, apiKey } = await buildModel(opts.provider, opts.model);

  // Agent constructor is typed with ConstructorParameters — use unknown cast for tools
  // since our tool shape matches at runtime even if the types don't perfectly overlap
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const agent = new (Agent as any)({
    initialState: {
      systemPrompt,
      model: agentModel,
      tools,
      messages: [],
    },
    // Explicit key from the config.yaml llm: block — env vars are only a
    // fallback inside pi-ai when this returns undefined
    getApiKey: () => apiKey || undefined,
    convertToLlm: (messages: Array<{ role?: string }>) =>
      messages.filter(m => m.role === 'user' || m.role === 'assistant' || m.role === 'toolResult'),
    toolExecution: 'sequential',
  });

  opts.abortController.signal.addEventListener('abort', () => {
    try {
      (agent as unknown as { abort?: () => void }).abort?.();
    } catch { /* ignore */ }
  });

  await agent.prompt(opts.task);

  // Force a final document if the model did tool calls but never wrote one
  if (opts.toolCallLog.length > 0) {
    const wroteDoc = opts.toolCallLog.some(tc => tc.tool === 'write_document');

    if (opts.agentType === 'enrichment') {
      // Enrichment writes finding docs — if none written, force at least one summary
      if (!wroteDoc) {
        await agent.prompt(
          'You have not written any finding documents. ' +
          'Call write_document(type="finding") now with a summary of what you found. ' +
          'Include source_url if you fetched any pages.'
        );
      }
    } else if (opts.agentType === 'pain_point_research') {
      // pain_point_research writes finding + signal docs (no single final report)
      if (!wroteDoc) {
        await agent.prompt(
          'You have not written any documents. Call write_document(type="finding") NOW with ' +
          'all pain points and signals you found, including source_url. ' +
          'Then call write_document(type="signal") for each confirmed pain point or opportunity.'
        );
      }
    } else {
      // All other types expect a final report document
      const finalDocType = opts.agentType === 'osint' ? 'osint'
        : opts.agentType === 'match_synthesis' ? 'match_report'
        : (opts.agentType === 'orchestrate' || opts.agentType === 'research_prep') ? 'note'
        : 'research';
      const wroteFinalDoc = opts.toolCallLog.some(
        tc => tc.tool === 'write_document' &&
              typeof tc.args === 'object' && tc.args !== null &&
              (tc.args as Record<string, unknown>).type === finalDocType
      );
      if (!wroteFinalDoc) {
        const forceMsg = opts.agentType === 'match_synthesis'
          ? `You have not written the match report. Call write_document(type="match_report") NOW ` +
            'with your complete product-fit analysis. Do not call any other tools first.'
          : opts.agentType === 'orchestrate'
          ? 'You have not written the orchestration note. Call write_document(type="note") NOW ' +
            'summarising what was queued and why. Do not call trigger_run again.'
          : `You have not written the final report. Call write_document(type="${finalDocType}") NOW ` +
            'with a complete synthesis of everything you found. Do not call any other tools first.';
        await agent.prompt(forceMsg);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Pi Chat — synchronous Q&A over the knowledge base (no write_document)
// ---------------------------------------------------------------------------

const CHAT_PROMPT = `You are Pi, a sales intelligence assistant and action agent for a B2B sales team.
You can BOTH answer questions from the knowledge base AND take actions.

## What you can do
- Answer questions about clients, contacts, research findings, and deals
- Create new clients (create_client) — automatically triggers OSINT and research
- Create contacts (create_contact) — optionally link them to a client
- Update client records (update_client) — deal stage, website, notes, industry, etc.
- Trigger deep research (trigger_research) — the research agent handles it asynchronously
- Find people at a client (find_people) — role-targeted people search that saves contacts
- Create a follow-up task (create_task) — a to-do/reminder on the rep's Home list
- Check agent status (get_system_status) — kanban view of running/queued/done tasks
- List all clients (list_clients) — full portfolio overview
- Get recent findings (get_recent_findings) — latest research intelligence per client

## Rules for answering questions
1. ALWAYS call search_kb before answering. Retry with different terms if results are thin.
2. For specific clients, call get_client for the full profile and linked documents.
3. If unsure of the exact client name, call search_clients first.
4. Cite every factual claim — include the document title or source URL.
5. If the KB has nothing relevant, say so and offer to trigger research.

## Rules for taking actions
1. Before create_client: call search_clients to confirm the client does not already exist.
2. After creating a client: confirm what was created and mention that research is being triggered.
3. For update_client: describe what fields changed and their new values.
4. For trigger_research: tell the user results appear asynchronously (typically 2–5 minutes).
5. To find PEOPLE at a client — executives, or a specific role/persona (e.g. "IT ops", CISO, CTO, DevOps) — call find_people with client_name and target_roles. Do this whenever the user asks to find, research, or identify contacts/people/personas at a company; never just say the KB has none — start the search.
6. When the user wants a reminder or follow-up ("remind me to…", "follow up with X on <date>", "add a task"), call create_task with a short title, the client_name if one applies, and a due_date in YYYY-MM-DD.
7. Do NOT call write_document in chat mode.

## General
- Stop after answering or completing the requested action. Never call more tools than needed.
- Never fabricate data — if it is not in the KB, say so clearly.`;

export interface ChatOptions {
  orgId: number;
  message: string;
  clientName?: string;
  history?: Array<{ role: string; content: string }>;
  orgName?: string;
  /** LLM provider name from config.yaml llm: block (legacy `brain` values already mapped) */
  provider: string;
  model: string;
  abortController: AbortController;
  // Live progress for the UI "thinking preview" — called with a human-readable
  // label for each agent step (tool calls, thinking turns)
  onEvent?: (label: string) => void;
}

// Human-readable labels for the chat thinking preview
function describeToolCall(toolName: string, args: Record<string, unknown>): string {
  const subject = (args.query ?? args.name ?? args.client_name ?? args.subject ?? '') as string;
  switch (toolName) {
    case 'search_kb':           return `Searching knowledge base: "${subject}"`;
    case 'get_client':          return `Reading client profile: ${subject}`;
    case 'search_clients':      return `Looking up clients: "${subject}"`;
    case 'list_clients':        return 'Listing clients';
    case 'get_recent_findings': return `Gathering recent findings${subject ? `: ${subject}` : ''}`;
    case 'trigger_research':    return `Queueing background research: ${subject}`;
    case 'find_people':         return `Finding people at: ${subject}`;
    case 'create_task':         return `Creating task: ${(args.title ?? subject) as string}`;
    case 'get_system_status':   return 'Checking agent status';
    case 'create_client':       return `Creating client: ${subject}`;
    case 'create_contact':      return `Creating contact: ${subject}`;
    case 'update_client':       return `Updating client: ${subject}`;
    default:                    return `Running ${toolName}`;
  }
}

export interface ChatResult {
  answer: string;
  sources: Array<{ title: string; url: string; type: string; snippet: string }>;
  toolCallsMade: number;
}

/**
 * Plain completion through a Pi-resolved model — no tools, no agent loop.
 * Lets the Python server's llm.py (kind 'pi') route small calls (triage,
 * summaries, NBA reasons) through providers only Pi can auth, e.g. the
 * subscription-OAuth ones. Returns the assistant text.
 */
export async function runPiComplete(opts: {
  provider: string;
  model: string;
  messages: Array<{ role: string; content: string }>;
  maxTokens?: number;
}): Promise<string> {
  const { completeSimple } = await import('@earendil-works/pi-ai');
  const { model, apiKey } = await buildModel(opts.provider, opts.model);
  const systemPrompt = opts.messages.filter(m => m.role === 'system').map(m => m.content).join('\n\n');
  const turns = opts.messages
    .filter(m => m.role !== 'system')
    .map(m => ({
      role: (m.role === 'assistant' ? 'assistant' : 'user') as 'assistant' | 'user',
      content: [{ type: 'text' as const, text: m.content }],
    }));
  const context: { systemPrompt?: string; messages: typeof turns } = { messages: turns };
  if (systemPrompt) context.systemPrompt = systemPrompt;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const result: any = await completeSimple(model, context as any, {
    apiKey: apiKey || undefined,
    maxTokens: opts.maxTokens ?? 1024,
  } as any);
  const text = (result?.content ?? [])
    .filter((c: { type?: string }) => c.type === 'text')
    .map((c: { text?: string }) => c.text ?? '')
    .join('')
    .trim();
  return text;
}


export async function runPiChat(opts: ChatOptions): Promise<ChatResult> {
  const sources: Array<{ title: string; url: string; type: string; snippet: string }> = [];
  const toolCallLog: Array<{ tool: string; args: unknown; result: string; ts: string }> = [];

  const allTools = buildTools(opts.orgId, -1, opts.clientName ?? '', toolCallLog, false, sources);
  const chatAllowlist = new Set([
    'search_kb', 'get_client', 'search_clients',
    'list_clients', 'get_recent_findings',
    'create_client', 'create_contact', 'update_client',
    'trigger_research', 'find_people', 'create_task', 'get_system_status',
    'get_contact_log', 'get_nba_queue', 'draft_outreach',
  ]);
  const tools = allTools.filter(t => chatAllowlist.has(t.name));

  // Build a lightweight client roster for system context. With many clients
  // the full roster dominated every request's token bill — large orgs get a
  // count plus the lookup tools instead.
  let rosterLines = '';
  try {
    const clients = await db.listClients(opts.orgId);
    if (clients.length > 15) {
      rosterLines = `\nThe org has ${clients.length} known clients. Use the search_clients or list_clients tools to look them up by name.`;
    } else if (clients.length) {
      rosterLines = '\nKNOWN CLIENTS:\n' + clients.map(c =>
        `- ${c.name} (${c.session_count} sessions, last active: ${c.last_activity?.slice(0, 10) ?? 'unknown'})`
      ).join('\n');
    }
  } catch { /* non-fatal */ }

  const scopeLine = opts.clientName
    ? `\nACTIVE SCOPE: The user is focused on "${opts.clientName}". Answer in that context unless told otherwise.\n`
    : '';
  const orgLine = opts.orgName ? `You are assisting the sales team at ${opts.orgName}.\n` : '';
  const systemPrompt = `${orgLine}${CHAT_PROMPT}${scopeLine}${rosterLines}`;

  const { model: agentModel, apiKey } = await buildModel(opts.provider, opts.model);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const agent = new (Agent as any)({
    initialState: { systemPrompt, model: agentModel, tools, messages: [] },
    getApiKey: () => apiKey || undefined,
    convertToLlm: (messages: Array<{ role?: string }>) =>
      messages.filter(m => m.role === 'user' || m.role === 'assistant' || m.role === 'toolResult'),
    toolExecution: 'sequential',
  });

  opts.abortController.signal.addEventListener('abort', () => {
    try { (agent as unknown as { abort?: () => void }).abort?.(); } catch { /* ignore */ }
  });

  // Surface agent lifecycle as readable progress events for the UI
  if (opts.onEvent) {
    const emit = opts.onEvent;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (agent as any).subscribe((event: { type?: string; toolName?: string; args?: unknown }) => {
      try {
        if (event.type === 'tool_execution_start' && event.toolName) {
          emit(describeToolCall(event.toolName, (event.args ?? {}) as Record<string, unknown>));
        } else if (event.type === 'turn_start') {
          emit('Thinking…');
        }
      } catch { /* progress is best-effort */ }
    });
  }

  // Inject prior conversation turns for multi-turn context
  if (opts.history && opts.history.length > 0) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (agent as any).state.messages = opts.history.map(m => ({
      role: m.role === 'ai' ? 'assistant' : 'user',
      content: [{ type: 'text' as const, text: m.content }],
    }));
  }

  await agent.prompt(opts.message);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const agentState = (agent as any).state as { messages: Array<{role?: string; content?: Array<{type?: string; text?: string}>}>; errorMessage?: string };

  // Surface any SDK-level error (rate limit, auth, network)
  if (agentState.errorMessage) {
    return {
      answer: `_(Chat agent error: ${agentState.errorMessage} — please try again)_`,
      sources,
      toolCallsMade: toolCallLog.length,
    };
  }

  // Extract the last assistant text as the answer
  const lastAssistant = [...agentState.messages].reverse().find(m => m.role === 'assistant');
  const answer = (
    lastAssistant?.content
      ?.filter(c => c.type === 'text')
      .map(c => c.text ?? '')
      .join('')
      .trim()
  ) || '_(Pi returned no text — the model may be rate-limited, try again)_';

  return { answer, sources, toolCallsMade: toolCallLog.length };
}
