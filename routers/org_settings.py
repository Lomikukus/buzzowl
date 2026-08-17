"""
routers/org_settings.py — per-org settings (autonomy level, budgets, kill
switch) + the autonomy audit surface.

  GET  /api/org/settings            (member: read)   effective settings
  POST /api/org/settings            (admin)          shallow-merge patch
  GET  /api/org/autonomy/status     (member)         level, budget used/max, kill switch
  GET  /api/org/autonomy/decisions  (member)         recent decision log (skips + actions)
"""

from fastapi import APIRouter, Depends, HTTPException

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
