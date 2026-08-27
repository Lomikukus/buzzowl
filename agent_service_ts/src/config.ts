import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { load as loadYaml } from 'js-yaml';

const embedUrl = (process.env.EMBED_URL ?? process.env.OLLAMA_URL ?? 'http://localhost:11434').replace(/\/$/, '');
const ollamaUrl = (process.env.OLLAMA_URL ?? 'http://localhost:11434').replace(/\/$/, '');

// ---------------------------------------------------------------------------
// llm: block from the mounted config.yaml — single source of truth for LLM
// providers. Absent/unreadable yaml degrades gracefully to built-in defaults.
// ---------------------------------------------------------------------------

export type ProviderKind = 'openai-compat' | 'anthropic';

interface YamlProviderDef {
  kind?: string;
  base_url?: string;
  api_key?: string;
  api_key_env?: string;
}

interface LlmYaml {
  providers?: Record<string, YamlProviderDef>;
  roles?: Record<string, { provider?: string; model?: string }>;
}

// Top-level embed_dim from the same mounted config.yaml (captured while
// scanning for the llm: block). undefined = not found in any candidate.
let yamlEmbedDim: number | undefined;
// hosted: block from config.yaml (Phase 6a) — only enforce_plans matters here.
let yamlHostedEnforce = false;

/** Recursively merge `overlay` into `base` (overlay wins), same as the server. */
function deepMerge(base: Record<string, unknown>, overlay: Record<string, unknown>): Record<string, unknown> {
  for (const [key, value] of Object.entries(overlay)) {
    const current = base[key];
    if (value && typeof value === 'object' && !Array.isArray(value)
        && current && typeof current === 'object' && !Array.isArray(current)) {
      deepMerge(current as Record<string, unknown>, value as Record<string, unknown>);
    } else {
      base[key] = value;
    }
  }
  return base;
}

function readYaml(path: string): Record<string, unknown> | null {
  try {
    return (loadYaml(readFileSync(path, 'utf-8')) as Record<string, unknown> | null) ?? null;
  } catch {
    return null;                              // missing or invalid — caller moves on
  }
}

function loadLlmBlock(): LlmYaml {
  const candidates = [
    process.env.CONFIG_YAML_PATH ?? '',
    '/app/config.yaml',                        // Docker: bind-mounted read-only
    resolve(process.cwd(), 'config.yaml'),
    resolve(process.cwd(), '../config.yaml'),  // dev: run from agent_service_ts/
  ].filter(Boolean);
  for (const path of candidates) {
    const parsed = readYaml(path);
    if (!parsed) continue;
    // Optional untracked overlay next to it (same mechanism as the server).
    const local = readYaml(path.replace(/config\.yaml$/, 'config.local.yaml'));
    if (local) deepMerge(parsed, local);
    if (yamlEmbedDim === undefined) {
      const dim = Number(parsed?.embed_dim);
      if (Number.isInteger(dim) && dim > 0) yamlEmbedDim = dim;
    }
    const hosted = parsed?.hosted as { enforce_plans?: boolean } | undefined;
    if (hosted && typeof hosted === 'object') yamlHostedEnforce = !!hosted.enforce_plans;
    const llm = parsed?.llm as LlmYaml | undefined;
    if (llm && typeof llm === 'object') return llm;
  }
  return {};
}

export const llmConfig: LlmYaml = loadLlmBlock();

// Legacy `brain` values map onto provider names; unknown values pass through
// so a provider name given as `brain` also works.
const BRAIN_TO_PROVIDER: Record<string, string> = {
  openrouter: 'openrouter',
  ollama: 'ollama',
  claude: 'anthropic',
};

export function brainToProvider(brainOrProvider: string): string {
  return BRAIN_TO_PROVIDER[brainOrProvider] ?? brainOrProvider;
}

export interface ResolvedProvider {
  name: string;
  kind: ProviderKind;
  /** '' = use the pi-ai built-in default endpoint for this provider */
  baseUrl: string;
  /** '' = let pi-ai fall back to its own env-var key resolution */
  apiKey: string;
}

const LOCALHOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0)(:|\/|$)/i;

// Fallbacks when config.yaml is missing or predates the llm: block.
const BUILTIN_PROVIDERS: Record<string, YamlProviderDef> = {
  openrouter: { kind: 'openai-compat', base_url: 'https://openrouter.ai/api/v1', api_key_env: 'OPENROUTER_API_KEY' },
  anthropic:  { kind: 'anthropic', api_key_env: 'ANTHROPIC_API_KEY' },
  openai:     { kind: 'openai-compat', api_key_env: 'OPENAI_API_KEY' },
  ollama:     { kind: 'openai-compat', base_url: `${ollamaUrl}/v1`, api_key: 'local' },
};

export function resolveProvider(name: string): ResolvedProvider {
  const key = brainToProvider(name);
  const def = llmConfig.providers?.[key] ?? BUILTIN_PROVIDERS[key] ?? {};
  const kind: ProviderKind = def.kind === 'anthropic' ? 'anthropic' : 'openai-compat';

  let baseUrl = (def.base_url ?? '').replace(/\/$/, '');

  // config.yaml is written from the host's perspective. Inside Docker,
  // localhost does not reach the host — when OLLAMA_URL points elsewhere
  // (host.docker.internal) remap a localhost base_url on the same port to it.
  if (baseUrl && process.env.OLLAMA_URL && LOCALHOST_RE.test(baseUrl)) {
    try {
      const target = new URL(baseUrl);
      const mapped = new URL(ollamaUrl);
      if (target.port === mapped.port) {
        baseUrl = `${mapped.origin}${target.pathname}`.replace(/\/$/, '');
      }
    } catch { /* keep baseUrl as-is */ }
  }

  let apiKey = def.api_key
    || (def.api_key_env ? (process.env[def.api_key_env] ?? '') : '');
  if (!apiKey && key === 'openrouter') {
    // Deployment quirk: the server's .env historically names the key OPENROUTE
    apiKey = process.env.OPENROUTER_API_KEY || process.env.OPENROUTE || '';
  }
  if (!apiKey && kind === 'openai-compat' && LOCALHOST_RE.test(def.base_url ?? '')) {
    // Keyless local servers (Ollama, LM Studio): pi-ai's openai-completions
    // provider rejects empty keys, so use a non-empty dummy.
    apiKey = 'local';
  }

  return { name: key, kind, baseUrl, apiKey };
}

/**
 * Per-org override (Phase 6a): if the org stored its own provider of that name
 * (light plan, keys decrypted from orgs.settings), use it; else the platform
 * config. A light org WITHOUT its own providers is refused when the operator
 * enforces plans (no platform spend for it) — mirrors llm.resolve() in Python.
 */
export async function resolveProviderForOrg(name: string, orgId?: number,
                                            overlayLoader?: (id: number) => Promise<import('./db.js').OrgLlmOverlay | null>)
  : Promise<ResolvedProvider & { fromOrg?: boolean }> {
  if (!orgId || !overlayLoader) return resolveProvider(name);
  const ov = await overlayLoader(orgId);
  if (!ov) return resolveProvider(name);
  const key = brainToProvider(name);
  const own = ov.providers?.[key];
  if (ov.plan !== 'premium' && own) {
    const kind: ProviderKind = own.kind === 'anthropic' ? 'anthropic' : 'openai-compat';
    return { name: key, kind, baseUrl: (own.base_url ?? '').replace(/\/$/, ''), apiKey: own.api_key || 'local', fromOrg: true };
  }
  if (ov.plan !== 'premium' && Object.keys(ov.providers ?? {}).length > 0) {
    // the org configured providers but not this one → its first provider owns every role
    const first = Object.keys(ov.providers)[0];
    const p = ov.providers[first];
    const kind: ProviderKind = p.kind === 'anthropic' ? 'anthropic' : 'openai-compat';
    return { name: first, kind, baseUrl: (p.base_url ?? '').replace(/\/$/, ''), apiKey: p.api_key || 'local', fromOrg: true };
  }
  if (ov.plan !== 'premium' && ov.enforce) {
    throw new Error('this workspace has no LLM provider configured — add your own key under Settings › LLM (light plan) or upgrade to premium');
  }
  return resolveProvider(name);
}

export const config = {
  port: parseInt(process.env.AGENT_SERVICE_PORT ?? '8001', 10),
  dbUrl: process.env.DATABASE_URL ?? 'postgresql://whisper:whisper@localhost:5432/whisper',
  ollamaUrl,
  openrouterApiKey: process.env.OPENROUTER_API_KEY ?? '',
  defaultBrain: (process.env.DEFAULT_BRAIN ?? 'openrouter') as string,
  defaultProvider: brainToProvider(process.env.DEFAULT_PROVIDER ?? process.env.DEFAULT_BRAIN ?? 'openrouter'),
  defaultModel: process.env.DEFAULT_MODEL ?? 'minimax/minimax-m2.7',
  // Inside Docker: SearXNG is reachable via its compose service name
  searxngUrl: (process.env.SEARXNG_URL ?? 'http://searxng:8080').replace(/\/$/, ''),
  browserServiceUrl: (process.env.BROWSER_SERVICE_URL ?? 'http://browser-service:3000').replace(/\/$/, ''),
  // Camofox (anti-bot Firefox) — permanent part of Pi's production setup
  camofoxUrl: (process.env.CAMOFOX_URL ?? '').replace(/\/$/, ''),
  serviceToken: process.env.AGENT_SERVICE_TOKEN ?? '',
  // Explicit dev backdoor — same env var and semantics as the Python server
  // (server.py / routers/internal.py). Without a serviceToken the API is
  // fail-closed (401 on everything but /health); this is the only way to open it.
  allowInsecureInternal: process.env.ALLOW_INSECURE_INTERNAL === '1',
  hostedEnforcePlans: yamlHostedEnforce,
  mainServerUrl: (process.env.MAIN_SERVER_URL ?? 'http://host.docker.internal:8000').replace(/\/$/, ''),
  // Embeddings — must match the main server's backend/model or vectors won't align.
  // When embedUrl is OpenRouter, the OpenRouter key is reused automatically.
  embedBackend: (process.env.EMBED_BACKEND ?? 'ollama') as 'ollama' | 'openai',
  embedUrl,
  embedApiKey: process.env.EMBED_API_KEY
    || (embedUrl.includes('openrouter') ? (process.env.OPENROUTER_API_KEY ?? '') : ''),
  embedModel: process.env.EMBED_MODEL ?? 'nomic-embed-text',
  // Priority: EMBED_DIM env > config.yaml embed_dim > 768. Must match the
  // vector(N) column in schema.sql and the main server's embed_dim.
  embedDim: parseInt(process.env.EMBED_DIM ?? '', 10) || yamlEmbedDim || 768,
  idleTimeoutMs: 600_000,   // fail if no new tool calls for this long (10 min)
  maxRunTimeMs:  5_400_000, // absolute hard cap (90 min)
  // Max agent runs executing simultaneously; further runs wait in a FIFO queue
  maxConcurrentRuns: parseInt(process.env.AGENT_MAX_CONCURRENT ?? '2', 10),
};
