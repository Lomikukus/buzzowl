/**
 * Subscription OAuth for the Pi agent service (P1c-1 / P1c-2).
 *
 * Connects consumer AI subscriptions to the agent service via the OAuth module
 * shipped in @earendil-works/pi-ai:
 *   - openai-codex   — ChatGPT Plus/Pro subscription (PKCE + paste-code flow)
 *   - github-copilot — GitHub Copilot (device-code flow)
 *
 * NOT exposed: the anthropic OAuth flow. Anthropic blocks third-party
 * subscription OAuth server-side since Jan 2026, so exposing it would only
 * produce logins that fail at the API.
 *
 * xai: pi-ai ships xai models in its MODEL registry, but its OAuth provider
 * registry (getOAuthProviders()) contains only anthropic / github-copilot /
 * openai-codex — there is no xai subscription OAuth flow to drive. xai stays
 * a plain API-key provider and is intentionally not handled here.
 *
 * How the HTTP endpoints map onto pi-ai's interactive login callbacks:
 *   POST /oauth/start   → calls the pi-ai login function. The function fires
 *                         onAuth({url, instructions}) almost immediately; we
 *                         capture that, park the still-pending login promise in
 *                         an in-memory session (10-min TTL) and return
 *                         {auth_url, session_id} to the caller.
 *   POST /oauth/complete→ openai-codex: resolves the deferred promise we handed
 *                         to onManualCodeInput with the user-pasted code (or
 *                         full redirect URL — pi-ai parses either), which lets
 *                         the parked login promise finish the PKCE exchange.
 *                         github-copilot: no code needed — the user typed the
 *                         device code on github.com; we simply await the parked
 *                         login promise, whose internal poll loop observes the
 *                         approval. Credentials are then persisted to the store.
 *
 * Note on openai-codex inside Docker: pi-ai also starts a localhost:1455
 * callback server in this process. Inside a container the user's browser can
 * never reach it, so the redirect lands on a dead http://localhost:1455/...
 * URL — the user pastes that full URL (or just the code) into /oauth/complete.
 */
import { chmodSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import {
  loginGitHubCopilot,
  loginOpenAICodex,
  refreshGitHubCopilotToken,
  refreshOpenAICodexToken,
  type OAuthCredentials,
} from '@earendil-works/pi-ai/oauth';

export type { OAuthCredentials } from '@earendil-works/pi-ai/oauth';

// ---------------------------------------------------------------------------
// Providers
// ---------------------------------------------------------------------------

export type SubscriptionProvider = 'openai-codex' | 'github-copilot';

export const SUBSCRIPTION_OAUTH_PROVIDERS: readonly SubscriptionProvider[] =
  ['openai-codex', 'github-copilot'];

export function isSubscriptionProvider(name: string): name is SubscriptionProvider {
  return (SUBSCRIPTION_OAUTH_PROVIDERS as readonly string[]).includes(name);
}

export class OAuthHttpError extends Error {
  constructor(public readonly statusCode: number, message: string) {
    super(message);
    this.name = 'OAuthHttpError';
  }
}

// ---------------------------------------------------------------------------
// Credential store — JSON file mapping provider → OAuthCredentials
// ---------------------------------------------------------------------------

type CredentialStore = Record<string, OAuthCredentials>;

function storePath(): string {
  return resolve(process.env.OAUTH_STORE_PATH ?? './data/oauth.json');
}

export function readStore(): CredentialStore {
  try {
    const parsed = JSON.parse(readFileSync(storePath(), 'utf-8')) as unknown;
    if (parsed && typeof parsed === 'object') return parsed as CredentialStore;
  } catch { /* missing or corrupt file → empty store */ }
  return {};
}

function writeStore(store: CredentialStore): void {
  const path = storePath();
  mkdirSync(dirname(path), { recursive: true });
  // Atomic replace; chmod 600 before the rename so the secret file is never
  // world-readable, even transiently.
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(store, null, 2), 'utf-8');
  chmodSync(tmp, 0o600);
  renameSync(tmp, path);
}

export function saveCredentials(provider: SubscriptionProvider, creds: OAuthCredentials): void {
  const store = readStore();
  store[provider] = creds;
  writeStore(store);
}

export function deleteCredentials(provider: SubscriptionProvider): boolean {
  const store = readStore();
  if (!(provider in store)) return false;
  delete store[provider];
  writeStore(store);
  return true;
}

// ---------------------------------------------------------------------------
// Access tokens with auto-refresh
// ---------------------------------------------------------------------------

/** Refresh this long before the recorded expiry to absorb clock skew. */
const EXPIRY_SKEW_MS = 60_000;

// Coalesce concurrent refreshes per provider (two agent runs starting at once
// must not both burn the single-use refresh token).
const inflightRefresh = new Map<SubscriptionProvider, Promise<OAuthCredentials>>();

function refreshCredentials(provider: SubscriptionProvider, creds: OAuthCredentials): Promise<OAuthCredentials> {
  const existing = inflightRefresh.get(provider);
  if (existing) return existing;
  const doRefresh = (async () => {
    switch (provider) {
      case 'openai-codex':
        return refreshOpenAICodexToken(creds.refresh);
      case 'github-copilot':
        return refreshGitHubCopilotToken(
          creds.refresh,
          typeof creds.enterpriseUrl === 'string' ? creds.enterpriseUrl : undefined,
        );
    }
  })();
  inflightRefresh.set(provider, doRefresh);
  return doRefresh.finally(() => inflightRefresh.delete(provider));
}

export interface OAuthAuth {
  /** Bearer/API key value the model provider expects (the OAuth access token). */
  apiKey: string;
  credentials: OAuthCredentials;
}

/**
 * Stored credentials for a provider, auto-refreshed (and re-persisted) via the
 * pi-ai refresh functions when expired. Returns null when not connected.
 * Throws when a refresh is needed but fails.
 */
export async function getOAuthAuth(provider: SubscriptionProvider): Promise<OAuthAuth | null> {
  let creds = readStore()[provider];
  if (!creds) return null;
  if (Date.now() >= creds.expires - EXPIRY_SKEW_MS) {
    creds = await refreshCredentials(provider, creds);
    saveCredentials(provider, creds);
  }
  return { apiKey: creds.access, credentials: creds };
}

/** Convenience wrapper: just the access token, or null when not connected. */
export async function getAccessToken(provider: SubscriptionProvider): Promise<string | null> {
  const auth = await getOAuthAuth(provider);
  return auth?.apiKey ?? null;
}

// ---------------------------------------------------------------------------
// Pending login sessions (POST /oauth/start → POST /oauth/complete)
// ---------------------------------------------------------------------------

const SESSION_TTL_MS = 10 * 60 * 1000;
/** Max time /oauth/start waits for pi-ai to produce the auth URL. */
const START_TIMEOUT_MS = 30_000;
/** /oauth/complete wait budget for the codex code→token exchange. */
const CODEX_EXCHANGE_TIMEOUT_MS = 30_000;
/** /oauth/complete wait budget for Copilot's device-approval poll before we report 'pending'. */
const COPILOT_POLL_WAIT_MS = 60_000;

interface PendingSession {
  provider: SubscriptionProvider;
  createdAt: number;
  authUrl: string;
  instructions?: string;
  /** The parked pi-ai login promise; resolves with credentials to persist. */
  login: Promise<OAuthCredentials>;
  /** openai-codex only: resolves the onManualCodeInput deferred. */
  submitCode?: (code: string) => void;
  codeSubmitted: boolean;
  /** Unwinds the parked login (TTL expiry, supersession, disconnect). */
  cancel: (reason: string) => void;
}

const sessions = new Map<string, PendingSession>();

setInterval(() => {
  const now = Date.now();
  for (const [id, s] of sessions) {
    if (now - s.createdAt > SESSION_TTL_MS) {
      sessions.delete(id);
      s.cancel('OAuth session expired (10 minute TTL)');
    }
  }
}, 60_000).unref();

function sleep(ms: number): Promise<void> {
  return new Promise(res => { setTimeout(res, ms).unref(); });
}

export interface OAuthSessionInfo {
  sessionId: string;
  provider: SubscriptionProvider;
  authUrl: string;
  /** e.g. Copilot's "Enter code: XXXX-XXXX" */
  instructions?: string;
  mode: 'paste-code' | 'device-code';
}

export async function startOAuthLogin(
  provider: SubscriptionProvider,
  opts: { enterpriseDomain?: string } = {},
): Promise<OAuthSessionInfo> {
  // A new login supersedes any pending one for the same provider — the codex
  // flow also binds localhost:1455 in this process, one at a time.
  for (const [id, s] of sessions) {
    if (s.provider === provider) {
      sessions.delete(id);
      s.cancel('superseded by a new login for this provider');
    }
  }

  let onAuthFired!: (info: { url: string; instructions?: string }) => void;
  const authInfo = new Promise<{ url: string; instructions?: string }>(res => { onAuthFired = res; });

  let session: PendingSession;

  if (provider === 'openai-codex') {
    let submit!: (code: string) => void;
    let fail!: (err: Error) => void;
    // Deferred handed to pi-ai as onManualCodeInput — /oauth/complete resolves
    // it with the pasted code (or full redirect URL); cancel rejects it, which
    // unwinds loginOpenAICodex and closes its callback server.
    const manualCode = new Promise<string>((res, rej) => { submit = res; fail = rej; });
    const login = loginOpenAICodex({
      onAuth: info => onAuthFired(info),
      onManualCodeInput: () => manualCode,
      // Only reached if the pasted input contained no parseable code.
      onPrompt: async () => {
        throw new Error('authorization code missing or unparseable — restart the login');
      },
    });
    session = {
      provider, createdAt: Date.now(), authUrl: '', login,
      submitCode: submit, codeSubmitted: false,
      cancel: reason => fail(new Error(reason)),
    };
  } else {
    const abort = new AbortController();
    const login = loginGitHubCopilot({
      // First prompt asks for a GitHub Enterprise domain; blank = github.com.
      onPrompt: async () => opts.enterpriseDomain ?? '',
      onAuth: (url, instructions) => onAuthFired({ url, instructions }),
      signal: abort.signal,
    });
    session = {
      provider, createdAt: Date.now(), authUrl: '', login,
      codeSubmitted: false,
      cancel: () => abort.abort(),
    };
  }

  // Keep rejections observed — a cancelled/expired session must not crash the
  // process with an unhandled rejection.
  session.login.catch(() => { /* surfaced via completeOAuthLogin or dropped */ });

  const raced = await Promise.race([
    authInfo,
    // If login fails before producing an auth URL (e.g. GitHub device-code
    // request rejected), surface that error instead of hanging.
    session.login.then(() => {
      throw new Error('login finished before producing an auth URL');
    }),
    sleep(START_TIMEOUT_MS).then(() => 'timeout' as const),
  ]);
  if (raced === 'timeout') {
    session.cancel('timed out waiting for the auth URL');
    throw new OAuthHttpError(504, `timed out starting the ${provider} OAuth flow`);
  }

  session.authUrl = raced.url;
  session.instructions = raced.instructions;
  const sessionId = randomUUID();
  sessions.set(sessionId, session);

  return {
    sessionId,
    provider,
    authUrl: session.authUrl,
    instructions: session.instructions,
    mode: provider === 'openai-codex' ? 'paste-code' : 'device-code',
  };
}

export type CompleteResult =
  | { status: 'ok'; provider: SubscriptionProvider }
  | { status: 'pending'; provider: SubscriptionProvider };

export async function completeOAuthLogin(sessionId: string, code?: string): Promise<CompleteResult> {
  const session = sessions.get(sessionId);
  if (!session) {
    throw new OAuthHttpError(404, 'unknown or expired session_id — call /oauth/start again');
  }

  if (session.provider === 'openai-codex' && !session.codeSubmitted) {
    const trimmed = (code ?? '').trim();
    if (!trimmed) {
      throw new OAuthHttpError(400,
        'code is required for openai-codex — paste the authorization code or the full localhost:1455 redirect URL');
    }
    session.codeSubmitted = true;
    session.submitCode?.(trimmed);
  }
  // github-copilot: no code here — the user enters the device code on
  // github.com; pi-ai's poll loop inside session.login observes the approval.

  const waitMs = session.provider === 'openai-codex' ? CODEX_EXCHANGE_TIMEOUT_MS : COPILOT_POLL_WAIT_MS;
  let creds: OAuthCredentials;
  try {
    const raced = await Promise.race([
      session.login.then(c => ({ creds: c })),
      sleep(waitMs).then(() => 'timeout' as const),
    ]);
    if (raced === 'timeout') {
      if (session.provider === 'github-copilot') {
        // Device approval simply hasn't happened yet — keep the session alive
        // (until TTL) so the caller can poll /oauth/complete again.
        return { status: 'pending', provider: session.provider };
      }
      sessions.delete(sessionId);
      session.cancel('token exchange timed out');
      throw new OAuthHttpError(504, 'openai-codex token exchange timed out');
    }
    creds = raced.creds;
  } catch (err) {
    if (err instanceof OAuthHttpError) throw err;
    sessions.delete(sessionId);
    throw new OAuthHttpError(400,
      `${session.provider} login failed: ${err instanceof Error ? err.message : String(err)}`);
  }

  saveCredentials(session.provider, creds);
  sessions.delete(sessionId);
  return { status: 'ok', provider: session.provider };
}

// ---------------------------------------------------------------------------
// Status / disconnect
// ---------------------------------------------------------------------------

export function oauthStatus(): Record<string, { connected: boolean; expires_at: string | null }> {
  const store = readStore();
  const out: Record<string, { connected: boolean; expires_at: string | null }> = {};
  for (const provider of SUBSCRIPTION_OAUTH_PROVIDERS) {
    const creds = store[provider];
    out[provider] = {
      connected: Boolean(creds),
      expires_at: creds ? new Date(creds.expires).toISOString() : null,
    };
  }
  return out;
}

export function disconnectOAuth(provider: SubscriptionProvider): boolean {
  // Also drop any pending login session for the provider.
  for (const [id, s] of sessions) {
    if (s.provider === provider) {
      sessions.delete(id);
      s.cancel('provider disconnected');
    }
  }
  return deleteCredentials(provider);
}
