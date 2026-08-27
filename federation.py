"""
federation.py — Matrix transport for shared clients between Buzzowl instances (Phase 5b).

Runs INSIDE the server process (started from server startup when enabled): one
matrix-nio bot per org that has an identity, one end-to-end-encrypted,
invite-only room per partner pair.

  outbound   share_outbox → sharing.process_outbox → (remote member) → federation_outbox
             → Node.outbox_tick → encrypted `de.buzzowl.sync` event in the partner room
  inbound    partner event → Node callback → federation_inbox (replay-safe) →
             Node.inbox_tick → applied with the same db.sharing_apply_* used locally

Trust: after pairing, an admin compares device fingerprints out-of-band and PINS
the partner's device; from then on data is sent only when every device in the
room is verified (nio raises OlmUnverifiedDeviceError otherwise → the row waits
and the partner shows "reverify"). Events from unverified devices sit in the
inbox unapplied until a matching device is pinned. Only `hello` may travel to
unverified devices (it carries the org's display name and nothing else).

Design references: docs/spikes/matrix-federation/README.md (spike, threat model,
event schema, GDPR position).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import context

logger = logging.getLogger("wk.federation")

EVENT_TYPE = "de.buzzowl.sync"
SCHEMA = 1
MAX_EVENT_BYTES = 36 * 1024          # plaintext budget; bigger docs go as encrypted media
DATA_KINDS = {"share_invite", "share_accept", "share_decline", "share_leave",
              "document", "document_ref", "document_delete", "profile", "monitor"}
OPEN_KINDS = {"hello"}               # may be sent/received before verification


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cfg() -> dict:
    return (context.config or {}).get("federation") or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def store_root() -> Path:
    import os
    base = Path(os.environ.get("FEDERATION_STORE_DIR") or _cfg().get("store_dir")
                or (Path(__file__).parent / "data" / "federation"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def fingerprint(ed25519: Optional[str]) -> str:
    """Human-comparable form of a device key: groups of 4."""
    if not ed25519:
        return ""
    s = ed25519.replace("=", "")
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


# ---------------------------------------------------------------------------
# One org's bot
# ---------------------------------------------------------------------------

class Node:
    def __init__(self, org_id: int, identity: dict, manager: "FederationManager"):
        self.org_id = org_id
        self.identity = identity
        self.manager = manager
        self.client = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self.online = False
        self.last_error: Optional[str] = None
        self._db = context.db_module

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        from nio import AsyncClient, AsyncClientConfig
        import plans
        ident = self.identity
        path = store_root() / str(self.org_id)
        path.mkdir(parents=True, exist_ok=True)
        cfg = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
        self.client = AsyncClient(ident["homeserver_url"], ident["mxid"], store_path=str(path), config=cfg)
        token = plans.try_decrypt_secret(ident.get("access_token_enc") or "",
                                         f"the federation access token for org {self.org_id}")
        if token is None:
            # Encryption key changed since the identity was stored — the token is
            # gone for good; surface it as a setup problem, not a crash.
            raise RuntimeError("stored access token cannot be decrypted — the encryption key "
                               "changed; connect this workspace to Matrix again")
        if not token or not ident.get("device_id"):
            raise RuntimeError("identity has no access token — set it up again")
        self.client.restore_login(ident["mxid"], ident["device_id"], token)
        self._register_callbacks()
        self._tasks = [asyncio.create_task(self._sync_loop(), name=f"fed-sync-{self.org_id}"),
                       asyncio.create_task(self._outbox_loop(), name=f"fed-outbox-{self.org_id}"),
                       asyncio.create_task(self._inbox_loop(), name=f"fed-inbox-{self.org_id}")]
        logger.info("federation node started for org %s as %s", self.org_id, ident["mxid"])

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass

    async def _sync_loop(self) -> None:
        backoff = 5
        while not self._stopping:
            try:
                await self._ensure_keys()
                # record our own fingerprint once the store is up
                try:
                    own = self.client.olm.account.identity_keys.get("ed25519") if self.client.olm else None
                    if own and own != self.identity.get("ed25519"):
                        await self._db.fed_upsert_identity(self.org_id, ed25519=own)
                        self.identity["ed25519"] = own
                except Exception:
                    pass
                self.online = True
                await self._db.fed_upsert_identity(self.org_id, status="online", last_error=None)
                await self.client.sync_forever(timeout=30000, full_state=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.online = False
                self.last_error = str(exc)[:300]
                logger.warning("federation sync loop (org %s) failed: %s — retry in %ss", self.org_id, exc, backoff)
                try:
                    await self._db.fed_upsert_identity(self.org_id, status="error", last_error=self.last_error)
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _ensure_keys(self) -> None:
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        if self.client.should_query_keys:
            await self.client.keys_query()

    # -- callbacks -------------------------------------------------------------

    def _register_callbacks(self) -> None:
        from nio import InviteMemberEvent, MegolmEvent, RoomEncryptedMedia, RoomMemberEvent, UnknownEvent
        c = self.client
        c.add_event_callback(self._on_invite, InviteMemberEvent)
        c.add_event_callback(self._on_member, RoomMemberEvent)
        c.add_event_callback(self._on_custom, UnknownEvent)
        c.add_event_callback(self._on_media, RoomEncryptedMedia)
        c.add_event_callback(self._on_undecryptable, MegolmEvent)

    async def _on_invite(self, room, event) -> None:
        if event.state_key != self.client.user_id or event.membership != "invite":
            return
        # A partner instance invited us: record it; an admin accepts in the UI.
        try:
            existing = await self._db.fed_partner_by_mxid(self.org_id, event.sender)
            if existing and existing.get("status") in ("blocked",):
                return
            await self._db.fed_upsert_partner(self.org_id, event.sender, room_id=room.room_id,
                                              direction="incoming", status="pending", last_event_at=_now())
            logger.info("federation: partner invite from %s → org %s (room %s)", event.sender, self.org_id, room.room_id)
        except Exception as exc:
            logger.warning("federation invite record failed: %s", exc)

    async def _on_member(self, room, event) -> None:
        """Membership watchdog: partner joined → say hello + refresh devices; a third
        member → block; partner left → status left."""
        try:
            partner = await self._db.fed_partner_by_room(self.org_id, room.room_id)
            if not partner:
                return
            members = [u for u in room.users.keys()]
            others = [u for u in members if u != self.client.user_id]
            if len(others) > 1:
                await self._db.fed_update_partner(partner["id"], status="blocked",
                                                  last_error=f"unexpected members in room: {', '.join(others)}")
                logger.warning("federation: room %s has extra members %s — partner blocked", room.room_id, others)
                return
            if event.state_key == partner["partner_mxid"]:
                if event.membership == "join":
                    await self._ensure_keys()
                    await self._refresh_seen_device(partner)
                    if partner.get("status") == "pending":
                        await self._db.fed_update_partner(partner["id"], status="active" if partner.get("pinned_ed25519") else "pending",
                                                          last_event_at=_now())
                    await self._db.fed_enqueue(self.org_id, partner["id"], "hello", {"org_name": self.identity.get("display_name") or ""})
                elif event.membership in ("leave", "ban"):
                    await self._db.fed_update_partner(partner["id"], status="left", last_event_at=_now())
        except Exception as exc:
            logger.warning("federation member callback failed: %s", exc)

    async def _refresh_seen_device(self, partner: dict) -> None:
        devs = self.partner_devices(partner["partner_mxid"])
        if not devs:
            return
        newest = devs[-1]
        upd = {"seen_device_id": newest["device_id"], "seen_ed25519": newest["ed25519"]}
        pinned = partner.get("pinned_ed25519")
        if pinned and any(d["ed25519"] != pinned for d in devs):
            upd["status"] = "reverify"
            upd["last_error"] = "partner has a device that is not the pinned one — verify again"
        await self._db.fed_update_partner(partner["id"], **upd)

    def partner_devices(self, mxid: str) -> list[dict]:
        out = []
        try:
            for d in self.client.device_store.active_user_devices(mxid):
                out.append({"device_id": d.device_id, "ed25519": d.ed25519, "curve25519": d.curve25519,
                            "verified": bool(getattr(d, "verified", False)), "fingerprint": fingerprint(d.ed25519)})
        except Exception:
            pass
        return out

    def _sender_ed25519(self, event) -> Optional[str]:
        """ed25519 of the device that sent this decrypted event (matched via curve key)."""
        try:
            for d in self.client.device_store.active_user_devices(event.sender):
                if getattr(event, "sender_key", None) and d.curve25519 == event.sender_key:
                    return d.ed25519
        except Exception:
            pass
        return None

    async def _on_custom(self, room, event) -> None:
        if getattr(event, "type", None) != EVENT_TYPE:
            return
        content = (event.source or {}).get("content", {}) or {}
        await self._ingest(room.room_id, event, content.get("kind"), content)

    async def _on_media(self, room, event) -> None:
        extra = (event.source or {}).get("content", {}).get("de.buzzowl") or {}
        if extra.get("kind") != "document_ref":
            return
        payload = {"kind": "document_ref", "group_key": extra.get("group_key"),
                   "file": {"url": event.url, "key": event.key, "iv": event.iv, "hashes": event.hashes},
                   "sha256": extra.get("sha256"), "doc_id": extra.get("doc_id")}
        await self._ingest(room.room_id, event, "document_ref", payload)

    async def _on_undecryptable(self, room, event) -> None:
        logger.warning("federation: undecryptable event %s in %s (org %s)", event.event_id, room.room_id, self.org_id)

    async def _ingest(self, room_id: str, event, kind: Optional[str], payload: dict) -> None:
        if not kind or (kind not in DATA_KINDS and kind not in OPEN_KINDS):
            return
        try:
            partner = await self._db.fed_partner_by_room(self.org_id, room_id)
            if not partner or event.sender != partner["partner_mxid"]:
                return                                   # only the paired bot may speak in this room
            ed = self._sender_ed25519(event)
            verified = bool(getattr(event, "verified", False)) or (bool(ed) and ed == partner.get("pinned_ed25519"))
            body = {k: v for k, v in payload.items() if k not in ("kind",)}
            iid = await self._db.fed_inbox_insert(self.org_id, partner_id=partner["id"], room_id=room_id,
                                                  event_id=event.event_id, sender=event.sender, sender_key=ed,
                                                  verified=verified, kind=kind, payload=body)
            if iid:
                await self._db.fed_update_partner(partner["id"], last_event_at=_now())
                if not partner.get("seen_ed25519") and ed:
                    await self._db.fed_update_partner(partner["id"], seen_device_id=getattr(event, "device_id", None), seen_ed25519=ed)
        except Exception as exc:
            logger.warning("federation ingest failed: %s", exc)

    # -- outbox → matrix ------------------------------------------------------------

    async def _outbox_loop(self) -> None:
        while not self._stopping:
            try:
                if self.online:
                    await self.outbox_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("federation outbox tick (org %s): %s", self.org_id, exc)
            await asyncio.sleep(float(_cfg().get("outbox_interval_s", 5)))

    async def outbox_tick(self, limit: int = 50) -> dict:
        from nio import RoomSendResponse
        stats = {"sent": 0, "held": 0, "errors": 0}
        for row in await self._db.fed_pending_outbox(self.org_id, limit):
            kind = row["kind"]
            if not row.get("room_id"):
                await self._db.fed_mark_outbox(row["id"], error="partner has no room yet"); stats["held"] += 1; continue
            if row.get("partner_status") in ("blocked", "left"):
                await self._db.fed_mark_outbox(row["id"], error=f"partner {row['partner_status']}"); stats["held"] += 1; continue
            if kind not in OPEN_KINDS and row.get("partner_status") != "active":
                await self._db.fed_mark_outbox(row["id"], error="partner not verified yet"); stats["held"] += 1; continue
            content = {"schema": SCHEMA, "kind": kind, **(row.get("payload") or {})}
            try:
                if kind == "document" and len(json.dumps(content).encode()) > MAX_EVENT_BYTES:
                    resp = await self._send_document_as_media(row["room_id"], content)
                else:
                    resp = await self.client.room_send(row["room_id"], EVENT_TYPE, content,
                                                       ignore_unverified_devices=(kind in OPEN_KINDS))
                if isinstance(resp, RoomSendResponse):
                    await self._db.fed_mark_outbox(row["id"], event_id=resp.event_id); stats["sent"] += 1
                else:
                    await self._db.fed_mark_outbox(row["id"], error=str(resp)[:300]); stats["errors"] += 1
            except Exception as exc:
                name = type(exc).__name__
                msg = f"{name}: {exc}"[:300]
                await self._db.fed_mark_outbox(row["id"], error=msg); stats["errors"] += 1
                if "Unverified" in name:
                    await self._db.fed_update_partner(row["partner_id"], status="reverify",
                                                      last_error="partner has an unverified device — verify it before sharing resumes")
        return stats

    async def _send_document_as_media(self, room_id: str, content: dict):
        """Large document → encrypted attachment + small m.file event with our marker."""
        data = json.dumps(content).encode()
        up, keys = await self.client.upload(io.BytesIO(data), content_type="application/json",
                                            filename="buzzowl-doc.json", encrypt=True, filesize=len(data))
        if not getattr(up, "content_uri", None):
            raise RuntimeError(f"upload failed: {up}")
        file_info = {"url": up.content_uri, **keys}
        return await self.client.room_send(room_id, "m.room.message", {
            "msgtype": "m.file", "body": "buzzowl-doc.json",
            "info": {"mimetype": "application/json", "size": len(data)},
            "file": file_info,
            "de.buzzowl": {"kind": "document_ref", "group_key": content.get("group_key"),
                           "doc_id": (content.get("doc") or {}).get("doc_id"),
                           "sha256": hashlib.sha256(data).hexdigest()},
        }, ignore_unverified_devices=False)

    async def _download_json(self, file: dict) -> dict:
        from nio.crypto.attachments import decrypt_attachment
        dl = await self.client.download(mxc=file["url"])
        body = getattr(dl, "body", None)
        if body is None:
            raise RuntimeError(f"download failed: {dl}")
        data = decrypt_attachment(body, file["key"]["k"], file["hashes"]["sha256"], file["iv"])
        return json.loads(data.decode())

    # -- inbox → local db ---------------------------------------------------------

    async def _inbox_loop(self) -> None:
        while not self._stopping:
            try:
                await self.inbox_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("federation inbox tick (org %s): %s", self.org_id, exc)
            await asyncio.sleep(float(_cfg().get("inbox_interval_s", 5)))

    async def inbox_tick(self, limit: int = 100) -> dict:
        stats = {"applied": 0, "held": 0, "errors": 0}
        for row in await self._db.fed_pending_inbox(self.org_id, limit):
            kind = row["kind"]
            if kind not in OPEN_KINDS and not row.get("verified"):
                stats["held"] += 1
                continue                                # waits until the sending device is pinned
            if row.get("partner_status") in ("blocked",):
                stats["held"] += 1
                continue
            try:
                await apply_inbox_event(self, row)
                await self._db.fed_mark_inbox(row["id"])
                stats["applied"] += 1
            except Exception as exc:
                await self._db.fed_mark_inbox(row["id"], error=f"{type(exc).__name__}: {exc}")
                stats["errors"] += 1
        return stats

    # -- actions used by the API -----------------------------------------------------

    async def create_partner_room(self, partner_mxid: str) -> str:
        from nio import EnableEncryptionBuilder, RoomCreateResponse, RoomPreset, RoomVisibility
        await self._ensure_keys()
        resp = await self.client.room_create(
            visibility=RoomVisibility.private,
            name="Buzzowl share",                        # opaque on purpose (state is plaintext)
            invite=[partner_mxid],
            preset=RoomPreset.private_chat,
            initial_state=[
                EnableEncryptionBuilder().as_dict(),
                {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "joined"}},
            ],
            power_level_override={
                "users": {self.client.user_id: 100}, "users_default": 0, "events_default": 0, "state_default": 100,
                "invite": 100, "kick": 100, "ban": 100, "redact": 0,
                "events": {"m.room.tombstone": 100, "m.room.power_levels": 100, "m.room.history_visibility": 100,
                           "m.room.encryption": 100, "m.room.name": 100, "m.room.topic": 100},
            },
        )
        if not isinstance(resp, RoomCreateResponse):
            raise RuntimeError(f"room_create failed: {resp}")
        return resp.room_id

    async def join_room(self, room_id: str) -> None:
        from nio import JoinResponse
        resp = await self.client.join(room_id)
        if not isinstance(resp, JoinResponse):
            raise RuntimeError(f"join failed: {resp}")
        await self._ensure_keys()

    async def leave_room(self, room_id: str) -> None:
        try:
            await self.client.room_leave(room_id)
        except Exception as exc:
            logger.debug("leave room %s: %s", room_id, exc)

    async def pin_device(self, partner_mxid: str, device_id: str) -> dict:
        await self._ensure_keys()
        for d in self.client.device_store.active_user_devices(partner_mxid):
            if d.device_id == device_id:
                self.client.verify_device(d)
                return {"device_id": d.device_id, "ed25519": d.ed25519}
        raise RuntimeError("device not (yet) known — wait for the partner to join and sync")

    async def unpin_all(self, partner_mxid: str) -> None:
        for d in self.client.device_store.active_user_devices(partner_mxid):
            try:
                self.client.unverify_device(d)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Applying inbound events (shared with the local transport's db helpers)
# ---------------------------------------------------------------------------

async def apply_inbox_event(node: Node, row: dict) -> None:
    db = context.db_module
    import sharing
    org_id, partner_id, kind = row["org_id"], row["partner_id"], row["kind"]
    p = row.get("payload") or {}
    partner = await db.fed_get_partner(partner_id) if partner_id else None
    if not partner:
        raise RuntimeError("partner unknown")

    if kind == "hello":
        name = (p.get("org_name") or "").strip()[:120]
        if name:
            await db.fed_update_partner(partner_id, partner_name=name)
        return

    key = str(p.get("group_key") or "")
    if not key:
        raise RuntimeError("group_key missing")

    if kind == "share_invite":
        await db.sharing_create_remote_invite(org_id, partner_id, remote_group_key=key, remote_invite_id=p.get("invite_id"),
                                              client_name=str(p.get("client_name") or "Shared client")[:200],
                                              scope=sharing.normalize_scope(p.get("scope")), message=str(p.get("message") or "")[:500])
        return

    if kind == "share_accept":
        group = await db.sharing_group_by_key(org_id, key)
        if not group:
            raise RuntimeError("accept for unknown group")
        await db.sharing_add_remote_member(group["id"], partner_id)
        if p.get("invite_id"):
            await db.sharing_respond_invite(int(p["invite_id"]), "accepted")
        scope = sharing.normalize_scope(group.get("scope"))
        await db.sharing_enqueue_client(group["id"], org_id, group["my_client_id"], scope["doc_types"])
        return

    if kind == "share_decline":
        if p.get("invite_id"):
            await db.sharing_respond_invite(int(p["invite_id"]), "declined")
        return

    if kind == "share_leave":
        group = await db.sharing_group_by_key(org_id, key)
        if group:
            await db.sharing_remove_remote_member(group["id"], partner_id)
            if group.get("monitor_partner_id") == partner_id:
                await db.sharing_set_monitor_remote(group["id"], None, org_id)
        return

    group = await db.sharing_group_by_key(org_id, key)
    if not group:
        raise RuntimeError("event for a group this org is not a member of")
    scope = sharing.normalize_scope(group.get("scope"))
    prov = {"shared_client_key": key, "origin_org_id": None, "origin_org_name": partner.get("partner_name") or partner["partner_mxid"],
            "partner_id": partner_id, "partner_mxid": partner["partner_mxid"], "event_id": row.get("event_id"),
            "remote": True, "synced_at": _now().isoformat()}

    if kind in ("document", "document_ref"):
        doc = p.get("doc")
        if kind == "document_ref":
            content = await node._download_json(p["file"])
            doc = content.get("doc")
        if not isinstance(doc, dict) or not doc.get("doc_id") or not doc.get("type"):
            raise RuntimeError("malformed document payload")
        if not sharing.is_shareable_doc(doc["type"], "remote", scope):
            return                                        # not in our scope → ignore
        out = {
            "doc_id": str(doc["doc_id"])[:200], "type": str(doc["type"])[:40], "title": str(doc.get("title") or "")[:500],
            "content": str(doc.get("content") or "")[:400_000], "visibility": doc.get("visibility") or "shared",
            "metadata": {k: v for k, v in (doc.get("metadata") or {}).items() if k not in ("owner_ids",)} if isinstance(doc.get("metadata"), dict) else {},
            "shared_doc_id": sharing.shared_doc_id(key, f"p{partner_id}", str(doc["doc_id"])[:200]),
        }
        try:
            out["embedding"] = await db.embed_text(f"{out['title']}\n{out['content'][:2000]}") or None
        except Exception:
            out["embedding"] = None
        prov["origin_doc_id"] = out["doc_id"]
        await db.sharing_apply_document(org_id, group["my_client_id"], out, prov)
        return

    if kind == "document_delete":
        sid = sharing.shared_doc_id(key, f"p{partner_id}", str(p.get("doc_id") or ""))
        await db.sharing_delete_document(org_id, sid)
        return

    if kind == "profile":
        patch = p.get("patch") or {}
        patch = sharing.profile_patch(patch if isinstance(patch, dict) else {}, scope)
        if patch:
            await db.sharing_apply_profile(org_id, group["my_client_id"], patch, prov)
        return

    if kind == "monitor":
        who = p.get("monitor")
        if who == "me":                                   # partner takes over
            await db.sharing_set_monitor_remote(group["id"], partner_id, None)
        elif who == "you":                                # partner hands over to us
            await db.sharing_set_monitor_remote(group["id"], None, org_id)
        return


# ---------------------------------------------------------------------------
# Manager (module singleton)
# ---------------------------------------------------------------------------

class FederationManager:
    def __init__(self):
        self.nodes: dict[int, Node] = {}
        self._started = False

    async def start(self) -> None:
        if self._started or not enabled():
            return
        self._started = True
        db = context.db_module
        if not (context.DB_AVAILABLE and db):
            return
        try:
            idents = await db.fed_list_identities()
        except Exception as exc:
            logger.warning("federation: cannot list identities: %s", exc)
            return
        for ident in idents:
            await self.ensure_node(ident["org_id"], ident)

    async def stop(self) -> None:
        for n in list(self.nodes.values()):
            await n.stop()
        self.nodes.clear()
        self._started = False

    async def ensure_node(self, org_id: int, identity: Optional[dict] = None) -> Optional[Node]:
        if org_id in self.nodes:
            return self.nodes[org_id]
        db = context.db_module
        identity = identity or await db.fed_get_identity(org_id)
        if not identity or not identity.get("access_token_enc"):
            return None
        node = Node(org_id, identity, self)
        try:
            await node.start()
        except Exception as exc:
            logger.warning("federation node for org %s failed to start: %s", org_id, exc)
            await db.fed_upsert_identity(org_id, status="error", last_error=str(exc)[:300])
            return None
        self.nodes[org_id] = node
        return node

    async def drop_node(self, org_id: int) -> None:
        n = self.nodes.pop(org_id, None)
        if n:
            await n.stop()

    def node(self, org_id: int) -> Optional[Node]:
        return self.nodes.get(org_id)


manager = FederationManager()


async def start() -> None:
    await manager.start()


async def stop() -> None:
    await manager.stop()


# ---------------------------------------------------------------------------
# Identity setup (login or register), used by the API
# ---------------------------------------------------------------------------

async def _shared_secret_register(homeserver_url: str, secret: str, username: str, password: str, device_name: str) -> dict:
    """Synapse admin registration (registration_shared_secret): nonce → HMAC → account."""
    import aiohttp
    base = homeserver_url.rstrip("/")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/_synapse/admin/v1/register") as r:
            nonce = (await r.json())["nonce"]
        mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
        mac.update(nonce.encode()); mac.update(b"\x00"); mac.update(username.encode()); mac.update(b"\x00")
        mac.update(password.encode()); mac.update(b"\x00"); mac.update(b"notadmin")
        async with s.post(f"{base}/_synapse/admin/v1/register", json={
            "nonce": nonce, "username": username, "password": password, "admin": False, "mac": mac.hexdigest(),
            "displayname": device_name}) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(f"registration failed: {data}")
            return data


async def setup_identity(org_id: int, *, homeserver_url: str, username: str, password: str, register: bool,
                         display_name: str) -> dict:
    """Log in (or register + log in) the org's bot on the homeserver, persist the
    encrypted token, start the node."""
    from nio import AsyncClient, AsyncClientConfig, LoginResponse, RegisterResponse
    import plans
    db = context.db_module
    hs = homeserver_url.rstrip("/")
    # Log in with the localpart (or a full mxid if given) — the homeserver's own
    # server_name is unknown here; the real mxid comes back in the login response.
    localpart = username[1:].split(":")[0] if username.startswith("@") else username
    login_user = username if username.startswith("@") else localpart
    path = store_root() / str(org_id)
    path.mkdir(parents=True, exist_ok=True)
    client = AsyncClient(hs, login_user, store_path=str(path), config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True))
    try:
        if register:
            secret = _cfg().get("registration_shared_secret") or ""
            if secret:
                await _shared_secret_register(hs, secret, localpart, password, "buzzowl-bot")
            else:
                reg = await client.register(localpart, password, device_name="buzzowl-bot")
                if not isinstance(reg, RegisterResponse):
                    raise RuntimeError(f"registration failed: {reg}")
        resp = await client.login(password, device_name="buzzowl-bot")
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"login failed: {resp}")
        mxid = resp.user_id
        ident = await db.fed_upsert_identity(org_id, homeserver_url=hs, mxid=mxid, device_id=resp.device_id,
                                             access_token_enc=plans.encrypt_secret(resp.access_token),
                                             display_name=display_name, status="configured", last_error=None)
    finally:
        await client.close()
    await manager.drop_node(org_id)
    await manager.ensure_node(org_id, ident)
    return ident


async def remove_identity(org_id: int) -> None:
    await manager.drop_node(org_id)
    await context.db_module.fed_delete_identity(org_id)


# ---------------------------------------------------------------------------
# Convenience for routers
# ---------------------------------------------------------------------------

async def enqueue(org_id: int, partner_id: int, kind: str, payload: dict) -> int:
    return await context.db_module.fed_enqueue(org_id, partner_id, kind, payload)


def status(org_id: int) -> dict:
    n = manager.node(org_id)
    return {"enabled": enabled(), "running": bool(n), "online": bool(n and n.online),
            "last_error": n.last_error if n else None,
            "own_ed25519": n.identity.get("ed25519") if n else None,
            "own_fingerprint": fingerprint(n.identity.get("ed25519")) if n else None}
