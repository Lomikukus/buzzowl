"""
Shared singletons for the Buzzowl server.

Every other module imports from here. This module has no project-level imports
so there are no circular dependencies.

Exports:
  config            — live config dict (mutated by /api/settings, not replaced)
  BASE_DIR          — project root Path
  SAMPLE_RATE       — 16000 Hz, constant
  console           — Rich console for coloured logging
  executor          — ThreadPoolExecutor for CPU-bound work
  pwd_context       — bcrypt context for password hashing
  _model_cache      — shared model cache (keyed "live:name" / "post:name" / etc.)
  _model_lock       — Lock protecting _model_cache
  _metadata_lock    — Lock protecting session metadata JSON files
  DB_AVAILABLE      — True if asyncpg/db.py loaded successfully
  db_module         — db module reference (None if unavailable)
  SCHEDULER_AVAILABLE — True if apscheduler installed
  _scheduler        — APScheduler instance, set at startup (may be None)
  _default_org_id() — async helper: returns first org id (single-tenant fallback)
  limiter           — slowapi Limiter carrying the app-wide default rate limit
  RATE_LIMIT_DEFAULT — that default, e.g. "300/minute" (env: RATE_LIMIT_DEFAULT)
  RateLimitMiddleware — middleware that applies it (None if slowapi is absent)
  configure_rate_limits(app) — call after include_router(): caches the route
                      table and exempts the agent-callback/health endpoints
"""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from passlib.context import CryptContext
from rich.console import Console

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000
BASE_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Shared singletons
# ---------------------------------------------------------------------------

console = Console()

# bcrypt context — used only in auth.py but kept here to avoid a separate module
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# WhisperX / faster-whisper model cache.
# Keys: "live:{name}", "post:{name}", "align:{lang_code}", "diarize"
_model_cache: dict = {}
_model_lock = threading.Lock()

# Prevents concurrent JSON reads/writes to the same staged session directory
_metadata_lock = threading.Lock()

# Active browser WebSocket connections — used to broadcast live text from the Mac app
_live_ws_connections: dict = {}   # WebSocket -> org_id (multi-tenant: broadcasts are org-scoped)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into `base` (overlay wins on conflicts)."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    """Load config.yaml (+ optional config.local.yaml overlay) and .env.

    Sensible defaults are applied for every key so callers can always use
    config.get("key") without KeyError.
    """
    load_dotenv(BASE_DIR / ".env")
    defaults: dict = {
        "live_model":     "base",
        "model":          "large-v2",
        "compute_type":   "int8",
        "hf_token":       "",
        "ollama_model":   "llama3.2",
        "vault_path":     "",
    }
    config_path = BASE_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            loaded = yaml.safe_load(f) or {}
        defaults.update(loaded)
    # Optional untracked overlay: keep machine-specific choices (models, local
    # experiments) out of the tracked config.yaml. Nested dicts merge, so a local
    # file can override a single llm role without repeating the whole block.
    local_path = BASE_DIR / os.environ.get("CONFIG_LOCAL", "config.local.yaml")
    if local_path.exists():
        with open(local_path) as f:
            overlay = yaml.safe_load(f) or {}
        _deep_merge(defaults, overlay)
    # Prefer .env HFTOKEN over config.yaml when the yaml value is blank
    if not defaults.get("hf_token"):
        defaults["hf_token"] = os.environ.get("HFTOKEN", "")
    # Prefer .env AGENT_SERVICE_TOKEN over config.yaml when the yaml value is blank
    if not defaults.get("agent_service_token"):
        defaults["agent_service_token"] = os.environ.get("AGENT_SERVICE_TOKEN", "")
    # Env var overrides for Docker deployments — set these in docker-compose, not config.yaml
    for _cfg_key, _env_var in [
        ("db_url",                   "DATABASE_URL"),
        ("agent_service_url_pi",     "AGENT_PI_URL"),
        ("searxng_url",              "SEARXNG_URL"),
        ("server_url",               "SERVER_URL"),
        ("transcription_mode",       "TRANSCRIPTION_MODE"),
        ("embed_backend",            "EMBED_BACKEND"),
        ("embed_url",                "EMBED_URL"),
        ("embed_api_key",            "EMBED_API_KEY"),
        ("embed_model",              "EMBED_MODEL"),
        ("smtp_host",                "SMTP_HOST"),
        ("smtp_port",                "SMTP_PORT"),
        ("smtp_user",                "SMTP_USER"),
        ("smtp_pass",                "SMTP_PASS"),
        ("smtp_from",                "SMTP_FROM"),
        ("smtp_from_name",           "SMTP_FROM_NAME"),
        ("imap_host",                "IMAP_HOST"),
        ("imap_port",                "IMAP_PORT"),
        ("imap_user",                "IMAP_USER"),
        ("imap_pass",                "IMAP_PASS"),
        ("imap_folder",              "IMAP_FOLDER"),
        ("openrouter_api_key",       "OPENROUTER_API_KEY"),
        ("anthropic_api_key",        "ANTHROPIC_API_KEY"),
    ]:
        _val = os.environ.get(_env_var)
        if _val:
            defaults[_cfg_key] = _val
    return defaults


# Live config dict — mutated in place by /api/settings; never replaced.
config: dict = load_config()

# Thread pool: transcription (CPU-bound) and vault writes run here.
# 2 workers caused queueing stalls when a post-pass and an export overlapped;
# overridable via executor_workers in config.yaml.
executor = ThreadPoolExecutor(max_workers=int(config.get("executor_workers", 8)))


# ---------------------------------------------------------------------------
# TTL micro-cache for hot read endpoints (clients/people/activity/products).
# 15s of staleness is invisible to users but absorbs the burst of identical
# queries fired on every page navigation. Writes call cache_clear().
# ---------------------------------------------------------------------------

_ttl_cache: dict = {}


def cache_get(key):
    entry = _ttl_cache.get(key)
    if entry is None:
        return None
    expires, value = entry
    if time.monotonic() > expires:
        _ttl_cache.pop(key, None)
        return None
    return value


def cache_set(key, value, ttl: float = 15.0) -> None:
    if len(_ttl_cache) > 4096:   # safety bound — never grows unchecked (many orgs share it)
        _ttl_cache.clear()
    _ttl_cache[key] = (time.monotonic() + ttl, value)


def cache_clear(org_id=None) -> None:
    """Invalidate cached reads. With org_id only that org's entries go (keys are
    tuples that contain the org id) — one tenant's write must not evict every
    other tenant's hot cache. Without org_id: everything (legacy/admin)."""
    if org_id is None:
        _ttl_cache.clear()
        return
    for k in [k for k in _ttl_cache if isinstance(k, tuple) and org_id in k]:
        _ttl_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Database layer (optional — graceful degradation)
# ---------------------------------------------------------------------------

try:
    import db as db_module   # type: ignore
    DB_AVAILABLE = True
except ImportError:
    db_module = None          # type: ignore
    DB_AVAILABLE = False


async def _default_org_id() -> Optional[int]:
    """Return the first org's id — single-tenant fallback until UI auth lands.

    Most background tasks (pipeline sweep, heartbeats) use this so they can
    proceed without a logged-in user.
    """
    if not DB_AVAILABLE or db_module is None:
        return None
    org = await db_module.get_first_org()
    return org["id"] if org else None


# ---------------------------------------------------------------------------
# APScheduler (optional)
# ---------------------------------------------------------------------------

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
    _scheduler: Optional[AsyncIOScheduler] = None
    SCHEDULER_AVAILABLE = True
except ImportError:
    _scheduler = None
    SCHEDULER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Rate limiter (slowapi) — shared singleton imported by server.py and routers
# ---------------------------------------------------------------------------

# App-wide default limit. Applies to EVERY route that carries no explicit
# @limiter.limit(...) decorator — which is 250+ of them (search, CSV import /
# export, user + invite listings, the operator API, …). Without it slowapi only
# guards the handful of decorated endpoints.
#
# Deliberately generous: the SPA fires many small requests per screen and polls
# a few endpoints every 3–15s, so this is a runaway-client / scraper / brute
# force backstop, not a UX throttle. Buckets are per (client IP, view function)
# — see key_style below — so 300/minute is ~5 req/s to one endpoint from one IP.
#
# Override at deploy time with RATE_LIMIT_DEFAULT (any slowapi rate string).
RATE_LIMIT_DEFAULT = (os.environ.get("RATE_LIMIT_DEFAULT") or "").strip() or "300/minute"

# Routes the default limit must never touch — all machine-to-machine, all
# token-authenticated, all bursty by design:
#   /api/internal/*          — action callbacks from the Pi agent service.
#   /api/agents/internal/*   — Pi asking for a child run mid-reasoning.
#   /api/agents/callback     — Pi's run-completion push; one research sweep
#                              fires a burst of these from a single container
#                              IP, so throttling it rate-limits the system
#                              against itself.
#   /api/health              — polled by uptime monitors on a tight interval.
# WebSocket routes need no entry (the middleware only sees http scopes) and
# neither does /static (a Mount has no .endpoint, so slowapi skips it).
RATE_LIMIT_EXEMPT_PREFIXES = ("/api/internal", "/api/agents/internal")
RATE_LIMIT_EXEMPT_PATHS = ("/api/health", "/api/agents/callback")


def _rate_limits_enabled() -> bool:
    """Whether the limiter enforces anything at all.

    Off under pytest: slowapi's counters are process-global and TestClient
    presents every request as the same client IP, so a suite that exercises one
    endpoint dozens of times would flake against the default limit. Explicit
    env override wins either way — tests/test_rate_limit.py switches it on.
    """
    override = (os.environ.get("RATE_LIMIT_ENABLED") or "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return "pytest" not in sys.modules


try:
    from slowapi import Limiter                  # type: ignore
    from slowapi.util import get_remote_address  # type: ignore
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[RATE_LIMIT_DEFAULT],
        # Bucket per view function rather than per URL, so /api/x/{id} cannot be
        # turned into unlimited buckets by varying the path parameter. No-op for
        # the existing decorated endpoints — they all sit on fixed paths.
        key_style="endpoint",
        enabled=_rate_limits_enabled(),
    )
    RATE_LIMIT_AVAILABLE = True
except ImportError:
    limiter = None          # type: ignore
    RATE_LIMIT_AVAILABLE = False


def iter_effective_routes(app) -> list:
    """Flat list of the routes an app actually serves.

    FastAPI >= 0.141 includes routers lazily: `app.routes` holds one opaque
    `_IncludedRouter` per `include_router()` call instead of the child routes.
    Anything that walks `app.routes` to work out which handler will serve a
    request therefore sees only the routes declared on the app object itself
    (27 here, out of 294) — and slowapi's middleware, which does exactly that,
    reads "no handler found" as "exempt". Left alone, the app-wide default
    limit would silently apply to nothing but the static HTML pages.

    `fastapi.routing.iter_route_contexts` flattens the tree back out with the
    include prefixes applied; each context proxies `.path`, `.endpoint` and
    `.matches()` through to the effective route, so the result is a drop-in
    for a route list. Older FastAPI keeps `app.routes` flat already.
    """
    routes = list(getattr(app, "routes", []))
    try:
        from fastapi.routing import iter_route_contexts  # type: ignore
    except ImportError:
        return routes
    return list(iter_route_contexts(routes))


def configure_rate_limits(app, lim=None) -> list:
    """Prepare `app` for the default rate limit. Call once, after every router
    is included (routes are fixed from then on).

    Two jobs:
      1. cache the flattened route table on `app.state` for the middleware;
      2. register the machine-to-machine / monitoring exemptions. slowapi
         tracks those by "module.function" name, so the exempt paths have to be
         resolved to their endpoint functions here.

    Endpoints carrying an explicit @limiter.limit(...) are left alone — their
    decorator overrides the default on its own.

    Returns the list of exempted route names (logged at startup, asserted in
    tests/test_rate_limit.py).
    """
    lim = limiter if lim is None else lim
    routes = iter_effective_routes(app)
    app.state.rate_limit_routes = routes
    if lim is None:
        return []

    exempted: list = []
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        path = getattr(route, "path", "") or ""
        if endpoint is None or not path:
            continue
        if path in RATE_LIMIT_EXEMPT_PATHS or path.startswith(RATE_LIMIT_EXEMPT_PREFIXES):
            # Called for the side effect (registers the name in the limiter's
            # exempt set); the wrapper it returns is deliberately discarded —
            # the route is already bound to the original function.
            lim.exempt(endpoint)
            exempted.append(f"{endpoint.__module__}.{endpoint.__name__}")
    return exempted


if RATE_LIMIT_AVAILABLE:
    try:
        from slowapi.middleware import (  # type: ignore
            SlowAPIMiddleware,
            _find_route_handler,
            _should_exempt,
            sync_check_limits,
        )

        class RateLimitMiddleware(SlowAPIMiddleware):
            """SlowAPIMiddleware that resolves handlers from the flattened route
            table (see iter_effective_routes) instead of raw `app.routes`.

            Behaviour is otherwise identical to the upstream middleware: routes
            with their own @limiter.limit(...) are left to that decorator,
            exempt routes pass straight through, everything else gets the
            app-wide default.
            """

            async def dispatch(self, request, call_next):
                app = request.app
                lim = app.state.limiter
                if not lim.enabled:
                    return await call_next(request)

                routes = getattr(app.state, "rate_limit_routes", None)
                if routes is None:                      # configure_rate_limits() not run
                    routes = iter_effective_routes(app)
                    app.state.rate_limit_routes = routes

                handler = _find_route_handler(routes, request.scope)
                if _should_exempt(lim, handler):
                    return await call_next(request)

                error_response, inject_headers = sync_check_limits(lim, request, handler, app)
                if error_response is not None:
                    return error_response

                response = await call_next(request)
                if inject_headers:
                    response = lim._inject_headers(response, request.state.view_rate_limit)
                return response

    except ImportError:  # pragma: no cover — slowapi moved its internals
        from slowapi.middleware import SlowAPIMiddleware as RateLimitMiddleware  # type: ignore
else:  # pragma: no cover — slowapi not installed
    RateLimitMiddleware = None  # type: ignore
