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
  GET    /api/export/deals.csv | clients.csv | contacts.csv   CSV export (org-scoped)
  POST   /api/deals/import-csv                          CSV import (idempotent: client+name upsert)
"""

import csv
import io
import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

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
        r = by.get(s["id"], {"count": 0, "total": 0.0, "weighted_explicit": 0.0, "total_default_prob": 0.0})
        weighted = r["weighted_explicit"] + r["total_default_prob"] * s["probability"] / 100.0
        out.append({"stage": s["id"], "label": s["label"], "count": r["count"], "total": r["total"],
                    "weighted": round(weighted, 2)})
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


# ---------------------------------------------------------------------------
# CSV export / import
# ---------------------------------------------------------------------------

DEAL_CSV_COLUMNS = ["client", "name", "stage", "value", "currency", "probability",
                    "expected_close", "owner_email", "status", "created_at", "id"]


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _meta(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v) or {}
        except json.JSONDecodeError:
            return {}
    return {}


@router.get("/api/export/deals.csv")
async def export_deals_csv(status: Optional[str] = None, user: dict = Depends(current_user)):
    """All deals of the org (open+won+lost unless ?status=). Header matches the importer,
    so export → import is a no-op round trip."""
    _require_db()
    users = {u["id"]: u for u in await db_module.list_users(user["org_id"])}
    rows = await db_module.list_deals(user["org_id"], status=status, limit=5000)
    out = []
    for d in rows:
        owner = users.get(d.get("owner_user_id")) or {}
        out.append([d.get("client_name"), d["name"], d["stage"], d.get("value"), d.get("currency"),
                    d.get("probability"), d.get("expected_close"), owner.get("email") or "",
                    d.get("status"), (d.get("created_at") or "").isoformat() if hasattr(d.get("created_at"), "isoformat") else d.get("created_at"),
                    d["id"]])
    return _csv_response("deals.csv", DEAL_CSV_COLUMNS, out)


@router.get("/api/export/clients.csv")
async def export_clients_csv(user: dict = Depends(current_user)):
    """Clients with their profile fields. `company` header = what the client importer expects."""
    _require_db()
    rows = await db_module.list_clients(user["org_id"])
    header = ["company", "industry", "status", "website", "location", "notes", "is_focus", "created_at", "id"]
    out = []
    for c in rows:
        m = _meta(c.get("metadata"))
        ca = c.get("created_at")
        out.append([c["name"], m.get("industry"), m.get("status"), m.get("website") or m.get("url"),
                    m.get("location") or m.get("hq"), m.get("notes"), 1 if m.get("is_focus") else 0,
                    ca.isoformat() if hasattr(ca, "isoformat") else ca, c["id"]])
    return _csv_response("clients.csv", header, out)


@router.get("/api/export/contacts.csv")
async def export_contacts_csv(user: dict = Depends(current_user)):
    """Contacts with company — same header the bulk client/contact importer reads
    (company, contact_name, email, role), so the file re-imports cleanly."""
    _require_db()
    clients = {c["id"]: c["name"] for c in await db_module.list_clients(user["org_id"])}
    rows = await db_module.list_contacts(user["org_id"])
    header = ["company", "contact_name", "email", "role", "phone", "linkedin", "notes", "id"]
    out = []
    for c in rows:
        m = _meta(c.get("metadata"))
        out.append([clients.get(c.get("client_id")) or m.get("company") or "", c["name"],
                    m.get("email"), m.get("role") or m.get("title"), m.get("phone"),
                    m.get("linkedin") or m.get("linkedin_url"), m.get("notes"), c["id"]])
    return _csv_response("contacts.csv", header, out)


@router.post("/api/deals/import-csv")
async def import_deals_csv(file: UploadFile = File(...), user: dict = Depends(current_user)):
    """Import deals from CSV. Columns (case-insensitive; extra columns ignored):
    client, name, stage, value, currency, probability, expected_close, owner_email, status.
    Idempotent: a row whose (client, name) already exists updates that deal instead of
    creating a duplicate — re-importing an export changes nothing. Unknown clients are
    reported, not created (import clients first)."""
    _require_db()
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no headers")
    reader.fieldnames = [(h or "").strip().lower() for h in reader.fieldnames]
    if "client" not in reader.fieldnames and "company" not in reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV needs a 'client' (or 'company') column")

    org_id = user["org_id"]
    users_by_email = {(u.get("email") or "").lower(): u for u in await db_module.list_users(org_id) if u.get("email")}
    client_cache: dict[str, Optional[dict]] = {}
    created, updated, unchanged, errors = [], [], [], []

    async def resolve_client(name: str) -> Optional[dict]:
        key = name.lower()
        if key in client_cache:
            return client_cache[key]
        c = await db_module.get_client(org_id, name)
        if not c:
            canon = await db_module.find_similar_client(org_id, name)
            c = await db_module.get_client(org_id, canon) if canon else None
        client_cache[key] = c
        return c

    for i, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        cname = row.get("client") or row.get("company") or ""
        dname = row.get("name") or row.get("deal") or ""
        if not cname:
            errors.append(f"row {i}: missing client"); continue
        client = await resolve_client(cname)
        if not client:
            errors.append(f"row {i}: unknown client {cname!r} — import clients first"); continue
        if not dname:
            dname = f"Deal — {client['name']}"
        try:
            stage = dl.validate_stage(row.get("stage") or "lead")
        except dl.DealError as exc:
            errors.append(f"row {i}: {exc}"); continue
        value = dl.parse_value(row.get("value")) if row.get("value") else None
        prob = row.get("probability")
        prob = int(float(prob)) if prob not in ("", None) else None
        if prob is not None and not 0 <= prob <= 100:
            errors.append(f"row {i}: probability out of range"); continue
        try:
            close = _parse_date(row.get("expected_close"))
        except HTTPException:
            errors.append(f"row {i}: expected_close must be YYYY-MM-DD"); continue
        owner = users_by_email.get((row.get("owner_email") or "").lower())
        status = (row.get("status") or "").lower() or dl.status_for_stage(stage)
        if status not in (dl.STATUS_OPEN, dl.STATUS_WON, dl.STATUS_LOST):
            errors.append(f"row {i}: status must be open|won|lost"); continue
        currency = (row.get("currency") or "EUR").upper()[:3]

        existing = next((d for d in await db_module.list_deals(org_id, client_id=client["id"], limit=500)
                         if d["name"].strip().lower() == dname.strip().lower()), None)
        if existing:
            patch = {}
            # empty CSV cells mean "leave as is" on update (only value/close/probability can be blank)
            candidates = [("stage", stage), ("currency", currency), ("status", status)]
            if row.get("value"):
                candidates.append(("value", value))
            if row.get("probability"):
                candidates.append(("probability", prob))
            if row.get("expected_close"):
                candidates.append(("expected_close", close))
            for k, v in candidates:
                if existing.get(k) != v and not (k == "value" and existing.get(k) is not None and v is not None
                                                 and abs(float(existing[k]) - float(v)) < 0.005):
                    patch[k] = v
            if owner and existing.get("owner_user_id") != owner["id"]:
                patch["owner_user_id"] = owner["id"]
            if patch:
                await db_module.update_deal(org_id, existing["id"], patch, actor_user_id=user["id"], note="csv import")
                updated.append({"row": i, "deal_id": existing["id"], "changed": sorted(patch)})
            else:
                unchanged.append(existing["id"])
        else:
            d = await db_module.create_deal(
                org_id, client["id"], dname, stage=stage, value=value, currency=currency, probability=prob,
                expected_close=close, owner_user_id=(owner or {}).get("id") or user["id"], status=status,
                metadata={"imported": True}, created_by=user["id"])
            created.append({"row": i, "deal_id": d["id"], "client": client["name"], "name": dname})
    return {"created": created, "updated": updated, "unchanged": len(unchanged), "errors": errors}

