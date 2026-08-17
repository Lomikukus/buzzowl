import pg from 'pg';
import { v4 as uuidv4 } from 'uuid';
import { config } from './config.js';

const { Pool } = pg;

export const pool = new Pool({ connectionString: config.dbUrl });

// -- Embeddings (mirrors db.py: openai-compatible API or local Ollama) --

const EMBED_DIM = config.embedDim;

// Truncate + L2-renormalize oversized vectors (matches db.py _fit_dim) — avoids
// the non-standard `dimensions` request param so any OpenAI-compatible gateway
// (incl. OpenRouter) works. Safe for MRL-trained models only.
function fitDim(vec: number[]): number[] | null {
  if (vec.length === EMBED_DIM) return vec;
  if (vec.length < EMBED_DIM) return null;
  const head = vec.slice(0, EMBED_DIM);
  const norm = Math.sqrt(head.reduce((s, x) => s + x * x, 0)) || 1;
  return head.map(x => x / norm);
}

export async function getEmbedding(text: string): Promise<number[] | null> {
  try {
    if (config.embedBackend === 'openai') {
      const resp = await fetch(`${config.embedUrl}/v1/embeddings`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${config.embedApiKey}`,
        },
        body: JSON.stringify({ model: config.embedModel, input: text }),
        signal: AbortSignal.timeout(10_000),
      });
      if (!resp.ok) return null;
      const data = await resp.json() as { data: Array<{ embedding: number[] }> };
      const vec = data.data?.[0]?.embedding;
      return vec ? fitDim(vec) : null;
    }
    const resp = await fetch(`${config.embedUrl}/api/embeddings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: config.embedModel, prompt: text }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!resp.ok) return null;
    const data = await resp.json() as { embedding: number[] };
    return data.embedding ?? null;
  } catch {
    return null;
  }
}

function vecToSql(v: number[]): string {
  return `[${v.join(',')}]`;
}

// -- Knowledge base search --

export interface KbResult {
  id: number;
  doc_id: string;
  type: string;
  title: string;
  content: string;
  metadata: Record<string, unknown>;
  score: number;
}

export async function searchKb(orgId: number, query: string, topK = 5): Promise<KbResult[]> {
  const embedding = await getEmbedding(query);

  if (embedding) {
    const vec = vecToSql(embedding);
    const { rows } = await pool.query<KbResult>(
      `WITH vec AS (
         SELECT id, (embedding <=> $1::vector) AS dist
         FROM documents WHERE org_id = $2 AND embedding IS NOT NULL
         ORDER BY embedding <=> $1::vector LIMIT 50
       ),
       fts AS (
         SELECT id, ts_rank(fts_doc, plainto_tsquery('english', $3)) AS rank
         FROM documents WHERE org_id = $2 AND fts_doc @@ plainto_tsquery('english', $3)
       )
       SELECT d.id, d.doc_id, d.type, d.title,
              LEFT(d.content, 800) AS content, d.metadata,
              COALESCE(1 - vec.dist, 0) * 0.6 + COALESCE(fts.rank, 0) * 0.4 AS score
       FROM documents d
       LEFT JOIN vec ON d.id = vec.id
       LEFT JOIN fts ON d.id = fts.id
       WHERE d.org_id = $2 AND (vec.id IS NOT NULL OR fts.id IS NOT NULL)
       ORDER BY score DESC LIMIT $4`,
      [vec, orgId, query, topK],
    );
    return rows;
  }

  // FTS-only fallback when Ollama is offline
  const { rows } = await pool.query<KbResult>(
    `SELECT id, doc_id, type, title, LEFT(content, 800) AS content, metadata,
            ts_rank(fts_doc, plainto_tsquery('english', $1)) AS score
     FROM documents
     WHERE org_id = $2 AND fts_doc @@ plainto_tsquery('english', $1)
     ORDER BY score DESC LIMIT $3`,
    [query, orgId, topK],
  );
  return rows;
}

// -- Clients --

export interface ClientRow {
  id: number;
  name: string;
  metadata: Record<string, unknown>;
  session_count: number;
  last_activity: string | null;
}

export async function getClient(orgId: number, name: string): Promise<{ client: ClientRow; docs: KbResult[] } | null> {
  const { rows } = await pool.query<ClientRow>(
    'SELECT id, name, metadata, session_count, last_activity FROM clients WHERE org_id = $1 AND name ILIKE $2',
    [orgId, name],
  );
  if (!rows[0]) return null;
  const client = rows[0];
  const { rows: docs } = await pool.query<KbResult>(
    `SELECT d.id, d.doc_id, d.type, d.title, LEFT(d.content, 400) AS content, d.metadata, 0 AS score
     FROM documents d
     JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client' AND dl.entity_id = $1
     ORDER BY d.created_at DESC LIMIT 20`,
    [client.id],
  );
  return { client, docs };
}

export async function searchClients(orgId: number, partial: string): Promise<{ name: string; session_count: number }[]> {
  const { rows } = await pool.query(
    `SELECT name, session_count FROM clients
     WHERE org_id = $1 AND name ILIKE $2
     ORDER BY session_count DESC LIMIT 5`,
    [orgId, `%${partial}%`],
  );
  return rows as { name: string; session_count: number }[];
}

export async function listClients(orgId: number): Promise<{ name: string; session_count: number; last_activity: string | null }[]> {
  const { rows } = await pool.query(
    `SELECT name, session_count, last_activity FROM clients
     WHERE org_id = $1 ORDER BY session_count DESC LIMIT 50`,
    [orgId],
  );
  return rows as { name: string; session_count: number; last_activity: string | null }[];
}

// -- Documents --

export async function writeDocument(params: {
  orgId: number;
  agentRunId: number;
  type: string;
  title: string;
  content: string;
  metadata: Record<string, unknown>;
  clientName?: string;
  sourceUrl?: string;
}): Promise<number> {
  const docId = `pi-${params.type}-${uuidv4().split('-')[0]}`;
  const meta = {
    ...params.metadata,
    ...(params.sourceUrl ? { source_url: params.sourceUrl } : {}),
  };

  const embedding = await getEmbedding(`${params.title} ${params.content}`.slice(0, 2000));

  const { rows } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (org_id, doc_id, type, title, content, metadata, source, agent_run_id, embedding)
     VALUES ($1, $2, $3, $4, $5, $6, 'agent', $7, $8)
     ON CONFLICT (org_id, doc_id) DO UPDATE
       SET title = EXCLUDED.title, content = EXCLUDED.content,
           metadata = EXCLUDED.metadata, updated_at = NOW()
     RETURNING id`,
    [params.orgId, docId, params.type, params.title, params.content,
     JSON.stringify(meta), params.agentRunId,
     embedding ? vecToSql(embedding) : null],
  );
  const docDbId = Number(rows[0].id);

  if (params.clientName) {
    const { rows: clients } = await pool.query<{ id: number }>(
      'SELECT id FROM clients WHERE org_id = $1 AND (name ILIKE $2 OR similarity(name, $2) > 0.6) ORDER BY similarity(name, $2) DESC LIMIT 1',
      [params.orgId, params.clientName],
    );
    if (clients[0]) {
      await pool.query(
        `INSERT INTO document_links (document_id, entity_type, entity_id)
         VALUES ($1, 'client', $2) ON CONFLICT DO NOTHING`,
        [docDbId, clients[0].id],
      );
    }
  }

  return docDbId;
}

// -- Agent runs --

export async function createAgentRun(params: {
  orgId: number;
  agentType: string;
  task: string;
}): Promise<number> {
  const { rows } = await pool.query<{ id: string }>(
    `INSERT INTO agent_runs (org_id, trigger_type, agent_type, status, task, tool_calls, output)
     VALUES ($1, 'external_service', $2, 'pending', $3, '[]', '{}')
     RETURNING id`,
    [params.orgId, params.agentType, params.task],
  );
  // pg returns BIGSERIAL as string — normalise to number
  return Number(rows[0].id);
}

// -- Internal API (Pi → FastAPI action bridge) --

async function callInternalApi(
  method: 'POST' | 'PATCH' | 'GET',
  path: string,
  body?: Record<string, unknown>,
): Promise<unknown> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (config.serviceToken) {
    headers['Authorization'] = `Bearer ${config.serviceToken}`;
  }
  const resp = await fetch(`${config.mainServerUrl}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30_000),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    throw new Error(`Internal API ${method} ${path} → ${resp.status}: ${text}`);
  }
  return resp.json();
}

export async function createClient(
  orgId: number,
  name: string,
  metadata?: Record<string, unknown>,
): Promise<{ id: number; name: string }> {
  return callInternalApi('POST', '/api/internal/clients', {
    org_id: orgId, name, metadata: metadata ?? {},
  }) as Promise<{ id: number; name: string }>;
}

export async function updateClient(
  orgId: number,
  name: string,
  patch: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return callInternalApi('PATCH', `/api/internal/clients/${encodeURIComponent(name)}`, {
    org_id: orgId, patch,
  }) as Promise<Record<string, unknown>>;
}

export async function createContact(
  orgId: number,
  name: string,
  opts?: { client?: string; role?: string; email?: string; linkedin_url?: string },
): Promise<{ id: number; name: string }> {
  return callInternalApi('POST', '/api/internal/contacts', {
    org_id: orgId, name, ...opts,
  }) as Promise<{ id: number; name: string }>;
}

export async function getSystemStatus(orgId: number): Promise<Record<string, unknown>> {
  return callInternalApi('GET', `/api/internal/system-status?org_id=${orgId}`) as Promise<Record<string, unknown>>;
}

export interface FindingRow {
  id: number;
  doc_id: string;
  title: string;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export async function getRecentFindings(
  orgId: number,
  clientName: string,
  n = 5,
): Promise<FindingRow[]> {
  const { rows: clients } = await pool.query<{ id: number }>(
    'SELECT id FROM clients WHERE org_id = $1 AND name ILIKE $2 LIMIT 1',
    [orgId, `%${clientName}%`],
  );
  if (!clients[0]) return [];

  const { rows } = await pool.query<FindingRow>(
    `SELECT d.id, d.doc_id, d.title, LEFT(d.content, 4000) AS content,
            d.metadata, d.created_at::text AS created_at
     FROM documents d
     JOIN document_links dl ON dl.document_id = d.id
     WHERE d.org_id = $1
       AND d.type IN ('finding', 'research')
       AND dl.entity_type = 'client'
       AND dl.entity_id = $2
     ORDER BY (d.type = 'research') DESC,
              (d.metadata->>'relevance_score')::int DESC NULLS LAST,
              d.created_at DESC
     LIMIT $3`,
    [orgId, clients[0].id, n],
  );
  return rows;
}

export async function updateAgentRun(id: number, patch: {
  status?: string;
  toolCalls?: unknown[];
  output?: Record<string, unknown>;
  error?: string;
}): Promise<void> {
  const sets: string[] = [];
  const vals: unknown[] = [];
  let i = 1;

  if (patch.status !== undefined) { sets.push(`status = $${i++}`); vals.push(patch.status); }
  if (patch.toolCalls !== undefined) { sets.push(`tool_calls = $${i++}`); vals.push(JSON.stringify(patch.toolCalls)); }
  if (patch.output !== undefined) { sets.push(`output = $${i++}`); vals.push(JSON.stringify(patch.output)); }
  if (patch.error !== undefined) { sets.push(`error = $${i++}`); vals.push(patch.error); }
  if (patch.status === 'done' || patch.status === 'failed' || patch.status === 'cancelled') {
    sets.push(`completed_at = NOW()`);
  }

  if (sets.length === 0) return;
  vals.push(id);
  await pool.query(`UPDATE agent_runs SET ${sets.join(', ')} WHERE id = $${i}`, vals);
}
