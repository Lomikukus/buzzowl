import Fastify from 'fastify';
import { v4 as uuidv4 } from 'uuid';
import { config, brainToProvider } from './config.js';
import { pool } from './db.js';
import { createRun, startRun, getRun, cancelRun, listRuns, queueStats } from './runner.js';
import { runPiChat, runPiComplete } from './agent.js';
import {
  OAuthHttpError, completeOAuthLogin, disconnectOAuth, isSubscriptionProvider,
  oauthStatus, startOAuthLogin,
} from './oauth.js';

const app = Fastify({ logger: true });

// -- Async chat runs (thinking-preview support) -------------------------------
// POST /chat with async_mode returns a chat_id immediately; the UI polls
// GET /chat/runs/:id for progress events and the final answer.

interface ChatRun {
  status: 'running' | 'done' | 'failed';
  events: string[];
  answer: string;
  sources: Array<{ title: string; url: string; type: string; snippet: string }>;
  tool_calls_made: number;
  error: string | null;
  completed_at: number | null;
}

const chatRuns = new Map<string, ChatRun>();

setInterval(() => {
  const cutoff = Date.now() - 10 * 60 * 1000;
  for (const [id, run] of chatRuns) {
    if (run.completed_at && run.completed_at < cutoff) chatRuns.delete(id);
  }
}, 60 * 1000).unref();

// -- Auth middleware --
app.addHook('preHandler', async (req, reply) => {
  if (req.url === '/health') return;  // health is unauthenticated
  if (!config.serviceToken) return;  // token not set → open (dev mode)
  const auth = req.headers.authorization ?? '';
  if (auth !== `Bearer ${config.serviceToken}`) {
    reply.code(401).send({ error: 'unauthorized' });
  }
});

// GET /health
app.get('/health', async () => {
  let db = 'ok';
  try { await pool.query('SELECT 1'); } catch { db = 'error'; }
  return { status: 'ok', db, candidate: 'pi-typescript', model: config.defaultModel, queue: queueStats() };
});

// POST /runs — enqueue a run
app.post('/runs', async (req, reply) => {
  const body = req.body as Record<string, unknown>;
  if (!body.task || !body.org_id) {
    return reply.code(400).send({ error: 'task and org_id are required' });
  }
  const agentType = (body.agent_type as string | undefined) ?? 'enrichment';
  const runReq = body as unknown as Parameters<typeof createRun>[0];
  const runId = await createRun(runReq);
  startRun(runId, runReq);
  return reply.code(202).send({ run_id: runId, status: 'queued' });
});

// GET /runs — list recent runs
app.get('/runs', async (req) => {
  const query = req.query as Record<string, string>;
  const orgId = query.org_id ? parseInt(query.org_id, 10) : undefined;
  const all = listRuns(orgId);
  return { runs: all.map(r => ({
    run_id: r.run_id, status: r.status, agent_type: r.agent_type,
    subject: r.subject, started_at: r.started_at, completed_at: r.completed_at,
    tool_calls_made: r.tool_calls.length,
  })) };
});

// GET /runs/:id — poll status
app.get('/runs/:id', async (req, reply) => {
  const { id } = req.params as { id: string };
  const run = getRun(parseInt(id, 10));
  if (!run) return reply.code(404).send({ error: 'run not found' });
  return {
    run_id: run.run_id,
    status: run.status,
    agent_type: run.agent_type,
    subject: run.subject,
    started_at: run.started_at,
    completed_at: run.completed_at,
    output: run.output,
    error: run.error,
    tool_calls: run.tool_calls,
  };
});

// POST /runs/:id/cancel
app.post('/runs/:id/cancel', async (req, reply) => {
  const { id } = req.params as { id: string };
  const ok = cancelRun(parseInt(id, 10));
  if (!ok) return reply.code(404).send({ error: 'run not found or already finished' });
  return { cancelled: true };
});

// POST /complete — plain, tool-less completion through a Pi-resolved model.
// Bridge for the Python server's llm.py (kind 'pi'): lets triage/summary/NBA
// calls use providers only Pi can auth (subscription OAuth). Body:
// {provider, model, messages:[{role,content}], max_tokens?} → {text}
app.post('/complete', async (req, reply) => {
  const body = req.body as {
    provider?: string; brain?: string; model?: string;
    messages?: Array<{ role: string; content: string }>;
    max_tokens?: number;
  };
  if (!Array.isArray(body.messages) || !body.messages.length) {
    return reply.code(400).send({ error: 'messages[] required' });
  }
  const provider = body.provider
    ?? (body.brain ? brainToProvider(body.brain) : config.defaultProvider);
  const model = body.model ?? config.defaultModel;
  try {
    const text = await runPiComplete({
      provider, model, messages: body.messages, maxTokens: body.max_tokens,
    });
    return { text, provider, model };
  } catch (err) {
    req.log.error({ err }, 'complete failed');
    return reply.code(502).send({ error: String((err as Error)?.message ?? err) });
  }
});

// POST /chat — Q&A over the knowledge base (no agent run created).
// Default: synchronous. With async_mode: returns {chat_id} for progress polling.
app.post('/chat', async (req, reply) => {
  const body = req.body as {
    message?: string;
    org_id?: number;
    client_name?: string;
    history?: Array<{ role: string; content: string }>;
    org_name?: string;
    provider?: string;  // provider name from the config.yaml llm: block
    brain?: string;     // legacy — mapped to a provider when provider is absent
    model?: string;
    async_mode?: boolean;
  };

  // provider wins; legacy brain values map openrouter→openrouter,
  // ollama→ollama, claude→anthropic
  const chatProvider = body.provider
    ?? (body.brain ? brainToProvider(body.brain) : config.defaultProvider);

  if (!body.message || !body.org_id) {
    return reply.code(400).send({ error: 'message and org_id are required' });
  }

  if (body.async_mode) {
    const chatId = uuidv4();
    const run: ChatRun = {
      status: 'running', events: [], answer: '', sources: [],
      tool_calls_made: 0, error: null, completed_at: null,
    };
    chatRuns.set(chatId, run);

    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 120_000);
    void runPiChat({
      orgId: body.org_id,
      message: body.message,
      clientName: body.client_name,
      history: body.history,
      orgName: body.org_name,
      provider: chatProvider,
      model: body.model ?? config.defaultModel,
      abortController: abort,
      onEvent: label => { run.events.push(label); },
    }).then(result => {
      run.status = 'done';
      run.answer = result.answer;
      run.sources = result.sources;
      run.tool_calls_made = result.toolCallsMade;
      console.log(`[chat] async org=${body.org_id} tools=${result.toolCallsMade} answerLen=${result.answer.length}`);
    }).catch(err => {
      run.status = 'failed';
      run.error = abort.signal.aborted ? 'chat timed out after 120s' : String(err);
      console.error('[chat] async error:', run.error);
    }).finally(() => {
      clearTimeout(timeout);
      run.completed_at = Date.now();
    });

    return reply.code(202).send({ chat_id: chatId });
  }

  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 90_000);

  try {
    const result = await runPiChat({
      orgId: body.org_id,
      message: body.message,
      clientName: body.client_name,
      history: body.history,
      orgName: body.org_name,
      provider: chatProvider,
      model: body.model ?? config.defaultModel,
      abortController: abort,
    });
    console.log(`[chat] org=${body.org_id} tools=${result.toolCallsMade} sources=${result.sources.length} answerLen=${result.answer.length}`);
    return { answer: result.answer, sources: result.sources, tool_calls_made: result.toolCallsMade };
  } catch (err) {
    const msg = abort.signal.aborted ? 'chat request timed out after 90s' : String(err);
    console.error('[chat] error:', msg);
    return reply.code(500).send({ error: msg });
  } finally {
    clearTimeout(timeout);
  }
});

// -- Subscription OAuth (P1c) ------------------------------------------------
// Connect consumer AI subscriptions (ChatGPT via openai-codex, GitHub Copilot)
// so agent runs/chat can use them as providers. Covered by the same bearer
// auth preHandler as every other route.
// Deliberately NOT exposed: the anthropic OAuth flow — Anthropic blocks
// third-party subscription OAuth server-side since Jan 2026.
// xai: pi-ai's OAuth registry has no xai provider (xai is API-key only).

const OAUTH_PROVIDER_HINT =
  "provider must be 'openai-codex' or 'github-copilot' " +
  '(anthropic subscription OAuth is blocked server-side by Anthropic since Jan 2026; ' +
  'xai has no OAuth flow in pi-ai — use an API key)';

// POST /oauth/start {provider, enterprise_domain?} → {auth_url, session_id, ...}
// openai-codex → mode 'paste-code': open auth_url, then POST /oauth/complete
//   with the pasted authorization code (or the full localhost:1455 redirect URL).
// github-copilot → mode 'device-code': open auth_url, enter the user code from
//   `instructions` on GitHub, then POST /oauth/complete (no code) to finish.
app.post('/oauth/start', async (req, reply) => {
  const body = (req.body ?? {}) as { provider?: string; enterprise_domain?: string };
  if (!body.provider || !isSubscriptionProvider(body.provider)) {
    return reply.code(400).send({ error: OAUTH_PROVIDER_HINT });
  }
  try {
    const s = await startOAuthLogin(body.provider, { enterpriseDomain: body.enterprise_domain });
    return {
      session_id: s.sessionId,
      auth_url: s.authUrl,
      provider: s.provider,
      mode: s.mode,
      instructions: s.instructions ?? null,
      expires_in: 600,
    };
  } catch (err) {
    const status = err instanceof OAuthHttpError ? err.statusCode : 500;
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`[oauth] start ${body.provider} failed:`, msg);
    return reply.code(status).send({ error: msg });
  }
});

// POST /oauth/complete {session_id, code?} → {ok, provider}
// 202 {status:'pending'} means Copilot's device approval hasn't been observed
// yet — call again (the session stays valid until its 10-minute TTL).
app.post('/oauth/complete', async (req, reply) => {
  const body = (req.body ?? {}) as { session_id?: string; code?: string };
  if (!body.session_id) {
    return reply.code(400).send({ ok: false, error: 'session_id is required' });
  }
  try {
    const result = await completeOAuthLogin(body.session_id, body.code);
    if (result.status === 'pending') {
      return reply.code(202).send({
        ok: false, status: 'pending', provider: result.provider,
        hint: 'authorization not observed yet — approve the device code on GitHub, then call /oauth/complete again',
      });
    }
    console.log(`[oauth] connected provider=${result.provider}`);
    return { ok: true, provider: result.provider };
  } catch (err) {
    const status = err instanceof OAuthHttpError ? err.statusCode : 500;
    const msg = err instanceof Error ? err.message : String(err);
    console.error('[oauth] complete failed:', msg);
    return reply.code(status).send({ ok: false, error: msg });
  }
});

// GET /oauth/status → {provider: {connected, expires_at}} map.
// expires_at is the ACCESS token expiry — an expired access token still counts
// as connected because getOAuthAuth() auto-refreshes on next use.
app.get('/oauth/status', async () => oauthStatus());

// POST /oauth/disconnect {provider} → forget stored credentials
app.post('/oauth/disconnect', async (req, reply) => {
  const body = (req.body ?? {}) as { provider?: string };
  if (!body.provider || !isSubscriptionProvider(body.provider)) {
    return reply.code(400).send({ error: OAUTH_PROVIDER_HINT });
  }
  const removed = disconnectOAuth(body.provider);
  console.log(`[oauth] disconnect provider=${body.provider} removed=${removed}`);
  return { ok: true, provider: body.provider, removed };
});

// GET /chat/runs/:id — poll an async chat run (progress events + final answer)
app.get('/chat/runs/:id', async (req, reply) => {
  const { id } = req.params as { id: string };
  const run = chatRuns.get(id);
  if (!run) return reply.code(404).send({ error: 'chat run not found or expired' });
  return {
    status: run.status,
    events: run.events,
    answer: run.answer,
    sources: run.sources,
    tool_calls_made: run.tool_calls_made,
    error: run.error,
  };
});

// Start server
const start = async () => {
  try {
    // Runs only live in this process — any of OUR OWN rows still queued/running
    // in the DB after a restart is a zombie. Mark only those failed so the UI
    // doesn't show forever-spinning runs (was a recurring issue before).
    //
    // Scope strictly to trigger_type = 'external_service' — the rows this
    // service itself inserts (see createAgentRun). The FastAPI server also
    // writes agent_runs (heartbeats, and the delegated mirror rows that carry
    // output.service_run_id), runs them in its OWN process, and reconciles them
    // with its own watcher. Failing those here is cross-boundary collateral
    // damage: it killed legitimately in-flight FastAPI runs (e.g. news/signals
    // OSINT) every time this container restarted on a deploy.
    try {
      const res = await pool.query(
        `UPDATE agent_runs SET status = 'failed', completed_at = NOW(),
                error = 'orphaned by agent service restart'
         WHERE status IN ('queued', 'running')
           AND trigger_type = 'external_service'`
      );
      if (res.rowCount) console.log(`[pi-agent] marked ${res.rowCount} orphaned run(s) as failed`);
    } catch (err) {
      console.warn('[pi-agent] orphan cleanup failed (non-fatal):', err);
    }

    await app.listen({ port: config.port, host: '0.0.0.0' });
    console.log(`[pi-agent] listening on :${config.port}`);
    console.log(`[pi-agent] provider=${config.defaultProvider} model=${config.defaultModel} maxConcurrent=${config.maxConcurrentRuns}`);
  } catch (err) {
    app.log.error(err);
    process.exit(1);
  }
};

void start();
