"""
db.py — PostgreSQL + pgvector layer for Buzzowl

Schema single source of truth is schema.sql (= version 1); everything beyond
v1 lives in migrations/NNN_name.sql. init_db() applies both automatically:
fresh DB → full schema.sql; legacy DB (no schema_version table) → one-time
baseline reconcile, then stamped v1; then pending migrations in order.

This module otherwise manages the connection pool and provides async
data-access functions for orgs, users, clients, contacts, documents, search.

All public functions are async. Use _run_coro_from_thread() for calls
from ThreadPoolExecutor workers (e.g. inside _do_export).
"""

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import requests
from pgvector.asyncpg import register_vector

logger = logging.getLogger("whisper.db")

_pool: Optional[asyncpg.Pool] = None
_embed_model: str = "nomic-embed-text"
_embed_dim: int = 768
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# ---------------------------------------------------------------------------
# Schema management (single source: schema.sql = v1, migrations/ beyond that)
# ---------------------------------------------------------------------------

# Same repo-root resolution pattern as context.BASE_DIR (db.py must not import
# context — context imports db).
BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
MIGRATIONS_DIR = BASE_DIR / "migrations"

# schema.sql IS this version; migration files must be numbered above it.
BASELINE_VERSION = 1

_SCHEMA_VERSION_DDL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version    INT PRIMARY KEY,
        applied_at TIMESTAMPTZ DEFAULT now()
    )
"""

# Migration files: NNN_short_name.sql (README.md etc. are ignored).
_MIGRATION_FILE_RE = re.compile(r"^(\d+)_[A-Za-z0-9][\w\-]*\.sql$")


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


async def _init_conn(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda obj: json.dumps(obj, default=str).replace(chr(0), ""),
        decoder=json.loads,
        schema="pg_catalog",
    )


# ---------------------------------------------------------------------------
# Schema migration runner
# ---------------------------------------------------------------------------
#
# Semantics (see migrations/README.md):
#   fresh DB (no orgs table)          → apply full schema.sql (stamps v1 itself)
#   legacy DB (no schema_version)     → replay the historical runtime DDL that
#                                       used to live here in init_db() (all
#                                       IF NOT EXISTS — the "baseline
#                                       reconcile"), then stamp v1
#   then                              → apply migrations/NNN_name.sql with
#                                       NNN > current version, in order, one
#                                       transaction per file, stamping each.

# Historical runtime DDL, verbatim from the pre-P1b init_db() block. Only the
# legacy baseline-reconcile path runs these; schema.sql already contains all
# of it for fresh installs. Do NOT extend this list — new DDL goes into
# migrations/ instead.
_BASELINE_RECONCILE_STATEMENTS: tuple[str, ...] = (
    # Phase 9.5: chat sessions
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id          BIGSERIAL PRIMARY KEY,
        org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
        title       TEXT NOT NULL DEFAULT 'New conversation',
        messages    JSONB NOT NULL DEFAULT '[]',
        client_name TEXT,
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_org ON chat_sessions(org_id, updated_at DESC)",
    # Seller Product Intelligence
    """
    CREATE TABLE IF NOT EXISTS seller_companies (
        id              BIGSERIAL PRIMARY KEY,
        org_id          BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        name            TEXT NOT NULL,
        website_url     TEXT,
        industry        TEXT,
        research_status TEXT NOT NULL DEFAULT 'pending',
        research_doc_id BIGINT,
        metadata        JSONB NOT NULL DEFAULT '{}',
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(org_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        id                BIGSERIAL PRIMARY KEY,
        org_id            BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        seller_company_id BIGINT NOT NULL,
        name              TEXT NOT NULL,
        category          TEXT,
        description       TEXT,
        key_features      JSONB NOT NULL DEFAULT '[]',
        pricing_info      TEXT,
        target_customer   TEXT,
        is_focus          BOOLEAN NOT NULL DEFAULT FALSE,
        priority          INTEGER NOT NULL DEFAULT 0,
        is_favorite       BOOLEAN NOT NULL DEFAULT FALSE,
        is_shared         BOOLEAN NOT NULL DEFAULT FALSE,
        status            TEXT NOT NULL DEFAULT 'draft',
        source_doc_id     BIGINT,
        metadata          JSONB NOT NULL DEFAULT '{}',
        created_at        TIMESTAMPTZ DEFAULT NOW(),
        updated_at        TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS website_url TEXT",
    # UI A/B: opt-in front-end theme variant ('classic' | 'carbon')
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS ui_variant TEXT NOT NULL DEFAULT 'classic'",
    # Phase 18: invite-key whitelist
    """
    CREATE TABLE IF NOT EXISTS invitations (
        id          BIGSERIAL PRIMARY KEY,
        org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        invite_key  TEXT NOT NULL UNIQUE,
        email       TEXT,
        role        TEXT NOT NULL DEFAULT 'member',
        created_by  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        expires_at  TIMESTAMPTZ NOT NULL,
        used_at     TIMESTAMPTZ,
        used_by     BIGINT REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invitations_org ON invitations(org_id, used_at, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_invitations_key ON invitations(invite_key)",
    # System-level registration keys (operator-issued, gates org creation)
    """
    CREATE TABLE IF NOT EXISTS registration_keys (
        id          BIGSERIAL PRIMARY KEY,
        reg_key     TEXT NOT NULL UNIQUE,
        label       TEXT,
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        expires_at  TIMESTAMPTZ,
        used_at     TIMESTAMPTZ,
        used_by_org BIGINT REFERENCES orgs(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reg_keys_key ON registration_keys(reg_key)",
    # Thesis evaluation: manual research timing (Session 88)
    """
    CREATE TABLE IF NOT EXISTS research_sessions (
        id              BIGSERIAL PRIMARY KEY,
        org_id          BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        user_id         BIGINT REFERENCES users(id) ON DELETE SET NULL,
        client_name     TEXT NOT NULL,
        started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ended_at        TIMESTAMPTZ,
        method          TEXT NOT NULL DEFAULT 'manual',
        sources_checked INTEGER,
        notes           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_research_sessions_org ON research_sessions(org_id, started_at)",
    # Usage analytics: what users ask the system (Session 88)
    """
    CREATE TABLE IF NOT EXISTS prompt_log (
        id         BIGSERIAL PRIMARY KEY,
        org_id     BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
        surface    TEXT NOT NULL,
        prompt     TEXT NOT NULL,
        context    JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prompt_log_org ON prompt_log(org_id, created_at)",
    # Per-contact outreach log (Session 92)
    """
    CREATE TABLE IF NOT EXISTS contact_log (
        id            BIGSERIAL PRIMARY KEY,
        org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
        client_name   TEXT NOT NULL,
        contact_name  TEXT,
        contact_email TEXT,
        subject       TEXT,
        body          TEXT,
        sent_at       TIMESTAMPTZ DEFAULT NOW(),
        replied       BOOLEAN NOT NULL DEFAULT false,
        follow_up     BOOLEAN NOT NULL DEFAULT false,
        source_doc_id BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_contact_log_user ON contact_log(org_id, user_id, sent_at DESC)",
    # User to-do / follow-up tasks (Deploy 2)
    """
    CREATE TABLE IF NOT EXISTS user_tasks (
        id           BIGSERIAL PRIMARY KEY,
        org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        user_id      BIGINT REFERENCES users(id) ON DELETE SET NULL,
        client_name  TEXT,
        title        TEXT NOT NULL,
        notes        TEXT,
        due_date     DATE,
        priority     INTEGER NOT NULL DEFAULT 5,
        status       TEXT NOT NULL DEFAULT 'open',   -- open | done
        source       TEXT NOT NULL DEFAULT 'manual', -- manual | follow_up | chat
        completed_at TIMESTAMPTZ,
        created_at   TIMESTAMPTZ DEFAULT NOW(),
        updated_at   TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_tasks_open ON user_tasks(org_id, user_id, due_date) WHERE status='open'",
    # Per-rep focus backfill (Session 90): focus is per-user via
    # metadata.focus_user_ids; metadata.is_focus stays the derived union.
    # Backfills legacy org-wide focus → attributed to the client's owner.
    # Idempotent: skips rows already migrated. Only runs on legacy DBs —
    # fresh installs have no legacy focus rows to convert.
    """
    UPDATE clients
    SET metadata = metadata
        || jsonb_build_object('focus_user_ids', jsonb_build_array(created_by))
    WHERE (metadata->>'is_focus')::boolean = true
      AND created_by IS NOT NULL
      AND COALESCE(jsonb_array_length(metadata->'focus_user_ids'), 0) = 0
    """,
)


def parse_migration_version(filename: str) -> Optional[int]:
    """Version prefix of a migrations/ file name, or None if it isn't one.

    Matches NNN_short_name.sql (numeric prefix, underscore, name). README.md,
    editor swap files, etc. return None and are ignored by the runner.
    """
    m = _MIGRATION_FILE_RE.match(filename)
    return int(m.group(1)) if m else None


def pending_migrations(
    current_version: int,
    migrations_dir: Optional[Path] = None,
) -> list[tuple[int, Path]]:
    """(version, path) of migration files above current_version, in order.

    Sorted numerically (002 < 010 — not lexicographically). Raises ValueError
    on duplicate version prefixes: two files claiming the same version is a
    repo bug that must fail loudly, not apply in arbitrary order.
    """
    directory = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    seen: dict[int, str] = {}
    out: list[tuple[int, Path]] = []
    for entry in sorted(directory.iterdir()):
        version = parse_migration_version(entry.name)
        if version is None:
            continue
        if version in seen:
            raise ValueError(
                f"Duplicate migration version {version}: {seen[version]} and {entry.name}"
            )
        seen[version] = entry.name
        if version > current_version:
            out.append((version, entry))
    out.sort(key=lambda item: item[0])
    return out


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = $1)",
        table,
    ))


async def _run_schema_migrations(
    pool: asyncpg.Pool,
    migrations_dir: Optional[Path] = None,
) -> None:
    """Bring the connected database up to the current schema version."""
    async with pool.acquire() as conn:
        # (a) Fresh database → the full schema.sql (which stamps v1 itself).
        if not await _table_exists(conn, "orgs"):
            logger.info("Fresh database — applying %s", SCHEMA_PATH.name)
            async with conn.transaction():
                await conn.execute(SCHEMA_PATH.read_text())

        # (b) Legacy database (pre-dates schema_version) → one-time baseline
        #     reconcile with the historical runtime DDL, then stamp v1.
        elif not await _table_exists(conn, "schema_version"):
            logger.info("Legacy database — baseline reconcile → schema v%d", BASELINE_VERSION)
            async with conn.transaction():
                for statement in _BASELINE_RECONCILE_STATEMENTS:
                    await conn.execute(statement)
                await conn.execute(_SCHEMA_VERSION_DDL)
                await conn.execute(
                    "INSERT INTO schema_version (version) VALUES ($1) ON CONFLICT DO NOTHING",
                    BASELINE_VERSION,
                )

        # (c) Apply pending migrations/NNN_name.sql, one transaction per file.
        current = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        for version, path in pending_migrations(int(current), migrations_dir):
            logger.info("Applying migration %s (schema v%d)", path.name, version)
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_version (version) VALUES ($1)", version
                )


async def init_db(
    db_url: str,
    embed_model: str,
    embed_dim: int,
    embed_backend: str = "",
    embed_url: str = "",
    embed_api_key: str = "",
    pool_min: int = 2,
    pool_max: int = 20,
) -> None:
    """Create connection pool. Schema is managed via schema.sql — not created here."""
    global _pool, _embed_model, _embed_dim, _embed_backend, _embed_url, _embed_api_key
    if not db_url:
        logger.info("db_url not set — DB layer disabled")
        return
    _embed_model = embed_model
    _embed_dim = embed_dim
    _embed_backend = embed_backend or os.environ.get("EMBED_BACKEND", "") or "ollama"
    _embed_url = embed_url
    _embed_api_key = embed_api_key or os.environ.get("EMBED_API_KEY", "")
    if not _embed_api_key and "openrouter" in _resolve_embed_url():
        # Embeddings via OpenRouter reuse the existing OpenRouter key — no
        # separate embeddings account needed
        _embed_api_key = (
            os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("OPENROUTE", "")
        )
    # Fresh database: the pool's connection init registers the pgvector codec,
    # which needs the extension to exist BEFORE schema.sql runs — create it here
    # with a plain connection (idempotent; harmless on an initialised DB).
    try:
        _boot = await asyncpg.connect(db_url, timeout=15)
        try:
            await _boot.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await _boot.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        finally:
            await _boot.close()
    except Exception as exc:
        logger.warning("could not ensure DB extensions (continuing): %s", exc)
    try:
        _pool = await asyncpg.create_pool(
            db_url,
            min_size=pool_min,
            max_size=pool_max,
            command_timeout=60,
            init=_init_conn,
        )
        try:
            from rich.console import Console
            Console().print("[green]DB connected[/green]")
        except ImportError:
            logger.info("DB connected")
    except Exception as exc:
        logger.warning("DB connection failed (non-fatal): %s", exc)
        try:
            from rich.console import Console
            Console().print(f"[yellow]DB unavailable (non-fatal): {exc}[/yellow]")
        except ImportError:
            pass
        _pool = None

    # Bring the schema up to date (fresh install / legacy baseline / pending
    # migrations). Best-effort like everything else in this layer: a failure
    # is logged loudly but the server keeps running on the existing schema.
    if _pool:
        try:
            await _run_schema_migrations(_pool)
        except Exception as exc:
            logger.warning(
                "Schema migration step failed (non-fatal — continuing on existing schema): %s",
                exc,
            )
            try:
                from rich.console import Console
                Console().print(f"[yellow]Schema migration failed (non-fatal): {exc}[/yellow]")
            except ImportError:
                pass

    # Best-effort sanity probe: warn loudly if stored vectors don't match the
    # configured embed_dim (never fatal — graceful degradation).
    await warn_on_embed_dim_mismatch()


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
#
# Backends:
#   ollama  — POST {embed_url}/api/embeddings   (default url http://localhost:11434,
#             overridable via EMBED_URL / OLLAMA_URL env or init_db kwargs)
#   openai  — POST {embed_url}/v1/embeddings    (OpenAI-compatible: openrouter.ai/api,
#             api.openai.com, api.jina.ai, …; embed_url is the base WITHOUT /v1;
#             requires an api key — when embed_url is OpenRouter, the OpenRouter
#             key is used automatically if EMBED_API_KEY is not set)
#
# Oversized vectors are truncated + L2-renormalized client-side (_fit_dim) instead
# of sending the non-standard `dimensions` request param — works on every gateway
# and is exactly what OpenAI does server-side. Use MRL-trained models only
# (text-embedding-3-*, qwen3-embedding, jina v3); classic models lose quality
# when truncated.
#
# get_embedding() is sync (requests) — only call it from ThreadPoolExecutor
# workers (e.g. _promote_session). From async code use `await embed_text()`,
# which runs the same call in a thread so the event loop is never blocked.

_embed_backend: str = "ollama"
_embed_url: str = ""
_embed_api_key: str = ""
# Running counters surfaced by /api/health — embeddings failing silently means
# the vector half of hybrid search is dead, so make failures observable.
embed_stats: dict = {"ok": 0, "fail": 0, "last_error": None}


def _resolve_embed_url() -> str:
    url = (
        _embed_url
        or os.environ.get("EMBED_URL", "")
        or os.environ.get("OLLAMA_URL", "")
        or "http://localhost:11434"
    )
    return url.rstrip("/")


def _fit_dim(vec: list[float]) -> list[float]:
    """Fit a returned vector to the schema's embed_dim.

    Longer vectors are truncated + L2-renormalized (safe for MRL-trained models).
    Shorter vectors are a config error — store nothing rather than corrupt search.
    """
    if not vec or len(vec) == _embed_dim:
        return vec
    if len(vec) < _embed_dim:
        logger.warning(
            "Embedding has %d dims but schema needs %d — check embed_model", len(vec), _embed_dim
        )
        return []
    head = vec[: _embed_dim]
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


def get_embedding(text: str, model: Optional[str] = None) -> list[float]:
    """Synchronous embedding call. Returns [] on failure.

    Do NOT call from async code — use `await embed_text()` instead.
    """
    use_model = model or _embed_model
    base = _resolve_embed_url()
    try:
        if _embed_backend == "openai":
            resp = requests.post(
                f"{base}/v1/embeddings",
                json={"model": use_model, "input": text[:4000]},
                headers={"Authorization": f"Bearer {_embed_api_key}"},
                timeout=10,
            )
            resp.raise_for_status()
            vec = resp.json()["data"][0]["embedding"]
        else:
            resp = requests.post(
                f"{base}/api/embeddings",
                json={"model": use_model, "prompt": text[:4000]},
                timeout=10,
            )
            resp.raise_for_status()
            vec = resp.json().get("embedding", [])
        result = _fit_dim([float(x) for x in vec])
        if result:
            embed_stats["ok"] += 1
        else:
            embed_stats["fail"] += 1
            embed_stats["last_error"] = "backend returned empty embedding"
        return result
    except Exception as exc:
        embed_stats["fail"] += 1
        embed_stats["last_error"] = str(exc)
        logger.warning("Embedding failed (%s @ %s): %s", _embed_backend, base, exc)
        return []


async def embed_text(text: str, model: Optional[str] = None) -> list[float]:
    """Async embedding — safe to call from request handlers and agent loops."""
    return await asyncio.to_thread(get_embedding, text, model)


async def warn_on_embed_dim_mismatch() -> None:
    """Startup probe: warn loudly if stored embeddings don't match embed_dim.

    Catches bring-your-own-embedding-provider misconfigurations (e.g. the model
    was switched but old vectors were never re-embedded), which would silently
    mix incompatible vector spaces in hybrid search.

    Strictly best-effort: skips silently when the pool is down, the documents
    table is empty, or the probe query fails. Never raises.
    """
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            stored_dim = await conn.fetchval(
                "SELECT vector_dims(embedding) FROM documents "
                "WHERE embedding IS NOT NULL LIMIT 1"
            )
        if stored_dim is None:  # no embedded documents yet — nothing to compare
            return
        if int(stored_dim) != _embed_dim:
            msg = (
                f"EMBEDDING DIMENSION MISMATCH: stored document vectors have "
                f"{stored_dim} dims but embed_dim is configured as {_embed_dim}. "
                f"Old and new embeddings live in different vector spaces — "
                f"vector search results will be unreliable until you re-embed: "
                f"python scripts/backfill_embeddings.py --all"
            )
            logger.warning(msg)
            try:
                from rich.console import Console
                Console().print(f"[bold red]{msg}[/bold red]")
            except ImportError:
                pass
    except Exception as exc:
        logger.debug("embed-dim probe skipped: %s", exc)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def get_org_by_slug(slug: str) -> Optional[dict]:
    # PHASE20-DRY: every async function below repeats `if not _pool: return default`.
    # Could be consolidated into a @require_db_pool decorator or _with_pool() helper.
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orgs WHERE slug = $1", slug)
        return dict(row) if row else None


async def get_first_org() -> Optional[dict]:
    """Return the first org in the DB — single-tenant convenience until UI auth lands."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orgs ORDER BY id LIMIT 1")
        return dict(row) if row else None


async def create_org(name: str, slug: str) -> dict:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING *",
            name, slug,
        )
        return dict(row)


async def get_org_settings(org_id: int) -> dict:
    """Per-org settings JSONB (autonomy level, budgets, ...). {} when unset/DB down."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        raw = await conn.fetchval("SELECT settings FROM orgs WHERE id = $1", org_id)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return dict(raw or {})


async def update_org_settings(org_id: int, patch: dict) -> dict:
    """Shallow-merge patch into orgs.settings and return the merged settings."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        # The pool's jsonb codec encodes dicts itself — pass the dict, not a
        # pre-dumped string (that would store a JSON string literal).
        raw = await conn.fetchval(
            "UPDATE orgs SET settings = COALESCE(settings, '{}'::jsonb) || $2::jsonb "
            "WHERE id = $1 RETURNING settings",
            org_id, patch,
        )
    if isinstance(raw, str):
        raw = json.loads(raw)
    return dict(raw or {})


async def count_autonomous_runs_today(org_id: int) -> int:
    """Autonomous actions started today (UTC) — the daily budget counter.
    Skip/observe decisions (agent_type autonomy_review) are not counted."""
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_runs WHERE org_id = $1 "
            "AND trigger_type = 'autonomous' AND agent_type <> 'autonomy_review' "
            "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc')",
            org_id,
        )
    return int(n or 0)


async def list_autonomy_decisions(org_id: int, limit: int = 50) -> list[dict]:
    """Recent autonomy decisions (skips + actions) for the audit surface."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, agent_type, status, task, output, created_at, completed_at "
            "FROM agent_runs WHERE org_id = $1 AND trigger_type = 'autonomous' "
            "ORDER BY created_at DESC LIMIT $2",
            org_id, limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("output"), str):
            try:
                d["output"] = json.loads(d["output"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Outreach (Phase 3) — documents.type='outreach', state machine in metadata
# ---------------------------------------------------------------------------

def _doc_meta(d: dict) -> dict:
    m = d.get("metadata")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except json.JSONDecodeError:
            m = {}
    return dict(m or {})


async def list_outreach(org_id: int, state: Optional[str] = None,
                        sender_user_id: Optional[int] = None,
                        client_name: Optional[str] = None,
                        limit: int = 100) -> list[dict]:
    """Outreach documents newest first, optionally filtered by state / sender / client."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, doc_id, title, content, metadata, source, agent_run_id,
                      created_by, created_at, updated_at
               FROM documents
               WHERE org_id = $1 AND type = 'outreach'
                 AND ($2::text IS NULL OR metadata->>'state' = $2)
                 AND ($3::bigint IS NULL OR (metadata->>'sender_user_id')::bigint = $3)
                 AND ($4::text IS NULL OR metadata->>'client' = $4)
               ORDER BY created_at DESC LIMIT $5""",
            org_id, state, sender_user_id, client_name, limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["metadata"] = _doc_meta(d)
        out.append(d)
    return out


async def update_document_metadata(org_id: int, int_id: int, metadata: dict,
                                   content: Optional[str] = None,
                                   title: Optional[str] = None) -> bool:
    """Replace metadata (and optionally content/title) of one document by int id."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        res = await conn.execute(
            """UPDATE documents
                  SET metadata = $3,
                      content  = COALESCE($4, content),
                      title    = COALESCE($5, title),
                      updated_at = NOW()
                WHERE org_id = $1 AND id = $2""",
            org_id, int_id, _sanitize_for_pg(metadata), content, title,
        )
    return res.endswith("1")


async def claim_next_approved_outreach(org_id: Optional[int] = None,
                                       exclude_org_ids: Optional[set] = None) -> Optional[dict]:
    """Send-worker pickup: atomically move ONE approved outreach doc to 'queued'
    (worker actor) and return it. Row-locked so two worker ticks never send the
    same mail. Returns None when nothing is approved."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT id, org_id, doc_id, title, content, metadata, created_by
                     FROM documents
                    WHERE type = 'outreach' AND metadata->>'state' = 'approved'
                      AND ($1::bigint IS NULL OR org_id = $1)
                      AND NOT (org_id = ANY($2::bigint[]))
                    ORDER BY (metadata->>'approved_at') NULLS FIRST, id
                    LIMIT 1 FOR UPDATE SKIP LOCKED""",
                org_id, list(exclude_org_ids or []),
            )
            if not row:
                return None
            d = dict(row)
            d["metadata"] = _doc_meta(d)
            import outreach as _o
            meta = _o.transition(d["metadata"], _o.QUEUED, actor=_o.WORKER, note="worker pickup")
            await conn.execute("UPDATE documents SET metadata = $2, updated_at = NOW() WHERE id = $1",
                               d["id"], _sanitize_for_pg(meta))
            d["metadata"] = meta
            return d


async def count_outreach_sent_today(org_id: int) -> int:
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        n = await conn.fetchval(
            """SELECT COUNT(*) FROM documents
                WHERE org_id = $1 AND type = 'outreach'
                  AND (metadata->>'sent_at')::timestamptz >= date_trunc('day', now() AT TIME ZONE 'utc')""",
            org_id)
    return int(n or 0)


async def last_outreach_sent_to(org_id: int, to_email: str):
    """Newest sent_at for this recipient (per-contact frequency floor)."""
    if not _pool or not to_email:
        return None
    async with _pool.acquire() as conn:
        return await conn.fetchval(
            """SELECT MAX((metadata->>'sent_at')::timestamptz) FROM documents
                WHERE org_id = $1 AND type = 'outreach'
                  AND lower(metadata->>'to_email') = lower($2)
                  AND metadata ? 'sent_at'""",
            org_id, to_email)


async def find_outreach_by_message_id(message_id: str) -> Optional[dict]:
    """IMAP poller: locate the outreach doc a reply/bounce refers to."""
    if not _pool or not message_id:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, org_id, doc_id, title, metadata FROM documents
                WHERE type = 'outreach' AND metadata->>'message_id' = $1 LIMIT 1""",
            message_id)
    if not row:
        return None
    d = dict(row)
    d["metadata"] = _doc_meta(d)
    return d


# ---------------------------------------------------------------------------
# Deals (Phase 4 CRM) — real table, stage history in deal_events
# ---------------------------------------------------------------------------

_DEAL_COLS = ("id, org_id, client_id, name, stage, value, currency, probability, expected_close, "
              "owner_user_id, status, closed_at, metadata, created_by, created_at, updated_at")


def _deal_row(r) -> dict:
    d = dict(r)
    if d.get("value") is not None:
        d["value"] = float(d["value"])
    m = d.get("metadata")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except json.JSONDecodeError:
            m = {}
    d["metadata"] = m or {}
    return d


async def create_deal(org_id: int, client_id: int, name: str, *, stage: str = "lead",
                      value: Optional[float] = None, currency: str = "EUR",
                      probability: Optional[int] = None, expected_close=None,
                      owner_user_id: Optional[int] = None, status: str = "open",
                      metadata: Optional[dict] = None, created_by: Optional[int] = None,
                      actor_agent_run_id: Optional[int] = None) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""INSERT INTO deals (org_id, client_id, name, stage, value, currency, probability,
                                       expected_close, owner_user_id, status, closed_at, metadata, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                            CASE WHEN $10 <> 'open' THEN NOW() ELSE NULL END, $11, $12)
                    RETURNING {_DEAL_COLS}""",
                org_id, client_id, name, stage, value, currency, probability, expected_close,
                owner_user_id, status, metadata or {}, created_by,
            )
            await conn.execute(
                """INSERT INTO deal_events (org_id, deal_id, kind, from_value, to_value, note,
                                            actor_user_id, actor_agent_run_id)
                   VALUES ($1,$2,'created',NULL,$3,$4,$5,$6)""",
                org_id, row["id"], stage, f"created in {stage}", created_by, actor_agent_run_id,
            )
    return _deal_row(row)


async def get_deal(org_id: int, deal_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT {'d.' + _DEAL_COLS.replace(', ', ', d.')}, c.name AS client_name,
                       u.display_name AS owner_name
                FROM deals d JOIN clients c ON c.id = d.client_id
                LEFT JOIN users u ON u.id = d.owner_user_id
                WHERE d.org_id = $1 AND d.id = $2""",
            org_id, deal_id)
    return _deal_row(row) if row else None


async def list_deals(org_id: int, *, status: Optional[str] = None, stage: Optional[str] = None,
                     client_id: Optional[int] = None, owner_user_id: Optional[int] = None,
                     limit: int = 500) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT d.{_DEAL_COLS.replace(', ', ', d.')}, c.name AS client_name,
                       u.display_name AS owner_name
                  FROM deals d
                  JOIN clients c ON c.id = d.client_id
             LEFT JOIN users u ON u.id = d.owner_user_id
                 WHERE d.org_id = $1
                   AND ($2::text IS NULL OR d.status = $2)
                   AND ($3::text IS NULL OR d.stage = $3)
                   AND ($4::bigint IS NULL OR d.client_id = $4)
                   AND ($5::bigint IS NULL OR d.owner_user_id = $5)
                 ORDER BY d.updated_at DESC LIMIT $6""",
            org_id, status, stage, client_id, owner_user_id, limit,
        )
    return [_deal_row(r) for r in rows]


async def update_deal(org_id: int, deal_id: int, patch: dict, *, actor_user_id: Optional[int] = None,
                      actor_agent_run_id: Optional[int] = None, note: str = "") -> Optional[dict]:
    """Patch scalar fields; stage/status/value changes write deal_events rows."""
    if not _pool:
        return None
    allowed = {"name", "stage", "value", "currency", "probability", "expected_close",
               "owner_user_id", "status", "metadata"}
    fields = {k: v for k, v in patch.items() if k in allowed}
    if not fields:
        return await get_deal(org_id, deal_id)
    async with _pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                f"SELECT {_DEAL_COLS} FROM deals WHERE org_id = $1 AND id = $2 FOR UPDATE",
                org_id, deal_id)
            if not before:
                return None
            sets, args = [], [org_id, deal_id]
            for k, v in fields.items():
                args.append(v)
                sets.append(f"{k} = ${len(args)}")
            if "status" in fields:
                sets.append("closed_at = CASE WHEN $" + str(args.index(fields["status"]) + 1) +
                            " <> 'open' THEN COALESCE(closed_at, NOW()) ELSE NULL END")
            sets.append("updated_at = NOW()")
            row = await conn.fetchrow(
                f"UPDATE deals SET {', '.join(sets)} WHERE org_id = $1 AND id = $2 RETURNING {_DEAL_COLS}",
                *args)
            events = []
            for k, kind in (("stage", "stage"), ("status", "status"), ("value", "value")):
                if k in fields and str(before[k]) != str(fields[k]):
                    events.append((kind, str(before[k]) if before[k] is not None else None,
                                   str(fields[k]) if fields[k] is not None else None))
            if note and not events:
                events.append(("note", None, None))
            for kind, frm, to in events:
                await conn.execute(
                    """INSERT INTO deal_events (org_id, deal_id, kind, from_value, to_value, note,
                                                actor_user_id, actor_agent_run_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    org_id, deal_id, kind, frm, to, note or None, actor_user_id, actor_agent_run_id)
    return _deal_row(row)


async def delete_deal(org_id: int, deal_id: int) -> bool:
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        res = await conn.execute("DELETE FROM deals WHERE org_id = $1 AND id = $2", org_id, deal_id)
    return res.endswith("1")


async def list_deal_events(org_id: int, deal_id: int, limit: int = 100) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT e.id, e.kind, e.from_value, e.to_value, e.note, e.actor_user_id,
                      e.actor_agent_run_id, e.created_at,
                      COALESCE(u.display_name,
                               CASE WHEN e.actor_user_id IS NULL THEN 'agent' END) AS actor_name
                 FROM deal_events e LEFT JOIN users u ON u.id = e.actor_user_id
                WHERE e.org_id = $1 AND e.deal_id = $2
                ORDER BY e.created_at DESC LIMIT $3""",
            org_id, deal_id, limit)
    return [dict(r) for r in rows]


async def pipeline_summary(org_id: int, owner_user_id: Optional[int] = None) -> list[dict]:
    """Per-stage counts + value totals for open deals (board header)."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT stage, COUNT(*) AS n, COALESCE(SUM(value),0) AS total,
                      COALESCE(SUM(CASE WHEN probability IS NOT NULL THEN value * probability / 100.0 END), 0) AS weighted_explicit,
                      COALESCE(SUM(CASE WHEN probability IS NULL THEN value END), 0) AS total_default_prob
                 FROM deals
                WHERE org_id = $1 AND status = 'open'
                  AND ($2::bigint IS NULL OR owner_user_id = $2)
                GROUP BY stage""",
            org_id, owner_user_id)
    # Deals without an explicit probability follow their stage's default — the
    # caller (router) knows the stage table and finishes the weighting.
    return [{"stage": r["stage"], "count": int(r["n"]), "total": float(r["total"]),
             "weighted_explicit": float(r["weighted_explicit"]),
             "total_default_prob": float(r["total_default_prob"])} for r in rows]


async def clients_with_legacy_deal_fields(org_id: int) -> list[dict]:
    """Clients whose metadata still carries free-text deal_stage / deal_value
    and that have no deals row yet — input for the one-time importer."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.name, c.created_by, c.metadata->>'deal_stage' AS deal_stage,
                      c.metadata->>'deal_value' AS deal_value
                 FROM clients c
                WHERE c.org_id = $1
                  AND (COALESCE(c.metadata->>'deal_stage','') <> '' OR COALESCE(c.metadata->>'deal_value','') <> '')
                  AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.client_id = c.id)""",
            org_id)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Client activity timeline (Phase 4) — read model, no table
# ---------------------------------------------------------------------------

async def client_timeline(org_id: int, client_id: int, limit: int = 100) -> list[dict]:
    """Chronological union of everything that happened around one client:
    documents (meetings, findings, signals, outreach…), contact_log,
    user_tasks, agent_runs (via documents.agent_run_id / task subject) and
    deal_events. Normalized to (ts, kind, actor, title, ref)."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        name = await conn.fetchval("SELECT name FROM clients WHERE org_id = $1 AND id = $2", org_id, client_id)
        if not name:
            return []
        rows = await conn.fetch(
            r"""
            (SELECT d.created_at AS ts, 'document:' || d.type AS kind,
                    COALESCE(d.source, 'human') AS actor, d.title AS title,
                    jsonb_build_object('doc_id', d.id, 'doc_type', d.type,
                                       'state', d.metadata->>'state',
                                       'signal_type', d.metadata->>'signal_type') AS ref
               FROM documents d
               JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
              WHERE d.org_id = $1 AND dl.entity_id = $2)
            UNION ALL
            (SELECT cl.sent_at AS ts, 'contact' AS kind,
                    COALESCE(u.display_name, 'rep') AS actor,
                    'Mail to ' || COALESCE(NULLIF(cl.contact_name,''), cl.contact_email) ||
                       CASE WHEN cl.subject <> '' THEN ': ' || cl.subject ELSE '' END AS title,
                    jsonb_build_object('contact_log_id', cl.id, 'replied', cl.replied,
                                       'follow_up', cl.follow_up, 'doc_id', cl.source_doc_id) AS ref
               FROM contact_log cl LEFT JOIN users u ON u.id = cl.user_id
              WHERE cl.org_id = $1 AND cl.client_name = $3)
            UNION ALL
            (SELECT COALESCE(t.completed_at, t.created_at) AS ts,
                    CASE WHEN t.status = 'done' THEN 'task_done' ELSE 'task' END AS kind,
                    COALESCE(u.display_name, 'rep') AS actor, t.title AS title,
                    jsonb_build_object('task_id', t.id, 'due_date', t.due_date, 'status', t.status,
                                       'deal_id', t.deal_id) AS ref
               FROM user_tasks t LEFT JOIN users u ON u.id = t.user_id
              WHERE t.org_id = $1 AND t.client_name = $3)
            UNION ALL
            (SELECT e.created_at AS ts, 'deal:' || e.kind AS kind,
                    COALESCE(u.display_name, CASE WHEN e.actor_agent_run_id IS NOT NULL THEN 'agent' ELSE 'system' END) AS actor,
                    d.name || CASE WHEN e.kind IN ('stage','status')
                                   THEN ': ' || COALESCE(e.from_value,'·') || ' → ' || COALESCE(e.to_value,'·')
                                   WHEN e.kind = 'value' THEN ': value ' || COALESCE(e.from_value,'·') || ' → ' || COALESCE(e.to_value,'·')
                                   ELSE COALESCE(': ' || e.note, '') END AS title,
                    jsonb_build_object('deal_id', d.id, 'event_id', e.id,
                                       'agent_run_id', e.actor_agent_run_id) AS ref
               FROM deal_events e JOIN deals d ON d.id = e.deal_id
               LEFT JOIN users u ON u.id = e.actor_user_id
              WHERE e.org_id = $1 AND d.client_id = $2)
            UNION ALL
            (SELECT r.created_at AS ts, 'agent_run:' || r.agent_type AS kind,
                    r.trigger_type AS actor,
                    left(regexp_replace(r.task, '\s+', ' ', 'g'), 120) AS title,
                    jsonb_build_object('run_id', r.id, 'status', r.status) AS ref
               FROM agent_runs r
              WHERE r.org_id = $1
                AND (r.task ILIKE 'Subject: ' || $3 || '%' OR r.task ILIKE 'Research: ' || $3 || '%'
                     OR r.task ILIKE '%' || $3 || '%' AND r.agent_type IN ('research','osint','orchestrate','pain_point_research','match_synthesis'))
                AND r.agent_type <> 'autonomy_review'
                AND r.trigger_type <> 'external_service')   -- Pi-side mirror rows of the same runs
            ORDER BY ts DESC NULLS LAST
            LIMIT $4
            """,
            org_id, client_id, name, limit)
    out = []
    for r in rows:
        d = dict(r)
        ref = d.get("ref")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref)
            except json.JSONDecodeError:
                ref = {}
        d["ref"] = ref or {}
        out.append(d)
    return out


async def create_user(
    org_id: int,
    username: str,
    display_name: str,
    password_hash: str,
    email: Optional[str] = None,
    role: str = "member",
) -> dict:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (org_id, username, display_name, email, password_hash, role)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
            """,
            org_id, username, display_name, email, password_hash, role,
        )
        return dict(row)


async def get_user_by_username(org_id: int, username: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE org_id = $1 AND username = $2",
            org_id, username,
        )
        return dict(row) if row else None


async def list_users(org_id: int) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, username, display_name, email, role, created_at FROM users WHERE org_id = $1 ORDER BY created_at",
            org_id,
        )
        return [dict(r) for r in rows]


async def get_user_settings(org_id: int, user_id: int) -> dict:
    """Per-user settings JSONB (outreach identity: display_name/reply_to/signature)."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT settings FROM users WHERE id = $1 AND org_id = $2", user_id, org_id)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return dict(raw or {})


async def update_user_settings(org_id: int, user_id: int, patch: dict) -> dict:
    """Shallow-merge patch into users.settings; returns merged settings."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        raw = await conn.fetchval(
            "UPDATE users SET settings = COALESCE(settings, '{}'::jsonb) || $3::jsonb "
            "WHERE id = $1 AND org_id = $2 RETURNING settings",
            user_id, org_id, patch,   # jsonb codec encodes the dict
        )
    if isinstance(raw, str):
        raw = json.loads(raw)
    return dict(raw or {})


async def get_user_identity(org_id: int, user_id: int) -> dict:
    """What outreach sends as: {display_name, reply_to, signature, email}."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT display_name, email, settings FROM users WHERE id = $1 AND org_id = $2",
            user_id, org_id)
    if not row:
        return {}
    s = row["settings"]
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except json.JSONDecodeError:
            s = {}
    s = s or {}
    return {
        "display_name": s.get("outreach_display_name") or row["display_name"],
        "reply_to": s.get("outreach_reply_to") or row["email"] or "",
        "signature": s.get("outreach_signature") or "",
        "email": row["email"] or "",
    }


async def create_session_token(user_id: int, token: str, expires_at) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_sessions (user_id, token, expires_at) VALUES ($1, $2, $3)",
            user_id, token, expires_at,
        )


async def get_user_by_token(token: str) -> Optional[dict]:
    """Returns user + org fields if the token is valid and not expired."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.org_id, u.username, u.display_name, u.email, u.role,
                   COALESCE(u.ui_variant, 'classic') AS ui_variant,
                   o.name AS org_name, o.slug AS org_slug
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            JOIN orgs  o ON o.id = u.org_id
            WHERE s.token = $1 AND s.expires_at > NOW()
            """,
            token,
        )
        return dict(row) if row else None


async def delete_session_token(token: str) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM user_sessions WHERE token = $1", token)


# ---------------------------------------------------------------------------
# Invitations (Phase 18)
# ---------------------------------------------------------------------------

async def create_invitation(
    org_id: int,
    invite_key: str,
    created_by: int,
    role: str = "member",
    email: Optional[str] = None,
    expires_at=None,
) -> dict:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO invitations (org_id, invite_key, email, role, created_by, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
            """,
            org_id, invite_key, email, role, created_by, expires_at,
        )
        return dict(row)


async def get_invitation_by_key(invite_key: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT i.*, o.slug AS org_slug, o.name AS org_name
            FROM invitations i
            JOIN orgs o ON o.id = i.org_id
            WHERE i.invite_key = $1
            """,
            invite_key,
        )
        return dict(row) if row else None


async def consume_invitation(invite_key: str, used_by_user_id: int) -> bool:
    """Mark invite as used. Returns True if the key was still valid (exactly 1 row updated)."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE invitations SET used_at = NOW(), used_by = $2
            WHERE invite_key = $1 AND used_at IS NULL AND expires_at > NOW()
            """,
            invite_key, used_by_user_id,
        )
        return result == "UPDATE 1"


async def list_invitations(org_id: int) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.id, i.invite_key, i.email, i.role,
                   i.created_at, i.expires_at, i.used_at,
                   cb.username AS created_by_username,
                   ub.username AS used_by_username
            FROM invitations i
            JOIN users cb ON cb.id = i.created_by
            LEFT JOIN users ub ON ub.id = i.used_by
            WHERE i.org_id = $1
            ORDER BY i.created_at DESC
            """,
            org_id,
        )
        return [dict(r) for r in rows]


async def delete_invitation(invitation_id: int, org_id: int) -> bool:
    """Delete an unused invitation. Returns True if deleted, False if used/not found."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM invitations WHERE id = $1 AND org_id = $2 AND used_at IS NULL RETURNING id",
            invitation_id, org_id,
        )
        return row is not None


async def update_user_role(user_id: int, org_id: int, role: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET role = $3 WHERE id = $1 AND org_id = $2 RETURNING id, username, display_name, email, role, created_at",
            user_id, org_id, role,
        )
        return dict(row) if row else None


async def update_user_email(user_id: int, org_id: int, email: Optional[str]) -> Optional[dict]:
    """Admin sets/clears a user's email (used so per-rep digests can be sent)."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET email = $3 WHERE id = $1 AND org_id = $2 RETURNING id, username, display_name, email, role, created_at",
            user_id, org_id, (email or "").strip() or None,
        )
        return dict(row) if row else None


async def set_user_ui_variant(user_id: int, variant: str) -> Optional[str]:
    """Persist a user's opt-in front-end theme ('classic' | 'carbon')."""
    if not _pool:
        return None
    variant = variant if variant in ("classic", "carbon") else "classic"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET ui_variant = $2 WHERE id = $1 RETURNING ui_variant",
            user_id, variant,
        )
        return row["ui_variant"] if row else None


async def delete_user(user_id: int, org_id: int) -> bool:
    """Delete a user. ON DELETE CASCADE removes their sessions automatically."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM users WHERE id = $1 AND org_id = $2 RETURNING id",
            user_id, org_id,
        )
        return row is not None


# ---------------------------------------------------------------------------
# Registration keys (system-level, operator-issued)
# ---------------------------------------------------------------------------

async def create_registration_key(reg_key: str, label: Optional[str] = None, expires_at=None) -> dict:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO registration_keys (reg_key, label, expires_at) VALUES ($1, $2, $3) RETURNING *",
            reg_key, label, expires_at,
        )
        return dict(row)


async def get_registration_key(reg_key: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM registration_keys WHERE reg_key = $1",
            reg_key,
        )
        return dict(row) if row else None


async def consume_registration_key(reg_key: str, org_id: int) -> bool:
    """Mark registration key as used. Returns True if 1 row updated (still valid)."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE registration_keys SET used_at = NOW(), used_by_org = $2
            WHERE reg_key = $1 AND used_at IS NULL
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            reg_key, org_id,
        )
        return result == "UPDATE 1"


async def list_registration_keys() -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rk.*, o.name AS org_name, o.slug AS org_slug
            FROM registration_keys rk
            LEFT JOIN orgs o ON o.id = rk.used_by_org
            ORDER BY rk.created_at DESC
            """,
        )
        return [dict(r) for r in rows]


async def list_orgs() -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.name, o.slug, o.created_at,
                   COUNT(u.id) AS user_count
            FROM orgs o
            LEFT JOIN users u ON u.org_id = o.id
            GROUP BY o.id
            ORDER BY o.created_at
            """,
        )
        return [dict(r) for r in rows]


async def delete_registration_key(key_id: int) -> bool:
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM registration_keys WHERE id = $1 AND used_at IS NULL RETURNING id",
            key_id,
        )
        return row is not None


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

# PHASE20-DRY: upsert_client and upsert_contact share ~120 lines of identical structure.
# Could be unified: async def upsert_entity_row(table, org_id, name, metadata, embedding, ...).
async def upsert_client(
    org_id: int,
    name: str,
    metadata: dict,
    embedding: list[float],
    date_str: Optional[str] = None,
    created_by: Optional[int] = None,
) -> int:
    """Upsert a client. Returns the client id, or -1 if DB is offline."""
    if not _pool:
        return -1
    vec = embedding if embedding else None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO clients (org_id, name, metadata, embedding, session_count, last_activity, created_by)
            VALUES ($1, $2, $3, $4, 1, $5, $6)
            ON CONFLICT (org_id, name) DO UPDATE SET
                session_count = clients.session_count + 1,
                last_activity = EXCLUDED.last_activity,
                metadata      = clients.metadata || EXCLUDED.metadata,
                embedding     = CASE
                                    WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding
                                    ELSE clients.embedding
                                END
            RETURNING id
            """,
            org_id, name, metadata, vec, date_str, created_by,
        )
        return row["id"]


# PHASE20-DRY: 15+ list_* functions below follow identical structure: pool check → acquire → fetch → [dict(r) for r in rows].
# Could be extracted into a generic list_from_table(table, org_id, query, *params) helper.
async def list_clients(org_id: int, sort: str = "name") -> list[dict]:
    if not _pool:
        return []
    # Default alphabetical; "recent" = most recently active first.
    order = ("c.last_activity DESC NULLS LAST, c.session_count DESC"
             if sort == "recent" else "lower(c.name) ASC")
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.id, c.name, c.metadata, c.session_count, c.last_activity,
                   c.created_by,
                   u.username     AS owner_username,
                   u.display_name AS owner_display_name
            FROM clients c
            LEFT JOIN users u ON u.id = c.created_by
            WHERE c.org_id = $1
            ORDER BY {order}
            """,
            org_id,
        )
        return [dict(r) for r in rows]


async def set_client_created_by(org_id: int, name: str, user_id: Optional[int]) -> Optional[dict]:
    """Reassign a client's primary owner (created_by). Returns the updated row."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE clients SET created_by = $3
            WHERE org_id = $1 AND lower(name) = lower($2)
            RETURNING id, org_id, name, metadata, created_by
            """,
            org_id, name, user_id,
        )
        return dict(row) if row else None


async def list_focus_clients(org_id: int) -> list[dict]:
    """Union of every rep's focus clients (metadata.is_focus, the derived flag).
    The research/OSINT heartbeats run over this union so all focus clients —
    regardless of which seller starred them — get monitored regularly."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, name, metadata, session_count, last_activity
               FROM clients
               WHERE org_id = $1 AND (metadata->>'is_focus')::boolean = true
               ORDER BY name""",
            org_id,
        )
        return [dict(r) for r in rows]


async def set_client_focus(
    org_id: int, name: str, user_id: int, focus: bool
) -> Optional[dict]:
    """Mark/unmark a client as *this user's* focus. Focus is per-rep: the
    user id is added to / removed from metadata.focus_user_ids. metadata.is_focus
    is kept as the derived union (true iff at least one rep focuses the client),
    so existing union readers (heartbeat, list_focus_clients) keep working and
    every focus client stays monitored. Done in one connection for atomicity."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT metadata FROM clients
                   WHERE org_id = $1 AND lower(name) = lower($2)
                   FOR UPDATE""",
                org_id, name,
            )
            if not row:
                return None
            meta = row["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta or "{}")
            meta = dict(meta or {})
            ids = meta.get("focus_user_ids")
            ids = [int(x) for x in ids] if isinstance(ids, list) else []
            if focus and user_id not in ids:
                ids.append(user_id)
            elif not focus:
                ids = [x for x in ids if x != user_id]
            patch = {"focus_user_ids": ids, "is_focus": bool(ids)}
            updated = await conn.fetchrow(
                """UPDATE clients SET metadata = metadata || $3
                   WHERE org_id = $1 AND lower(name) = lower($2)
                   RETURNING id, org_id, name, metadata, session_count,
                             last_activity, created_by, created_at""",
                org_id, name, patch,
            )
            return dict(updated) if updated else None


async def get_client_last_doc_dates(org_id: int) -> dict:
    """Latest document created_at per client id — one query, used by the
    heartbeat staleness gate to avoid N+1 lookups."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT dl.entity_id AS client_id, MAX(d.created_at) AS last_doc
               FROM document_links dl
               JOIN documents d ON d.id = dl.document_id
               WHERE d.org_id = $1 AND dl.entity_type = 'client'
               GROUP BY dl.entity_id""",
            org_id,
        )
        return {r["client_id"]: r["last_doc"] for r in rows}


async def get_client(org_id: int, name: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, org_id, name, metadata, session_count,
                      last_activity, created_by, created_at
               FROM clients
               WHERE org_id = $1
                 AND (lower(name) = lower($2) OR similarity(name, $2) > 0.6)
               ORDER BY similarity(name, $2) DESC
               LIMIT 1""",
            org_id, name,
        )
        return dict(row) if row else None


async def delete_client(org_id: int, name: str) -> bool:
    """Delete a client and all linked documents/entity_links. Returns True if deleted."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM clients WHERE org_id = $1 AND lower(name) = lower($2) RETURNING id",
            org_id, name,
        )
        return row is not None


async def cancel_all_running_agent_runs(org_id: int) -> int:
    """Cancel all pending/running agent_runs for an org. Returns count updated."""
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE agent_runs SET status = 'cancelled', completed_at = NOW()
               WHERE org_id = $1 AND status IN ('pending', 'running')""",
            org_id,
        )
        parts = result.split()
        return int(parts[1]) if len(parts) > 1 else 0


async def get_client_by_id(org_id: int, client_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, metadata FROM clients WHERE org_id = $1 AND id = $2",
            org_id, client_id,
        )
        return dict(row) if row else None


async def count_client_document_links(org_id: int) -> dict[int, int]:
    """Return {client_id: linked_document_count} for every client in the org.

    document_links has no FK to clients, so this is a best-effort join filtered
    by entity_type='client'. Used to pick the canonical record when merging.
    """
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT dl.entity_id AS client_id, COUNT(*) AS n
            FROM document_links dl
            JOIN clients c ON c.id = dl.entity_id
            WHERE dl.entity_type = 'client' AND c.org_id = $1
            GROUP BY dl.entity_id
            """,
            org_id,
        )
        return {int(r["client_id"]): int(r["n"]) for r in rows}


def _merge_rowcount(tag: object) -> int:
    """Parse an asyncpg execute() status tag ('UPDATE 3' / 'DELETE 0') to int."""
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def merge_clients(org_id: int, dupe_id: int, canonical_id: int) -> Optional[dict]:
    """Merge duplicate client `dupe_id` into `canonical_id`, in one transaction.

    Steps (all scoped to org_id, all inside a single transaction):
      1. Re-point document_links from dupe -> canonical, collision-guarded so we
         never violate the (document_id, entity_type='client', entity_id)
         composite PK. Links that would collide are deleted instead of moved.
      2. Re-point contacts.client_id from dupe -> canonical (the only real FK).
      3. Union metadata.owner_ids and metadata.focus_user_ids from dupe into
         canonical, and recompute is_focus = (focus_user_ids non-empty).
      4. Delete the dupe client row.

    Returns {org_id, canonical_id, dupe_id, dupe_name, canonical_name,
    links_moved, links_dropped, contacts_moved} on success, or None if either
    row is missing / the DB is disabled. Destructive of the dupe row — callers
    must not re-run for the same pair.
    """
    if not _pool:
        return None
    if dupe_id == canonical_id:
        return None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            # Lock both rows in a consistent order to avoid deadlocks; confirm org.
            rows = await conn.fetch(
                """SELECT id, org_id, name, metadata FROM clients
                   WHERE org_id = $1 AND id = ANY($2::bigint[])
                   FOR UPDATE""",
                org_id, sorted([dupe_id, canonical_id]),
            )
            by_id = {int(r["id"]): r for r in rows}
            if dupe_id not in by_id or canonical_id not in by_id:
                return None

            # 1. document_links: drop links that would collide, move the rest.
            dropped = await conn.execute(
                """DELETE FROM document_links dl
                   WHERE dl.entity_type = 'client' AND dl.entity_id = $1
                     AND EXISTS (
                         SELECT 1 FROM document_links x
                         WHERE x.document_id = dl.document_id
                           AND x.entity_type = 'client'
                           AND x.entity_id = $2
                     )""",
                dupe_id, canonical_id,
            )
            moved = await conn.execute(
                """UPDATE document_links
                   SET entity_id = $2
                   WHERE entity_type = 'client' AND entity_id = $1""",
                dupe_id, canonical_id,
            )

            # 2. contacts.client_id (the only FK to clients).
            contacts_moved = await conn.execute(
                """UPDATE contacts SET client_id = $2
                   WHERE org_id = $1 AND client_id = $3""",
                org_id, canonical_id, dupe_id,
            )

            # 3. Union owner_ids / focus_user_ids into canonical metadata.
            def _meta(row) -> dict:
                m = row["metadata"]
                if isinstance(m, str):
                    m = json.loads(m or "{}")
                return dict(m or {})

            def _int_list(v) -> list[int]:
                return [int(x) for x in v] if isinstance(v, list) else []

            dupe_meta = _meta(by_id[dupe_id])
            can_meta = _meta(by_id[canonical_id])
            owner_ids = sorted(set(
                _int_list(can_meta.get("owner_ids")) + _int_list(dupe_meta.get("owner_ids"))
            ))
            focus_ids = sorted(set(
                _int_list(can_meta.get("focus_user_ids")) + _int_list(dupe_meta.get("focus_user_ids"))
            ))
            patch = {
                "owner_ids": owner_ids,
                "focus_user_ids": focus_ids,
                "is_focus": bool(focus_ids),
            }
            await conn.execute(
                "UPDATE clients SET metadata = metadata || $2 WHERE id = $1",
                canonical_id, patch,
            )

            # 4. Delete the dupe.
            await conn.execute("DELETE FROM clients WHERE id = $1", dupe_id)

            return {
                "org_id": org_id,
                "canonical_id": canonical_id,
                "dupe_id": dupe_id,
                "dupe_name": by_id[dupe_id]["name"],
                "canonical_name": by_id[canonical_id]["name"],
                "links_moved": _merge_rowcount(moved),
                "links_dropped": _merge_rowcount(dropped),
                "contacts_moved": _merge_rowcount(contacts_moved),
            }


# ---------------------------------------------------------------------------
# Entity deduplication helpers
# ---------------------------------------------------------------------------

# PHASE20-DRY: find_similar_client and find_similar_contact are near-identical.
# Could be unified: async def find_similar_entity(table, org_id, name, threshold).
async def find_similar_client(org_id: int, name: str, threshold: float = 0.7) -> Optional[str]:
    """Return the canonical client name if a similar one exists, else None.

    Uses pg_trgm similarity — catches typos and minor spelling variations.
    Does NOT merge genuinely distinct entities (e.g. 'IBM' vs 'IBM Germany').
    """
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT name FROM clients
            WHERE org_id = $1 AND similarity(name, $2) >= $3
            ORDER BY similarity(name, $2) DESC
            LIMIT 1
            """,
            org_id, name, threshold,
        )
        return row["name"] if row else None


async def find_similar_contact(org_id: int, name: str, threshold: float = 0.7) -> Optional[str]:
    """Return the canonical contact name if a similar one exists, else None."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT name FROM contacts
            WHERE org_id = $1 AND similarity(name, $2) >= $3
            ORDER BY similarity(name, $2) DESC
            LIMIT 1
            """,
            org_id, name, threshold,
        )
        return row["name"] if row else None


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

async def upsert_contact(
    org_id: int,
    name: str,
    metadata: dict,
    embedding: list[float],
    client_id: Optional[int] = None,
    date_str: Optional[str] = None,
    created_by: Optional[int] = None,
) -> int:
    """Upsert a contact. Returns the contact id, or -1 if DB is offline."""
    if not _pool:
        return -1
    vec = embedding if embedding else None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO contacts (org_id, name, metadata, embedding, client_id, session_count, last_activity, created_by)
            VALUES ($1, $2, $3, $4, $5, 1, $6, $7)
            ON CONFLICT (org_id, name) DO UPDATE SET
                session_count = contacts.session_count + 1,
                last_activity = EXCLUDED.last_activity,
                client_id     = COALESCE(EXCLUDED.client_id, contacts.client_id),
                metadata      = contacts.metadata || EXCLUDED.metadata,
                embedding     = CASE
                                    WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding
                                    ELSE contacts.embedding
                                END
            RETURNING id
            """,
            org_id, name, metadata, vec, client_id, date_str, created_by,
        )
        return row["id"]


async def list_contacts(org_id: int, client_id: Optional[int] = None) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        if client_id:
            rows = await conn.fetch(
                """
                SELECT id, name, metadata, client_id, session_count, last_activity
                FROM contacts WHERE org_id = $1 AND client_id = $2
                ORDER BY session_count DESC
                """,
                org_id, client_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, name, metadata, client_id, session_count, last_activity
                FROM contacts WHERE org_id = $1
                ORDER BY session_count DESC, last_activity DESC NULLS LAST
                """,
                org_id,
            )
        return [dict(r) for r in rows]


async def get_contact(org_id: int, name: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, name, metadata, client_id, session_count, last_activity, created_at
               FROM contacts WHERE org_id = $1 AND lower(name) = lower($2)""",
            org_id, name,
        )
        if not row:
            return None
        d = dict(row)
        if d.get("last_activity"):
            d["last_activity"] = str(d["last_activity"])
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        return d


async def search_clients(org_id: int, partial_name: str, limit: int = 5) -> list[dict]:
    """Fuzzy client lookup by partial name (ILIKE). Used by chat tool calling."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, metadata, session_count, last_activity
            FROM clients
            WHERE org_id = $1 AND name ILIKE $2
            ORDER BY session_count DESC
            LIMIT $3
            """,
            org_id, f"%{partial_name}%", limit,
        )
        return [dict(r) for r in rows]


async def search_contacts(org_id: int, partial_name: str, limit: int = 5) -> list[dict]:
    """Fuzzy contact lookup by partial name (ILIKE). Used by chat tool calling."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, metadata, client_id, session_count, last_activity
            FROM contacts
            WHERE org_id = $1 AND name ILIKE $2
            ORDER BY session_count DESC
            LIMIT $3
            """,
            org_id, f"%{partial_name}%", limit,
        )
        return [dict(r) for r in rows]


async def get_client_findings(org_id: int, client_name: str, n: int = 5) -> list[dict]:
    """Recent research findings linked to a client, sorted by relevance then date."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        client = await conn.fetchrow(
            "SELECT id FROM clients WHERE org_id = $1 AND lower(name) = lower($2)",
            org_id, client_name,
        )
        if not client:
            return []
        rows = await conn.fetch(
            """
            SELECT d.id, d.doc_id, d.title, d.content, d.metadata, d.created_at
            FROM documents d
            JOIN document_links dl ON dl.document_id = d.id
            WHERE d.org_id = $1
              AND d.type = 'finding'
              AND dl.entity_type = 'client'
              AND dl.entity_id = $2
            ORDER BY (d.metadata->>'relevance_score')::int DESC NULLS LAST,
                     d.created_at DESC
            LIMIT $3
            """,
            org_id, client["id"], n,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

async def create_chat_session(
    org_id: int,
    user_id: int,
    title: str = "New conversation",
    client_name: Optional[str] = None,
) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (org_id, user_id, title, client_name)
            VALUES ($1, $2, $3, $4)
            RETURNING id, org_id, user_id, title, messages, client_name, created_at, updated_at
            """,
            org_id, user_id, title, client_name,
        )
        return dict(row) if row else None


async def get_chat_session(session_id: int, org_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_sessions WHERE id = $1 AND org_id = $2",
            session_id, org_id,
        )
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("messages"), str):
            import json as _json
            d["messages"] = _json.loads(d["messages"])
        return d


async def list_chat_sessions(org_id: int, user_id: Optional[int] = None) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, user_id, title, client_name,
                   CASE WHEN jsonb_typeof(messages) = 'array'
                        THEN jsonb_array_length(messages) ELSE 0 END AS message_count,
                   created_at, updated_at
            FROM chat_sessions
            WHERE org_id = $1
              AND ($2::bigint IS NULL OR user_id = $2 OR user_id IS NULL)
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            org_id, user_id,
        )
        return [dict(r) for r in rows]


async def append_chat_turn(
    session_id: int,
    org_id: int,
    user_content: str,
    ai_content: str,
    sources: list,
) -> Optional[dict]:
    """Append a user+AI turn to the session's messages JSONB array."""
    if not _pool:
        return None
    import json as _json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    async with _pool.acquire() as conn:
        # Read current messages, append in Python, write back whole array.
        # Avoids JSONB || type-coercion issues with asyncpg string parameters.
        row = await conn.fetchrow(
            "SELECT messages FROM chat_sessions WHERE id = $1 AND org_id = $2",
            session_id, org_id,
        )
        if not row:
            return None
        current = row["messages"] or []
        if isinstance(current, str):
            current = _json.loads(current)

        current.append({"role": "user", "content": user_content, "created_at": now})
        current.append({"role": "ai",   "content": ai_content,   "sources": sources, "created_at": now})

        # Pass Python list directly — the jsonb codec on the connection encodes it once.
        # Do NOT pass json.dumps(current): the codec would double-encode it into a string scalar.
        row = await conn.fetchrow(
            """
            UPDATE chat_sessions
            SET messages   = $3,
                updated_at = NOW()
            WHERE id = $1 AND org_id = $2
            RETURNING id, title, messages, client_name, updated_at
            """,
            session_id, org_id, current,
        )
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("messages"), str):
            d["messages"] = _json.loads(d["messages"])
        return d


async def update_chat_session_title(session_id: int, org_id: int, title: str) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE chat_sessions SET title = $3, updated_at = NOW() WHERE id = $1 AND org_id = $2",
            session_id, org_id, title,
        )


async def delete_chat_session(session_id: int, org_id: int) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_sessions WHERE id = $1 AND org_id = $2",
            session_id, org_id,
        )


async def list_signals(
    org_id: int,
    client_name: Optional[str] = None,
    signal_type: Optional[str] = None,
    days: int = 30,
    limit: int = 20,
    offset: int = 0,
    min_relevance: Optional[int] = None,
    subjects: Optional[list[str]] = None,
    scope: Optional[str] = None,
) -> list[dict]:
    """Return recent type=signal documents for the org.

    `subjects` restricts to signals about a specific set of client names (used
    to scope the home news panel to a rep's own clients). `scope` filters by
    metadata.scope: 'market' = industry-wide signals only, 'client' = exclude
    market signals, None = all."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        # Pi-written signals carry no metadata.subject — they're tied to a client
        # only via document_links. LEFT JOIN the linked client so the client
        # filters, the "mine" scoping, and the frontend client tag all work; the
        # name is folded into metadata.subject below when missing.
        rows = await conn.fetch(
            """
            SELECT d.id, d.doc_id, d.title, d.content, d.metadata, d.created_at,
                   lc.name AS linked_client
            FROM documents d
            LEFT JOIN LATERAL (
                SELECT c.name
                FROM document_links dl
                JOIN clients c ON c.id = dl.entity_id
                WHERE dl.document_id = d.id AND dl.entity_type = 'client'
                ORDER BY c.name
                LIMIT 1
            ) lc ON TRUE
            WHERE d.org_id = $1
              AND d.type = 'signal'
              AND ($2::int IS NULL OR d.created_at >= NOW() - ($2 * interval '1 day'))
              AND ($3::text IS NULL OR d.metadata->>'subject' = $3 OR lc.name = $3)
              AND ($4::text IS NULL OR d.metadata->>'signal_type' = $4)
              AND ($5::int IS NULL OR (d.metadata->>'relevance_score')::int >= $5)
              AND ($8::text[] IS NULL OR d.metadata->>'subject' = ANY($8) OR lc.name = ANY($8))
              AND ($9::text IS NULL
                   OR ($9 = 'market'  AND d.metadata->>'scope' = 'market')
                   OR ($9 = 'client'  AND COALESCE(d.metadata->>'scope','') <> 'market'))
            ORDER BY d.created_at DESC
            LIMIT $6 OFFSET $7
            """,
            org_id, days if days else None, client_name, signal_type, min_relevance,
            limit, offset, subjects, scope,
        )
        out = []
        for r in rows:
            d = dict(r)
            linked = d.pop("linked_client", None)
            meta = d.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if not meta.get("subject") and linked:
                meta = {**meta, "subject": linked}
                d["metadata"] = meta
            out.append(d)
        return out


# ---------------------------------------------------------------------------
# Market-news monitoring config (org-level, singleton documents row)
# ---------------------------------------------------------------------------
# Curated economics/industry news pages the market_monitor heartbeat
# fingerprints (mirrors clients.metadata.monitored_sources, but org-scoped).
# Stored as a singleton type=market_config doc so no schema migration is needed.

async def get_market_config(org_id: int) -> dict:
    """Return the org's market-news config: {sources: [{url,label,last_fp,...}]}."""
    if not _pool:
        return {"sources": []}
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT metadata FROM documents
               WHERE org_id = $1 AND type = 'market_config'
               ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 1""",
            org_id,
        )
    meta = (row["metadata"] if row else None) or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta.setdefault("sources", [])
    return meta


async def save_market_config(org_id: int, config: dict) -> dict:
    """Upsert the singleton market_config doc. `config` is the full metadata
    blob (sources + rotation state); it's stored as-is."""
    if not _pool:
        return config
    meta = {**(config or {}), "updated_at": datetime.now(timezone.utc).isoformat()}
    meta.setdefault("sources", [])
    await index_document(
        org_id=org_id,
        doc_id="market-config",
        doc_type="market_config",
        title="Market news sources",
        content="Org-level economics/industry news pages monitored for change.",
        metadata=meta,
        embedding=[],
        source="human",
    )
    return meta


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

async def index_document(
    org_id: int,
    doc_id: str,
    doc_type: str,
    title: str,
    content: str,
    metadata: dict,
    embedding: list[float],
    source: str = "human",
    created_by: Optional[int] = None,
    agent_run_id: Optional[int] = None,
    visibility: str = "shared",
) -> int:
    """Upsert a document. Returns the document id, or -1 if DB is offline."""
    if not _pool:
        return -1
    title   = title.replace('\x00', '') if title else title
    content = content.replace('\x00', '') if content else content
    metadata = _sanitize_for_pg(metadata)
    vec = embedding if embedding else None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO documents (
                org_id, doc_id, type, title, content, metadata,
                embedding, source, created_by, agent_run_id, visibility
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (org_id, doc_id) DO UPDATE SET
                title      = EXCLUDED.title,
                content    = EXCLUDED.content,
                metadata   = EXCLUDED.metadata,
                embedding  = CASE
                                 WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding
                                 ELSE documents.embedding
                             END,
                updated_at = NOW()
            RETURNING id
            """,
            org_id, doc_id, doc_type, title, content,
            metadata, vec, source, created_by, agent_run_id, visibility,
        )
        return row["id"]


async def link_document(document_id: int, entity_type: str, entity_id: int) -> None:
    """Link a document to a client or contact (idempotent)."""
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO document_links (document_id, entity_type, entity_id)
            VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
            """,
            document_id, entity_type, entity_id,
        )


async def list_documents(
    org_id: int,
    doc_type: Optional[str] = None,
    client_id: Optional[int] = None,
    contact_id: Optional[int] = None,
) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        if client_id is not None:
            rows = await conn.fetch(
                """
                SELECT d.id, d.doc_id, d.type, d.title, d.metadata,
                       d.source, d.visibility, d.created_at, d.updated_at
                FROM documents d
                JOIN document_links dl
                  ON dl.document_id = d.id
                 AND dl.entity_type = 'client'
                 AND dl.entity_id   = $2
                WHERE d.org_id = $1
                ORDER BY d.created_at DESC
                """,
                org_id, client_id,
            )
        elif contact_id is not None:
            rows = await conn.fetch(
                """
                SELECT d.id, d.doc_id, d.type, d.title, d.metadata,
                       d.source, d.visibility, d.created_at, d.updated_at
                FROM documents d
                JOIN document_links dl
                  ON dl.document_id = d.id
                 AND dl.entity_type = 'contact'
                 AND dl.entity_id   = $2
                WHERE d.org_id = $1
                ORDER BY d.created_at DESC
                """,
                org_id, contact_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, doc_id, type, title, metadata,
                       source, visibility, created_at, updated_at
                FROM documents
                WHERE org_id = $1
                  AND ($2::text IS NULL OR type = $2)
                ORDER BY created_at DESC
                """,
                org_id, doc_type,
            )
        return [dict(r) for r in rows]


async def list_unlinked_documents(org_id: int, doc_type: Optional[str] = None) -> list[dict]:
    """Return documents that have no entries in document_links."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        type_clause = "AND d.type = $2" if doc_type else ""
        params = [org_id, doc_type] if doc_type else [org_id]
        rows = await conn.fetch(
            f"""
            SELECT d.id, d.doc_id, d.type, d.title, d.metadata, d.created_at
            FROM documents d
            WHERE d.org_id = $1 {type_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM document_links dl WHERE dl.document_id = d.id
              )
            ORDER BY d.created_at DESC
            """,
            *params,
        )
        return [dict(r) for r in rows]


async def get_document(org_id: int, doc_id: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, org_id, doc_id, type, title, content, metadata,
                      source, visibility, agent_run_id, created_by,
                      created_at, updated_at
               FROM documents WHERE org_id = $1 AND doc_id = $2""",
            org_id, doc_id,
        )
        return dict(row) if row else None


async def get_document_by_int_id(org_id: int, int_id: int) -> Optional[dict]:
    """Look up a document by its integer primary key (for foreign-key joins)."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, org_id, doc_id, type, title, content, metadata,
                      source, visibility, agent_run_id, created_by,
                      created_at, updated_at
               FROM documents WHERE org_id = $1 AND id = $2""",
            org_id, int_id,
        )
        return dict(row) if row else None


async def update_document(
    org_id: int,
    doc_id: str,
    patch: dict,
) -> Optional[dict]:
    """Patch title, content, and/or metadata on a document. Returns updated row or None."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE documents SET
                title      = COALESCE($3, title),
                content    = COALESCE($4, content),
                metadata   = CASE WHEN $5::jsonb IS NOT NULL
                                  THEN metadata || $5
                                  ELSE metadata END,
                updated_at = NOW()
            WHERE org_id = $1 AND doc_id = $2
            RETURNING *
            """,
            org_id, doc_id,
            patch.get("title"), patch.get("content"), patch.get("metadata"),
        )
        return dict(row) if row else None


async def update_client_metadata(org_id: int, name: str, patch: dict) -> Optional[dict]:
    """Merge patch into client metadata. Returns updated client row or None."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE clients SET metadata = metadata || $3
            WHERE org_id = $1 AND lower(name) = lower($2)
            RETURNING id, org_id, name, metadata, session_count, last_activity, created_by, created_at
            """,
            org_id, name, patch,
        )
        return dict(row) if row else None


async def update_contact_metadata(org_id: int, name: str, patch: dict) -> Optional[dict]:
    """Merge patch into contact metadata. Returns updated contact row or None."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE contacts SET metadata = metadata || $3
            WHERE org_id = $1 AND lower(name) = lower($2)
            RETURNING id, org_id, client_id, name, metadata, session_count, last_activity, created_by, created_at
            """,
            org_id, name, patch,
        )
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def hybrid_search(
    org_id: int,
    query: str,
    doc_type: Optional[str] = None,
    client_id: Optional[int] = None,
    top_k: int = 10,
) -> list[dict]:
    """Hybrid vector + FTS + trigram search across documents, clients, and contacts.

    Weights: vector 0.5, FTS 0.3, trigram 0.2.
    FTS uses websearch_to_tsquery (supports phrases, OR, exclusions).
    Trigram uses similarity() on names and word_similarity() on titles.
    Falls back to FTS+trigram if Ollama is offline.
    """
    if not _pool or not query.strip():
        return []

    # PHASE20-DRY: this function contains 3-4 near-identical SQL CTE blocks (~180 lines total).
    # Could be parametrized into a single block with conditional filters.
    embedding = await embed_text(query)

    async with _pool.acquire() as conn:
        if embedding:
            if client_id is not None:
                # Narrow search: only documents linked to a specific client
                rows = await conn.fetch(
                    """
                    SELECT
                        'document'::text                                                   AS result_type,
                        d.id::text                                                         AS result_id,
                        d.title                                                            AS display_title,
                        left(d.content, 200)                                              AS snippet,
                        d.type                                                            AS subtype,
                        d.metadata,
                        (1 - (d.embedding <=> $1))                                       AS vec_score,
                        ts_rank_cd(d.fts_doc, websearch_to_tsquery('english',$2), 1)     AS fts_score,
                        word_similarity($2, d.title)                                      AS trgm_score,
                        (0.5 * (1 - (d.embedding <=> $1)) +
                         0.3 * ts_rank_cd(d.fts_doc, websearch_to_tsquery('english',$2), 1) +
                         0.2 * word_similarity($2, d.title))                             AS combined_score
                    FROM documents d
                    JOIN document_links dl
                      ON dl.document_id = d.id
                     AND dl.entity_type = 'client'
                     AND dl.entity_id   = $4
                    WHERE d.org_id = $3
                      AND d.embedding IS NOT NULL
                      AND ($5::text IS NULL OR d.type = $5)
                      AND (
                            d.fts_doc @@ websearch_to_tsquery('english',$2)
                         OR (1 - (d.embedding <=> $1)) > 0.3
                         OR word_similarity($2, d.title) > 0.2
                      )
                    ORDER BY combined_score DESC
                    LIMIT $6
                    """,
                    embedding, query, org_id, client_id, doc_type, top_k,
                )
            else:
                rows = await conn.fetch(
                    """
                    WITH docs AS (
                        SELECT
                            'document'::text                                               AS result_type,
                            d.id::text                                                     AS result_id,
                            d.title                                                        AS display_title,
                            left(d.content, 200)                                          AS snippet,
                            d.type                                                        AS subtype,
                            d.metadata,
                            (1 - (d.embedding <=> $1))                                   AS vec_score,
                            ts_rank_cd(d.fts_doc, websearch_to_tsquery('english',$2), 1) AS fts_score,
                            word_similarity($2, d.title)                                  AS trgm_score
                        FROM documents d
                        WHERE d.org_id = $3
                          AND d.embedding IS NOT NULL
                          AND ($4::text IS NULL OR d.type = $4)
                          AND (
                                d.fts_doc @@ websearch_to_tsquery('english',$2)
                             OR (1 - (d.embedding <=> $1)) > 0.3
                             OR word_similarity($2, d.title) > 0.2
                          )
                    ),
                    cls AS (
                        SELECT
                            'client'::text,
                            c.id::text,
                            c.name,
                            left(coalesce(c.metadata->>'notes', ''), 200),
                            'client'::text,
                            c.metadata,
                            (1 - (c.embedding <=> $1)),
                            ts_rank_cd(c.fts_doc, websearch_to_tsquery('english',$2), 1),
                            similarity($2, c.name)
                        FROM clients c
                        WHERE c.org_id = $3
                          AND c.embedding IS NOT NULL
                          AND (
                                c.fts_doc @@ websearch_to_tsquery('english',$2)
                             OR (1 - (c.embedding <=> $1)) > 0.3
                             OR similarity($2, c.name) > 0.2
                          )
                    ),
                    cts AS (
                        SELECT
                            'contact'::text,
                            ct.id::text,
                            ct.name,
                            left(
                                coalesce(ct.metadata->>'role', '') || ' · ' ||
                                coalesce(ct.metadata->>'company', ''), 200
                            ),
                            'contact'::text,
                            ct.metadata,
                            (1 - (ct.embedding <=> $1)),
                            ts_rank_cd(ct.fts_doc, websearch_to_tsquery('english',$2), 1),
                            similarity($2, ct.name)
                        FROM contacts ct
                        WHERE ct.org_id = $3
                          AND ct.embedding IS NOT NULL
                          AND (
                                ct.fts_doc @@ websearch_to_tsquery('english',$2)
                             OR (1 - (ct.embedding <=> $1)) > 0.3
                             OR similarity($2, ct.name) > 0.2
                          )
                    ),
                    all_results AS (
                        SELECT * FROM docs
                        UNION ALL SELECT * FROM cls
                        UNION ALL SELECT * FROM cts
                    )
                    SELECT
                        result_type, result_id, display_title, snippet, subtype, metadata,
                        vec_score, fts_score, trgm_score,
                        (0.5 * vec_score + 0.3 * fts_score + 0.2 * trgm_score) AS combined_score
                    FROM all_results
                    ORDER BY combined_score DESC
                    LIMIT $5
                    """,
                    embedding, query, org_id, doc_type, top_k,
                )
        else:
            # Ollama offline — FTS + trigram only
            rows = await conn.fetch(
                """
                WITH docs AS (
                    SELECT
                        'document'::text                                               AS result_type,
                        d.id::text                                                     AS result_id,
                        d.title                                                        AS display_title,
                        left(d.content, 200)                                          AS snippet,
                        d.type                                                        AS subtype,
                        d.metadata,
                        0.0::float8                                                   AS vec_score,
                        ts_rank_cd(d.fts_doc, websearch_to_tsquery('english',$1), 1) AS fts_score,
                        word_similarity($1, d.title)                                  AS trgm_score
                    FROM documents d
                    WHERE d.org_id = $2
                      AND ($3::text IS NULL OR d.type = $3)
                      AND (
                            d.fts_doc @@ websearch_to_tsquery('english',$1)
                         OR word_similarity($1, d.title) > 0.2
                      )
                ),
                cls AS (
                    SELECT
                        'client'::text,
                        c.id::text,
                        c.name,
                        left(coalesce(c.metadata->>'notes', ''), 200),
                        'client'::text,
                        c.metadata,
                        0.0::float8,
                        ts_rank_cd(c.fts_doc, websearch_to_tsquery('english',$1), 1),
                        similarity($1, c.name)
                    FROM clients c
                    WHERE c.org_id = $2
                      AND (
                            c.fts_doc @@ websearch_to_tsquery('english',$1)
                         OR similarity($1, c.name) > 0.2
                      )
                ),
                cts AS (
                    SELECT
                        'contact'::text,
                        ct.id::text,
                        ct.name,
                        left(
                            coalesce(ct.metadata->>'role', '') || ' · ' ||
                            coalesce(ct.metadata->>'company', ''), 200
                        ),
                        'contact'::text,
                        ct.metadata,
                        0.0::float8,
                        ts_rank_cd(ct.fts_doc, websearch_to_tsquery('english',$1), 1),
                        similarity($1, ct.name)
                    FROM contacts ct
                    WHERE ct.org_id = $2
                      AND (
                            ct.fts_doc @@ websearch_to_tsquery('english',$1)
                         OR similarity($1, ct.name) > 0.2
                      )
                ),
                all_results AS (
                    SELECT * FROM docs
                    UNION ALL SELECT * FROM cls
                    UNION ALL SELECT * FROM cts
                )
                SELECT result_type, result_id, display_title, snippet, subtype, metadata,
                       vec_score, fts_score, trgm_score,
                       (0.6 * fts_score + 0.4 * trgm_score) AS combined_score
                FROM all_results
                ORDER BY combined_score DESC
                LIMIT $4
                """,
                query, org_id, doc_type, top_k,
            )

    return [
        {
            "type": r["result_type"],
            "id": r["result_id"],
            "display_title": r["display_title"],
            "snippet": r["snippet"] or "",
            "subtype": r["subtype"],
            "metadata": r["metadata"] if r["metadata"] else {},
            "vec_score": round(float(r["vec_score"]), 4),
            "fts_score": round(float(r["fts_score"]), 4),
            "trgm_score": round(float(r["trgm_score"]), 4),
            "combined_score": round(float(r["combined_score"]), 4),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

async def create_agent_run(
    org_id: int,
    agent_type: str,
    task: str,
    trigger_type: str = "manual",
    triggered_by: Optional[int] = None,
) -> int:
    """Insert a pending agent_runs row. Returns run id, or -1 if DB is offline."""
    if not _pool:
        return -1
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_runs (org_id, agent_type, task, trigger_type, triggered_by, status)
            VALUES ($1, $2, $3, $4, $5, 'pending')
            RETURNING id
            """,
            org_id, agent_type, task, trigger_type, triggered_by,
        )
        return row["id"]


async def update_agent_run(
    run_id: int,
    status: str,
    tool_calls: Optional[list] = None,
    output: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Update status, tool_calls, output, and/or error on an agent_runs row."""
    if not _pool or run_id == -1:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs SET
                status       = $2,
                tool_calls   = COALESCE($3::jsonb, tool_calls),
                output       = COALESCE($4::jsonb, output),
                error        = COALESCE($5, error),
                completed_at = CASE WHEN $2 IN ('done', 'failed') THEN NOW()
                                    ELSE completed_at END
            WHERE id = $1
            """,
            run_id,
            status,
            tool_calls if tool_calls is not None else None,
            output     if output     is not None else None,
            error,
        )


async def get_agent_run(run_id: int, org_id: Optional[int] = None) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        if org_id is not None:
            row = await conn.fetchrow(
                "SELECT * FROM agent_runs WHERE id = $1 AND org_id = $2", run_id, org_id
            )
        else:
            row = await conn.fetchrow("SELECT * FROM agent_runs WHERE id = $1", run_id)
        return dict(row) if row else None


async def list_documents_by_run_id(org_id: int, run_id: int) -> list[dict]:
    """Return documents written by a specific agent run."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, doc_id, type, title, metadata, created_at
            FROM documents
            WHERE org_id = $1 AND agent_run_id = $2
            ORDER BY created_at ASC
            """,
            org_id, run_id,
        )
        return [dict(r) for r in rows]


async def patch_agent_run_output(run_id: int, patch: dict) -> None:
    """Merge patch into agent_runs.output JSONB without overwriting existing keys."""
    if not _pool or run_id == -1:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runs SET output = COALESCE(output, '{}'::jsonb) || $2 WHERE id = $1",
            run_id, patch,
        )


async def get_agent_activity(org_id: int, days: int = 7) -> dict:
    """Return a weekly activity summary: per-type counts + recent run list."""
    if not _pool:
        return {"summary": [], "runs": []}
    async with _pool.acquire() as conn:
        summary_rows = await conn.fetch(
            """
            SELECT agent_type,
                   COUNT(*)                                      AS total,
                   COUNT(*) FILTER (WHERE status = 'done')      AS done,
                   COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
                   COUNT(*) FILTER (WHERE status IN ('pending','running')) AS active
            FROM agent_runs
            WHERE org_id = $1
              AND created_at >= NOW() - ($2 || ' days')::interval
            GROUP BY agent_type
            ORDER BY total DESC
            """,
            org_id, str(days),
        )
        run_rows = await conn.fetch(
            """
            SELECT id, agent_type, status, task, trigger_type,
                   created_at, completed_at, error
            FROM agent_runs
            WHERE org_id = $1
              AND created_at >= NOW() - ($2 || ' days')::interval
            ORDER BY created_at DESC
            LIMIT 50
            """,
            org_id, str(days),
        )
        return {
            "summary": [dict(r) for r in summary_rows],
            "runs":    [dict(r) for r in run_rows],
        }


async def list_agent_runs(org_id: int, limit: int = 20) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_type, status, task, trigger_type,
                   triggered_by, created_at, completed_at, error
            FROM agent_runs
            WHERE org_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id, limit,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

async def list_heartbeats(org_id: Optional[int] = None) -> list[dict]:
    """Return enabled heartbeats, optionally filtered to one org."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        if org_id is not None:
            rows = await conn.fetch(
                "SELECT * FROM heartbeats WHERE org_id = $1 AND enabled = TRUE ORDER BY id",
                org_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM heartbeats WHERE enabled = TRUE ORDER BY id"
            )
    return [dict(r) for r in rows]


async def seed_default_heartbeats(org_id: int) -> None:
    """Insert the default cron schedules for a new org. Safe to call multiple times."""
    if not _pool:
        return
    defaults = [
        ("enrichment",      "0 7 * * *",   "Enrich any client or contact records updated in the last 24 hours."),
        ("research",        "0 8 * * 1-5", "Check all active clients. For any not updated in 14 days, produce a fresh research brief."),
        ("osint",           "0 9 * * *",   "Run OSINT research on all active clients. Search for recent news, leadership changes, and growth signals. Write a type=osint report for each."),
        ("org",             "0 10 * * *",  "Deduplicate contacts. Link unlinked documents. Flag stale entries. Run after enrichment, research, and OSINT have completed."),
        ("quality_digest",  "0 7 * * 1",   "Generate weekly research quality digest: coverage stats, avg relevance per subject, flag low-coverage clients."),
        ("weekly_digest",   "0 8 * * 1",   "Send weekly Telegram digest: sessions, research runs, new findings, signals, stale clients."),
        ("stale_clients",   "0 9 * * 1",   "Check for clients with no activity in 30 days and send a Telegram nudge."),
        ("match_monitor",   "0 6 * * 1",   "For every active client, queue a pain_point_research + match_synthesis run if no match report exists within the last 7 days."),
        ("source_monitor",  "0 6 * * *",   "Daily no-LLM sweep: fingerprint every client's monitored news/press pages; auto-research focus clients on change, flag the rest with a New-info badge."),
        ("nba_queue",       "30 7 * * *",  "Compute the daily next-best-action queue: score every client from fresh signals, source changes, open outreach, and staleness; write the ranked snapshot with reasons."),
        ("market_monitor",  "0 5 * * *",   "Fingerprint curated economics/industry news pages; on change research the development, rotate through client industries, write market signals, and apply the important ones to the clients they affect."),
        ("jobs_monitor",    "0 4 * * 1",   "Scan clients' careers pages for open positions; list them and infer what the company likely needs from the role mix, feeding the match analysis."),
        ("rep_digest",      "0 8 */2 * *", "Every 2 days: build a short per-rep client digest (what's new + top next actions) for each seller; store as pending for admin review/send and Telegram-remind the admin."),
        ("task_reminder",   "0 8 * * *",   "Email each rep the tasks they have due today or overdue, so follow-ups don't slip."),
        ("research_qa",     "30 6 * * *",  "Sample recent agent-written research and flag quality problems (no LLM): stale synthesis that lags newer findings, cross-client contamination, and claims with no sources. Write flags into each doc + a QA summary report."),
    ]
    async with _pool.acquire() as conn:
        for agent_type, cron_expr, task in defaults:
            await conn.execute(
                """
                INSERT INTO heartbeats (org_id, agent_type, cron_expr, task)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                org_id, agent_type, cron_expr, task,
            )


async def update_heartbeat_last_run(hb_id: int) -> None:
    """Stamp last_run_at = NOW() after a heartbeat job fires."""
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE heartbeats SET last_run_at = NOW() WHERE id = $1",
            hb_id,
        )


async def list_all_heartbeats(org_id: int) -> list[dict]:
    """Return all heartbeats for an org including disabled — for admin UI."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM heartbeats WHERE org_id = $1 ORDER BY id",
            org_id,
        )
    return [dict(r) for r in rows]


async def create_heartbeat(
    org_id: int,
    agent_type: str,
    cron_expr: str,
    task: str,
    enabled: bool = True,
) -> dict:
    """Insert a new heartbeat row and return it."""
    if not _pool:
        raise RuntimeError("DB unavailable")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO heartbeats (org_id, agent_type, cron_expr, task, enabled)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            org_id, agent_type, cron_expr, task, enabled,
        )
    return dict(row)


async def get_heartbeat(hb_id: int, org_id: int) -> Optional[dict]:
    """Return a single heartbeat row by ID scoped to org, or None."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM heartbeats WHERE id = $1 AND org_id = $2",
            hb_id, org_id,
        )
    return dict(row) if row else None


async def update_heartbeat(
    hb_id: int,
    org_id: int,
    agent_type: str,
    cron_expr: str,
    task: str,
    enabled: bool,
) -> Optional[dict]:
    """Update heartbeat fields and return the updated row, or None if not found."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE heartbeats
            SET agent_type = $3, cron_expr = $4, task = $5, enabled = $6
            WHERE id = $1 AND org_id = $2
            RETURNING *
            """,
            hb_id, org_id, agent_type, cron_expr, task, enabled,
        )
    return dict(row) if row else None


async def delete_heartbeat(hb_id: int, org_id: int) -> bool:
    """Delete a heartbeat row. Returns True if a row was deleted."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM heartbeats WHERE id = $1 AND org_id = $2",
            hb_id, org_id,
        )
    return status == "DELETE 1"


# ---------------------------------------------------------------------------
# Research Tasks (Phase 7: Deep Research Engine)
# ---------------------------------------------------------------------------

async def enqueue_research_task(
    org_id: int,
    subject_type: str,
    subject: str,
    task_type: str,
    payload: dict,
    depth: int = 0,
    parent_task_id: Optional[int] = None,
    priority: int = 5,
    agent_run_id: Optional[int] = None,
) -> int:
    """Insert a new research task. Returns task id, or -1 if DB is offline."""
    if not _pool:
        return -1
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 5
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO research_tasks
                (org_id, subject_type, subject, task_type, payload, depth,
                 parent_task_id, priority, assigned_agent_run_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            org_id, subject_type, subject, task_type,
            payload, depth, parent_task_id, priority, agent_run_id,
        )
        return row["id"]


async def claim_research_task(org_id: Optional[int] = None) -> Optional[dict]:
    """Atomically claim one pending task for this org (or any org when None). Returns task dict or None.

    Uses SELECT FOR UPDATE SKIP LOCKED inside an explicit transaction so the
    UPDATE is part of the same atomic unit as the SELECT. Multiple concurrent
    workers can call this simultaneously without double-claiming.
    """
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT rt.* FROM research_tasks rt
                JOIN orgs o ON o.id = rt.org_id
                WHERE ($1::bigint IS NULL OR rt.org_id = $1)
                  AND rt.status = 'pending'
                  AND COALESCE((o.settings->>'suspended')::boolean, false) = false   -- hosted: lapsed subscription
                ORDER BY rt.priority DESC, rt.id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                org_id,
            )
            if not row:
                return None
            await conn.execute(
                "UPDATE research_tasks SET status = 'running' WHERE id = $1",
                row["id"],
            )
            return {**dict(row), "status": "running"}


def _sanitize_for_pg(obj):
    """Recursively strip null bytes from strings so PostgreSQL accepts them."""
    if isinstance(obj, str):
        return obj.replace('\x00', '')
    if isinstance(obj, dict):
        return {k: _sanitize_for_pg(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_pg(v) for v in obj]
    return obj


async def complete_research_task(
    task_id: int,
    result: dict,
    new_tasks: list,
    max_depth: int = 3,
) -> bool:
    """Mark a task done and insert child tasks if depth allows.

    Returns True if no pending or running non-aggregate tasks remain for this
    subject — the caller should then enqueue an aggregate task.
    Each entry in new_tasks is a dict with keys: task_type, payload, priority,
    and optionally subject_type and subject (defaults to parent values).
    """
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        task_row = await conn.fetchrow(
            "SELECT * FROM research_tasks WHERE id = $1",
            task_id,
        )
        if not task_row:
            return False

        child_depth = (task_row["depth"] or 0) + 1
        org_id = task_row["org_id"]
        subject = task_row["subject"]
        subject_type = task_row["subject_type"]
        parent_agent_run_id = task_row["assigned_agent_run_id"]

        result = _sanitize_for_pg(result)

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE research_tasks
                SET status = 'done', result = $2, completed_at = NOW()
                WHERE id = $1
                """,
                task_id, result,
            )
            if child_depth <= max_depth and new_tasks:
                for t in new_tasks:
                    await conn.execute(
                        """
                        INSERT INTO research_tasks
                            (org_id, subject_type, subject, task_type,
                             payload, depth, parent_task_id, priority,
                             assigned_agent_run_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        org_id,
                        t.get("subject_type", subject_type),
                        t.get("subject", subject),
                        t["task_type"],
                        t.get("payload", {}),
                        child_depth,
                        task_id,
                        t.get("priority", 5),
                        parent_agent_run_id,
                    )

        # Query quality signal: when all fetch_url children of a web_search task
        # are done, store the mean relevance as query_score in the parent task result.
        if (
            task_row["task_type"] == "fetch_url"
            and isinstance(result.get("relevance_score"), (int, float))
            and task_row.get("parent_task_id")
        ):
            parent_id = task_row["parent_task_id"]
            pending_siblings = await conn.fetchval(
                """
                SELECT COUNT(*) FROM research_tasks
                WHERE parent_task_id = $1
                  AND task_type = 'fetch_url'
                  AND status IN ('pending', 'running')
                """,
                parent_id,
            )
            if pending_siblings == 0:
                score_rows = await conn.fetch(
                    """
                    SELECT (result->>'relevance_score')::float AS score
                    FROM research_tasks
                    WHERE parent_task_id = $1
                      AND task_type = 'fetch_url'
                      AND status = 'done'
                      AND result ? 'relevance_score'
                    """,
                    parent_id,
                )
                valid_scores = [r["score"] for r in score_rows if r["score"] is not None]
                if valid_scores:
                    query_score = round(sum(valid_scores) / len(valid_scores), 2)
                    await conn.execute(
                        """
                        UPDATE research_tasks
                        SET result = COALESCE(result, '{}'::jsonb) || $2
                        WHERE id = $1
                        """,
                        parent_id, {"query_score": query_score},
                    )

        pending_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM research_tasks
            WHERE org_id = $1
              AND subject = $2
              AND status IN ('pending', 'running')
              AND task_type != 'aggregate'
            """,
            org_id, subject,
        )
        return pending_count == 0


async def list_research_tasks(
    org_id: int,
    status: Optional[str] = None,
    subject: Optional[str] = None,
) -> list[dict]:
    """List research tasks for an org, optionally filtered by status and/or subject."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, subject_type, subject, task_type, payload, depth,
                   parent_task_id, status, priority, assigned_agent_run_id,
                   result, created_at, completed_at
            FROM research_tasks
            WHERE org_id = $1
              AND ($2::text IS NULL OR status = $2)
              AND ($3::text IS NULL OR lower(subject) = lower($3))
            ORDER BY created_at DESC
            LIMIT 500
            """,
            org_id, status, subject,
        )
        return [dict(r) for r in rows]


async def get_research_task(task_id: int, org_id: Optional[int] = None) -> Optional[dict]:
    """Return a single research task with its children list."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        if org_id is not None:
            row = await conn.fetchrow(
                "SELECT * FROM research_tasks WHERE id = $1 AND org_id = $2", task_id, org_id
            )
        else:
            row = await conn.fetchrow("SELECT * FROM research_tasks WHERE id = $1", task_id)
        if not row:
            return None
        task = dict(row)
        children = await conn.fetch(
            """
            SELECT id, task_type, subject, subject_type, status, depth,
                   payload, created_at, completed_at
            FROM research_tasks WHERE parent_task_id = $1 ORDER BY id
            """,
            task_id,
        )
        task["children"] = [dict(c) for c in children]
        return task


async def cancel_research_subject(org_id: int, subject: str) -> int:
    """Mark all pending/running tasks for a subject as skipped. Returns count updated."""
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE research_tasks
            SET status = 'skipped', completed_at = NOW()
            WHERE org_id = $1 AND lower(subject) = lower($2)
              AND status IN ('pending', 'running')
            """,
            org_id, subject,
        )
        parts = result.split()
        return int(parts[1]) if len(parts) > 1 else 0


# ---------------------------------------------------------------------------
# Seller companies + products (seller product intelligence)
# ---------------------------------------------------------------------------

async def get_seller_company(org_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM seller_companies WHERE org_id = $1",
            org_id,
        )
        return dict(row) if row else None


async def upsert_seller_company(
    org_id: int,
    name: str,
    website_url: Optional[str] = None,
    industry: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO seller_companies (org_id, name, website_url, industry, metadata)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (org_id) DO UPDATE
              SET name = EXCLUDED.name,
                  website_url = COALESCE(EXCLUDED.website_url, seller_companies.website_url),
                  industry = COALESCE(EXCLUDED.industry, seller_companies.industry),
                  updated_at = NOW()
            RETURNING id
            """,
            org_id, name, website_url, industry,
            _sanitize_for_pg(metadata or {}),
        )
        return row["id"] if row else 0


async def update_seller_company_status(
    org_id: int,
    status: str,
    research_doc_id: Optional[int] = None,
) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE seller_companies
               SET research_status = $2,
                   research_doc_id = COALESCE($3, research_doc_id),
                   updated_at = NOW()
             WHERE org_id = $1
            """,
            org_id, status, research_doc_id,
        )


async def create_product(
    org_id: int,
    seller_company_id: int,
    name: str,
    category: Optional[str] = None,
    description: Optional[str] = None,
    key_features: Optional[list] = None,
    pricing_info: Optional[str] = None,
    target_customer: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO products
              (org_id, seller_company_id, name, category, description,
               key_features, pricing_info, target_customer, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            org_id, seller_company_id, name, category, description,
            _sanitize_for_pg(key_features or []),
            pricing_info, target_customer,
            _sanitize_for_pg(metadata or {}),
        )
        return row["id"] if row else 0


async def list_products(
    org_id: int,
    focus_only: bool = False,
    shared_only: bool = False,
) -> list[dict]:
    if not _pool:
        return []
    conditions = ["org_id = $1", "status != 'deleted'"]
    if focus_only:
        conditions.append("is_focus = TRUE")
    if shared_only:
        conditions.append("is_shared = TRUE")
    where = " AND ".join(conditions)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM products WHERE {where} ORDER BY priority DESC, is_favorite DESC, name ASC",
            org_id,
        )
        return [dict(r) for r in rows]


async def get_product(product_id: int, org_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND org_id = $2",
            product_id, org_id,
        )
        return dict(row) if row else None


async def update_product(product_id: int, org_id: int, patch_dict: dict) -> Optional[dict]:
    if not _pool or not patch_dict:
        return None
    _ALLOWED = {
        "name", "description", "category", "key_features", "pricing_info",
        "target_customer", "is_focus", "priority", "is_favorite", "is_shared",
        "status", "metadata", "website_url", "source_doc_id",
    }
    safe = {k: v for k, v in patch_dict.items() if k in _ALLOWED}
    if not safe:
        return None
    set_parts = []
    values = [product_id, org_id]
    for key, val in safe.items():
        if key in ("key_features", "metadata"):
            val = _sanitize_for_pg(val)
        values.append(val)
        set_parts.append(f"{key} = ${len(values)}")
    set_parts.append("updated_at = NOW()")
    sql = f"UPDATE products SET {', '.join(set_parts)} WHERE id = $1 AND org_id = $2 RETURNING *"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(sql, *values)
        return dict(row) if row else None


async def delete_product(product_id: int, org_id: int) -> bool:
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE products SET status = 'deleted', updated_at = NOW() WHERE id = $1 AND org_id = $2",
            product_id, org_id,
        )
        return result.split()[-1] == "1"


# ---------------------------------------------------------------------------
# Match report helpers
# ---------------------------------------------------------------------------

async def get_match_reports(org_id: int, client_name: Optional[str] = None) -> list[dict]:
    """Return match_report documents for the org, optionally filtered by client."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        if client_name:
            rows = await conn.fetch(
                """
                SELECT d.id, d.content, d.metadata, d.created_at, d.agent_run_id,
                       c.name AS client_name
                FROM documents d
                JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
                JOIN clients c ON c.id = dl.entity_id
                WHERE d.org_id = $1 AND d.type = 'match_report' AND c.name ILIKE $2
                ORDER BY d.created_at DESC
                LIMIT 10
                """,
                org_id, client_name,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT d.id, d.content, d.metadata, d.created_at, d.agent_run_id,
                       c.name AS client_name
                FROM documents d
                LEFT JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
                LEFT JOIN clients c ON c.id = dl.entity_id
                WHERE d.org_id = $1 AND d.type = 'match_report'
                ORDER BY d.created_at DESC
                LIMIT 100
                """,
                org_id,
            )
    return [dict(r) for r in rows]


async def get_client_match_status(org_id: int, client_name: str) -> Optional[dict]:
    """Return match_status metadata for a client."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT metadata->>'match_status' AS match_status,
                   metadata->>'match_updated_at' AS match_updated_at,
                   metadata->>'match_run_id' AS match_run_id
            FROM clients
            WHERE org_id = $1 AND name ILIKE $2
            LIMIT 1
            """,
            org_id, client_name,
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Thread-safe helpers (for use inside ThreadPoolExecutor workers)
# ---------------------------------------------------------------------------

def _run_coro_from_thread(coro):
    """Schedule a coroutine on the main event loop from a worker thread.

    Blocks until the coroutine completes or times out (30 s). Returns the
    coroutine's result, or None on timeout/error.
    Safe to call from ThreadPoolExecutor because it uses the main loop,
    not asyncio.run() which would create a conflicting loop.
    """
    if _main_loop is None or _pool is None:
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
    try:
        return future.result(timeout=30)
    except Exception as exc:
        logger.warning("DB op failed (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Thesis evaluation: research sessions + prompt log (Session 88)
# ---------------------------------------------------------------------------

async def start_research_session(org_id: int, user_id: int, client_name: str, method: str = "manual") -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO research_sessions (org_id, user_id, client_name, method)
               VALUES ($1, $2, $3, $4) RETURNING id, started_at""",
            org_id, user_id, client_name, method,
        )
        return dict(row)


async def stop_research_session(
    session_id: int, org_id: int,
    sources_checked: Optional[int] = None, notes: Optional[str] = None,
) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE research_sessions
               SET ended_at = NOW(), sources_checked = COALESCE($3, sources_checked),
                   notes = COALESCE($4, notes)
               WHERE id = $1 AND org_id = $2 AND ended_at IS NULL
               RETURNING id, client_name, started_at, ended_at,
                         EXTRACT(EPOCH FROM (ended_at - started_at))::int AS duration_secs""",
            session_id, org_id, sources_checked, notes,
        )
        return dict(row) if row else None


async def list_research_sessions(org_id: int, limit: int = 200) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT rs.*, u.display_name AS user_name,
                      EXTRACT(EPOCH FROM (rs.ended_at - rs.started_at))::int AS duration_secs
               FROM research_sessions rs LEFT JOIN users u ON u.id = rs.user_id
               WHERE rs.org_id = $1 ORDER BY rs.started_at DESC LIMIT $2""",
            org_id, limit,
        )
        return [dict(r) for r in rows]


def log_prompt(org_id: int, user_id: Optional[int], surface: str, prompt: str, context: Optional[dict] = None) -> None:
    """Fire-and-forget usage logging — must never block or break the request."""
    if not _pool:
        return

    async def _write() -> None:
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO prompt_log (org_id, user_id, surface, prompt, context) VALUES ($1, $2, $3, $4, $5)",
                    org_id, user_id, surface, prompt[:4000], context or {},
                )
        except Exception as exc:
            logger.warning("prompt_log write failed (non-fatal): %s", exc)

    try:
        asyncio.get_running_loop()
        asyncio.create_task(_write())
    except RuntimeError:
        pass  # no running loop (sync context) — skip rather than block


async def list_prompts(org_id: int, limit: int = 100, surface: Optional[str] = None) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT pl.id, pl.surface, pl.prompt, pl.context, pl.created_at,
                      u.display_name AS user_name
               FROM prompt_log pl LEFT JOIN users u ON u.id = pl.user_id
               WHERE pl.org_id = $1 AND ($3::text IS NULL OR pl.surface = $3)
               ORDER BY pl.created_at DESC LIMIT $2""",
            org_id, limit, surface,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Contact log — durable per-contact outreach record (Home "Last contacted")
# ---------------------------------------------------------------------------

async def log_contact(org_id: int, user_id: Optional[int], client_name: str,
                      contact_name: str = "", contact_email: str = "",
                      subject: str = "", body: str = "",
                      source_doc_id: Optional[int] = None) -> int:
    """Record one exported/sent outreach mail to a contact. Returns row id (-1 if DB off)."""
    if not _pool:
        return -1
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO contact_log
                 (org_id, user_id, client_name, contact_name, contact_email, subject, body, source_doc_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            org_id, user_id, client_name, contact_name or "", contact_email or "",
            subject or "", (body or "")[:8000], source_doc_id,
        )
        return row["id"]


async def list_contact_log(org_id: int, user_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """Recent contacts, newest first. Scoped to one rep when user_id is given."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, user_id, client_name, contact_name, contact_email, subject, body,
                      sent_at, replied, follow_up, source_doc_id
               FROM contact_log
               WHERE org_id = $1 AND ($2::bigint IS NULL OR user_id = $2)
               ORDER BY sent_at DESC LIMIT $3""",
            org_id, user_id, limit,
        )
        return [dict(r) for r in rows]


async def update_contact_log(log_id: int, org_id: int,
                             replied: Optional[bool] = None,
                             follow_up: Optional[bool] = None) -> bool:
    """Toggle replied / follow_up on a contact-log row. Returns True if a row changed."""
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE contact_log
               SET replied   = COALESCE($3, replied),
                   follow_up = COALESCE($4, follow_up)
               WHERE id = $1 AND org_id = $2
               RETURNING id""",
            log_id, org_id, replied, follow_up,
        )
        return row is not None


# ---------------------------------------------------------------------------
# User to-do / follow-up tasks (Deploy 2)
# ---------------------------------------------------------------------------

async def create_task(org_id: int, user_id: Optional[int], title: str,
                      client_name: Optional[str] = None, notes: Optional[str] = None,
                      due_date=None, priority: int = 5, source: str = "manual",
                      recurrence: Optional[str] = None, snooze_until=None,
                      deal_id: Optional[int] = None) -> Optional[dict]:
    """Create a to-do. due_date is a datetime.date or None. Returns the row (None if DB off).
    recurrence: daily|weekly|monthly|None; snooze_until: aware datetime|None; deal_id: deals.id|None."""
    if not _pool or not (title or "").strip():
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO user_tasks
                 (org_id, user_id, client_name, title, notes, due_date, priority, source,
                  recurrence, snooze_until, deal_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
            org_id, user_id, (client_name or None), title.strip(),
            (notes or None), due_date, int(priority or 5), source,
            recurrence, snooze_until, deal_id,
        )
        return dict(row) if row else None


async def get_task(task_id: int, org_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM user_tasks WHERE id = $1 AND org_id = $2", task_id, org_id)
        return dict(row) if row else None


async def list_tasks(org_id: int, user_id: Optional[int] = None,
                     include_done: bool = False, limit: int = 200,
                     include_snoozed: bool = False, deal_id: Optional[int] = None,
                     client_name: Optional[str] = None) -> list[dict]:
    """Tasks for an org, optionally scoped to one rep / deal / client. Open first, soonest due first.
    Snoozed tasks (snooze_until in the future) are hidden unless include_snoozed — that is what
    keeps them out of Home, the NBA queue and the reminder mail until they wake up."""
    if not _pool:
        return []
    conds = ["org_id = $1"]
    params: list = [org_id]
    if user_id is not None:
        params.append(user_id)
        conds.append(f"user_id = ${len(params)}")
    if deal_id is not None:
        params.append(deal_id)
        conds.append(f"deal_id = ${len(params)}")
    if client_name:
        params.append(client_name)
        conds.append(f"client_name = ${len(params)}")
    if not include_done:
        conds.append("status = 'open'")
    if not include_snoozed:
        conds.append("(snooze_until IS NULL OR snooze_until <= NOW())")
    where = " AND ".join(conds)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT * FROM user_tasks WHERE {where}
                ORDER BY (status = 'done'), (due_date IS NULL), due_date ASC,
                         priority DESC, created_at DESC
                LIMIT {int(limit)}""",
            *params,
        )
        return [dict(r) for r in rows]


async def update_task(task_id: int, org_id: int, patch: dict) -> Optional[dict]:
    """Patch title/notes/due_date/priority/status/client_name/recurrence/snooze_until/deal_id.
    Stamps completed_at on done. Recurrence spawning lives in the router (needs the row back)."""
    if not _pool or not patch:
        return None
    allowed = {"title", "notes", "due_date", "priority", "status", "client_name",
               "recurrence", "snooze_until", "deal_id"}
    safe = {k: v for k, v in patch.items() if k in allowed}
    if not safe:
        return None
    sets, params = [], [task_id, org_id]
    for k, v in safe.items():
        params.append(v)
        sets.append(f"{k} = ${len(params)}")
    if safe.get("status") == "done":
        sets.append("completed_at = NOW()")
    elif safe.get("status") == "open":
        sets.append("completed_at = NULL")
    sets.append("updated_at = NOW()")
    sql = f"UPDATE user_tasks SET {', '.join(sets)} WHERE id = $1 AND org_id = $2 RETURNING *"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
        return dict(row) if row else None


async def delete_task(task_id: int, org_id: int) -> bool:
    if not _pool:
        return False
    async with _pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM user_tasks WHERE id = $1 AND org_id = $2", task_id, org_id
        )
        return res.split()[-1] == "1"


async def list_reminder_tasks(org_id: int) -> list[dict]:
    """Open tasks due today or overdue, joined to the owner's email + name — for the daily reminder."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT t.id, t.user_id, t.title, t.client_name, t.due_date, t.priority,
                      t.recurrence, t.deal_id,
                      u.email AS user_email, u.display_name AS user_name
               FROM user_tasks t JOIN users u ON u.id = t.user_id
               WHERE t.org_id = $1 AND t.status = 'open'
                 AND t.due_date IS NOT NULL AND t.due_date <= CURRENT_DATE
                 AND (t.snooze_until IS NULL OR t.snooze_until <= NOW())
               ORDER BY t.user_id, t.due_date ASC""",
            org_id,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Shared clients (Phase 6a) — see sharing.py for the rules
# ---------------------------------------------------------------------------

async def get_org(org_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, name, slug, created_at FROM orgs WHERE id = $1", org_id)
        return dict(row) if row else None


async def find_users_global(query: str, exclude_org_id: Optional[int] = None, limit: int = 5) -> list[dict]:
    """Invite lookup across orgs: exact email or username/display-name prefix.
    Returns the minimum needed to pick a person (no emails unless matched exactly)."""
    if not _pool or not (query or "").strip():
        return []
    q = query.strip()
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT u.id, u.org_id, u.username, u.display_name,
                      CASE WHEN lower(u.email) = lower($1) THEN u.email END AS email,
                      o.name AS org_name
                 FROM users u JOIN orgs o ON o.id = u.org_id
                WHERE ($2::bigint IS NULL OR u.org_id <> $2)
                  AND (lower(u.email) = lower($1) OR lower(u.username) LIKE lower($1) || '%'
                       OR lower(u.display_name) LIKE lower($1) || '%')
                ORDER BY (lower(u.email) = lower($1)) DESC, u.display_name
                LIMIT $3""",
            q, exclude_org_id, limit,
        )
        return [dict(r) for r in rows]


def _grp(r) -> dict:
    d = dict(r)
    if isinstance(d.get("scope"), str):
        try:
            d["scope"] = json.loads(d["scope"])
        except json.JSONDecodeError:
            d["scope"] = {}
    if d.get("key") is not None:
        d["key"] = str(d["key"])
    return d


async def sharing_create_group(org_id: int, client_id: int, user_id: Optional[int], name: str,
                               scope: Optional[dict] = None) -> dict:
    """Create a share group with this org's client as the owner member. The owner
    also monitors by default."""
    if not _pool:
        raise RuntimeError("DB unavailable")
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO shared_clients (name, created_by_org, created_by_user, monitor_org_id, scope)
                   VALUES ($1, $2, $3, $2, COALESCE($4::jsonb, '{"doc_types": ["research","osint","finding","signal"], "profile": true, "sources": true, "contacts": false}'::jsonb))
                   RETURNING *""",
                name, org_id, user_id, scope)
            await conn.execute(
                """INSERT INTO shared_client_members (shared_client_id, org_id, client_id, role, joined_by)
                   VALUES ($1, $2, $3, 'owner', $4)""",
                row["id"], org_id, client_id, user_id)
    return _grp(row)


async def sharing_get_group(group_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM shared_clients WHERE id = $1", group_id)
    return _grp(row) if row else None


async def sharing_group_for_client(client_id: int) -> Optional[dict]:
    """Active share group of a client row (or None)."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT sc.*, scm.role AS my_role, scm.org_id AS member_org_id
                 FROM shared_client_members scm JOIN shared_clients sc ON sc.id = scm.shared_client_id
                WHERE scm.client_id = $1 AND scm.left_at IS NULL AND sc.status = 'active'""",
            client_id)
    return _grp(row) if row else None


async def sharing_list_members(group_id: int) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT scm.*, o.name AS org_name, c.name AS client_name, u.display_name AS joined_by_name
                 FROM shared_client_members scm
                 JOIN orgs o ON o.id = scm.org_id
                 JOIN clients c ON c.id = scm.client_id
                 LEFT JOIN users u ON u.id = scm.joined_by
                WHERE scm.shared_client_id = $1
                ORDER BY scm.joined_at""",
            group_id)
    return [dict(r) for r in rows]


async def sharing_list_for_org(org_id: int) -> list[dict]:
    """All active share groups this org belongs to, with members."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT sc.*, scm.client_id AS my_client_id, scm.role AS my_role
                 FROM shared_client_members scm JOIN shared_clients sc ON sc.id = scm.shared_client_id
                WHERE scm.org_id = $1 AND scm.left_at IS NULL AND sc.status = 'active'
                ORDER BY sc.created_at DESC""",
            org_id)
    out = []
    for r in rows:
        g = _grp(r)
        g["members"] = await sharing_list_members(g["id"])
        out.append(g)
    return out


async def sharing_add_member(group_id: int, org_id: int, client_id: int, user_id: Optional[int],
                             role: str = "member") -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO shared_client_members (shared_client_id, org_id, client_id, role, joined_by)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (shared_client_id, org_id) DO UPDATE
                  SET client_id = EXCLUDED.client_id, joined_by = EXCLUDED.joined_by,
                      joined_at = NOW(), left_at = NULL, role = EXCLUDED.role""",
            group_id, org_id, client_id, role, user_id)


async def sharing_leave(group_id: int, org_id: int) -> dict:
    """Mark the org as left; hand monitoring over; close the group when nobody is left."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE shared_client_members SET left_at = NOW() WHERE shared_client_id = $1 AND org_id = $2 AND left_at IS NULL",
                group_id, org_id)
            remaining = await conn.fetch(
                "SELECT org_id FROM shared_client_members WHERE shared_client_id = $1 AND left_at IS NULL ORDER BY joined_at",
                group_id)
            if not remaining:
                await conn.execute("UPDATE shared_clients SET status = 'closed', updated_at = NOW() WHERE id = $1", group_id)
                return {"closed": True, "monitor_org_id": None}
            g = await conn.fetchrow("SELECT monitor_org_id FROM shared_clients WHERE id = $1", group_id)
            new_monitor = g["monitor_org_id"]
            if new_monitor == org_id or new_monitor not in [r["org_id"] for r in remaining]:
                new_monitor = remaining[0]["org_id"]
                await conn.execute("UPDATE shared_clients SET monitor_org_id = $2, updated_at = NOW() WHERE id = $1",
                                   group_id, new_monitor)
            return {"closed": False, "monitor_org_id": new_monitor}


async def sharing_set_monitor(group_id: int, org_id: int) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE shared_clients SET monitor_org_id = $2, updated_at = NOW() WHERE id = $1",
                           group_id, org_id)


async def sharing_update_scope(group_id: int, scope: dict) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE shared_clients SET scope = $2, updated_at = NOW() WHERE id = $1", group_id, scope)


# --- invites ---------------------------------------------------------------

async def sharing_create_invite(group_id: int, from_org_id: int, from_user_id: Optional[int], *,
                                to_user_id: Optional[int] = None, to_org_id: Optional[int] = None,
                                to_email: Optional[str] = None, message: str = "",
                                to_partner_id: Optional[int] = None) -> dict:
    if not _pool:
        raise RuntimeError("DB unavailable")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO share_invites (shared_client_id, from_org_id, from_user_id, to_org_id, to_user_id, to_email, message, to_partner_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *""",
            group_id, from_org_id, from_user_id, to_org_id, to_user_id, (to_email or None), (message or None), to_partner_id)
    return dict(row)


async def sharing_get_invite(invite_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT i.*, sc.name AS client_name, sc.key AS group_key, sc.scope, sc.status AS group_status,
                      fo.name AS from_org_name, fu.display_name AS from_user_name, tu.display_name AS to_user_name,
                      fp.partner_name AS from_partner_name, fp.partner_mxid AS from_partner_mxid,
                      tp.partner_name AS to_partner_name, tp.partner_mxid AS to_partner_mxid
                 FROM share_invites i
                 JOIN shared_clients sc ON sc.id = i.shared_client_id
                 LEFT JOIN orgs fo ON fo.id = i.from_org_id
                 LEFT JOIN users fu ON fu.id = i.from_user_id
                 LEFT JOIN users tu ON tu.id = i.to_user_id
                 LEFT JOIN federation_partners fp ON fp.id = i.from_partner_id
                 LEFT JOIN federation_partners tp ON tp.id = i.to_partner_id
                WHERE i.id = $1""",
            invite_id)
    return _grp(row) if row else None


async def sharing_list_invites(org_id: int, direction: str = "incoming", status: str = "pending") -> list[dict]:
    if not _pool:
        return []
    col = "i.to_org_id" if direction == "incoming" else "i.from_org_id"
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT i.*, sc.name AS client_name, sc.key AS group_key, sc.scope,
                       fo.name AS from_org_name, fu.display_name AS from_user_name,
                       tou.name AS to_org_name, tu.display_name AS to_user_name,
                       fp.partner_name AS from_partner_name, fp.partner_mxid AS from_partner_mxid,
                       tp.partner_name AS to_partner_name, tp.partner_mxid AS to_partner_mxid
                  FROM share_invites i
                  JOIN shared_clients sc ON sc.id = i.shared_client_id
                  LEFT JOIN orgs fo ON fo.id = i.from_org_id
                  LEFT JOIN orgs tou ON tou.id = i.to_org_id
                  LEFT JOIN users fu ON fu.id = i.from_user_id
                  LEFT JOIN users tu ON tu.id = i.to_user_id
                  LEFT JOIN federation_partners fp ON fp.id = i.from_partner_id
                  LEFT JOIN federation_partners tp ON tp.id = i.to_partner_id
                 WHERE {col} = $1 AND ($2 = 'all' OR i.status = $2)
                 ORDER BY i.created_at DESC LIMIT 200""",
            org_id, status)
    return [_grp(r) for r in rows]


async def sharing_respond_invite(invite_id: int, status: str) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE share_invites SET status = $2, responded_at = NOW() WHERE id = $1", invite_id, status)


# --- outbox + apply (LocalTransport) ----------------------------------------

async def sharing_pending_outbox(limit: int = 200) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM share_outbox WHERE processed_at IS NULL AND attempts < 5 ORDER BY id LIMIT $1", limit)
    return [dict(r) for r in rows]


async def sharing_mark_outbox(outbox_id: int, error: Optional[str]) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        if error is None:
            await conn.execute("UPDATE share_outbox SET processed_at = NOW(), attempts = attempts + 1, error = NULL WHERE id = $1", outbox_id)
        else:
            await conn.execute("UPDATE share_outbox SET attempts = attempts + 1, error = $2 WHERE id = $1", outbox_id, error)


async def sharing_get_document(document_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, org_id, doc_id, type, title, content, metadata, visibility, source,
                      created_at, updated_at, embedding
                 FROM documents WHERE id = $1""",
            document_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("metadata"), str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except json.JSONDecodeError:
            d["metadata"] = {}
    return d


async def sharing_enqueue_client(group_id: int, origin_org_id: int, client_id: int, doc_types: list[str]) -> int:
    """Full sync of one member's client: every shareable linked doc + the profile."""
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        n = await conn.fetchval(
            """WITH ins AS (
                   INSERT INTO share_outbox (shared_client_id, origin_org_id, kind, ref_id, payload)
                   SELECT $1, $2, 'document', d.id, jsonb_build_object('client_id', $3::bigint)
                     FROM document_links dl JOIN documents d ON d.id = dl.document_id
                    WHERE dl.entity_type = 'client' AND dl.entity_id = $3
                      AND d.org_id = $2 AND d.source <> 'shared' AND d.type = ANY($4::text[])
                   RETURNING 1)
               SELECT COUNT(*) FROM ins""",
            group_id, origin_org_id, client_id, doc_types)
        await conn.execute(
            "INSERT INTO share_outbox (shared_client_id, origin_org_id, kind, ref_id) VALUES ($1, $2, 'profile', $3)",
            group_id, origin_org_id, client_id)
    return int(n or 0) + 1


async def sharing_apply_document(target_org_id: int, target_client_id: int, doc: dict, provenance: dict) -> None:
    """Upsert the replicated copy in the member org and link it to their client.
    Runs with buzzowl.sync='on' so the triggers stay silent."""
    if not _pool:
        return
    meta = dict(doc.get("metadata") or {})
    meta["shared_from"] = provenance
    meta.pop("owner_ids", None)
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('buzzowl.sync', 'on', true)")
            row = await conn.fetchrow(
                """INSERT INTO documents (org_id, doc_id, type, title, content, metadata, visibility, source, embedding)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'shared', $8)
                   ON CONFLICT (org_id, doc_id) DO UPDATE
                      SET title = EXCLUDED.title, content = EXCLUDED.content, metadata = EXCLUDED.metadata,
                          type = EXCLUDED.type, embedding = EXCLUDED.embedding, updated_at = NOW()
                   RETURNING id""",
                target_org_id, doc["shared_doc_id"], doc["type"], doc["title"], doc.get("content") or "",
                meta, doc.get("visibility") or "shared", doc.get("embedding"))
            await conn.execute(
                """INSERT INTO document_links (document_id, entity_type, entity_id) VALUES ($1, 'client', $2)
                   ON CONFLICT DO NOTHING""",
                row["id"], target_client_id)


async def sharing_delete_document(target_org_id: int, shared_doc_id_: str) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('buzzowl.sync', 'on', true)")
            await conn.execute("DELETE FROM documents WHERE org_id = $1 AND doc_id = $2 AND source = 'shared'",
                               target_org_id, shared_doc_id_)


async def sharing_apply_profile(target_org_id: int, target_client_id: int, patch: dict, provenance: dict) -> None:
    if not _pool or not patch:
        return
    merged = dict(patch)
    merged["shared_profile_from"] = provenance
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('buzzowl.sync', 'on', true)")
            await conn.execute("UPDATE clients SET metadata = metadata || $3 WHERE org_id = $1 AND id = $2",
                               target_org_id, target_client_id, merged)


async def sharing_leave_cleanup(target_org_id: int, group_key: str, delete_copies: bool) -> int:
    """After leaving: either delete the replicated copies or just flag them as detached."""
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('buzzowl.sync', 'on', true)")
            if delete_copies:
                res = await conn.execute(
                    "DELETE FROM documents WHERE org_id = $1 AND source = 'shared' AND doc_id LIKE $2",
                    target_org_id, f"shared:{group_key}:%")
            else:
                res = await conn.execute(
                    """UPDATE documents SET metadata = metadata || '{"shared_detached": true}'::jsonb
                        WHERE org_id = $1 AND source = 'shared' AND doc_id LIKE $2""",
                    target_org_id, f"shared:{group_key}:%")
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


async def get_user_by_id(user_id: int) -> Optional[dict]:
    """Cross-org lookup by id (used to address share invites); no password hash."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, org_id, username, display_name, email, role FROM users WHERE id = $1", user_id)
        return dict(row) if row else None


async def sharing_non_monitor_client_ids(org_id: int) -> set:
    """Client ids of this org that sit in an active share group where ANOTHER org
    runs the monitoring — heartbeats/monitor skip these (results arrive via sync)."""
    if not _pool:
        return set()
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT scm.client_id
                 FROM shared_client_members scm JOIN shared_clients sc ON sc.id = scm.shared_client_id
                WHERE scm.org_id = $1 AND scm.left_at IS NULL AND sc.status = 'active'
                  AND ((sc.monitor_org_id IS NOT NULL AND sc.monitor_org_id <> $1) OR sc.monitor_partner_id IS NOT NULL)""",
            org_id)
    return {r["client_id"] for r in rows}


# ---------------------------------------------------------------------------
# LLM usage metering (Phase 6a)
# ---------------------------------------------------------------------------

async def record_llm_usage(org_id: int, *, provider: str, model: str, prompt_tokens: int = 0,
                           completion_tokens: int = 0, cost_usd: Optional[float] = None,
                           role: Optional[str] = None, surface: Optional[str] = None,
                           source: str = "python", user_id: Optional[int] = None,
                           agent_run_id: Optional[int] = None) -> None:
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO llm_usage_events (org_id, user_id, surface, role, provider, model,
                        prompt_tokens, completion_tokens, total_tokens, cost_usd, source, agent_run_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                org_id, user_id, surface, role, provider, model, int(prompt_tokens or 0),
                int(completion_tokens or 0), int(prompt_tokens or 0) + int(completion_tokens or 0),
                cost_usd, source, agent_run_id)
    except Exception as exc:
        logger.debug("record_llm_usage failed: %s", exc)


async def llm_usage_month_cost(org_id: int) -> float:
    if not _pool:
        return 0.0
    async with _pool.acquire() as conn:
        v = await conn.fetchval(
            """SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage_events
                WHERE org_id = $1 AND created_at >= date_trunc('month', NOW())""", org_id)
    return float(v or 0)


async def llm_usage_summary(org_id: int, days: int = 31) -> dict:
    """Month-to-date totals + per-day + per-model breakdown for the org's usage view."""
    if not _pool:
        return {"month": {}, "by_day": [], "by_model": []}
    async with _pool.acquire() as conn:
        month = await conn.fetchrow(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                      COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                      COALESCE(SUM(cost_usd),0) AS cost_usd,
                      COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
                 FROM llm_usage_events WHERE org_id = $1 AND created_at >= date_trunc('month', NOW())""", org_id)
        by_day = await conn.fetch(
            """SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS calls,
                      COALESCE(SUM(total_tokens),0) AS tokens, COALESCE(SUM(cost_usd),0) AS cost_usd
                 FROM llm_usage_events WHERE org_id = $1 AND created_at >= NOW() - ($2 || ' days')::interval
                GROUP BY 1 ORDER BY 1""", org_id, str(int(days)))
        by_model = await conn.fetch(
            """SELECT provider, model, source, COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens,
                      COALESCE(SUM(cost_usd),0) AS cost_usd
                 FROM llm_usage_events WHERE org_id = $1 AND created_at >= date_trunc('month', NOW())
                GROUP BY 1,2,3 ORDER BY cost_usd DESC, tokens DESC LIMIT 20""", org_id)
    return {"month": {k: (float(v) if k == "cost_usd" else int(v)) for k, v in dict(month).items()},
            "by_day": [{"day": r["day"].isoformat(), "calls": int(r["calls"]), "tokens": int(r["tokens"]),
                        "cost_usd": float(r["cost_usd"])} for r in by_day],
            "by_model": [{**dict(r), "calls": int(r["calls"]), "tokens": int(r["tokens"]),
                          "cost_usd": float(r["cost_usd"])} for r in by_model]}


# ---------------------------------------------------------------------------
# Matrix federation (Phase 5b) — see federation.py
# ---------------------------------------------------------------------------

async def fed_get_identity(org_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM federation_identities WHERE org_id = $1", org_id)
        return dict(row) if row else None


async def fed_list_identities() -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM federation_identities WHERE status <> 'disabled' ORDER BY org_id")
        return [dict(r) for r in rows]


async def fed_upsert_identity(org_id: int, **fields) -> dict:
    """Create/update the org's bot identity. fields: homeserver_url, mxid, device_id,
    access_token_enc, ed25519, display_name, status, last_error, last_sync_at."""
    if not _pool:
        raise RuntimeError("DB unavailable")
    allowed = {"homeserver_url", "mxid", "device_id", "access_token_enc", "ed25519", "display_name",
               "status", "last_error", "last_sync_at"}
    f = {k: v for k, v in fields.items() if k in allowed}
    async with _pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM federation_identities WHERE org_id = $1", org_id)
        if not exists:
            row = await conn.fetchrow(
                """INSERT INTO federation_identities (org_id, homeserver_url, mxid, device_id, access_token_enc,
                       ed25519, display_name, status, last_error)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,COALESCE($8,'configured'),$9) RETURNING *""",
                org_id, f.get("homeserver_url", ""), f.get("mxid", ""), f.get("device_id"), f.get("access_token_enc"),
                f.get("ed25519"), f.get("display_name"), f.get("status"), f.get("last_error"))
            return dict(row)
        sets, params = [], [org_id]
        for k, v in f.items():
            params.append(v); sets.append(f"{k} = ${len(params)}")
        sets.append("updated_at = NOW()")
        row = await conn.fetchrow(f"UPDATE federation_identities SET {', '.join(sets)} WHERE org_id = $1 RETURNING *", *params)
        return dict(row)


async def fed_delete_identity(org_id: int) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM federation_identities WHERE org_id = $1", org_id)


# --- partners ---------------------------------------------------------------

async def fed_get_partner(partner_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM federation_partners WHERE id = $1", partner_id)
        return dict(row) if row else None


async def fed_partner_by_room(org_id: int, room_id: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM federation_partners WHERE org_id = $1 AND room_id = $2", org_id, room_id)
        return dict(row) if row else None


async def fed_partner_by_mxid(org_id: int, mxid: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM federation_partners WHERE org_id = $1 AND partner_mxid = $2", org_id, mxid)
        return dict(row) if row else None


async def fed_list_partners(org_id: int, statuses: Optional[list] = None) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM federation_partners WHERE org_id = $1
                AND ($2::text[] IS NULL OR status = ANY($2::text[]))
                ORDER BY status = 'active' DESC, created_at DESC""", org_id, statuses)
        return [dict(r) for r in rows]


async def fed_upsert_partner(org_id: int, partner_mxid: str, **fields) -> dict:
    allowed = {"partner_name", "room_id", "direction", "status", "pinned_device_id", "pinned_ed25519",
               "seen_device_id", "seen_ed25519", "verified_at", "verified_by", "last_event_at", "last_error"}
    f = {k: v for k, v in fields.items() if k in allowed}
    if not _pool:
        raise RuntimeError("DB unavailable")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM federation_partners WHERE org_id = $1 AND partner_mxid = $2", org_id, partner_mxid)
        if not row:
            row = await conn.fetchrow(
                """INSERT INTO federation_partners (org_id, partner_mxid, partner_name, room_id, direction, status)
                   VALUES ($1,$2,$3,$4,COALESCE($5,'outgoing'),COALESCE($6,'pending')) RETURNING *""",
                org_id, partner_mxid, f.get("partner_name"), f.get("room_id"), f.get("direction"), f.get("status"))
            return dict(row)
        sets, params = [], [row["id"]]
        for k, v in f.items():
            params.append(v); sets.append(f"{k} = ${len(params)}")
        if not sets:
            return dict(await conn.fetchrow("SELECT * FROM federation_partners WHERE id = $1", row["id"]))
        sets.append("updated_at = NOW()")
        r2 = await conn.fetchrow(f"UPDATE federation_partners SET {', '.join(sets)} WHERE id = $1 RETURNING *", *params)
        return dict(r2)


async def fed_update_partner(partner_id: int, **fields) -> Optional[dict]:
    allowed = {"partner_name", "room_id", "direction", "status", "pinned_device_id", "pinned_ed25519",
               "seen_device_id", "seen_ed25519", "verified_at", "verified_by", "last_event_at", "last_error"}
    f = {k: v for k, v in fields.items() if k in allowed}
    if not _pool or not f:
        return None
    sets, params = [], [partner_id]
    for k, v in f.items():
        params.append(v); sets.append(f"{k} = ${len(params)}")
    sets.append("updated_at = NOW()")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(f"UPDATE federation_partners SET {', '.join(sets)} WHERE id = $1 RETURNING *", *params)
        return dict(row) if row else None


async def fed_delete_partner(partner_id: int) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM federation_partners WHERE id = $1", partner_id)


# --- outbox / inbox ---------------------------------------------------------

async def fed_enqueue(org_id: int, partner_id: int, kind: str, payload: dict) -> int:
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO federation_outbox (org_id, partner_id, kind, payload) VALUES ($1,$2,$3,$4) RETURNING id",
            org_id, partner_id, kind, payload)


async def fed_pending_outbox(org_id: int, limit: int = 50) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT o.*, p.room_id, p.status AS partner_status, p.partner_mxid
                 FROM federation_outbox o JOIN federation_partners p ON p.id = o.partner_id
                WHERE o.org_id = $1 AND o.sent_at IS NULL AND o.attempts < 20
                ORDER BY o.id LIMIT $2""", org_id, limit)
        return [dict(r) for r in rows]


async def fed_mark_outbox(outbox_id: int, *, event_id: Optional[str] = None, error: Optional[str] = None) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        if error is None:
            await conn.execute("UPDATE federation_outbox SET sent_at = NOW(), event_id = $2, attempts = attempts + 1, error = NULL WHERE id = $1",
                               outbox_id, event_id)
        else:
            await conn.execute("UPDATE federation_outbox SET attempts = attempts + 1, error = $2 WHERE id = $1", outbox_id, error[:500])


async def fed_inbox_insert(org_id: int, *, partner_id: Optional[int], room_id: str, event_id: str, sender: str,
                           sender_key: Optional[str], verified: bool, kind: str, payload: dict) -> Optional[int]:
    """Replay-safe: returns None when the event_id was seen before."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO federation_inbox (org_id, partner_id, room_id, event_id, sender, sender_key, verified, kind, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (event_id) DO NOTHING RETURNING id""",
            org_id, partner_id, room_id, event_id, sender, sender_key, verified, kind, payload)


async def fed_pending_inbox(org_id: int, limit: int = 100) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT i.*, p.status AS partner_status, p.partner_name, p.partner_mxid
                 FROM federation_inbox i LEFT JOIN federation_partners p ON p.id = i.partner_id
                WHERE i.org_id = $1 AND i.applied_at IS NULL
                ORDER BY i.id LIMIT $2""", org_id, limit)
        return [dict(r) for r in rows]


async def fed_mark_inbox(inbox_id: int, *, error: Optional[str] = None) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        if error is None:
            await conn.execute("UPDATE federation_inbox SET applied_at = NOW(), error = NULL WHERE id = $1", inbox_id)
        else:
            await conn.execute("UPDATE federation_inbox SET error = $2 WHERE id = $1", inbox_id, error[:500])


async def fed_mark_inbox_verified(partner_id: int) -> int:
    """After pinning: events that arrived from the (now pinned) device become applicable."""
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        res = await conn.execute(
            """UPDATE federation_inbox SET verified = TRUE WHERE partner_id = $1 AND applied_at IS NULL
                AND sender_key = (SELECT pinned_ed25519 FROM federation_partners WHERE id = $1)""", partner_id)
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


# --- remote members / group key ---------------------------------------------

async def sharing_group_by_key(org_id: int, key: str) -> Optional[dict]:
    """The org's local group row for a cross-instance group key (member must be active)."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT sc.*, scm.client_id AS my_client_id, scm.role AS my_role
                 FROM shared_clients sc JOIN shared_client_members scm ON scm.shared_client_id = sc.id
                WHERE sc.key = $2::uuid AND scm.org_id = $1 AND scm.left_at IS NULL""", org_id, key)
    return _grp(row) if row else None


async def sharing_create_group_with_key(org_id: int, client_id: int, user_id: Optional[int], name: str,
                                        key: str, scope: Optional[dict] = None, role: str = "member",
                                        monitor_local: bool = False, monitor_partner_id: Optional[int] = None) -> dict:
    """Local mirror of a group that exists on a partner instance (same key)."""
    if not _pool:
        raise RuntimeError("DB unavailable")
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO shared_clients (key, name, created_by_org, created_by_user, monitor_org_id, monitor_partner_id, scope)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6, COALESCE($7::jsonb, '{"doc_types": ["research","osint","finding","signal"], "profile": true, "sources": true, "contacts": false}'::jsonb))
                   RETURNING *""",
                key, name, org_id, user_id, org_id if monitor_local else None, monitor_partner_id, scope)
            await conn.execute(
                "INSERT INTO shared_client_members (shared_client_id, org_id, client_id, role, joined_by) VALUES ($1,$2,$3,$4,$5)",
                row["id"], org_id, client_id, role, user_id)
    return _grp(row)


async def sharing_list_remote_members(group_id: int) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT rm.*, p.partner_mxid, p.partner_name, p.status AS partner_status, p.org_id AS local_org_id
                 FROM shared_client_remote_members rm JOIN federation_partners p ON p.id = rm.partner_id
                WHERE rm.shared_client_id = $1 ORDER BY rm.joined_at""", group_id)
        return [dict(r) for r in rows]


async def sharing_add_remote_member(group_id: int, partner_id: int, role: str = "member") -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO shared_client_remote_members (shared_client_id, partner_id, role)
               VALUES ($1,$2,$3) ON CONFLICT (shared_client_id, partner_id) DO UPDATE SET left_at = NULL, joined_at = NOW(), role = EXCLUDED.role""",
            group_id, partner_id, role)


async def sharing_remove_remote_member(group_id: int, partner_id: int) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE shared_client_remote_members SET left_at = NOW() WHERE shared_client_id = $1 AND partner_id = $2",
                           group_id, partner_id)


async def sharing_set_monitor_remote(group_id: int, partner_id: Optional[int], org_id: Optional[int]) -> None:
    """Exactly one of partner_id / org_id holds monitoring."""
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE shared_clients SET monitor_partner_id = $2, monitor_org_id = $3, updated_at = NOW() WHERE id = $1",
                           group_id, partner_id, org_id)


async def sharing_create_remote_invite(org_id: int, from_partner_id: int, *, remote_group_key: str, remote_invite_id: Optional[int],
                                       client_name: str, scope: Optional[dict], message: str) -> dict:
    """An invite that arrived from a partner instance: stored as a local pending invite
    (from_org NULL, from_partner set); a placeholder group row is created on accept."""
    if not _pool:
        raise RuntimeError("DB unavailable")
    async with _pool.acquire() as conn:
        # a lightweight local group row so the invite can reference shared_client_id (NOT NULL);
        # it becomes the real mirror on accept (member added then), or stays orphaned/closed on decline.
        grp = await conn.fetchrow(
            """SELECT id FROM shared_clients WHERE key = $1::uuid""", remote_group_key)
        if not grp:
            grp = await conn.fetchrow(
                """INSERT INTO shared_clients (key, name, created_by_org, scope, status)
                   VALUES ($1::uuid, $2, $3, COALESCE($4::jsonb, '{"doc_types": ["research","osint","finding","signal"], "profile": true, "sources": true, "contacts": false}'::jsonb), 'active')
                   RETURNING id""", remote_group_key, client_name, org_id, scope)
        row = await conn.fetchrow(
            """INSERT INTO share_invites (shared_client_id, from_org_id, from_partner_id, to_org_id, message, remote_group_key, remote_invite_id)
               VALUES ($1, NULL, $2, $3, $4, $5::uuid, $6) RETURNING *""",
            grp["id"], from_partner_id, org_id, message or None, remote_group_key, remote_invite_id)
        return dict(row)


async def sharing_group_row_by_key(key: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM shared_clients WHERE key = $1::uuid", key)
    return _grp(row) if row else None


async def sharing_add_member_to_group(group_id: int, org_id: int, client_id: int, user_id: Optional[int], role: str = "member",
                                      monitor_local: bool = False) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO shared_client_members (shared_client_id, org_id, client_id, role, joined_by)
                   VALUES ($1,$2,$3,$4,$5) ON CONFLICT (shared_client_id, org_id) DO UPDATE
                      SET client_id = EXCLUDED.client_id, joined_by = EXCLUDED.joined_by, joined_at = NOW(), left_at = NULL, role = EXCLUDED.role""",
                group_id, org_id, client_id, role, user_id)
            if monitor_local:
                await conn.execute("UPDATE shared_clients SET monitor_org_id = $2, monitor_partner_id = NULL WHERE id = $1", group_id, org_id)


# ---------------------------------------------------------------------------
# Telegram per-user linking (notifications.py)
# ---------------------------------------------------------------------------

async def list_users_with_settings(org_id: int) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, org_id, username, display_name, email, role, settings FROM users WHERE org_id = $1 ORDER BY id", org_id)
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("settings"), str):
                try:
                    d["settings"] = json.loads(d["settings"])
                except json.JSONDecodeError:
                    d["settings"] = {}
            d["settings"] = d.get("settings") or {}
            out.append(d)
        return out


async def get_user_with_settings(user_id: int) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            """SELECT u.id, u.org_id, u.username, u.display_name, u.email, u.role, u.settings, o.name AS org_name
                 FROM users u JOIN orgs o ON o.id = u.org_id WHERE u.id = $1""", user_id)
    if not r:
        return None
    d = dict(r)
    if isinstance(d.get("settings"), str):
        try:
            d["settings"] = json.loads(d["settings"])
        except json.JSONDecodeError:
            d["settings"] = {}
    d["settings"] = d.get("settings") or {}
    return d


async def find_user_by_telegram_chat(chat_id: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            """SELECT u.id, u.org_id, u.username, u.display_name, u.email, u.role, u.settings, o.name AS org_name
                 FROM users u JOIN orgs o ON o.id = u.org_id
                WHERE u.settings->'telegram'->>'chat_id' = $1 LIMIT 1""", str(chat_id))
    return dict(r) if r else None


async def find_user_by_telegram_link_code(code: str) -> Optional[dict]:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        r = await conn.fetchrow(
            """SELECT u.id, u.org_id, u.username, u.display_name, u.email, u.role, u.settings, o.name AS org_name
                 FROM users u JOIN orgs o ON o.id = u.org_id
                WHERE u.settings->'telegram_link'->>'code' = $1 LIMIT 1""", code)
    return dict(r) if r else None


async def patch_user_settings_by_id(user_id: int, patch: dict) -> dict:
    """Shallow-merge into users.settings by user id (org-agnostic; used by the bot)."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET settings = COALESCE(settings, '{}'::jsonb) || $2::jsonb WHERE id = $1 RETURNING settings",
            user_id, patch)
    s = row["settings"] if row else {}
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except json.JSONDecodeError:
            s = {}
    return s or {}


async def remove_user_setting_keys(user_id: int, keys: list[str]) -> None:
    if not _pool or not keys:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE users SET settings = COALESCE(settings, '{}'::jsonb) - $2::text[] WHERE id = $1",
                           user_id, keys)
