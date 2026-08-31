"""
routers/org_settings.py — per-org settings (autonomy level, budgets, kill
switch) + the autonomy audit surface.

  PATCH /api/org                    (admin)          rename the org
  POST  /api/org/setup              (admin)          mark the first-run wizard done
  GET  /api/org/settings            (member: read)   effective settings
  POST /api/org/settings            (admin)          shallow-merge patch
  GET  /api/org/autonomy/status     (member)         level, budget used/max, kill switch
  GET  /api/org/autonomy/decisions  (member)         recent decision log (skips + actions)
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

import autonomy
from context import DB_AVAILABLE, db_module
from routers.auth import current_user

router = APIRouter(prefix="/api/org")

_ALLOWED_KEYS = set(autonomy.DEFAULT_SETTINGS)


# ---------------------------------------------------------------------------
# Org profile + first-run setup wizard completion flag
# ---------------------------------------------------------------------------

@router.patch("")
async def update_org(body: dict, user: dict = Depends(current_user)):
    """Rename the org (admin only). Used by Settings and the setup wizard."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    name = str((body or {}).get("name") or "").strip()
    if not name or len(name) > 120:
        raise HTTPException(status_code=400, detail="name must be 1-120 characters")
    org = await db_module.update_org_name(user["org_id"], name)
    return {"ok": True, "org": org}


@router.post("/setup")
async def complete_setup(body: dict, user: dict = Depends(current_user)):
    """Mark the first-run wizard done (admin only) — Finish and Skip both call
    this the same way; there is no partial/half-done state to track."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if not (body or {}).get("completed"):
        raise HTTPException(status_code=400, detail="completed must be true")
    await db_module.update_org_settings(
        user["org_id"], {"setup_completed_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


@router.get("/settings")
async def get_settings(user: dict = Depends(current_user)):
    return await autonomy.settings(user["org_id"])


@router.post("/settings")
async def save_settings(body: dict, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    patch: dict = {}
    for k, v in (body or {}).items():
        if k not in _ALLOWED_KEYS:
            raise HTTPException(status_code=400, detail=f"unknown setting {k!r}")
        if k == "autonomy_level":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="autonomy_level must be 0-3")
            if not 0 <= v <= 3:
                raise HTTPException(status_code=400, detail="autonomy_level must be 0-3")
        elif k in ("max_autonomous_runs_per_day", "cooldown_hours",
                   "outreach_max_per_day", "outreach_contact_floor_days"):
            try:
                v = float(v) if k == "cooldown_hours" else int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{k} must be a number")
            if v < 0:
                raise HTTPException(status_code=400, detail=f"{k} must be >= 0")
        elif k in ("kill_switch", "outreach_enabled", "outreach_kill_switch"):
            v = bool(v)
        elif k == "outreach_quiet_hours":
            try:
                v = [int(v[0]), int(v[1])]
            except (TypeError, ValueError, IndexError):
                raise HTTPException(status_code=400, detail="outreach_quiet_hours must be [start_hour, end_hour]")
            if not (0 <= v[0] <= 24 and 0 <= v[1] <= 24):
                raise HTTPException(status_code=400, detail="quiet hours must be 0-24")
        patch[k] = v
    if not patch:
        raise HTTPException(status_code=400, detail="empty patch")
    await db_module.update_org_settings(user["org_id"], patch)
    return await autonomy.settings(user["org_id"])


@router.get("/autonomy/status")
async def autonomy_status(user: dict = Depends(current_user)):
    org_id = user["org_id"]
    s = await autonomy.settings(org_id)
    budget = await autonomy.check_budget(org_id)
    return {
        "level": int(s.get("autonomy_level", 0)),
        "level_name": autonomy.LEVEL_NAMES.get(int(s.get("autonomy_level", 0)), "off"),
        "kill_switch": bool(s.get("kill_switch")),
        "used_today": budget.used_today,
        "max_per_day": budget.max_per_day or s.get("max_autonomous_runs_per_day"),
        "cooldown_hours": s.get("cooldown_hours"),
        "budget_ok": budget.ok,
        "budget_reason": budget.reason,
        "summary": autonomy.describe(s),
    }


@router.get("/autonomy/decisions")
async def autonomy_decisions(limit: int = 50, user: dict = Depends(current_user)):
    if not DB_AVAILABLE or db_module is None:
        return {"decisions": []}
    rows = await db_module.list_autonomy_decisions(user["org_id"], limit=min(max(limit, 1), 200))
    return {"decisions": rows}


# ---------------------------------------------------------------------------
# Plans, per-org LLM providers, usage (Phase 6a hosted)
# ---------------------------------------------------------------------------

import os as _os

import llm as _llm
import plans as _plans
from context import config as _config


def _hosted() -> dict:
    return (_config or {}).get("hosted") or {}


def _operator_key() -> str:
    return _hosted().get("operator_key") or _os.environ.get("HOSTED_OPERATOR_KEY", "")


def _operator_key_matches(request_headers) -> bool:
    key = _operator_key()
    return bool(key) and request_headers.get("x-operator-key", "") == key


def _operator_ok(request_headers) -> bool:
    """Budget-change gate (unchanged semantics): in hosted mode (signup enabled)
    a budget edit needs the operator key (config hosted.operator_key or env
    HOSTED_OPERATOR_KEY); on a self-hosted install the org admin decides.

    The `plan` field itself is gated separately (see set_plan) — premium is
    an upsell on self-hosted installs, so switching plan always needs the
    operator key, in both hosted and self-hosted mode."""
    if not _hosted().get("signup_enabled"):
        return True
    return _operator_key_matches(request_headers)


@router.get("/plan")
async def get_plan(user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    settings = await db_module.get_org_settings(user["org_id"])
    usage = await db_module.llm_usage_summary(user["org_id"], days=31)
    budget = _plans.budget_usd(settings, _config)
    month_cost = float((usage.get("month") or {}).get("cost_usd") or 0)
    own = ((settings.get("llm") or {}).get("providers") or {})
    # Stored keys that no longer decrypt (encryption key changed) count as absent.
    broken = sorted(n for n, p in own.items()
                    if p.get("api_key") and not _plans.key_readable(p.get("api_key", "")))
    hosted = _hosted()
    return {
        "plan": _plans.plan_of(settings),
        "plans": list(_plans.PLANS),
        "budget_usd": budget,
        "month_cost_usd": round(month_cost, 4),
        "budget_used_pct": round(100 * month_cost / budget, 1) if budget else None,
        "enforce_plans": _plans.enforce_plans(_config),
        "hosted_mode": bool(hosted.get("signup_enabled")),
        # Self-hosted installs sell premium as an upsell link, not a self-service
        # switch (see set_plan) — the frontend uses these two to decide whether
        # to render the switch (hosted) or the "✦ Premium" link (self-hosted).
        "self_hosted": not hosted.get("signup_enabled"),
        "premium_url": hosted.get("premium_url") or "https://buzzowl.app",
        "has_own_providers": bool(own) and len(broken) < len(own),
        "keys_need_reconnect": broken,
        "usage": usage,
    }


@router.post("/plan")
async def set_plan(body: dict, request: Request, user: dict = Depends(current_user)):
    """Budget edits stay self-service for a self-hosted admin (no operator key
    needed — see _operator_ok). Changing `plan` itself is a different story:
    premium is the hosted offering at buzzowl.app, so on every install —
    hosted or self-hosted — it always needs the operator key. The operator
    control-plane route (routers/operator.py POST /orgs/{id}/plan) is the
    real self-service-free path for that; this endpoint just refuses cleanly
    with an upsell instead of quietly flipping the plan for free."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    patch: dict = {}
    if "plan" in body:
        if not _operator_key():
            raise HTTPException(
                status_code=403,
                detail="Self-service plan changes are disabled on this install. "
                       "Premium is available at https://buzzowl.app")
        if not _operator_key_matches(request.headers):
            raise HTTPException(status_code=403, detail="a valid x-operator-key header is required to change plan")
        p = str(body.get("plan") or "").lower()
        if p not in _plans.PLANS:
            raise HTTPException(status_code=400, detail="plan must be light|premium")
        patch["plan"] = p
    if "llm_budget_usd_per_month" in body:
        if not _operator_ok(request.headers):
            raise HTTPException(status_code=403, detail="budget changes are done by the operator on this deployment")
        v = body.get("llm_budget_usd_per_month")
        if v in (None, ""):
            patch["llm_budget_usd_per_month"] = None
        else:
            try:
                patch["llm_budget_usd_per_month"] = max(0.0, float(v))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="budget must be a number")
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to change")
    await db_module.update_org_settings(user["org_id"], patch)
    _llm.invalidate_org_overlay(user["org_id"])
    return await get_plan(user)


@router.get("/llm")
async def get_org_llm(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    settings = await db_module.get_org_settings(user["org_id"])
    return {"llm": _plans.public_org_llm(settings.get("llm") or {}),
            "platform_roles": list(((_config.get("llm") or {}).get("roles") or {}).keys())}


@router.post("/llm")
async def set_org_llm(body: dict, user: dict = Depends(current_user)):
    """Store this org's own providers/roles (keys encrypted at rest). Empty/masked
    api_key keeps the stored key. Body: {providers: {...}, roles: {...}}"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    settings = await db_module.get_org_settings(user["org_id"])
    incoming = _plans.sanitize_org_llm(body or {})
    merged = _plans.merge_org_llm(settings.get("llm") or {}, incoming)
    await db_module.update_org_settings(user["org_id"], {"llm": merged})
    _llm.invalidate_org_overlay(user["org_id"])
    return {"ok": True, "llm": _plans.public_org_llm(merged)}


@router.delete("/llm")
async def clear_org_llm(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    await db_module.update_org_settings(user["org_id"], {"llm": {"providers": {}, "roles": {}}})
    _llm.invalidate_org_overlay(user["org_id"])
    return {"ok": True}


@router.post("/llm/test")
async def test_org_llm(body: dict, user: dict = Depends(current_user)):
    """Round-trip a tiny completion through the org's effective provider for a role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    role = str((body or {}).get("role") or "default")
    await _llm.ensure_org_overlay(user["org_id"], force=True)
    try:
        provider, model = _llm.resolve(role, None, user["org_id"])
        text = await _llm.acomplete("Reply with the single word OK.", role=role, org_id=user["org_id"],
                                    max_tokens=5, timeout=30, surface="llm_test")
        return {"ok": True, "provider": provider.name, "model": model, "reply": text[:40]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


# ---------------------------------------------------------------------------
# Subscription routing (ChatGPT-Codex / GitHub Copilot)
#
# The OAuth credential itself lives in agent-pi, not here — connecting is
# routers/llm_config.py's /api/llm/oauth/pi/*. What was missing is the other
# half: *choosing* to route this workspace's chat through it. That choice
# cannot be an entry in the org's llm block, because plans.sanitize_org_llm
# rejects kind 'pi' outright ("platform-only"), and rightly so: on a hosted
# multi-tenant install one org must not be able to spend the deployment's
# personal subscription. So it is stored as its own org setting and only
# offered when this install is self-hosted — one tenant, one owner, their own
# agent-pi. Hosted installs keep the old behaviour (config.yaml decides).
# ---------------------------------------------------------------------------

_SUB_PROVIDERS = ("openai-codex", "github-copilot")


def _subscriptions_offerable() -> tuple[bool, str]:
    """(offerable, reason_when_not) — same two gates the UI shows."""
    if _hosted().get("signup_enabled"):
        return False, ("Subscription logins are a deployment-level setting on a hosted "
                       "install — the operator configures them, not a workspace.")
    if not _config.get("llm_oauth_gray_flows"):
        return False, ("Subscription logins are disabled — set llm_oauth_gray_flows: true "
                       "in config.yaml after reviewing the provider's terms of service.")
    return True, ""


def _stored_subscription(settings: dict) -> dict:
    sub = settings.get("llm_subscription")
    return sub if isinstance(sub, dict) else {}


@router.get("/llm/subscription")
async def get_org_subscription(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    settings = await db_module.get_org_settings(user["org_id"])
    sub = _stored_subscription(settings)
    offerable, reason = _subscriptions_offerable()
    return {"provider": sub.get("provider") or None,
            "model": sub.get("model") or "",
            "offerable": offerable,
            "reason": reason}


@router.post("/llm/subscription")
async def set_org_subscription(body: dict, user: dict = Depends(current_user)):
    """Route this workspace's chat through a connected subscription.

    Body: {provider: 'openai-codex'|'github-copilot', model: '<model id>'} —
    or {provider: null} to clear it and fall back to the configured providers.
    The subscription must already be connected in agent-pi; this refuses
    otherwise rather than storing a choice that would fail on first use."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    offerable, reason = _subscriptions_offerable()
    if not offerable:
        raise HTTPException(status_code=403, detail=reason)

    provider = ((body or {}).get("provider") or "").strip() or None
    if provider is None:
        await db_module.update_org_settings(user["org_id"], {"llm_subscription": {}})
        return {"ok": True, "provider": None, "model": ""}
    if provider not in _SUB_PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"provider must be one of {', '.join(_SUB_PROVIDERS)}")
    model = str((body or {}).get("model") or "").strip()[:120]
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    # Refuse a choice that cannot work yet. _pi_oauth_forward carries the
    # gray-flow gate, the missing-token 503 and the token-mismatch 502 with
    # their actionable messages, so a failure here already reads as advice.
    from routers.llm_config import _pi_oauth_forward
    status = await _pi_oauth_forward("GET", "/oauth/status", {"provider": provider}, user)
    if not ((status or {}).get(provider) or {}).get("connected"):
        raise HTTPException(status_code=400,
                            detail=f"{provider} is not connected yet — complete the login first.")

    # Prove the chosen model actually runs on this account before storing it.
    # agent-pi builds the model from the id we send (agent.ts buildOAuthModel),
    # so a plan that cannot drive it fails at generation time — better here than
    # on the user's first chat message.
    probe = await _pi_oauth_forward("POST", "/complete", {
        "provider": provider,
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        # Roomy enough that a reasoning model still has budget left for text.
        "max_tokens": 256,
    }, user)
    if not (probe or {}).get("text"):
        # An id the account does not have comes back as HTTP 200 with an empty
        # body and zero token usage rather than an error, which is exactly what
        # this catches. The picker offers the account's own list, so this is
        # the rare case of a model that vanished between listing and use.
        raise HTTPException(status_code=400,
                            detail=f"{provider} returned nothing for model '{model}' — your account "
                                   "cannot drive it. Reload the page to refresh the model list.")

    await db_module.update_org_settings(
        user["org_id"], {"llm_subscription": {"provider": provider, "model": model}})
    return {"ok": True, "provider": provider, "model": model}


@router.get("/usage")
async def get_usage(days: int = 31, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    return await db_module.llm_usage_summary(user["org_id"], days=min(max(days, 1), 366))
