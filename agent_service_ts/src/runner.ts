import * as db from './db.js';
import { runPiAgent } from './agent.js';
import { config, brainToProvider } from './config.js';

export interface RunRequest {
  agent_type?: string;
  task: string;
  org_id: number;
  subject?: string;
  subject_type?: string;
  /** Provider name from the config.yaml llm: block (openrouter | anthropic | ollama | …) */
  provider?: string;
  /** Legacy field — mapped to a provider (openrouter→openrouter, ollama→ollama, claude→anthropic) */
  brain?: string;
  model?: string;
  max_queries?: number;
  callback_url?: string;
  use_browser_fetch?: boolean;
}

export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

export interface RunState {
  run_id: number;
  status: RunStatus;
  agent_type: string;
  subject: string;
  started_at: string | null;
  completed_at: string | null;
  output: Record<string, unknown>;
  error: string | null;
  tool_calls: Array<{ tool: string; args: unknown; result: string; ts: string }>;
  abort: AbortController;
}

const runs = new Map<number, RunState>();

// -- Concurrency gate ---------------------------------------------------------
// Only maxConcurrentRuns agent loops execute at once; the rest wait FIFO in
// 'queued' state. Unbounded parallelism overloaded the LLM backend and browser
// chain (Session 81: bulk CSV import), forcing heartbeats to be disabled.

let activeSlots = 0;
const slotWaiters: Array<() => void> = [];

async function acquireSlot(): Promise<void> {
  if (activeSlots < config.maxConcurrentRuns) {
    activeSlots++;
    return;
  }
  await new Promise<void>(resolve => slotWaiters.push(resolve));
}

function releaseSlot(): void {
  const next = slotWaiters.shift();
  if (next) next();          // hand the slot to the next waiter (activeSlots unchanged)
  else activeSlots--;
}

export function queueStats(): { active: number; queued: number; max_concurrent: number } {
  return { active: activeSlots, queued: slotWaiters.length, max_concurrent: config.maxConcurrentRuns };
}

// Evict finished runs so the in-memory map stays bounded (it previously grew
// forever — the service degraded over multi-day uptimes). DB rows are permanent.
const EVICT_AFTER_MS = 60 * 60 * 1000;
setInterval(() => {
  const cutoff = Date.now() - EVICT_AFTER_MS;
  for (const [id, state] of runs) {
    if (state.completed_at && Date.parse(state.completed_at) < cutoff) runs.delete(id);
  }
}, 10 * 60 * 1000).unref();

export async function createRun(body: RunRequest): Promise<number> {
  const agentType = body.agent_type ?? 'research';
  const subject = body.subject ?? body.task.slice(0, 60);

  const runId = await db.createAgentRun({
    orgId: body.org_id,
    agentType,
    task: body.task,
  });

  const state: RunState = {
    run_id: runId,
    status: 'queued',
    agent_type: agentType,
    subject,
    started_at: null,
    completed_at: null,
    output: {},
    error: null,
    tool_calls: [],
    abort: new AbortController(),
  };
  runs.set(runId, state);
  return runId;
}

export function startRun(runId: number, body: RunRequest): void {
  // Non-blocking — intentionally not awaited
  void executeRun(runId, body);
}

async function executeRun(runId: number, body: RunRequest): Promise<void> {
  const state = runs.get(runId);
  if (!state) return;

  await acquireSlot();

  // Cancelled (or evicted) while waiting in the queue — never started
  if (state.status !== 'queued') {
    releaseSlot();
    if (state.status === 'cancelled') {
      state.completed_at = new Date().toISOString();
      await db.updateAgentRun(runId, { status: 'cancelled', error: 'cancelled while queued' });
    }
    return;
  }

  state.status = 'running';
  state.started_at = new Date().toISOString();
  await db.updateAgentRun(runId, { status: 'running' });

  const provider = body.provider
    ?? (body.brain ? brainToProvider(body.brain) : config.defaultProvider);
  const model = body.model ?? config.defaultModel;
  const subject = body.subject ?? body.task.slice(0, 60);

  // Activity-based timeout: fail only if no new tool calls for idleTimeoutMs.
  // maxRunTimeMs is an absolute hard cap regardless of activity.
  let lastToolCallCount = 0;
  let lastActivityTime = Date.now();
  let intervalId: ReturnType<typeof setInterval>;
  let absoluteId: ReturnType<typeof setTimeout>;

  const activityPromise = new Promise<never>((_, reject) => {
    intervalId = setInterval(() => {
      const current = state.tool_calls.length;
      if (current > lastToolCallCount) {
        lastToolCallCount = current;
        lastActivityTime = Date.now();
      }
      const idleMs = Date.now() - lastActivityTime;
      if (idleMs > config.idleTimeoutMs) {
        state.abort.abort();
        reject(new Error(
          `run timed out: no tool calls for ${Math.round(idleMs / 1000)}s ` +
          `(${lastToolCallCount} total tool calls made)`
        ));
      }
    }, 30_000);
  });

  const absolutePromise = new Promise<never>((_, reject) => {
    absoluteId = setTimeout(() => {
      state.abort.abort();
      reject(new Error(`run exceeded absolute max runtime of ${config.maxRunTimeMs / 60000} min`));
    }, config.maxRunTimeMs);
  });

  try {
    await Promise.race([
      runPiAgent({
        orgId: body.org_id,
        agentRunId: runId,
        agentType: state.agent_type,
        task: body.task,
        subject,
        provider,
        model,
        abortController: state.abort,
        toolCallLog: state.tool_calls,
        useBrowserFetch: body.use_browser_fetch ?? false,
      }),
      activityPromise,
      absolutePromise,
    ]);

    const output = {
      tool_calls_made: state.tool_calls.length,
      documents_written: state.tool_calls.filter(t => t.tool === 'write_document').length,
      searches_made: state.tool_calls.filter(t => t.tool === 'web_search').length,
    };

    // A run that searched and fetched for minutes but wrote nothing is not a
    // success: it cost tokens and left the knowledge base untouched. Report it
    // as failed so the UI says so and the follow-up chain (brief, matching)
    // does not run on nothing. agent.ts already nudged the model twice by here.
    if (output.documents_written === 0 && output.tool_calls_made > 0) {
      const msg = `run wrote no documents (${output.tool_calls_made} tool calls, ` +
        `${output.searches_made} searches) — the model never called write_document`;
      state.status = 'failed';
      state.completed_at = new Date().toISOString();
      state.error = msg;
      state.output = output;
      await db.updateAgentRun(runId, { status: 'failed', toolCalls: state.tool_calls, output, error: msg });
      console.warn(`[pi-agent] run ${runId}: ${msg}`);
      if (body.callback_url) {
        await fireCallback(body.callback_url, {
          run_id: runId, status: 'failed', agent_type: state.agent_type,
          subject, output, error: msg,
        });
      }
      return;
    }

    state.status = 'done';
    state.completed_at = new Date().toISOString();
    state.output = output;
    await db.updateAgentRun(runId, { status: 'done', toolCalls: state.tool_calls, output });

    if (body.callback_url) {
      await fireCallback(body.callback_url, {
        run_id: runId, status: 'done', agent_type: state.agent_type,
        subject, output, error: null,
      });
    }
  } catch (err) {
    // A user cancel aborts the agent mid-run — record it as cancelled, not failed.
    // (cancelRun mutates status from another call stack, so widen the narrowed type)
    const wasCancelled = (state.status as RunStatus) === 'cancelled';
    const errMsg = wasCancelled ? 'cancelled by user' : String(err);
    state.status = wasCancelled ? 'cancelled' : 'failed';
    state.completed_at = new Date().toISOString();
    state.error = errMsg;
    await db.updateAgentRun(runId, {
      status: state.status,
      toolCalls: state.tool_calls,
      error: errMsg,
    });
    if (body.callback_url) {
      await fireCallback(body.callback_url, {
        run_id: runId, status: state.status, agent_type: state.agent_type,
        subject, output: {}, error: errMsg,
      });
    }
  } finally {
    clearInterval(intervalId!);
    clearTimeout(absoluteId!);
    releaseSlot();
  }
}

async function fireCallback(url: string, payload: Record<string, unknown>): Promise<void> {
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (config.serviceToken) headers['Authorization'] = `Bearer ${config.serviceToken}`;
    await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10_000),
    });
  } catch (err) {
    console.warn('[runner] callback failed:', err);
  }
}

export function getRun(runId: number): RunState | undefined {
  return runs.get(runId);
}

export function cancelRun(runId: number): boolean {
  const state = runs.get(runId);
  if (!state || state.status === 'done' || state.status === 'failed') return false;
  state.abort.abort();
  state.status = 'cancelled';
  return true;
}

export function listRuns(orgId?: number): RunState[] {
  const all = [...runs.values()];
  return orgId !== undefined
    ? all.filter(() => true)  // org filtering via DB; in-memory store has all
    : all;
}
