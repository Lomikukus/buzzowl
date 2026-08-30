"""
routers/llm_config.py — LLM provider status, admin config editor, and OAuth
connect flows (OpenRouter PKCE + Pi subscription-login proxy).

Extracted from routers/transcription.py (WP1, no behavior change):
  GET  /api/llm/status                       (+ legacy alias GET /api/ollama)
  GET  /api/llm/config
  POST /api/llm/config
  POST /api/llm/oauth/openrouter/start
  POST /api/llm/oauth/openrouter/complete
  GET  /api/llm/oauth/pi/status
  POST /api/llm/oauth/pi/start
  POST /api/llm/oauth/pi/complete
  POST /api/llm/oauth/pi/disconnect
"""

import os
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException

import llm
import plans
from context import BASE_DIR, DB_AVAILABLE, config, db_module
from routers.auth import current_user

router = APIRouter()


@router.get("/api/llm/status")          # canonical route
@router.get("/api/ollama")              # legacy route kept for the UI
async def llm_provider_status(user: dict = Depends(current_user)):
    """Generalized LLM provider status (was the Ollama-only /api/ollama probe).

    Back-compat: knowledge.html and product.html populate model dropdowns from
    `models` (+ `default_model`), so those keys are kept — filled from the
    configured llm role models instead of the live Ollama tag list.
    """
    try:
        providers = llm.status()
    except Exception:
        providers = []
    ok = any(p.get("reachable") or p.get("has_key") for p in providers)

    default_model = ""
    try:
        _, default_model = llm.resolve("summary")
    except Exception:
        pass

    models: list[str] = []
    role_names = sorted({r for p in providers for r in p.get("roles", [])})
    for role_name in role_names:
        try:
            _, m = llm.resolve(role_name)
        except Exception:
            continue
        # Exclude embedding models — they can't generate chat responses
        if m and "embed" not in m.lower() and m not in models:
            models.append(m)

    return {
        "providers": providers,
        "ok": ok,
        "status": "ok" if ok else "offline",
        "models": models,
        "default_model": default_model,
    }


# ---------------------------------------------------------------------------
# LLM provider configuration (admin) — edits the llm: block in config.yaml
# ---------------------------------------------------------------------------

_CONFIG_YAML_PATH = BASE_DIR / "config.yaml"
_KEY_MASK = "•••"
_LLM_KINDS = ("openai-compat", "anthropic", "pi")


def _masked_llm_block() -> dict:
    """Current effective llm block with inline api_key values masked."""
    block = llm._effective_config()
    providers = {}
    for name, raw in (block.get("providers") or {}).items():
        entry = {k: v for k, v in raw.items() if k != "api_key"}
        try:
            # Full resolution chain (inline > env var > legacy env fallbacks)
            entry["has_key"] = bool(llm._get_provider(name).resolve_key())
        except llm.LLMError:
            entry["has_key"] = False
        if raw.get("api_key"):
            entry["api_key"] = _KEY_MASK
        providers[name] = entry
    return {"providers": providers, "roles": dict(block.get("roles") or {}),
            "explicit": isinstance(config.get("llm"), dict)}


@router.get("/api/llm/config")
async def get_llm_config(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return _masked_llm_block()


@router.post("/api/llm/config")
async def save_llm_config(body: dict, user: dict = Depends(current_user)):
    """Replace the llm: block. api_key semantics: empty or masked value on a
    provider keeps the existing inline key; a real value overwrites it. Keys
    are never echoed back (responses mask them)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not os.access(_CONFIG_YAML_PATH, os.W_OK):
        # Standard docker-compose.yml mounts config.yaml :ro — this endpoint can
        # never actually persist there. Workspace admins use the DB-backed
        # per-org override instead (routers/org_settings.py GET/POST /api/org/llm).
        raise HTTPException(
            status_code=403,
            detail="config.yaml is read-only on this install — workspace LLM settings "
                   "are stored in the database instead (Settings › LLM Providers)")

    providers = body.get("providers")
    roles = body.get("roles")
    if not isinstance(providers, dict) or not providers:
        raise HTTPException(status_code=400, detail="providers must be a non-empty object")
    if not isinstance(roles, dict) or not roles:
        raise HTTPException(status_code=400, detail="roles must be a non-empty object")

    existing = (config.get("llm") or {}).get("providers", {}) if isinstance(config.get("llm"), dict) else {}
    clean_providers: dict = {}
    for name, raw in providers.items():
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"provider {name!r} must be an object")
        kind = raw.get("kind", "openai-compat")
        if kind not in _LLM_KINDS:
            raise HTTPException(status_code=400, detail=f"provider {name!r}: unknown kind {kind!r}")
        base_url = (raw.get("base_url") or "").strip()
        if kind == "openai-compat" and not base_url:
            raise HTTPException(status_code=400, detail=f"provider {name!r}: base_url required for openai-compat")
        entry: dict = {"kind": kind}
        if base_url:
            entry["base_url"] = base_url
        api_key_env = (raw.get("api_key_env") or "").strip()
        if api_key_env:
            entry["api_key_env"] = api_key_env
        api_key = raw.get("api_key") or ""
        if api_key and api_key != _KEY_MASK:
            entry["api_key"] = api_key
        elif existing.get(name, {}).get("api_key"):
            entry["api_key"] = existing[name]["api_key"]      # keep stored key
        headers_raw = raw.get("headers")
        if headers_raw is not None:
            if not isinstance(headers_raw, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in headers_raw.items()):
                raise HTTPException(status_code=400,
                                    detail=f"provider {name!r}: headers must be an object of string to string")
            if headers_raw:
                entry["headers"] = headers_raw
        elif existing.get(name, {}).get("headers"):
            entry["headers"] = existing[name]["headers"]      # keep stored headers (e.g. pi_provider bridge)
        clean_providers[name] = entry

    clean_roles: dict = {}
    for role_name, entry in roles.items():
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"role {role_name!r} must be an object")
        provider_name = entry.get("provider", "")
        model = (entry.get("model") or "").strip()
        if provider_name not in clean_providers:
            raise HTTPException(status_code=400,
                                detail=f"role {role_name!r} references unknown provider {provider_name!r}")
        if not model:
            raise HTTPException(status_code=400, detail=f"role {role_name!r}: model required")
        clean_roles[role_name] = {"provider": provider_name, "model": model}
    if "default" not in clean_roles:
        raise HTTPException(status_code=400, detail="a 'default' role is required")

    new_block = {"providers": clean_providers, "roles": clean_roles}

    # Persist to config.yaml (whole-file rewrite — comments are lost, accepted)
    try:
        raw_cfg = yaml.safe_load(_CONFIG_YAML_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw_cfg = {}
    raw_cfg["llm"] = new_block
    _CONFIG_YAML_PATH.write_text(
        yaml.safe_dump(raw_cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    config["llm"] = new_block   # live config is mutated in place, never replaced
    return {"ok": True, **_masked_llm_block()}


# ---------------------------------------------------------------------------
# OpenRouter OAuth (PKCE) — "Connect" flow that provisions a user-controlled
# API key (no copy-paste). The only fully-permitted subscription-style login;
# the provisioned key is stored at the ORG level (routers/org_settings.py
# POST /api/org/llm's persistence idiom), not in config.yaml — config.yaml is
# read-only in the standard docker-compose deployment.
# ---------------------------------------------------------------------------

_OR_AUTH_URL = "https://openrouter.ai/auth"
_OR_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
_or_pending: dict = {}   # user_id → (code_verifier, expires_monotonic)


@router.post("/api/llm/oauth/openrouter/start")
async def openrouter_oauth_start(body: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    import base64
    import hashlib
    import secrets
    import time as _time
    callback_url = (body.get("callback_url") or "").strip()
    if not callback_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="callback_url required")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _or_pending[user["id"]] = (verifier, _time.monotonic() + 600)
    auth_url = (f"{_OR_AUTH_URL}?callback_url={callback_url}"
                f"&code_challenge={challenge}&code_challenge_method=S256")
    return {"auth_url": auth_url}


@router.post("/api/llm/oauth/openrouter/complete")
async def openrouter_oauth_complete(body: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    import time as _time
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    pending = _or_pending.pop(user["id"], None)
    if not pending or pending[1] < _time.monotonic():
        raise HTTPException(status_code=400, detail="No pending connect — start again")
    verifier = pending[0]

    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.post(_OR_KEYS_URL, json={
                "code": code, "code_verifier": verifier,
                "code_challenge_method": "S256",
            })
            r.raise_for_status()
            key = (r.json() or {}).get("key", "")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"OpenRouter key exchange failed: {exc}")
    if not key:
        raise HTTPException(status_code=502, detail="OpenRouter returned no key")

    # Persist the provisioned key at the ORG level — DB-backed, encrypted at
    # rest, and unaffected by a read-only config.yaml mount. Reuses the exact
    # helpers POST /api/org/llm is built on (routers/org_settings.set_org_llm).
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable — cannot store the connected key")
    org_id = user["org_id"]
    settings = await db_module.get_org_settings(org_id)
    existing_llm = settings.get("llm") or {}

    # merge_org_llm() returns 'incoming' as the new roles verbatim (it only
    # back-fills preserved api_keys on the providers side) — so an org that
    # already has roles must have its own roles carried through here, or they
    # would be wiped by this connect-only save.
    new_roles = dict(existing_llm.get("roles") or {})
    if not new_roles:
        # First LLM setup for this org — point 'default' at the new provider so
        # a connect-only flow is immediately usable, no separate role save needed.
        platform_roles = (llm._effective_config().get("roles") or {})
        default_model = (platform_roles.get("default") or {}).get("model") or llm._FALLBACK_MODEL
        new_roles = {"default": {"provider": "openrouter", "model": default_model}}

    new_block = {
        "providers": {"openrouter": {"kind": "openai-compat",
                                     "base_url": "https://openrouter.ai/api/v1",
                                     "api_key": key}},
        "roles": new_roles,
    }
    incoming = plans.sanitize_org_llm(new_block)
    merged = plans.merge_org_llm(existing_llm, incoming)
    await db_module.update_org_settings(org_id, {"llm": merged})
    llm.invalidate_org_overlay(org_id)
    return {"ok": True, "connected": "openrouter", **plans.public_org_llm(merged)}


# ---------------------------------------------------------------------------
# Subscription-login proxy → Pi service /oauth/* (ChatGPT-Codex, GitHub
# Copilot). Gray-zone flows: only exposed when llm_oauth_gray_flows is true
# (neither is officially sanctioned for third-party apps without a whitelist;
# Anthropic's flow is banned outright and never exposed).
# ---------------------------------------------------------------------------

_GRAY_OAUTH_PROVIDERS = ("openai-codex", "github-copilot")


async def _pi_oauth_forward(method: str, path: str, payload: Optional[dict],
                            user: dict) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    provider = (payload or {}).get("provider", "")
    if provider in _GRAY_OAUTH_PROVIDERS and not config.get("llm_oauth_gray_flows"):
        raise HTTPException(
            status_code=403,
            detail="Subscription logins are disabled — set llm_oauth_gray_flows: true "
                   "in config.yaml after reviewing the provider's terms of service.")
    token = config.get("agent_service_token", "")
    if not token and os.environ.get("ALLOW_INSECURE_INTERNAL", "") not in ("1", "true"):
        raise HTTPException(
            status_code=503,
            detail="AGENT_SERVICE_TOKEN is not configured — the agent service refuses all "
                   "requests. Set the same value for server and agent-pi in .env, then run "
                   "`docker compose up -d` (a plain restart does not reload .env).")

    import httpx
    base = config.get("agent_service_url_pi") or config.get("agent_service_url", "http://localhost:8001")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=70) as hc:
            if method == "GET":
                r = await hc.get(f"{base}{path}", headers=headers)
            else:
                r = await hc.post(f"{base}{path}", json=payload or {}, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Pi service unreachable: {exc}")
    body = r.json() if r.content else {}
    if r.status_code == 401:
        raise HTTPException(
            status_code=502,
            detail="The agent service rejected the shared token — server and agent-pi disagree "
                   "on AGENT_SERVICE_TOKEN. Align the value in .env and run `docker compose up -d`.")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code,
                            detail=body.get("error") or body.get("detail") or "Pi OAuth error")
    return body


@router.get("/api/llm/oauth/pi/status")
async def pi_oauth_status(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    enabled = bool(config.get("llm_oauth_gray_flows"))
    providers = {}
    if enabled:
        try:
            providers = await _pi_oauth_forward("GET", "/oauth/status", None, user)
        except HTTPException:
            providers = {}
    return {"enabled": enabled, "providers": providers}


@router.post("/api/llm/oauth/pi/start")
async def pi_oauth_start(body: dict, user: dict = Depends(current_user)):
    return await _pi_oauth_forward("POST", "/oauth/start", body, user)


@router.post("/api/llm/oauth/pi/complete")
async def pi_oauth_complete(body: dict, user: dict = Depends(current_user)):
    return await _pi_oauth_forward("POST", "/oauth/complete", body, user)


@router.post("/api/llm/oauth/pi/disconnect")
async def pi_oauth_disconnect(body: dict, user: dict = Depends(current_user)):
    return await _pi_oauth_forward("POST", "/oauth/disconnect", body, user)
