"""
routers/deals.py — CRM pipeline (Phase 4).

  GET    /api/deals?status=&stage=&client_id=&mine=1     list (board data)
  POST   /api/deals                                     create
  GET    /api/deals/stages                              stage definitions
  GET    /api/deals/summary?mine=1                      per-stage counts + totals
  POST   /api/deals/import-legacy                       one-time: clients.metadata.deal_* → deals (admin)
  GET    /api/deals/{id}                                one deal + events
  PATCH  /api/deals/{id}                                edit fields (value, name, owner, close date, …)
  POST   /api/deals/{id}/stage    {"stage": "...", "note": ""}   stage transition (writes deal_events)
  DELETE /api/deals/{id}                                (admin)
  GET    /api/clients/{client_id}/timeline               unified activity timeline
"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

import deals as dl
from context import DB_AVAILABLE, db_module
from routers.auth import current_user

router = APIRouter()


def _require_db():
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")


def _parse_date(v) -> Optional[date]:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="expected_close must be YYYY-MM-DD")


def _view(d: dict) -> dict:
    d = dict(d)
    d["weighted_value"] = dl.weighted_value(d.get("value"), d.get("probability"), d.get("stage") or "")
    d["stage_label"] = (dl.stage_info(d.get("stage") or "") or {}).get("label", d.get("stage"))
    return d


# ---------------------------------------------------------------------------

@router.get("/api/deals/stages")
async def get_stages(user: dict = Depends(current_user)):
    return {"stages": dl.stages()}


@router.get("/api/deals/summary")
async def deals_summary(mine: int = 0, user: dict = Depends(current_user)):
    _require_db()
    rows = await db_module.pipeline_summary(user["org_id"], user["id"] if mine else None)
    by = {r["stage"]: r for r in rows}
    out = []
    for s in dl.stages():
        if s["status"] != dl.STATUS_OPEN:
            continue
        r = by.get(s["id"], {"count": 0, "total": 0.0, "weighted": 0.0})
        out.append({"stage": s["id"], "label": s["label"], **{k: r[k] for k in ("count", "total", "weighted")}})
    return {"stages": out, "total": sum(x["total"] for x in out),
            "weighted": sum(x["weighted"] for x in out), "count": sum(x["count"] for x in out)}


@router.get("/api/deals")
async def list_deals(status: Optional[str] = None, stage: Optional[str] = None,
                     client_id: Optional[int] = None, mine: int = 0, limit: int = 500,
                     user: dict = Depends(current_user)):
    _require_db()
    if status and status not in (dl.STATUS_OPEN, dl.STATUS_WON, dl.STATUS_LOST):
        raise HTTPException(status_code=400, detail="status must be open|won|lost")
    rows = await db_module.list_deals(user["org_id"], status=status, stage=stage, client_id=client_id,
                                      owner_user_id=user["id"] if mine else None, limit=min(max(limit, 1), 2000))
    return {"deals": [_view(d) for d in rows]}


@router.post("/api/deals")
async def create_deal(body: dict, user: dict = Depends(current_user)):
    _require_db()
    client_id = body.get("client_id")
    client_name = (body.get("client_name") or body.get("client") or "").strip()
    if not client_id and client_name:
        c = await db_module.get_client(user["org_id"], client_name)
        if not c:
            raise HTTPException(status_code=404, detail=f"client {client_name!r} not found")
        client_id = c["id"]
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id or client_name required")
    name = (body.get("name") or "").strip() or f"Deal — {client_name or client_id}"
    try:
        stage = dl.validate_stage(body.get("stage") or "lead")
    except dl.DealError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    value = dl.parse_value(body.get("value")) if body.get("value") not in (None, "") else None
    prob = body.get("probability")
    if prob not in (None, ""):
        try:
            prob = int(prob)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="probability must be 0-100")
        if not 0 <= prob <= 100:
            raise HTTPException(status_code=400, detail="probability must be 0-100")
    else:
        prob = None
    row = await db_module.create_deal(
        user["org_id"], int(client_id), name, stage=stage, value=value,
        currency=(body.get("currency") or "EUR").upper()[:3], probability=prob,
        expected_close=_parse_date(body.get("expected_close")),
        owner_user_id=body.get("owner_user_id") or user["id"],
        status=dl.status_for_stage(stage), metadata=body.get("metadata") or {},
        created_by=user["id"])
    return _view(row)


@router.get("/api/deals/{deal_id}")
async def get_deal(deal_id: int, user: dict = Depends(current_user)):
    _require_db()
    d = await db_module.get_deal(user["org_id"], deal_id)
    if not d:
        raise HTTPException(status_code=404, detail="deal not found")
    events = await db_module.list_deal_events(user["org_id"], deal_id)
    return {**_view(d), "events": events}


@router.patch("/api/deals/{deal_id}")
async def patch_deal(deal_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    patch: dict = {}
    if "name" in body:
        patch["name"] = (body.get("name") or "").strip() or None
        if not patch["name"]:
            raise HTTPException(status_code=400, detail="name cannot be empty")
    if "value" in body:
        patch["value"] = dl.parse_value(body.get("value")) if body.get("value") not in (None, "") else None
    if "currency" in body:
        patch["currency"] = (body.get("currency") or "EUR").upper()[:3]
    if "probability" in body:
        p = body.get("probability")
        if p in (None, ""):
            patch["probability"] = None
        else:
            try:
                p = int(p)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="probability must be 0-100")
            if not 0 <= p <= 100:
                raise HTTPException(status_code=400, detail="probability must be 0-100")
            patch["probability"] = p
    if "expected_close" in body:
        patch["expected_close"] = _parse_date(body.get("expected_close"))
    if "owner_user_id" in body:
        patch["owner_user_id"] = body.get("owner_user_id") or None
    if "metadata" in body and isinstance(body.get("metadata"), dict):
        patch["metadata"] = body["metadata"]
    if "stage" in body:
        raise HTTPException(status_code=400, detail="use POST /api/deals/{id}/stage to change the stage")
    row = await db_module.update_deal(user["org_id"], deal_id, patch, actor_user_id=user["id"],
                                      note=(body.get("note") or "").strip())
    if not row:
        raise HTTPException(status_code=404, detail="deal not found")
    return _view(row)


@router.post("/api/deals/{deal_id}/stage")
async def move_stage(deal_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    try:
        stage = dl.validate_stage(body.get("stage") or "")
    except dl.DealError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    d = await db_module.get_deal(user["org_id"], deal_id)
    if not d:
        raise HTTPException(status_code=404, detail="deal not found")
    dl.transition_check(d["status"], stage)
    patch = {"stage": stage, "status": dl.status_for_stage(stage)}
    # keep an explicit probability if the user set one; otherwise follow the stage
    if d.get("probability") is None or d.get("probability") == dl.default_probability(d["stage"]):
        patch["probability"] = dl.default_probability(stage)
    row = await db_module.update_deal(user["org_id"], deal_id, patch, actor_user_id=user["id"],
                                      note=(body.get("note") or "").strip())
    return _view(row)


@router.delete("/api/deals/{deal_id}")
async def delete_deal(deal_id: int, user: dict = Depends(current_user)):
    _require_db()
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    ok = await db_module.delete_deal(user["org_id"], deal_id)
    if not ok:
        raise HTTPException(status_code=404, detail="deal not found")
    return {"ok": True}


@router.post("/api/deals/import-legacy")
async def import_legacy(user: dict = Depends(current_user)):
    """One-time: turn free-text clients.metadata.deal_stage/deal_value into deals rows.
    Idempotent — clients that already have a deal are skipped."""
    _require_db()
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    rows = await db_module.clients_with_legacy_deal_fields(user["org_id"])
    created, skipped = [], []
    for c in rows:
        stage = dl.normalize_legacy_stage(c.get("deal_stage")) or "lead"
        value = dl.parse_value(c.get("deal_value"))
        if not c.get("deal_stage") and value is None:
            skipped.append(c["name"])
            continue
        d = await db_module.create_deal(
            user["org_id"], c["id"], f"Deal — {c['name']}", stage=stage, value=value,
            status=dl.status_for_stage(stage), owner_user_id=c.get("created_by"),
            metadata={"imported_from": {"deal_stage": c.get("deal_stage"), "deal_value": c.get("deal_value")}},
            created_by=user["id"])
        created.append({"client": c["name"], "deal_id": d["id"], "stage": stage, "value": value})
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Client timeline
# ---------------------------------------------------------------------------

@router.get("/api/clients/{client_id}/timeline")
async def client_timeline(client_id: int, limit: int = 100, user: dict = Depends(current_user)):
    _require_db()
    rows = await db_module.client_timeline(user["org_id"], client_id, limit=min(max(limit, 1), 500))
    return {"items": rows}
