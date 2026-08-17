"""
federation_bot.py — Phase 5 spike: two Buzzowl installs exchange one client card
over Matrix, end-to-end encrypted, with invite/accept as the consent handshake.

Roles (env ROLE):
  sender    — registers/logs in, creates an E2EE room, invites the partner bot,
              waits for the join (= consent), sends
                (1) a de.buzzowl.client_card timeline event (ENCRYPTED),
                (2) an updated card via m.relates_to/m.replace,
                (3) a de.buzzowl.index STATE event (to prove state is plaintext),
                (4) an oversized card (> 64 KiB) to measure the limit,
                (5) an encrypted media attachment (m.file with `file` key)
              and writes /store/proof.json.
  receiver  — registers/logs in, syncs forever, auto-joins invites from the
              expected partner (= "admin accepted" in the real UI), decrypts
              inbound events, downloads+decrypts the attachment, fetches the raw
              server-side event to show the homeserver only stores ciphertext,
              posts the card to Buzzowl's inbound endpoint (optional), writes
              /store/proof.json and exits.

Everything the receiver stores is treated as untrusted remote content: schema
checks, no HTML, provenance attached.
"""

import asyncio
import hashlib
import io
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import aiohttp
from nio import (
    AsyncClient, AsyncClientConfig, EnableEncryptionBuilder, InviteMemberEvent,
    LoginResponse, MatrixRoom, MegolmEvent, RegisterResponse, RoomCreateResponse,
    RoomEncryptedMedia, RoomPreset, RoomVisibility, RoomSendResponse, UnknownEvent,
    ErrorResponse,
)
from nio.crypto.attachments import decrypt_attachment

HS = os.environ.get("HOMESERVER", "http://synapse:8008")
ROLE = os.environ.get("ROLE", "receiver")
USER = os.environ["BOT_USER"]              # e.g. buzzowl-a
PASSWORD = os.environ["BOT_PASSWORD"]
PARTNER = os.environ.get("PARTNER_MXID", "")  # e.g. @buzzowl-b:synapse
ORG_NAME = os.environ.get("ORG_NAME", USER)
STORE = os.environ.get("STORE_DIR", "/store")
BUZZOWL_URL = os.environ.get("BUZZOWL_URL", "")            # e.g. http://host.docker.internal:8000
BUZZOWL_TOKEN = os.environ.get("AGENT_SERVICE_TOKEN", "")
BUZZOWL_ORG_ID = int(os.environ.get("BUZZOWL_ORG_ID", "0") or 0)
TIMEOUT_S = int(os.environ.get("TIMEOUT_S", "180"))

CARD_TYPE = "de.buzzowl.client_card"
INDEX_TYPE = "de.buzzowl.index"
SCHEMA_VERSION = 1

os.makedirs(STORE, exist_ok=True)
PROOF: dict = {"role": ROLE, "user": USER, "started": datetime.now(timezone.utc).isoformat(), "steps": []}


def log(msg: str, **kv):
    line = f"[{ROLE}] {msg}"
    if kv:
        line += " " + json.dumps(kv, default=str)
    print(line, flush=True)
    PROOF["steps"].append({"t": time.time(), "msg": msg, **kv})


def save_proof():
    with open(os.path.join(STORE, "proof.json"), "w") as f:
        json.dump(PROOF, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Login with a persistent device (same device_id across runs → stable E2EE keys)
# ---------------------------------------------------------------------------

async def connect() -> AsyncClient:
    cfg = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
    client = AsyncClient(HS, f"@{USER}:synapse", store_path=STORE, config=cfg)
    cred_path = os.path.join(STORE, "credentials.json")
    if os.path.exists(cred_path):
        cred = json.load(open(cred_path))
        client.restore_login(cred["user_id"], cred["device_id"], cred["access_token"])
        log("restored login", device_id=cred["device_id"])
        return client
    # first run: register (dev homeserver allows it) then login
    reg = await client.register(USER, PASSWORD, device_name=f"buzzowl-bot-{ROLE}")
    if isinstance(reg, RegisterResponse):
        log("registered", user_id=reg.user_id, device_id=reg.device_id)
    else:
        log("register skipped/failed (maybe exists)", detail=str(reg)[:120])
    resp = await client.login(PASSWORD, device_name=f"buzzowl-bot-{ROLE}")
    if not isinstance(resp, LoginResponse):
        raise SystemExit(f"login failed: {resp}")
    json.dump({"user_id": resp.user_id, "device_id": resp.device_id, "access_token": resp.access_token},
              open(cred_path, "w"))
    log("logged in", user_id=resp.user_id, device_id=resp.device_id)
    return client


async def ensure_keys(client: AsyncClient):
    if client.should_upload_keys:
        await client.keys_upload()
    if client.should_query_keys:
        await client.keys_query()


async def raw_event(client: AsyncClient, room_id: str, event_id: str) -> dict:
    """What the homeserver actually stores/serves for this event (raw JSON)."""
    url = f"{HS}/_matrix/client/v3/rooms/{room_id}/event/{event_id}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers={"Authorization": f"Bearer {client.access_token}"}) as r:
            return await r.json()


def build_card(padding: int = 0) -> dict:
    """The payload a Buzzowl install shares. Only fields inside the share scope."""
    card = {
        "schema": SCHEMA_VERSION,
        "kind": "client_card",
        "card_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"buzzowl:{ORG_NAME}:client:acme-corp")),
        "sender_org": ORG_NAME,
        "shared_at": datetime.now(timezone.utc).isoformat(),
        "share_scope": ["profile", "contacts", "findings_summary"],
        "client": {
            "name": "Acme Corp",
            "industry": "Manufacturing / Industrial Automation",
            "website": "https://acme.example",
            "location": "Berlin, DE",
            "summary": "Strategic account; automation initiative underway; downtime pain in Q3.",
        },
        "contacts": [
            {"name": "Jane Doe", "role": "CTO", "email": "jane.doe@acme.example"},
            {"name": "John Smith", "role": "Head of Operations"},
        ],
        "findings_summary": [
            {"title": "Unplanned downtime on line 3 (Q3)", "type": "pain_point", "date": "2026-08-01",
             "source_url": "https://acme.example/news/q3-update"},
        ],
        "provenance": {"instance": ORG_NAME, "generated_by": "buzzowl/spike"},
    }
    if padding:
        card["_padding"] = "x" * padding
    return card


# ---------------------------------------------------------------------------
# SENDER
# ---------------------------------------------------------------------------

async def run_sender():
    client = await connect()
    await client.sync(timeout=5000, full_state=True)
    await ensure_keys(client)

    # 1) create the org-pair room: private, invite-only, encrypted from the first event
    resp = await client.room_create(
        visibility=RoomVisibility.private,
        name=f"Buzzowl share: {ORG_NAME} ↔ {PARTNER}",
        topic="Buzzowl federation room — one per org pair; membership = consent",
        invite=[PARTNER],
        preset=RoomPreset.private_chat,
        initial_state=[EnableEncryptionBuilder().as_dict()],
    )
    if not isinstance(resp, RoomCreateResponse):
        raise SystemExit(f"room_create failed: {resp}")
    room_id = resp.room_id
    log("room created + partner invited", room_id=room_id, partner=PARTNER)
    PROOF["room_id"] = room_id

    # 2) wait for the partner to JOIN (this is the consent moment)
    deadline = time.time() + TIMEOUT_S
    joined = False
    while time.time() < deadline:
        await client.sync(timeout=3000)
        await ensure_keys(client)
        room: MatrixRoom = client.rooms.get(room_id)
        if room and PARTNER in room.users:
            joined = True
            break
    if not joined:
        log("partner did not join in time"); save_proof(); await client.close(); return
    log("partner joined (consent given)", members=list(client.rooms[room_id].users.keys()),
        encrypted=client.rooms[room_id].encrypted)
    await client.sync(timeout=2000)   # pick up partner device keys
    await ensure_keys(client)
    devices = client.device_store.active_user_devices(PARTNER)
    log("partner devices known", devices=[(d.device_id, d.ed25519, d.verified) for d in devices])
    PROOF["partner_devices"] = [{"device_id": d.device_id, "ed25519": d.ed25519} for d in devices]

    # 3) send the client card as an ENCRYPTED custom timeline event
    card = build_card()
    card_bytes = len(json.dumps(card).encode())
    r = await client.room_send(room_id, message_type=CARD_TYPE, content=card, ignore_unverified_devices=True)
    if not isinstance(r, RoomSendResponse):
        raise SystemExit(f"card send failed: {r}")
    log("card sent (encrypted)", event_id=r.event_id, plaintext_bytes=card_bytes)
    PROOF["card_event_id"] = r.event_id
    server_view = await raw_event(client, room_id, r.event_id)
    PROOF["server_view_card"] = server_view
    log("server view of card", type=server_view.get("type"),
        has_ciphertext="ciphertext" in server_view.get("content", {}),
        plaintext_leaked=("Acme" in json.dumps(server_view)))

    # 4) update the card (m.replace)
    card2 = build_card()
    card2["client"]["summary"] += " UPDATE: CFO approved automation budget."
    upd = {"m.new_content": card2, "m.relates_to": {"rel_type": "m.replace", "event_id": r.event_id}, **card2}
    r2 = await client.room_send(room_id, message_type=CARD_TYPE, content=upd, ignore_unverified_devices=True)
    log("card update sent (m.replace)", ok=isinstance(r2, RoomSendResponse), event_id=getattr(r2, "event_id", None))

    # 5) STATE event with an "index" — expected to be PLAINTEXT on the server
    st = await client.room_put_state(room_id, INDEX_TYPE, {"shared_client_ids": [card["card_id"]], "note": "STATE IS NOT ENCRYPTED"}, state_key="")
    st_id = getattr(st, "event_id", None)
    if st_id:
        sv = await raw_event(client, room_id, st_id)
        PROOF["server_view_state"] = sv
        log("server view of STATE event", type=sv.get("type"), content_plaintext=sv.get("content"))

    # 6) oversized card → measure the event size limit
    big = build_card(padding=70_000)
    rb = await client.room_send(room_id, message_type=CARD_TYPE, content=big, ignore_unverified_devices=True)
    log("oversized card (~70 KiB) result", ok=isinstance(rb, RoomSendResponse),
        error=(getattr(rb, "status_code", None), getattr(rb, "message", None)) if not isinstance(rb, RoomSendResponse) else None)
    PROOF["oversize_result"] = str(rb)[:300]

    # 7) encrypted media attachment for a "large document" (~120 KiB markdown)
    doc = ("# Research: Acme Corp — full report\n\n" + ("Lorem ipsum finding text. " * 5000)).encode()
    up, keys = await client.upload(io.BytesIO(doc), content_type="text/markdown", filename="acme-research.md",
                                   encrypt=True, filesize=len(doc))
    if isinstance(up, ErrorResponse):
        log("upload failed", error=str(up))
    else:
        file_info = {"url": up.content_uri, **keys}
        rf = await client.room_send(room_id, message_type="m.room.message", content={
            "msgtype": "m.file", "body": "acme-research.md",
            "info": {"mimetype": "text/markdown", "size": len(doc)},
            "file": file_info,
            "de.buzzowl": {"kind": "document", "doc_type": "research", "card_id": card["card_id"], "sha256": hashlib.sha256(doc).hexdigest()},
        }, ignore_unverified_devices=True)
        log("encrypted attachment sent", ok=isinstance(rf, RoomSendResponse), bytes=len(doc), mxc=up.content_uri)
        PROOF["attachment_sha256"] = hashlib.sha256(doc).hexdigest()

    save_proof()
    await client.close()
    log("sender done")


# ---------------------------------------------------------------------------
# RECEIVER
# ---------------------------------------------------------------------------

def validate_card(c: dict) -> list[str]:
    errs = []
    if c.get("schema") != SCHEMA_VERSION: errs.append("schema version")
    if c.get("kind") != "client_card": errs.append("kind")
    if not isinstance(c.get("client"), dict) or not c["client"].get("name"): errs.append("client.name")
    if not isinstance(c.get("contacts", []), list): errs.append("contacts")
    for k in ("card_id", "sender_org", "shared_at"):
        if not isinstance(c.get(k), str): errs.append(k)
    return errs


async def post_inbound(card: dict, prov: dict) -> dict:
    if not BUZZOWL_URL or not BUZZOWL_TOKEN or not BUZZOWL_ORG_ID:
        return {"skipped": "BUZZOWL_URL/AGENT_SERVICE_TOKEN/BUZZOWL_ORG_ID not set"}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BUZZOWL_URL}/api/internal/federation/inbound",
                          headers={"Authorization": f"Bearer {BUZZOWL_TOKEN}", "Content-Type": "application/json"},
                          json={"org_id": BUZZOWL_ORG_ID, "card": card, "provenance": prov}, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return {"status": r.status, "body": (await r.text())[:300]}


async def run_receiver():
    client = await connect()
    got = {"card": None, "update": None, "attachment": None, "state": None}
    done = asyncio.Event()

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key != client.user_id or event.membership != "invite":
            return
        if PARTNER and event.sender != PARTNER:
            log("IGNORED invite from unexpected sender", sender=event.sender, room=room.room_id)
            return
        # In the product this is the admin clicking "Accept partner" in the UI.
        j = await client.join(room.room_id)
        log("invite accepted (consent)", room=room.room_id, from_=event.sender, ok=not isinstance(j, ErrorResponse))

    async def on_custom(room: MatrixRoom, event: UnknownEvent):
        if event.type != CARD_TYPE:
            return
        content = event.source.get("content", {})
        rel = content.get("m.relates_to") or {}
        card = content.get("m.new_content") if rel.get("rel_type") == "m.replace" else content
        errs = validate_card(card or {})
        prov = {"room_id": room.room_id, "event_id": event.event_id, "sender": event.sender,
                "server_ts": event.server_timestamp, "decrypted": getattr(event, "decrypted", None),
                "verified_device": getattr(event, "verified", None), "sender_key": getattr(event, "sender_key", None),
                "received_at": datetime.now(timezone.utc).isoformat()}
        if rel.get("rel_type") == "m.replace":
            got["update"] = {"card": card, "prov": prov, "replaces": rel.get("event_id")}
            log("card UPDATE received + decrypted", replaces=rel.get("event_id"), errors=errs)
        else:
            got["card"] = {"card": card, "prov": prov}
            log("card received + decrypted", event_id=event.event_id, sender=event.sender, errors=errs,
                client=card.get("client", {}).get("name"), contacts=len(card.get("contacts", [])),
                verified_device=prov["verified_device"])
            with open(os.path.join(STORE, "inbox_card.json"), "w") as f:
                json.dump({"card": card, "provenance": prov}, f, indent=2)
            sv = await raw_event(client, room.room_id, event.event_id)
            PROOF["server_view_card"] = sv
            log("server view of card (what Synapse stores)", type=sv.get("type"),
                has_ciphertext="ciphertext" in sv.get("content", {}), plaintext_leaked=("Acme" in json.dumps(sv)))
            if not errs:
                res = await post_inbound(card, prov)
                log("posted to Buzzowl inbound", **res)
                PROOF["inbound_post"] = res
        maybe_done()

    async def on_media(room: MatrixRoom, event: RoomEncryptedMedia):
        dl = await client.download(mxc=event.url)
        if isinstance(dl, ErrorResponse):
            log("attachment download failed", error=str(dl)); return
        data = decrypt_attachment(dl.body, event.key["k"], event.hashes["sha256"], event.iv)
        sha = hashlib.sha256(data).hexdigest()
        with open(os.path.join(STORE, "inbox_" + event.body), "wb") as f:
            f.write(data)
        got["attachment"] = {"bytes": len(data), "sha256": sha, "extra": event.source.get("content", {}).get("de.buzzowl")}
        log("encrypted attachment received + decrypted", bytes=len(data), sha256=sha, body=event.body)
        maybe_done()

    async def on_undecryptable(room: MatrixRoom, event: MegolmEvent):
        log("UNDECRYPTABLE event (no session)", event_id=event.event_id, sender=event.sender)
        PROOF.setdefault("undecryptable", []).append(event.event_id)

    def maybe_done():
        if got["card"] and got["update"] and got["attachment"]:
            done.set()

    client.add_event_callback(on_invite, InviteMemberEvent)
    client.add_event_callback(on_custom, UnknownEvent)
    client.add_event_callback(on_media, RoomEncryptedMedia)
    client.add_event_callback(on_undecryptable, MegolmEvent)

    log("receiver syncing; waiting for invite from partner", partner=PARTNER)
    sync_task = asyncio.create_task(client.sync_forever(timeout=10000, full_state=True))
    try:
        await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
        log("all expected items received")
    except asyncio.TimeoutError:
        log("timeout waiting for items", got={k: bool(v) for k, v in got.items()})
    finally:
        # state event view (plaintext on server) for the report
        for room_id, room in client.rooms.items():
            try:
                st = await client.room_get_state_event(room_id, INDEX_TYPE, "")
                if hasattr(st, "content"):
                    got["state"] = st.content
                    log("STATE event readable in plaintext via API", content=st.content)
            except Exception:
                pass
        PROOF["received"] = {k: (v if k != "card" and k != "update" else {"prov": v["prov"], "client": v["card"].get("client", {}).get("name")}) if v else None for k, v in got.items()}
        save_proof()
        sync_task.cancel()
        try:
            await sync_task
        except (asyncio.CancelledError, Exception):
            pass
        await client.close()
        log("receiver done")


if __name__ == "__main__":
    try:
        asyncio.run(run_sender() if ROLE == "sender" else run_receiver())
    except Exception as exc:  # keep the proof even on failure
        PROOF["error"] = repr(exc)
        save_proof()
        raise
