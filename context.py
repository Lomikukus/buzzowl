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
"""

import os
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
_live_ws_connections: set = set()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.yaml from the project root, merge with .env, return as dict.

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
    if len(_ttl_cache) > 256:   # safety bound — never grows unchecked
        _ttl_cache.clear()
    _ttl_cache[key] = (time.monotonic() + ttl, value)


def cache_clear() -> None:
    _ttl_cache.clear()


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

try:
    from slowapi import Limiter                  # type: ignore
    from slowapi.util import get_remote_address  # type: ignore
    limiter = Limiter(key_func=get_remote_address)
    RATE_LIMIT_AVAILABLE = True
except ImportError:
    limiter = None          # type: ignore
    RATE_LIMIT_AVAILABLE = False
