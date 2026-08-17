"""
routers/sharing.py — shared clients between orgs (Phase 6a).

  GET    /api/sharing/lookup?q=                       find a person to invite (other orgs)
  POST   /api/clients/{client_id}/share               invite someone to share this client
  GET    /api/clients/{client_id}/sharing             group + members + pending invites for a client
  GET    /api/sharing/invites?direction=incoming|outgoing&status=pending
  POST   /api/sharing/invites/{id}/accept   {client_id? | client_name?}   (= consent; links/creates my client)
  POST   /api/sharing/invites/{id}/decline
  POST   /api/sharing/invites/{id}/revoke
  GET    /api/sharing/groups                          my active share groups
  POST   /api/sharing/groups/{id}/leave     {delete_copies: bool}
  POST   /api/sharing/groups/{id}/monitor   {org_id}  who runs monitoring
  POST   /api/sharing/groups/{id}/scope     {doc_types, profile, sources}
  POST   /api/sharing/groups/{id}/sync                enqueue a full re-sync (members)
  POST   /api/sharing/process                         drain the outbox now (admin)
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

import sharing
from context import DB_AVAILABLE, db_module
from routers.auth import current_user

logger = logging.getLogger("wk.sharing")
router = APIRouter()


def _require_db():
    if not DB_AVAILABLE or db_module is None:
        raise HTTPException(status_code=503, detail="Database unavailable")


async def _my_client(user: dict, client_id: int) -> dict:
    c = await db_module.get_client_by_id(user["org_id"], client_id)
    if not c:
        raise HTTPException(status_code=404, detail="client not found")
    return c


def _member_of(group_members: list[dict], org_id: int) -> Optional[dict]:
    return next((m for m in group_members if m["org_id"] == org_id and m.get("left_at") is None), None)


# ---------------------------------------------------------------------------

@router.get("/api/sharing/lookup")
async def lookup(q: str = "", user: dict = Depends(current_user)):
    _require_db()
    if len((q or "").strip()) < 3:
        return {"users": []}
    rows = await db_module.find_users_global(q, exclude_org_id=user["org_id"], limit=5)
    return {"users": [{"user_id": r["id"], "org_id": r["org_id"], "org_name": r["org_name"],
                       "display_name": r["display_name"], "username": r["username"], "email": r.get("email")}
                      for r in rows]}


@router.get("/api/sharing/partners")
async def list_share_partners(user: dict = Depends(current_user)):
    """Verified federation partners (other installs) any member may share with."""
    _require_db()
    try:
        rows = await db_module.fed_list_partners(user["org_id"], statuses=["active"])
    except Exception:
        rows = []
    return {"partners": [{"id": p["id"], "partner_mxid": p["partner_mxid"], "partner_name": p.get("partner_name")} for p in rows]}


@router.post("/api/clients/{client_id}/share")
async def share_client(client_id: int, body: dict, user: dict = Depends(current_user)):
    """Invite a person from another org to share this client. Creates the share
    group on first use (this org = owner + monitor)."""
    _require_db()
    client = await _my_client(user, client_id)
    to_user_id = body.get("to_user_id")
    to_email = (body.get("to_email") or "").strip().lower()
    to_org_id = None
    to_partner_id = body.get("to_partner_id")
    if to_partner_id:
        # cross-instance: a verified federation partner (Phase 5b)
        import federation
        partner = await db_module.fed_get_partner(int(to_partner_id))
        if not partner or partner["org_id"] != user["org_id"]:
            raise HTTPException(status_code=404, detail="partner not found")
        if partner["status"] != "active":
            raise HTTPException(status_code=409, detail="verify the partner's device before sharing with them")
        group = await db_module.sharing_group_for_client(client_id)
        if group and group.get("member_org_id") != user["org_id"]:
            raise HTTPException(status_code=409, detail="client belongs to another org's share group")
        if not group:
            group = await db_module.sharing_create_group(user["org_id"], client_id, user["id"], client["name"],
                                                         scope=sharing.normalize_scope(body.get("scope")))
        remote = await db_module.sharing_list_remote_members(group["id"])
        if any(m["partner_id"] == partner["id"] and m.get("left_at") is None for m in remote):
            raise HTTPException(status_code=409, detail="that partner already shares this client")
        pending = [i for i in await db_module.sharing_list_invites(user["org_id"], "outgoing", "pending")
                   if i["shared_client_id"] == group["id"] and i.get("to_partner_id") == partner["id"]]
        if pending:
            return {"ok": True, "invite": pending[0], "group": group, "note": "invite already pending"}
        inv = await db_module.sharing_create_invite(group["id"], user["org_id"], user["id"], to_partner_id=partner["id"],
                                                    message=(body.get("message") or "").strip())
        await federation.enqueue(user["org_id"], partner["id"], "share_invite", {
            "group_key": group["key"], "invite_id": inv["id"], "client_name": client["name"],
            "scope": sharing.normalize_scope(group.get("scope")), "message": (body.get("message") or "").strip()[:500]})
        logger.info("share invite #%s: org %s client %r → partner %s", inv["id"], user["org_id"], client["name"], partner["partner_mxid"])
        return {"ok": True, "invite": inv, "group": group, "remote": True}
    if to_user_id:
        u = await db_module.get_user_by_id(int(to_user_id))
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        to_org_id = u["org_id"]
        to_email = to_email or (u.get("email") or "")
    elif to_email:
        hits = await db_module.find_users_global(to_email, exclude_org_id=None, limit=1)
        if hits and (hits[0].get("email") or "").lower() == to_email:
            to_user_id, to_org_id = hits[0]["id"], hits[0]["org_id"]
    else:
        raise HTTPException(status_code=400, detail="to_user_id or to_email required")
    if to_org_id == user["org_id"]:
        raise HTTPException(status_code=400, detail="that person is in your own org — clients are already shared inside an org")

    group = await db_module.sharing_group_for_client(client_id)
    if group and group.get("member_org_id") != user["org_id"]:
        raise HTTPException(status_code=409, detail="client belongs to another org's share group")
    if not group:
        group = await db_module.sharing_create_group(user["org_id"], client_id, user["id"], client["name"],
                                                     scope=sharing.normalize_scope(body.get("scope")))
    if to_org_id:
        members = await db_module.sharing_list_members(group["id"])
        if _member_of(members, to_org_id):
            raise HTTPException(status_code=409, detail="that org already shares this client")
        pending = [i for i in await db_module.sharing_list_invites(user["org_id"], "outgoing", "pending")
                   if i["shared_client_id"] == group["id"] and i.get("to_org_id") == to_org_id]
        if pending:
            return {"ok": True, "invite": pending[0], "group": group, "note": "invite already pending"}
    inv = await db_module.sharing_create_invite(group["id"], user["org_id"], user["id"], to_user_id=to_user_id,
                                                to_org_id=to_org_id, to_email=to_email or None,
                                                message=(body.get("message") or "").strip())
    logger.info("share invite #%s: org %s client %r → org %s / user %s / %s", inv["id"], user["org_id"],
                client["name"], to_org_id, to_user_id, to_email)
    return {"ok": True, "invite": inv, "group": group}


@router.get("/api/clients/{client_id}/sharing")
async def client_sharing(client_id: int, user: dict = Depends(current_user)):
    _require_db()
    await _my_client(user, client_id)
    group = await db_module.sharing_group_for_client(client_id)
    if not group:
        return {"group": None, "members": [], "invites": []}
    members = await db_module.sharing_list_members(group["id"])
    remote = [m for m in await db_module.sharing_list_remote_members(group["id"]) if m.get("left_at") is None]
    invites = [i for i in await db_module.sharing_list_invites(user["org_id"], "outgoing", "pending")
               if i["shared_client_id"] == group["id"]]
    return {"group": group, "members": members, "remote_members": remote, "invites": invites,
            "i_monitor": group.get("monitor_org_id") == user["org_id"] and not group.get("monitor_partner_id"),
            "my_org_id": user["org_id"]}


@router.get("/api/sharing/invites")
async def list_invites(direction: str = "incoming", status: str = "pending", user: dict = Depends(current_user)):
    _require_db()
    if direction not in ("incoming", "outgoing"):
        raise HTTPException(status_code=400, detail="direction must be incoming|outgoing")
    rows = await db_module.sharing_list_invites(user["org_id"], direction, status)
    return {"invites": rows}


@router.post("/api/sharing/invites/{invite_id}/accept")
async def accept_invite(invite_id: int, body: dict, user: dict = Depends(current_user)):
    """Consent: link (or create) my client for this shared company and start syncing
    both ways."""
    _require_db()
    inv = await db_module.sharing_get_invite(invite_id)
    if not inv or inv["status"] != "pending":
        raise HTTPException(status_code=404, detail="invite not found or not pending")
    if inv.get("to_org_id") not in (None, user["org_id"]):
        raise HTTPException(status_code=403, detail="this invite is addressed to another org")
    if inv.get("to_org_id") is None:
        # email-only invite: claim it if the email matches mine
        if (inv.get("to_email") or "").lower() != (user.get("email") or "").lower():
            raise HTTPException(status_code=403, detail="invite is addressed to another email")
    if inv.get("group_status") != "active":
        raise HTTPException(status_code=409, detail="share group is closed")

    # which of my clients?
    client = None
    if body.get("client_id"):
        client = await db_module.get_client_by_id(user["org_id"], int(body["client_id"]))
        if not client:
            raise HTTPException(status_code=404, detail="client not found")
    else:
        name = (body.get("client_name") or inv["client_name"]).strip()
        client = await db_module.get_client(user["org_id"], name)
        if not client:
            cid = await db_module.upsert_client(user["org_id"], name, {"created_via": "share_invite"}, [],
                                                created_by=user["id"])
            client = await db_module.get_client_by_id(user["org_id"], cid)
    existing = await db_module.sharing_group_for_client(client["id"])
    if existing and existing["id"] != inv["shared_client_id"]:
        raise HTTPException(status_code=409, detail=f"'{client['name']}' is already shared in another group — leave it first")

    if inv.get("from_partner_id"):
        # Cross-instance invite (Phase 5b): the group row with the remote key already
        # exists locally (created on receipt); we join it, the partner keeps monitoring
        # by default, and both sides start their full sync.
        import federation
        partner = await db_module.fed_get_partner(inv["from_partner_id"])
        if not partner or partner["org_id"] != user["org_id"] or partner["status"] != "active":
            raise HTTPException(status_code=409, detail="verify the partner before accepting")
        await db_module.sharing_add_member_to_group(inv["shared_client_id"], user["org_id"], client["id"], user["id"])
        await db_module.sharing_add_remote_member(inv["shared_client_id"], partner["id"], role="owner")
        await db_module.sharing_set_monitor_remote(inv["shared_client_id"], partner["id"], None)
        await db_module.sharing_respond_invite(invite_id, "accepted")
        await federation.enqueue(user["org_id"], partner["id"], "share_accept",
                                 {"group_key": str(inv["remote_group_key"]), "invite_id": inv.get("remote_invite_id")})
        scope = sharing.normalize_scope(inv.get("scope"))
        queued = await db_module.sharing_enqueue_client(inv["shared_client_id"], user["org_id"], client["id"], scope["doc_types"])
        stats = await sharing.process_outbox(limit=500)
        logger.info("remote share invite #%s accepted by org %s (client %r): queued %s", invite_id, user["org_id"], client["name"], queued)
        return {"ok": True, "group_id": inv["shared_client_id"], "client_id": client["id"], "client_name": client["name"],
                "queued": queued, "sync": stats, "remote": True}

    await db_module.sharing_add_member(inv["shared_client_id"], user["org_id"], client["id"], user["id"])
    await db_module.sharing_respond_invite(invite_id, "accepted")
    # full sync in both directions
    scope = sharing.normalize_scope(inv.get("scope"))
    members = await db_module.sharing_list_members(inv["shared_client_id"])
    queued = 0
    for m in members:
        if m.get("left_at") is None:
            queued += await db_module.sharing_enqueue_client(inv["shared_client_id"], m["org_id"], m["client_id"],
                                                             scope["doc_types"])
    stats = await sharing.process_outbox(limit=500)
    logger.info("share invite #%s accepted by org %s (client %r): queued %s, %s", invite_id, user["org_id"],
                client["name"], queued, stats)
    return {"ok": True, "group_id": inv["shared_client_id"], "client_id": client["id"], "client_name": client["name"],
            "queued": queued, "sync": stats}


@router.post("/api/sharing/invites/{invite_id}/decline")
async def decline_invite(invite_id: int, user: dict = Depends(current_user)):
    _require_db()
    inv = await db_module.sharing_get_invite(invite_id)
    if not inv or inv["status"] != "pending":
        raise HTTPException(status_code=404, detail="invite not found or not pending")
    if inv.get("to_org_id") not in (None, user["org_id"]):
        raise HTTPException(status_code=403, detail="not yours")
    await db_module.sharing_respond_invite(invite_id, "declined")
    if inv.get("from_partner_id"):
        import federation
        await federation.enqueue(user["org_id"], inv["from_partner_id"], "share_decline",
                                 {"group_key": str(inv["remote_group_key"]), "invite_id": inv.get("remote_invite_id")})
    return {"ok": True}


@router.post("/api/sharing/invites/{invite_id}/revoke")
async def revoke_invite(invite_id: int, user: dict = Depends(current_user)):
    _require_db()
    inv = await db_module.sharing_get_invite(invite_id)
    if not inv or inv["status"] != "pending" or inv["from_org_id"] != user["org_id"]:
        raise HTTPException(status_code=404, detail="invite not found")
    await db_module.sharing_respond_invite(invite_id, "revoked")
    return {"ok": True}


@router.get("/api/sharing/groups")
async def list_groups(user: dict = Depends(current_user)):
    _require_db()
    return {"groups": await db_module.sharing_list_for_org(user["org_id"]), "my_org_id": user["org_id"]}


@router.post("/api/sharing/groups/{group_id}/leave")
async def leave_group(group_id: int, body: dict, user: dict = Depends(current_user)):
    """Stop sharing. Copies received so far are kept (flagged 'detached') unless
    delete_copies is true; what the others already received stays with them."""
    _require_db()
    group = await db_module.sharing_get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    members = await db_module.sharing_list_members(group_id)
    if not _member_of(members, user["org_id"]):
        raise HTTPException(status_code=403, detail="not a member")
    remote = [m for m in await db_module.sharing_list_remote_members(group_id) if m.get("left_at") is None]
    for m in remote:
        import federation
        if m.get("local_org_id") == user["org_id"]:
            await federation.enqueue(user["org_id"], m["partner_id"], "share_leave", {"group_key": group["key"]})
            await db_module.sharing_remove_remote_member(group_id, m["partner_id"])
    res = await db_module.sharing_leave(group_id, user["org_id"])
    n = await db_module.sharing_leave_cleanup(user["org_id"], group["key"], bool(body.get("delete_copies")))
    return {"ok": True, **res, "copies_affected": n}


@router.post("/api/sharing/groups/{group_id}/monitor")
async def set_monitor(group_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    members = await db_module.sharing_list_members(group_id)
    if not _member_of(members, user["org_id"]):
        raise HTTPException(status_code=403, detail="not a member")
    group = await db_module.sharing_get_group(group_id)
    remote = [m for m in await db_module.sharing_list_remote_members(group_id) if m.get("left_at") is None]
    if body.get("partner_id"):
        # hand monitoring to a remote member
        import federation
        pid = int(body["partner_id"])
        if not any(m["partner_id"] == pid for m in remote):
            raise HTTPException(status_code=400, detail="that partner is not a member")
        await db_module.sharing_set_monitor_remote(group_id, pid, None)
        await federation.enqueue(user["org_id"], pid, "monitor", {"group_key": group["key"], "monitor": "you"})
        return {"ok": True, "monitor_partner_id": pid}
    target = int(body.get("org_id") or user["org_id"])
    if not _member_of(members, target):
        raise HTTPException(status_code=400, detail="target org is not a member")
    await db_module.sharing_set_monitor_remote(group_id, None, target)
    if remote and target == user["org_id"]:
        import federation
        for m in remote:
            if m.get("local_org_id") == user["org_id"]:
                await federation.enqueue(user["org_id"], m["partner_id"], "monitor", {"group_key": group["key"], "monitor": "me"})
    return {"ok": True, "monitor_org_id": target}


@router.post("/api/sharing/groups/{group_id}/scope")
async def set_scope(group_id: int, body: dict, user: dict = Depends(current_user)):
    _require_db()
    members = await db_module.sharing_list_members(group_id)
    me = _member_of(members, user["org_id"])
    if not me:
        raise HTTPException(status_code=403, detail="not a member")
    scope = sharing.normalize_scope(body)
    await db_module.sharing_update_scope(group_id, scope)
    return {"ok": True, "scope": scope}


@router.post("/api/sharing/groups/{group_id}/sync")
async def resync(group_id: int, user: dict = Depends(current_user)):
    _require_db()
    group = await db_module.sharing_get_group(group_id)
    members = await db_module.sharing_list_members(group_id) if group else []
    if not group or not _member_of(members, user["org_id"]):
        raise HTTPException(status_code=404, detail="group not found")
    scope = sharing.normalize_scope(group.get("scope"))
    queued = 0
    for m in members:
        if m.get("left_at") is None:
            queued += await db_module.sharing_enqueue_client(group_id, m["org_id"], m["client_id"], scope["doc_types"])
    stats = await sharing.process_outbox(limit=1000)
    return {"ok": True, "queued": queued, "sync": stats}


@router.post("/api/sharing/process")
async def process_now(user: dict = Depends(current_user)):
    _require_db()
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return await sharing.process_outbox(limit=1000)
