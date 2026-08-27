"""
routers/operator.py — hosting hooks for a control plane (Phase 6b).

Billing-agnostic. A separate, private control plane (website + Stripe + user
management) provisions tenants, sets plans when subscriptions change, hands
users into their workspace, and suspends lapsed accounts — through this API.
Nothing here knows about Stripe.

Auth: header `X-Operator-Key` == config hosted.operator_key (or env
HOSTED_OPERATOR_KEY). Fail closed: no key configured → every call is 401.

  GET    /api/operator/orgs                         list tenants (plan, suspended, users, month cost)
  POST   /api/operator/orgs                         create tenant: {name, slug?, admin_email, admin_name?, plan?,
                                                    llm_budget_usd_per_month?, external_ref?} → org + login token
  GET    /api/operator/orgs/{id}                    detail + usage
  POST   /api/operator/orgs/{id}/plan               {plan?, llm_budget_usd_per_month?, external_ref?}
  POST   /api/operator/orgs/{id}/suspend            {reason?}   writes are refused (402) while suspended
  POST   /api/operator/orgs/{id}/resume
  POST   /api/operator/orgs/{id}/login-token        {email?} → 30-day session token for the (admin) user → SSO
  GET    /api/operator/orgs/{id}/usage?days=31
  DELETE /api/operator/orgs/{id}                    hard delete (data of the tenant is gone) — needs {"confirm": slug}
"""

import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

import llm
import plans
from context import DB_AVAILABLE, config, db_module
from routers.auth import _limit

logger = logging.getLogger("wk.operator")
router = APIRouter(prefix="/api/operator")

# Tighter than the app-wide default: X-Operator-Key is a single shared secret
# guarding tenant creation, deletion and SSO login tokens, so every endpoint
# here doubles as a brute-force oracle. A real control plane makes a handful of
# calls per minute; 30 leaves plenty of headroom.
_OPERATOR_RATE = "30/minute"


def _hosted() -> dict:
    return (config or {}).get("hosted") or {}


def _check_key(request: Request) -> None:
    key = _hosted().get("operator_key") or os.environ.get("HOSTED_OPERATOR_KEY", "")
    if not key:
        raise HTTPException(status_code=401, detail="operator API disabled: no operator key configured")
    if not secrets.compare_digest(request.headers.get("x-operator-key", ""), key):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="DB unavailable")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "org"


async def _org_view(org: dict, with_usage: bool = False) -> dict:
    settings = await db_module.get_org_settings(org["id"])
    users = await db_module.list_users(org["id"])
    own_llm = ((settings.get("llm") or {}).get("providers") or {})
    # Keys stored under a previous encryption key are unusable — report them as
    # missing so a rotation shows up here instead of as failing agent runs.
    orphaned = [n for n, p in own_llm.items()
                if p.get("api_key") and not plans.key_readable(p.get("api_key", ""))]
    out = {
        "id": org["id"], "name": org["name"], "slug": org["slug"], "created_at": org.get("created_at"),
        "plan": plans.plan_of(settings), "suspended": bool(settings.get("suspended")),
        "suspended_reason": settings.get("suspended_reason"),
        "llm_budget_usd_per_month": settings.get("llm_budget_usd_per_month"),
        "external_ref": settings.get("external_ref"),          # the control plane's id (e.g. a Stripe customer)
        "has_own_llm": bool(own_llm) and len(orphaned) < len(own_llm),
        "llm_keys_need_reconnect": sorted(orphaned),
        "users": [{"id": u["id"], "username": u["username"], "email": u.get("email"), "role": u["role"]} for u in users],
    }
    if with_usage:
        out["usage"] = await db_module.llm_usage_summary(org["id"], days=31)
        out["month_cost_usd"] = float((out["usage"].get("month") or {}).get("cost_usd") or 0)
    else:
        try:
            out["month_cost_usd"] = await db_module.llm_usage_month_cost(org["id"])
        except Exception:
            out["month_cost_usd"] = None
    return out


async def _get_org(org_id: int) -> dict:
    org = await db_module.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="org not found")
    return org


@router.get("/orgs")
@_limit(_OPERATOR_RATE)
async def list_orgs(request: Request):
    _check_key(request)
    rows = await db_module.list_orgs()
    return {"orgs": [await _org_view(o) for o in rows]}


@router.post("/orgs")
@_limit(_OPERATOR_RATE)
async def create_org(body: dict, request: Request):
    """Provision a tenant for a customer: org + admin user + plan. Returns a login
    token so the control plane can drop the user straight into the workspace."""
    _check_key(request)
    name = (body.get("name") or "").strip()[:120]
    email = (body.get("admin_email") or "").strip().lower()
    if not name or not email or "@" not in email:
        raise HTTPException(status_code=400, detail="name and admin_email are required")
    slug = _slug(body.get("slug") or name)
    base, n = slug, 2
    while await db_module.get_org_by_slug(slug):
        slug = f"{base}-{n}"; n += 1
    plan = str(body.get("plan") or _hosted().get("default_plan") or plans.PLAN_LIGHT).lower()
    if plan not in plans.PLANS:
        raise HTTPException(status_code=400, detail="plan must be light|premium")

    from routers.auth import pwd_context
    org = await db_module.create_org(name, slug)
    try:
        await db_module.seed_default_heartbeats(org["id"])
    except Exception as exc:
        logger.warning("seed heartbeats failed for org %s: %s", org["id"], exc)
    username = _slug(email.split("@")[0]) or "admin"
    display = (body.get("admin_name") or email.split("@")[0]).strip()[:120]
    # A password the user never sees — they arrive via SSO/login token; they can set one later.
    user = await db_module.create_user(org_id=org["id"], username=username, display_name=display,
                                       password_hash=pwd_context.hash(secrets.token_urlsafe(24)), email=email, role="admin")
    settings = {"plan": plan, "signup": "operator", "provisioned_at": datetime.now(timezone.utc).isoformat()}
    if body.get("llm_budget_usd_per_month") not in (None, ""):
        settings["llm_budget_usd_per_month"] = float(body["llm_budget_usd_per_month"])
    if body.get("external_ref"):
        settings["external_ref"] = str(body["external_ref"])[:120]
    await db_module.update_org_settings(org["id"], settings)
    token = secrets.token_urlsafe(32)
    await db_module.create_session_token(user["id"], token, datetime.now(timezone.utc) + timedelta(days=30))
    logger.info("operator: provisioned org %s (%s) plan=%s admin=%s", org["id"], slug, plan, email)
    return {"ok": True, "org": await _org_view(org), "admin_user_id": user["id"], "login_token": token,
            "login_path": f"/login#token={token}"}


@router.get("/orgs/{org_id}")
@_limit(_OPERATOR_RATE)
async def get_org(org_id: int, request: Request):
    _check_key(request)
    return await _org_view(await _get_org(org_id), with_usage=True)


@router.post("/orgs/{org_id}/plan")
@_limit(_OPERATOR_RATE)
async def set_plan(org_id: int, body: dict, request: Request):
    _check_key(request)
    await _get_org(org_id)
    patch: dict = {}
    if "plan" in body:
        p = str(body.get("plan") or "").lower()
        if p not in plans.PLANS:
            raise HTTPException(status_code=400, detail="plan must be light|premium")
        patch["plan"] = p
    if "llm_budget_usd_per_month" in body:
        v = body.get("llm_budget_usd_per_month")
        patch["llm_budget_usd_per_month"] = None if v in (None, "") else max(0.0, float(v))
    if "external_ref" in body:
        patch["external_ref"] = str(body.get("external_ref") or "")[:120] or None
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to change")
    await db_module.update_org_settings(org_id, patch)
    llm.invalidate_org_overlay(org_id)
    return {"ok": True, "org": await _org_view(await _get_org(org_id))}


@router.post("/orgs/{org_id}/suspend")
@_limit(_OPERATOR_RATE)
async def suspend(org_id: int, body: dict, request: Request):
    """Subscription lapsed: the workspace becomes read-only (writes → 402) and
    agents/heartbeats skip it. Data is kept."""
    _check_key(request)
    await _get_org(org_id)
    await db_module.update_org_settings(org_id, {"suspended": True, "suspended_reason": str((body or {}).get("reason") or "")[:200],
                                                 "suspended_at": datetime.now(timezone.utc).isoformat()})
    _invalidate_suspension_cache(org_id)
    return {"ok": True, "org": await _org_view(await _get_org(org_id))}


@router.post("/orgs/{org_id}/resume")
@_limit(_OPERATOR_RATE)
async def resume(org_id: int, request: Request):
    _check_key(request)
    await _get_org(org_id)
    await db_module.update_org_settings(org_id, {"suspended": False, "suspended_reason": None})
    _invalidate_suspension_cache(org_id)
    return {"ok": True, "org": await _org_view(await _get_org(org_id))}


@router.post("/orgs/{org_id}/login-token")
@_limit(_OPERATOR_RATE)
async def login_token(org_id: int, body: dict, request: Request):
    """SSO hand-off: a session token for a user of the tenant (by email; default: the
    first admin). The control plane redirects the browser to /login#token=…"""
    _check_key(request)
    await _get_org(org_id)
    users = await db_module.list_users(org_id)
    email = (body.get("email") or "").strip().lower()
    user = next((u for u in users if (u.get("email") or "").lower() == email), None) if email else \
        next((u for u in users if u["role"] == "admin"), None) or (users[0] if users else None)
    if not user:
        raise HTTPException(status_code=404, detail="no such user in this org")
    token = secrets.token_urlsafe(32)
    days = int(body.get("days") or 30)
    await db_module.create_session_token(user["id"], token, datetime.now(timezone.utc) + timedelta(days=max(1, min(days, 90))))
    return {"ok": True, "user_id": user["id"], "login_token": token, "login_path": f"/login#token={token}"}


@router.get("/orgs/{org_id}/usage")
@_limit(_OPERATOR_RATE)
async def usage(org_id: int, request: Request, days: int = 31):
    _check_key(request)
    await _get_org(org_id)
    return await db_module.llm_usage_summary(org_id, days=min(max(days, 1), 366))


@router.delete("/orgs/{org_id}")
@_limit(_OPERATOR_RATE)
async def delete_org(org_id: int, body: dict, request: Request):
    _check_key(request)
    org = await _get_org(org_id)
    if (body or {}).get("confirm") != org["slug"]:
        raise HTTPException(status_code=400, detail="confirm must equal the org slug")
    async with db_module._pool.acquire() as conn:
        await conn.execute("DELETE FROM orgs WHERE id = $1", org_id)     # cascades everywhere
    _invalidate_suspension_cache(org_id)
    logger.warning("operator: DELETED org %s (%s)", org_id, org["slug"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Suspension check used by routers.auth.current_user (cached 30 s per org)
# ---------------------------------------------------------------------------

_susp_cache: dict = {}


def _invalidate_suspension_cache(org_id: int) -> None:
    _susp_cache.pop(org_id, None)


async def is_suspended(org_id: int) -> bool:
    import time
    hit = _susp_cache.get(org_id)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    try:
        s = await db_module.get_org_settings(org_id)
        val = bool(s.get("suspended"))
    except Exception:
        val = False
    _susp_cache[org_id] = (now + 30.0, val)
    return val
