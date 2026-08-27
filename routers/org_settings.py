"""
routers/org_settings.py — per-org settings (autonomy level, budgets, kill
switch) + the autonomy audit surface.

  GET  /api/org/settings            (member: read)   effective settings
  POST /api/org/settings            (admin)          shallow-merge patch
  GET  /api/org/autonomy/status     (member)         level, budget used/max, kill switch
  GET  /api/org/autonomy/decisions  (member)         recent decision log (skips + actions)
"""

from fastapi import APIRouter, Depends, HTTPException, Request

import autonomy
from context import DB_AVAILABLE, db_module
from routers.auth import current_user

router = APIRouter(prefix="/api/org")

_ALLOWED_KEYS = set(autonomy.DEFAULT_SETTINGS)


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


def _operator_ok(request_headers) -> bool:
    """Plan/budget changes are billing events. In hosted mode (signup enabled) they
    need the operator key (config hosted.operator_key or env HOSTED_OPERATOR_KEY);
    on a self-hosted install the org admin decides."""
    if not _hosted().get("signup_enabled"):
        return True
    key = _hosted().get("operator_key") or _os.environ.get("HOSTED_OPERATOR_KEY", "")
    return bool(key) and request_headers.get("x-operator-key", "") == key


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
    return {
        "plan": _plans.plan_of(settings),
        "plans": list(_plans.PLANS),
        "budget_usd": budget,
        "month_cost_usd": round(month_cost, 4),
        "budget_used_pct": round(100 * month_cost / budget, 1) if budget else None,
        "enforce_plans": _plans.enforce_plans(_config),
        "hosted_mode": bool(_hosted().get("signup_enabled")),
        "has_own_providers": bool(own) and len(broken) < len(own),
        "keys_need_reconnect": broken,
        "usage": usage,
    }


@router.post("/plan")
async def set_plan(body: dict, request: Request, user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not _operator_ok(request.headers):
        raise HTTPException(status_code=403, detail="plan changes are done by the operator on this deployment")
    patch: dict = {}
    if "plan" in body:
        p = str(body.get("plan") or "").lower()
        if p not in _plans.PLANS:
            raise HTTPException(status_code=400, detail="plan must be light|premium")
        patch["plan"] = p
    if "llm_budget_usd_per_month" in body:
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


@router.get("/usage")
async def get_usage(days: int = 31, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")
    return await db_module.llm_usage_summary(user["org_id"], days=min(max(days, 1), 366))
