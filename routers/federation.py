"""
routers/federation.py — Matrix federation admin API (Phase 5b).

  GET    /api/federation/status                     identity + runtime state (admin)
  POST   /api/federation/identity   {homeserver_url, username, password, register, display_name}
  DELETE /api/federation/identity
  GET    /api/federation/partners                   all partners (with devices seen by our bot)
  POST   /api/federation/partners   {mxid}          invite a partner instance (creates the E2EE room)
  POST   /api/federation/partners/{id}/accept       accept an incoming invite (join the room)
  POST   /api/federation/partners/{id}/verify {device_id}   pin the partner's device (after out-of-band compare)
  POST   /api/federation/partners/{id}/disconnect   leave the room; shares with this partner stop
  DELETE /api/federation/partners/{id}              forget a left/blocked/pending partner
  POST   /api/federation/tick                       drain outbox/inbox now (admin, debugging)
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import federation
from context import DB_AVAILABLE, db_module
from routers.auth import current_user

logger = logging.getLogger("wk.federation")
router = APIRouter(prefix="/api/federation")


def _admin(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")


def _pub_identity(i: dict | None) -> dict | None:
    if not i:
        return None
    return {k: i.get(k) for k in ("org_id", "homeserver_url", "mxid", "device_id", "ed25519", "display_name",
                                  "status", "last_error", "last_sync_at", "created_at")} | \
           {"fingerprint": federation.fingerprint(i.get("ed25519"))}


def _pub_partner(p: dict, node) -> dict:
    devices = node.partner_devices(p["partner_mxid"]) if node else []
    return {**{k: p.get(k) for k in ("id", "partner_mxid", "partner_name", "room_id", "direction", "status",
                                     "pinned_device_id", "pinned_ed25519", "seen_device_id", "seen_ed25519",
                                     "verified_at", "last_event_at", "last_error", "created_at")},
            "pinned_fingerprint": federation.fingerprint(p.get("pinned_ed25519")),
            "devices": devices}


@router.get("/status")
async def status(user: dict = Depends(current_user)):
    _admin(user)
    ident = await db_module.fed_get_identity(user["org_id"])
    return {"identity": _pub_identity(ident), **federation.status(user["org_id"]),
            "config": {"enabled": federation.enabled(),
                       "default_homeserver": (federation._cfg().get("homeserver_url") or ""),
                       "registration": "shared_secret" if federation._cfg().get("registration_shared_secret") else "open_or_login"}}


@router.post("/identity")
async def set_identity(body: dict, user: dict = Depends(current_user)):
    _admin(user)
    hs = (body.get("homeserver_url") or federation._cfg().get("homeserver_url") or "").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not hs or not username or not password:
        raise HTTPException(status_code=400, detail="homeserver_url, username and password are required")
    org = await db_module.get_org(user["org_id"])
    display = (body.get("display_name") or (org or {}).get("name") or "Buzzowl").strip()[:120]
    try:
        ident = await federation.setup_identity(user["org_id"], homeserver_url=hs, username=username, password=password,
                                                register=bool(body.get("register")), display_name=display)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not set up the identity: {str(exc)[:300]}")
    return {"ok": True, "identity": _pub_identity(ident), **federation.status(user["org_id"])}


@router.delete("/identity")
async def delete_identity(user: dict = Depends(current_user)):
    _admin(user)
    await federation.remove_identity(user["org_id"])
    return {"ok": True}


@router.get("/partners")
async def list_partners(user: dict = Depends(current_user)):
    _admin(user)
    node = federation.manager.node(user["org_id"])
    rows = await db_module.fed_list_partners(user["org_id"])
    return {"partners": [_pub_partner(p, node) for p in rows], **federation.status(user["org_id"])}


@router.post("/partners")
async def invite_partner(body: dict, user: dict = Depends(current_user)):
    """Create the encrypted room and invite the partner's bot. Nothing is shared by this."""
    _admin(user)
    mxid = (body.get("mxid") or "").strip()
    if not mxid.startswith("@") or ":" not in mxid:
        raise HTTPException(status_code=400, detail="mxid must look like @buzzowl:their-homeserver.example")
    node = federation.manager.node(user["org_id"])
    if not node or not node.online:
        raise HTTPException(status_code=409, detail="this org's Matrix identity is not online — set it up first")
    if mxid == node.client.user_id:
        raise HTTPException(status_code=400, detail="that is your own bot")
    existing = await db_module.fed_partner_by_mxid(user["org_id"], mxid)
    if existing and existing["status"] in ("pending", "active", "reverify"):
        return {"ok": True, "partner": _pub_partner(existing, node), "note": "already invited/connected"}
    try:
        room_id = await node.create_partner_room(mxid)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"could not create the room: {str(exc)[:300]}")
    p = await db_module.fed_upsert_partner(user["org_id"], mxid, room_id=room_id, direction="outgoing", status="pending",
                                           last_error=None, pinned_device_id=None, pinned_ed25519=None, verified_at=None)
    return {"ok": True, "partner": _pub_partner(p, node)}


@router.post("/partners/{partner_id}/accept")
async def accept_partner(partner_id: int, user: dict = Depends(current_user)):
    _admin(user)
    p = await db_module.fed_get_partner(partner_id)
    if not p or p["org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="partner not found")
    node = federation.manager.node(user["org_id"])
    if not node or not node.online:
        raise HTTPException(status_code=409, detail="identity not online")
    if p["direction"] != "incoming" or not p.get("room_id"):
        raise HTTPException(status_code=400, detail="nothing to accept")
    try:
        await node.join_room(p["room_id"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"join failed: {str(exc)[:300]}")
    await db_module.fed_enqueue(user["org_id"], partner_id, "hello", {"org_name": node.identity.get("display_name") or ""})
    p = await db_module.fed_update_partner(partner_id, last_error=None)
    return {"ok": True, "partner": _pub_partner(p, node), "next": "compare fingerprints, then verify"}


@router.post("/partners/{partner_id}/verify")
async def verify_partner(partner_id: int, body: dict, user: dict = Depends(current_user)):
    """Pin the partner bot's device after the admins compared fingerprints out-of-band."""
    _admin(user)
    p = await db_module.fed_get_partner(partner_id)
    if not p or p["org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="partner not found")
    node = federation.manager.node(user["org_id"])
    if not node or not node.online:
        raise HTTPException(status_code=409, detail="identity not online")
    device_id = (body.get("device_id") or p.get("seen_device_id") or "").strip()
    if not device_id:
        devs = node.partner_devices(p["partner_mxid"])
        if len(devs) == 1:
            device_id = devs[0]["device_id"]
    if not device_id:
        raise HTTPException(status_code=409, detail="no partner device known yet — wait until they joined and synced")
    try:
        pinned = await node.pin_device(p["partner_mxid"], device_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300])
    p = await db_module.fed_update_partner(partner_id, pinned_device_id=pinned["device_id"], pinned_ed25519=pinned["ed25519"],
                                           verified_at=datetime.now(timezone.utc), verified_by=user["id"],
                                           status="active", last_error=None)
    n = await db_module.fed_mark_inbox_verified(partner_id)
    await db_module.fed_enqueue(user["org_id"], partner_id, "hello", {"org_name": node.identity.get("display_name") or ""})
    return {"ok": True, "partner": _pub_partner(p, node), "inbox_unlocked": n}


@router.post("/partners/{partner_id}/disconnect")
async def disconnect_partner(partner_id: int, user: dict = Depends(current_user)):
    _admin(user)
    p = await db_module.fed_get_partner(partner_id)
    if not p or p["org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="partner not found")
    node = federation.manager.node(user["org_id"])
    if node and p.get("room_id"):
        await node.leave_room(p["room_id"])
        await node.unpin_all(p["partner_mxid"])
    p = await db_module.fed_update_partner(partner_id, status="left", last_error=None)
    return {"ok": True, "partner": _pub_partner(p, node), "note": "already-received copies stay with the partner"}


@router.delete("/partners/{partner_id}")
async def forget_partner(partner_id: int, user: dict = Depends(current_user)):
    _admin(user)
    p = await db_module.fed_get_partner(partner_id)
    if not p or p["org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="partner not found")
    if p["status"] == "active":
        raise HTTPException(status_code=409, detail="disconnect first")
    await db_module.fed_delete_partner(partner_id)
    return {"ok": True}


@router.post("/tick")
async def tick(user: dict = Depends(current_user)):
    _admin(user)
    node = federation.manager.node(user["org_id"])
    if not node:
        raise HTTPException(status_code=409, detail="identity not running")
    return {"outbox": await node.outbox_tick(), "inbox": await node.inbox_tick()}
