"""
plans.py — hosted plans, per-org LLM overrides, key encryption, usage pricing (Phase 6a).

Plans
-----
  light    the org brings its own LLM: providers/keys live in orgs.settings.llm
           (encrypted at rest). No platform spend.
  premium  the org uses the platform's providers from config.yaml; usage is
           metered and capped by a monthly USD budget (soft block).

Self-hosted installs (hosted.enforce_plans false, the default) never block:
an org without its own providers simply falls back to config.yaml.

Keys at rest: AES-256-GCM with a key derived from BUZZOWL_SECRET_KEY (env),
falling back to the agent_service_token — the same derivation the Pi service
uses (agent_service_ts/src/secrets.ts) so both sides can read one org's keys
without any key ever travelling over HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

PLAN_LIGHT, PLAN_PREMIUM = "light", "premium"
PLANS = (PLAN_LIGHT, PLAN_PREMIUM)
DEFAULT_PREMIUM_BUDGET_USD = 20.0

_ENC_PREFIX = "enc:v1:"

# USD per 1M tokens (input, output) — rough public list prices, override per
# install with config `llm_prices: {"<model>": [in, out]}`. Unknown model → cost
# NULL (tokens are still counted).
DEFAULT_PRICES = {
    "gpt-4o": (2.5, 10.0), "gpt-4o-mini": (0.15, 0.60), "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1-nano": (0.10, 0.40), "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0), "o3": (2.0, 8.0), "o4-mini": (1.1, 4.4),
    "claude-sonnet-4": (3.0, 15.0), "claude-sonnet-4-5": (3.0, 15.0), "claude-opus-4": (15.0, 75.0),
    "claude-haiku-4-5": (0.80, 4.0), "claude-3-5-haiku": (0.80, 4.0),
    "deepseek-chat": (0.27, 1.10), "deepseek-v3": (0.27, 1.10), "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-r1": (0.55, 2.19), "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.0),
    "llama-3.3-70b-instruct": (0.10, 0.30), "qwen3": (0.10, 0.30),
}


# ---------------------------------------------------------------------------
# key encryption
# ---------------------------------------------------------------------------

def _secret_material() -> bytes:
    raw = os.environ.get("BUZZOWL_SECRET_KEY", "")
    if not raw:
        try:
            import context
            raw = (context.config or {}).get("agent_service_token", "") or ""
        except Exception:
            raw = ""
    if not raw:
        raw = "buzzowl-insecure-dev-key"   # never used in a hardened install (token is required)
    return hashlib.sha256(raw.encode()).digest()   # 32 bytes → AES-256


def encrypt_secret(plain: str) -> str:
    """'sk-…' → 'enc:v1:<b64url(nonce|ct|tag)>' (AES-256-GCM). Idempotent on already-encrypted input."""
    if not plain or plain.startswith(_ENC_PREFIX):
        return plain or ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_secret_material()).encrypt(nonce, plain.encode(), None)
    return _ENC_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")


def decrypt_secret(token: str) -> str:
    if not token or not token.startswith(_ENC_PREFIX):
        return token or ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    b = token[len(_ENC_PREFIX):]
    b += "=" * (-len(b) % 4)
    raw = base64.urlsafe_b64decode(b)
    return AESGCM(_secret_material()).decrypt(raw[:12], raw[12:], None).decode()


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_ENC_PREFIX)


def mask_key(value: str) -> str:
    """For API responses: never return a usable key."""
    if not value:
        return ""
    if is_encrypted(value):
        return "••••••••(stored)"
    return value[:3] + "…" + value[-4:] if len(value) > 10 else "••••"


# ---------------------------------------------------------------------------
# plan + settings helpers (pure)
# ---------------------------------------------------------------------------

def plan_of(settings: dict) -> str:
    p = str((settings or {}).get("plan") or PLAN_LIGHT).lower()
    return p if p in PLANS else PLAN_LIGHT


def budget_usd(settings: dict, config: dict) -> Optional[float]:
    """Monthly USD cap for platform-paid usage (premium). None = unlimited."""
    s = settings or {}
    if s.get("llm_budget_usd_per_month") not in (None, ""):
        try:
            return float(s["llm_budget_usd_per_month"])
        except (TypeError, ValueError):
            pass
    hosted = (config or {}).get("hosted") or {}
    if plan_of(s) == PLAN_PREMIUM:
        try:
            return float(hosted.get("premium_monthly_budget_usd", DEFAULT_PREMIUM_BUDGET_USD))
        except (TypeError, ValueError):
            return DEFAULT_PREMIUM_BUDGET_USD
    return None


def enforce_plans(config: dict) -> bool:
    return bool(((config or {}).get("hosted") or {}).get("enforce_plans"))


def sanitize_org_llm(block: dict) -> dict:
    """Validate + encrypt an org's llm override before storing:
    {providers: {name: {kind, base_url, api_key, headers}}, roles: {role: {provider, model}}}."""
    out = {"providers": {}, "roles": {}}
    provs = (block or {}).get("providers") or {}
    for name, raw in provs.items():
        if not isinstance(raw, dict):
            continue
        n = str(name).strip()[:40]
        if not n:
            continue
        kind = str(raw.get("kind") or "openai-compat")
        if kind not in ("openai-compat", "anthropic"):
            continue   # 'pi' bridge is platform-only
        key = str(raw.get("api_key") or "")
        out["providers"][n] = {
            "kind": kind,
            "base_url": str(raw.get("base_url") or "").rstrip("/")[:300],
            "api_key": encrypt_secret(key) if key else "",
            "headers": {str(k)[:60]: str(v)[:300] for k, v in (raw.get("headers") or {}).items()} if isinstance(raw.get("headers"), dict) else {},
        }
    for role, raw in ((block or {}).get("roles") or {}).items():
        if isinstance(raw, dict) and raw.get("provider"):
            out["roles"][str(role)[:40]] = {"provider": str(raw["provider"])[:40], "model": str(raw.get("model") or "")[:120]}
    return out


def merge_org_llm(existing: dict, incoming: dict) -> dict:
    """Keep stored (encrypted) keys when the client sends an empty/masked api_key."""
    ex_p = ((existing or {}).get("providers") or {})
    for name, p in (incoming.get("providers") or {}).items():
        if not p.get("api_key") and name in ex_p and ex_p[name].get("api_key"):
            p["api_key"] = ex_p[name]["api_key"]
    return incoming


def public_org_llm(block: dict) -> dict:
    """Read view: keys masked, roles as-is."""
    provs = {}
    for name, p in ((block or {}).get("providers") or {}).items():
        provs[name] = {**p, "api_key": mask_key(p.get("api_key", "")), "has_key": bool(p.get("api_key"))}
    return {"providers": provs, "roles": dict((block or {}).get("roles") or {})}


def overlay_for_llm(block: dict) -> dict:
    """Runtime view for llm.resolve: keys decrypted (never leaves the process)."""
    provs = {}
    for name, p in ((block or {}).get("providers") or {}).items():
        provs[name] = {**p, "api_key": decrypt_secret(p.get("api_key", ""))}
    return {"providers": provs, "roles": dict((block or {}).get("roles") or {})}


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

def price_for(model: str, config: Optional[dict] = None) -> Optional[tuple]:
    m = (model or "").lower()
    if ":free" in m:
        return (0.0, 0.0)
    table = dict(DEFAULT_PRICES)
    for k, v in ((config or {}).get("llm_prices") or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 2:
            table[str(k).lower()] = (float(v[0]), float(v[1]))
    if m in table:
        return table[m]
    # strip vendor prefix "openai/gpt-4o-mini" and date suffixes "…-2025-08-01"
    base = m.split("/")[-1]
    for k in sorted(table, key=len, reverse=True):
        if base == k or base.startswith(k + "-") or base.startswith(k + ":"):
            return table[k]
    return None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, config: Optional[dict] = None) -> Optional[float]:
    p = price_for(model, config)
    if p is None:
        return None
    return round((prompt_tokens or 0) / 1e6 * p[0] + (completion_tokens or 0) / 1e6 * p[1], 6)
