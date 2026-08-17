import { Type } from '@sinclair/typebox';
import * as db from './db.js';
import { config } from './config.js';
import { webSearch, fetchPage, fetchPageBrowser, fetchPageCamofox } from './search.js';

// AgentTool shape as defined by @earendil-works/pi-agent-core
export interface ToolResult {
  content: Array<{ type: 'text'; text: string }>;
  details: Record<string, unknown>;
  terminate?: boolean;
}

export type ExecuteFn<P> = (
  toolCallId: string,
  params: P,
  signal: AbortSignal,
  onUpdate?: (r: ToolResult) => void,
) => Promise<ToolResult>;

export interface AgentTool {
  name: string;
  label: string;
  description: string;
  parameters: ReturnType<typeof Type.Object>;
  execute: ExecuteFn<Record<string, unknown>>;
}

// -- Tool builders (close over orgId / agentRunId / toolCallLog) --

export interface SourceRef {
  title: string;
  url: string;
  type: string;
  snippet: string;
}

export function buildTools(
  orgId: number,
  agentRunId: number,
  subject: string,
  toolCallLog: Array<{ tool: string; args: unknown; result: string; ts: string }>,
  useBrowserFetch = false,
  sourcesAcc?: SourceRef[],
): AgentTool[] {
  function log(tool: string, args: unknown, result: string) {
    toolCallLog.push({ tool, args, result: result.replace(/\0/g, '').slice(0, 300), ts: new Date().toISOString() });
  }

  // URLs the agent actually fetched this run — appended as clickable links to
  // the final osint/research report so article links are never lost when the
  // model writes prose-only citations.
  const fetchedUrls: string[] = [];

  const searchKb: AgentTool = {
    name: 'search_kb',
    label: 'Search Knowledge Base',
    description: 'Hybrid semantic + full-text search across the knowledge base. Use this first to find existing documents, client profiles, and research.',
    parameters: Type.Object({
      query: Type.String({ description: 'Search query' }),
      top_k: Type.Optional(Type.Number({ description: 'Max results (default 5)' })),
    }),
    execute: async (_id, params) => {
      const p = params as { query: string; top_k?: number };
      const results = await db.searchKb(orgId, p.query, Math.min(p.top_k ?? 5, 10));
      // Cap per-result content — full documents here were the main chat token
      // burner (5 full docs per search × several searches per answer)
      const text = results.length
        ? results.map(r => `[${r.type}] ${r.title}\n${r.content.slice(0, 600)}${r.content.length > 600 ? ' …' : ''}\n`).join('\n---\n')
        : '(no results)';
      log('search_kb', p, text);
      if (sourcesAcc) {
        for (const r of results) {
          const meta = r.metadata as Record<string, unknown>;
          sourcesAcc.push({
            title: r.title,
            url: (meta.source_url as string) ?? '',
            type: r.type,
            snippet: r.content.slice(0, 200),
          });
        }
      }
      return { content: [{ type: 'text', text }], details: { count: results.length } };
    },
  };

  const getClient: AgentTool = {
    name: 'get_client',
    label: 'Get Client Profile',
    description: 'Retrieve a full client profile including metadata and linked documents.',
    parameters: Type.Object({
      name: Type.String({ description: 'Exact or approximate client name' }),
    }),
    execute: async (_id, params) => {
      const p = params as { name: string };
      const result = await db.getClient(orgId, p.name);
      if (!result) {
        log('get_client', p, 'not found');
        return { content: [{ type: 'text', text: `No client found matching "${p.name}"` }], details: {} };
      }
      const text = `Client: ${result.client.name}\nSessions: ${result.client.session_count}\n`
        + `Metadata: ${JSON.stringify(result.client.metadata, null, 2)}\n\n`
        + `Linked documents (${result.docs.length}):\n`
        + result.docs.map(d => `- [${d.type}] ${d.title}`).join('\n');
      log('get_client', p, text);
      return { content: [{ type: 'text', text }], details: { doc_count: result.docs.length } };
    },
  };

  const searchClients: AgentTool = {
    name: 'search_clients',
    label: 'Search Clients',
    description: 'Fuzzy search for client names. Use when the exact name is unknown.',
    parameters: Type.Object({
      partial_name: Type.String({ description: 'Partial client name' }),
    }),
    execute: async (_id, params) => {
      const p = params as { partial_name: string };
      const rows = await db.searchClients(orgId, p.partial_name);
      const text = rows.length
        ? rows.map(r => `${r.name} (${r.session_count} sessions)`).join('\n')
        : '(no matches)';
      log('search_clients', p, text);
      return { content: [{ type: 'text', text }], details: { count: rows.length } };
    },
  };

  const webSearchTool: AgentTool = {
    name: 'web_search',
    label: 'Web Search',
    description: 'Search the web for current information. Returns URLs, titles, and snippets.',
    parameters: Type.Object({
      query: Type.String({ description: 'Search query' }),
      n_results: Type.Optional(Type.Number({ description: 'Number of results (default 5)' })),
    }),
    execute: async (_id, params) => {
      const p = params as { query: string; n_results?: number };
      const results = await webSearch(p.query, p.n_results ?? 5);
      const text = results.length
        ? results.map(r => `${r.title}\n${r.url}\n${r.snippet}`).join('\n\n')
        : '(no results — SearXNG and DDG both returned empty)';
      log('web_search', p, text);
      return { content: [{ type: 'text', text }], details: { count: results.length } };
    },
  };

  const fetchPageTool: AgentTool = {
    name: 'fetch_page',
    label: 'Fetch Web Page',
    description: 'Fetch and extract text from a URL. Use on promising search results.',
    parameters: Type.Object({
      url: Type.String({ description: 'URL to fetch' }),
    }),
    execute: async (_id, params) => {
      const p = params as { url: string };
      // Prefer Camofox when configured (research/osint benchmark variant), else browser service, else plain.
      // Each stage already falls back internally (camofox → browser-service → plain fetch,
      // see search.ts); this outer guard makes the tool itself unthrowable — if the whole
      // chain errors unexpectedly, degrade to plain fetch, which always returns a string.
      let text: string;
      try {
        text = config.camofoxUrl
          ? await fetchPageCamofox(p.url)
          : useBrowserFetch ? await fetchPageBrowser(p.url) : await fetchPage(p.url);
      } catch (err) {
        console.warn(`[fetch_page] browser fetch chain failed for ${p.url} — degrading to plain fetch: ${String(err)}`);
        text = await fetchPage(p.url);
      }
      log('fetch_page', p, text);
      // Remember pages that returned real content so the final report can list them.
      if (text && text.length > 80 && p.url?.startsWith('http') && !fetchedUrls.includes(p.url)) {
        fetchedUrls.push(p.url);
      }
      return { content: [{ type: 'text', text }], details: { url: p.url, chars: text.length } };
    },
  };

  const writeDocument: AgentTool = {
    name: 'write_document',
    label: 'Write Document',
    description: 'Save a research finding or final report to the knowledge base. '
      + 'Use type="finding" for individual facts, type="signal" for a notable development, '
      + 'type="research" for the final synthesised report. '
      + 'Always include source_url for findings and signals — the URL of the page the fact came '
      + 'from — so it stays clickable. For market/industry-wide developments not tied to one '
      + 'company, set scope="market" and industry="<sector>" and do NOT set client_name. '
      + 'After writing the final report, you may stop.',
    parameters: Type.Object({
      type: Type.String({ description: '"finding" | "signal" | "research" | "osint"' }),
      title: Type.String({ description: 'Document title' }),
      content: Type.String({ description: 'Full markdown content, including ## Sources section' }),
      source_url: Type.Optional(Type.String({ description: 'Source URL (required for findings)' })),
      client_name: Type.Optional(Type.String({ description: 'Link this document to a client by name' })),
      scope: Type.Optional(Type.String({ description: '"market" for an industry-wide development not tied to a single client (skips client linking)' })),
      industry: Type.Optional(Type.String({ description: 'Industry/sector this development concerns (for scope="market")' })),
      relevance_score: Type.Optional(Type.Number({ description: 'Importance 1-5 (5 = sector-defining / major strategic event)' })),
      signal_type: Type.Optional(Type.String({ description: '"opportunity" | "risk" | "pain_point" | "news"' })),
    }),
    execute: async (_id, params) => {
      const p = params as {
        type: string; title: string; content: string;
        source_url?: string; client_name?: string; scope?: string; industry?: string;
        relevance_score?: number; signal_type?: string;
      };
      try {
        // Strip benchmark isolation tag (e.g. "Sartorius [pi]" → "Sartorius")
        const effectiveClient = (p.client_name ?? subject).replace(/\s*\[.*?\]\s*$/, '').trim();
        const docTitle = (p.type === 'research' || p.type === 'osint') ? `${p.title} [Pi]` : p.title;

        // Safety net: ensure every fetched article URL is clickable in the final
        // report. Models often write prose-only "## Sources" without the URLs;
        // append any fetched URL not already present verbatim in the content.
        let content = p.content;
        if (p.type === 'osint' || p.type === 'research') {
          const missing = fetchedUrls.filter(u => !content.includes(u)).slice(0, 60);
          if (missing.length) {
            const links = missing.map(u => {
              let host = u;
              try { host = new URL(u).hostname.replace(/^www\./, ''); } catch { /* keep raw */ }
              return `- [${host}](${u})`;
            }).join('\n');
            content += `\n\n## Source links\n${links}\n`;
          }
        }

        // Per-fact docs (finding/signal) should stay traceable to the page they
        // came from. Models often omit source_url on signals; when this run did
        // fetch pages, fall back to the most recently fetched one so the
        // "Read article" link isn't lost. Final reports keep their own ## Sources.
        const isFinalDoc = p.type === 'osint' || p.type === 'research' || p.type === 'match_report';
        let sourceUrl = p.source_url ?? '';
        if (!sourceUrl && !isFinalDoc && fetchedUrls.length) {
          sourceUrl = fetchedUrls[fetchedUrls.length - 1];
        }

        // Market/industry-wide signals are not tied to a single company — skip
        // client linking (so they don't get fuzzy-attached to a lookalike client)
        // and tag scope + industry so the UI can surface them under "Market".
        const isMarket = p.scope === 'market';
        const metadata: Record<string, unknown> = { source_url: sourceUrl, service: 'pi' };
        if (isMarket) metadata.scope = 'market';
        if (p.industry) metadata.industry = p.industry;
        if (typeof p.relevance_score === 'number') metadata.relevance_score = p.relevance_score;
        if (p.signal_type) metadata.signal_type = p.signal_type;

        const docId = await db.writeDocument({
          orgId,
          agentRunId,
          type: p.type,
          title: docTitle,
          content,
          metadata,
          clientName: isMarket ? undefined : effectiveClient,
          sourceUrl: sourceUrl,
        });
        const msg = `Saved as document #${docId}${p.client_name ? ` linked to ${p.client_name}` : ''}`;
        log('write_document', { type: p.type, title: p.title }, msg);
        // Final reports end the agent loop natively (pi-agent-core stops when a tool returns
        // terminate). The force-final mechanism in agent.ts remains as a backstop.
        return { content: [{ type: 'text', text: msg }], details: { doc_id: docId }, terminate: isFinalDoc };
      } catch (err) {
        const msg = `Error writing document: ${String(err)}`;
        log('write_document', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  // -- Action tools (chat only) --

  const listClientsTool: AgentTool = {
    name: 'list_clients',
    label: 'List All Clients',
    description: 'Return all clients in the knowledge base with session counts and last activity. Use to get a full portfolio overview.',
    parameters: Type.Object({}),
    execute: async (_id, _params, _signal) => {
      const rows = await db.listClients(orgId);
      const text = rows.length
        ? rows.map(r =>
            `${r.name} (${r.session_count} sessions, last active: ${r.last_activity?.slice(0, 10) ?? 'never'})`
          ).join('\n')
        : '(no clients found)';
      log('list_clients', {}, text);
      return { content: [{ type: 'text', text }], details: { count: rows.length } };
    },
  };

  const getRecentFindingsTool: AgentTool = {
    name: 'get_recent_findings',
    label: 'Get Recent Findings',
    description: 'Get the latest research findings for a specific client, ordered by relevance. Use to summarise what is already known before triggering new research.',
    parameters: Type.Object({
      client_name: Type.String({ description: 'Client name (exact or partial)' }),
      n: Type.Optional(Type.Number({ description: 'Number of findings to return (default 20, max 100)' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { client_name: string; n?: number };
      const rows = await db.getRecentFindings(orgId, p.client_name, Math.min(p.n ?? 20, 100));
      const text = rows.length
        ? rows.map(r =>
            `[${r.created_at.slice(0, 10)}] ${r.title}\n${r.content.slice(0, 400)}`
          ).join('\n\n---\n\n')
        : `(no findings found for "${p.client_name}")`;
      log('get_recent_findings', p, text);
      return { content: [{ type: 'text', text }], details: { count: rows.length } };
    },
  };

  const createClientTool: AgentTool = {
    name: 'create_client',
    label: 'Create Client',
    description: 'Create a new client in the knowledge base. Automatically triggers OSINT and deep research. Always call search_clients first to confirm the client does not already exist.',
    parameters: Type.Object({
      name: Type.String({ description: 'Client company name as it should appear in the KB' }),
      metadata: Type.Optional(Type.Object({
        industry:    Type.Optional(Type.String()),
        website:     Type.Optional(Type.String()),
        deal_stage:  Type.Optional(Type.String()),
        notes:       Type.Optional(Type.String()),
      }, { additionalProperties: true })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { name: string; metadata?: Record<string, unknown> };
      try {
        const result = await db.createClient(orgId, p.name, p.metadata);
        const msg = `Client "${p.name}" created (id: ${result.id}). OSINT and research runs have been triggered automatically.`;
        log('create_client', p, msg);
        return { content: [{ type: 'text', text: msg }], details: result };
      } catch (err) {
        const msg = `Error creating client: ${String(err)}`;
        log('create_client', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  const createContactTool: AgentTool = {
    name: 'create_contact',
    label: 'Create Contact',
    description: 'Create a new contact (person) in the knowledge base, optionally linked to a client.',
    parameters: Type.Object({
      name:         Type.String({ description: 'Contact full name' }),
      client:       Type.Optional(Type.String({ description: 'Client company name to link this contact to' })),
      role:         Type.Optional(Type.String({ description: 'Job title or role' })),
      email:        Type.Optional(Type.String({ description: 'Email address' })),
      linkedin_url: Type.Optional(Type.String({ description: 'LinkedIn profile URL (https://linkedin.com/in/...)' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { name: string; client?: string; role?: string; email?: string; linkedin_url?: string };
      try {
        const result = await db.createContact(orgId, p.name, { client: p.client, role: p.role, email: p.email, linkedin_url: p.linkedin_url });
        const msg = `Contact "${p.name}" created (id: ${result.id})${p.client ? ` linked to "${p.client}"` : ''}.`;
        log('create_contact', p, msg);
        return { content: [{ type: 'text', text: msg }], details: result };
      } catch (err) {
        const msg = `Error creating contact: ${String(err)}`;
        log('create_contact', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  const updateClientTool: AgentTool = {
    name: 'update_client',
    label: 'Update Client',
    description: 'Update metadata fields on an existing client (deal_stage, website, notes, industry, deal_value, hq, etc.). Use search_clients first if unsure of the exact name.',
    parameters: Type.Object({
      name:  Type.String({ description: 'Exact client name' }),
      patch: Type.Object({}, { additionalProperties: true, description: 'Fields to update: deal_stage, website, industry, notes, deal_value, hq, employees, etc.' }),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { name: string; patch: Record<string, unknown> };
      try {
        await db.updateClient(orgId, p.name, p.patch);
        const msg = `Client "${p.name}" updated. Fields changed: ${Object.keys(p.patch).join(', ')}.`;
        log('update_client', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      } catch (err) {
        const msg = `Error updating client: ${String(err)}`;
        log('update_client', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  const triggerResearchTool: AgentTool = {
    name: 'trigger_research',
    label: 'Trigger Research',
    description: 'Queue a deep research run for a subject via the research agent. Use when the user asks to research a company or when existing knowledge is stale. Returns immediately — research runs asynchronously (typically 2–5 minutes).',
    parameters: Type.Object({
      subject: Type.String({ description: 'Company or person name to research' }),
      angles:  Type.Optional(Type.String({ description: 'Specific focus areas or angles for the research' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { subject: string; angles?: string };
      try {
        const resp = await fetch(`${config.mainServerUrl}/api/research/trigger`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subject: p.subject, org_id: orgId, angles: p.angles ?? '' }),
          signal: AbortSignal.timeout(15_000),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json() as Record<string, unknown>;
        const runId = data.run_id ?? data.task_id ?? 'queued';
        const msg = `Research triggered for "${p.subject}" (run_id: ${runId}). Results will appear in the knowledge base when complete.`;
        log('trigger_research', p, msg);
        return { content: [{ type: 'text', text: msg }], details: data };
      } catch (err) {
        const msg = `Error triggering research: ${String(err)}`;
        log('trigger_research', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  const triggerRunTool: AgentTool = {
    name: 'trigger_run',
    label: 'Trigger Agent Run',
    description:
      'Queue a research or analysis run for a subject on the correct agent service. ' +
      'Use for: "research", "osint", "pain_point_research", "product_research" only. ' +
      'Do NOT use for enrichment, match_synthesis, or contact_extraction — those trigger automatically. ' +
      'Returns immediately — runs execute asynchronously.',
    parameters: Type.Object({
      agent_type: Type.String({
        description: 'Agent type: "research" | "osint" | "pain_point_research" | "product_research"',
      }),
      subject: Type.String({ description: 'Subject name (client or company)' }),
      task: Type.Optional(Type.String({
        description: 'Custom task description. A sensible default is used if omitted.',
      })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { agent_type: string; subject: string; task?: string };
      try {
        const resp = await fetch(`${config.mainServerUrl}/api/agents/internal/run`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(config.serviceToken ? { Authorization: `Bearer ${config.serviceToken}` } : {}),
          },
          body: JSON.stringify({
            agent_type: p.agent_type,
            subject: p.subject,
            task: p.task ?? '',
            org_id: orgId,
            provider: config.defaultProvider,
            brain: config.defaultBrain,  // legacy field, kept for older servers
            model: config.defaultModel,
          }),
          signal: AbortSignal.timeout(15_000),
        });
        if (!resp.ok) {
          const text = await resp.text().catch(() => '');
          throw new Error(`HTTP ${resp.status}: ${text}`);
        }
        const data = await resp.json() as Record<string, unknown>;
        const runId = data.run_id ?? 'queued';
        const msg = `Queued ${p.agent_type} run for "${p.subject}" (run_id: ${runId}). Results will appear in the knowledge base when complete.`;
        log('trigger_run', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: data };
      } catch (err) {
        const msg = `Error triggering ${p.agent_type} run: ${String(err)}`;
        log('trigger_run', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: {} };
      }
    },
  };

  const findPeopleTool: AgentTool = {
    name: 'find_people',
    label: 'Find People',
    description:
      'Start a people-search agent for a client to find named people (executives, managers, or a ' +
      'specific role/persona) and add them as contacts. Use whenever the user asks to find, research, ' +
      'or identify people / contacts / personas at a company — do not just report that the KB has none. ' +
      'Pass target_roles to steer toward a persona (e.g. "CISO, IT-Architekt, DevOps"). ' +
      'Returns immediately — the search runs asynchronously (typically 2–5 minutes).',
    parameters: Type.Object({
      client_name:  Type.String({ description: 'Company to find people at' }),
      target_roles: Type.Optional(Type.String({ description: 'Optional comma-separated roles/personas to prioritise (e.g. "CISO, IT ops, CTO")' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { client_name: string; target_roles?: string };
      try {
        const resp = await fetch(`${config.mainServerUrl}/api/internal/find-people`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(config.serviceToken ? { Authorization: `Bearer ${config.serviceToken}` } : {}),
          },
          body: JSON.stringify({
            org_id: orgId,
            client_name: p.client_name,
            target_roles: p.target_roles ?? '',
          }),
          signal: AbortSignal.timeout(15_000),
        });
        if (!resp.ok) {
          const text = await resp.text().catch(() => '');
          throw new Error(`HTTP ${resp.status}: ${text}`);
        }
        const data = await resp.json() as Record<string, unknown>;
        const runId = data.run_id ?? 'queued';
        const rolesMsg = p.target_roles ? ` targeting roles: ${p.target_roles}` : '';
        const msg = `Started a people-search for "${p.client_name}"${rolesMsg} (run_id: ${runId}). `
          + 'It will find named people, save profile findings, and add contacts when done — check the client\'s Contacts in a few minutes.';
        log('find_people', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: data };
      } catch (err) {
        const msg = `Error starting people search: ${String(err)}`;
        log('find_people', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: {} };
      }
    },
  };

  const createTaskTool: AgentTool = {
    name: 'create_task',
    label: 'Create Task',
    description:
      'Create a to-do / follow-up reminder for the rep. Use when the user says things like ' +
      '"remind me to…", "follow up with X on <date>", or "add a task". The task appears on their ' +
      'Home "My Tasks" list and, if a client is set, feeds the daily "who to contact today" queue.',
    parameters: Type.Object({
      title:       Type.String({ description: 'Short task description, e.g. "Call about renewal"' }),
      client_name: Type.Optional(Type.String({ description: 'Related client, if any' })),
      due_date:    Type.Optional(Type.String({ description: 'Due date in YYYY-MM-DD' })),
      notes:       Type.Optional(Type.String({ description: 'Optional extra detail' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { title: string; client_name?: string; due_date?: string; notes?: string };
      try {
        const resp = await fetch(`${config.mainServerUrl}/api/internal/tasks`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(config.serviceToken ? { Authorization: `Bearer ${config.serviceToken}` } : {}),
          },
          body: JSON.stringify({
            org_id: orgId,
            title: p.title,
            client_name: p.client_name ?? '',
            due_date: p.due_date ?? '',
            notes: p.notes ?? '',
          }),
          signal: AbortSignal.timeout(15_000),
        });
        if (!resp.ok) {
          const text = await resp.text().catch(() => '');
          throw new Error(`HTTP ${resp.status}: ${text}`);
        }
        const data = await resp.json() as Record<string, unknown>;
        const clientBit = data.client_name ? ` for ${data.client_name}` : '';
        const whenBit = data.due_date ? ` due ${String(data.due_date).slice(0, 10)}` : '';
        const msg = `Created task "${p.title}"${clientBit}${whenBit}. It's on the rep's Home "My Tasks" list.`;
        log('create_task', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: data };
      } catch (err) {
        const msg = `Error creating task: ${String(err)}`;
        log('create_task', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: {} };
      }
    },
  };

  const getSystemStatusTool: AgentTool = {
    name: 'get_system_status',
    label: 'Get System Status',
    description: 'Get a kanban-style view of all active and recent agent runs and research queue tasks (todo / in-progress / done). Use when the user asks what is running, what is queued, or whether research has finished.',
    parameters: Type.Object({}),
    execute: async (_id, _params, _signal) => {
      try {
        const status = await db.getSystemStatus(orgId);
        const ar = status.agent_runs as Record<string, Array<Record<string, unknown>>>;
        const rq = status.research_queue as Record<string, Array<Record<string, unknown>>>;

        const lines: string[] = ['## System Status\n', '### Agent Runs'];
        const fmt = (list: Array<Record<string, unknown>>) =>
          list.length ? list.map(r => `${r.agent_type} — ${r.task} [${r.status}]`).join(', ') : 'none';

        lines.push(`In Progress (${(ar.in_progress ?? []).length}): ${fmt(ar.in_progress ?? [])}`);
        lines.push(`Todo (${(ar.todo ?? []).length}): ${fmt(ar.todo ?? [])}`);
        lines.push(`Recently Done (${(ar.done ?? []).length}): ${fmt(ar.done ?? [])}`);
        lines.push('\n### Research Queue');
        const fmtTask = (list: Array<Record<string, unknown>>) =>
          list.length ? list.map(t => String(t.subject)).join(', ') : 'none';
        lines.push(`Running: ${fmtTask(rq.running ?? [])}`);
        lines.push(`Pending: ${fmtTask(rq.pending ?? [])}`);
        lines.push(`Recently Done: ${fmtTask(rq.done ?? [])}`);

        const text = lines.join('\n');
        log('get_system_status', {}, text);
        return { content: [{ type: 'text', text }], details: status };
      } catch (err) {
        const msg = `Error fetching system status: ${String(err)}`;
        log('get_system_status', {}, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  // -- Decision-context reads (Phase 2 autonomy) ---------------------------
  const getContactLogTool: AgentTool = {
    name: 'get_contact_log',
    label: 'Get Contact Log',
    description: 'Read the outreach history (mails logged as sent, replies, follow-ups) for a client or the whole org. Call this before deciding whether new research or outreach is worthwhile — a client contacted last week without reply is not a candidate for more outreach yet.',
    parameters: Type.Object({
      client_name: Type.Optional(Type.String({ description: 'Client name (substring match). Omit for org-wide.' })),
      limit: Type.Optional(Type.Number({ description: 'Max rows (default 10)' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { client_name?: string; limit?: number };
      try {
        const rows = await db.getContactLog(orgId, p.client_name, Math.min(p.limit ?? 10, 50));
        const text = rows.length
          ? rows.map(r => `${String(r.sent_at).slice(0, 10)} → ${r.client_name}` +
              (r.contact_name ? ` / ${r.contact_name}` : '') +
              (r.subject ? `: "${r.subject}"` : '') +
              (r.replied ? ' [replied]' : '') + (r.follow_up ? ' [follow-up]' : '')).join('\n')
          : 'No outreach logged.';
        log('get_contact_log', p, text);
        return { content: [{ type: 'text', text }], details: { rows } };
      } catch (err) {
        const msg = `Error reading contact log: ${String(err)}`;
        log('get_contact_log', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  const getNbaQueueTool: AgentTool = {
    name: 'get_nba_queue',
    label: 'Get Next-Best-Action Queue',
    description: 'Read the current next-best-action queue (who the reps should contact today and why, with the suggested action). Use it to see whether a client is already flagged for action before triggering more research.',
    parameters: Type.Object({
      client_name: Type.Optional(Type.String({ description: 'Filter to one client (substring match)' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { client_name?: string };
      try {
        const q = await db.getNbaQueue(orgId, p.client_name);
        const text = q.entries.length
          ? `Queue computed ${q.computed_at ?? '?'}\n` +
            q.entries.map(e => `${e.rank}. ${e.client} — ${e.suggested_action} (score ${e.score})` +
              (e.is_focus ? ' ★' : '') + `: ${e.reason}`).join('\n')
          : 'No next-best-action queue available' + (p.client_name ? ` for ${p.client_name}` : '') + '.';
        log('get_nba_queue', p, text);
        return { content: [{ type: 'text', text }], details: q };
      } catch (err) {
        const msg = `Error reading NBA queue: ${String(err)}`;
        log('get_nba_queue', p, msg);
        return { content: [{ type: 'text', text: msg }], details: {} };
      }
    },
  };

  // -- Supervised outreach (Phase 3): agent may DRAFT only ------------------
  const draftOutreachTool: AgentTool = {
    name: 'draft_outreach',
    label: 'Draft Outreach Mail',
    description:
      'Create a DRAFT outreach email for a client contact and put it into the rep\'s approval queue. ' +
      'You never send mail: a human reviews, may edit, and approves before anything leaves the system. ' +
      'Use only when a concrete, sourced reason exists (fresh signal, pain point, follow-up due) and no ' +
      'recent outreach to the same contact is logged (check get_contact_log first). Requires the org to ' +
      'allow agent-drafted outreach (autonomy level 3) — otherwise the call is refused.',
    parameters: Type.Object({
      client_name: Type.String({ description: 'Client the mail is about (must exist)' }),
      subject:     Type.String({ description: 'Email subject line' }),
      body:        Type.String({ description: 'Email body (plain text or simple HTML). Cite the concrete signal you are reacting to.' }),
      to_email:    Type.Optional(Type.String({ description: 'Recipient address, if known' })),
      to_contact:  Type.Optional(Type.String({ description: 'Recipient name, if known' })),
      purpose:     Type.Optional(Type.String({ description: 'One line: why now (e.g. "new CFO + downtime pain signal")' })),
    }),
    execute: async (_id, params, _signal) => {
      const p = params as { client_name: string; subject: string; body: string; to_email?: string; to_contact?: string; purpose?: string };
      try {
        const resp = await fetch(`${config.mainServerUrl}/api/internal/outreach/draft`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(config.serviceToken ? { Authorization: `Bearer ${config.serviceToken}` } : {}),
          },
          body: JSON.stringify({
            org_id: orgId, agent_run_id: agentRunId > 0 ? agentRunId : undefined,
            client_name: p.client_name, subject: p.subject, body: p.body,
            to_email: p.to_email ?? '', to_contact: p.to_contact ?? '', purpose: p.purpose ?? '',
          }),
          signal: AbortSignal.timeout(15_000),
        });
        if (!resp.ok) {
          const text = await resp.text().catch(() => '');
          throw new Error(`HTTP ${resp.status}: ${text}`);
        }
        const data = await resp.json() as Record<string, unknown>;
        const msg = `Draft #${data.id} created for ${p.client_name} ("${p.subject}") — state ${data.state}. ` +
          `A rep must review and approve it in the Outreach queue before it is sent.`;
        log('draft_outreach', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: data };
      } catch (err) {
        const msg = `Could not create outreach draft: ${String(err)}`;
        log('draft_outreach', p, msg);
        return { content: [{ type: 'text' as const, text: msg }], details: {} };
      }
    },
  };

  return [
    searchKb, getClient, searchClients, webSearchTool, fetchPageTool, writeDocument,
    listClientsTool, getRecentFindingsTool,
    createClientTool, createContactTool, updateClientTool,
    triggerResearchTool, triggerRunTool, findPeopleTool, createTaskTool, getSystemStatusTool,
    getContactLogTool, getNbaQueueTool, draftOutreachTool,
  ];
}
