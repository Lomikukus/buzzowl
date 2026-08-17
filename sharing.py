"""
sharing.py — shared clients: rules + transport + outbox worker (Phase 6a).

Model
-----
A *share group* (`shared_clients`) links one clients row per member org. Company
knowledge flows between members; personal data never does:

  shared   : documents of the group's scope.doc_types (research, osint, finding,
             signal by default), the profile keys in PROFILE_KEYS (industry, website,
             location, description, monitored sources …)
  private  : contacts, notes, meetings/transcripts, outreach, deals, tasks, owners,
             focus flags, deal fields — regardless of scope

Changes are detected by DB triggers (migration 005) that write `share_outbox`
rows; `process_outbox()` drains them through a Transport. `LocalTransport`
replicates inside one deployment (hosted ↔ hosted); a Matrix transport will
implement the same three calls for cross-instance sharing (Phase 5b).

Replicated documents carry `source='shared'`, a namespaced doc_id
(`shared:<group key>:<origin org>:<origin doc_id>`) and `metadata.shared_from`
provenance, and are linked to the member's own client row. Monitoring runs on
exactly one member (`shared_clients.monitor_org_id`) — the others receive results.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Protocol

logger = logging.getLogger("wk.sharing")

DEFAULT_DOC_TYPES = ["research", "osint", "finding", "signal"]

# Client metadata keys that ARE company knowledge and may replicate.
PROFILE_KEYS = (
    "industry", "website", "url", "domain", "location", "hq", "country",
    "description", "summary", "company_size", "employees", "founded", "linkedin",
    "tags", "monitored_sources", "sources_discovered_at",
)
# Keys that must never replicate (documented for reviewers; PROFILE_KEYS is the allowlist).
PRIVATE_KEYS_EXAMPLES = ("notes", "owner_ids", "is_focus", "deal_stage", "deal_value",
                         "status", "news_pending", "last_monitor_at")

DEFAULT_SCOPE = {"doc_types": DEFAULT_DOC_TYPES, "profile": True, "sources": True, "contacts": False}


class SharingError(ValueError):
    pass


def normalize_scope(scope: Optional[dict]) -> dict:
    s = dict(DEFAULT_SCOPE)
    if isinstance(scope, dict):
        if isinstance(scope.get("doc_types"), list):
            s["doc_types"] = [str(t) for t in scope["doc_types"] if str(t) in DEFAULT_DOC_TYPES]
        for k in ("profile", "sources", "contacts"):
            if k in scope:
                s[k] = bool(scope[k])
    return s


def shared_doc_id(group_key: str, origin_org_id: int, origin_doc_id: str) -> str:
    return f"shared:{group_key}:{origin_org_id}:{origin_doc_id}"


def profile_patch(metadata: dict, scope: dict) -> dict:
    """The subset of a client's metadata that may replicate under this scope."""
    scope = normalize_scope(scope)
    if not scope.get("profile"):
        return {}
    out = {}
    for k in PROFILE_KEYS:
        if k in ("monitored_sources", "sources_discovered_at") and not scope.get("sources"):
            continue
        if k in metadata:
            out[k] = metadata[k]
    return out


def is_shareable_doc(doc_type: str, source: str, scope: dict) -> bool:
    return source != "shared" and doc_type in normalize_scope(scope)["doc_types"]


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------

class Transport(Protocol):
    async def apply_document(self, target_org_id: int, target_client_id: int, doc: dict, provenance: dict) -> None: ...
    async def delete_document(self, target_org_id: int, shared_doc_id: str) -> None: ...
    async def apply_profile(self, target_org_id: int, target_client_id: int, patch: dict, provenance: dict) -> None: ...


class LocalTransport:
    """Same deployment: write straight into the member org's tables, with the
    session flag that keeps the triggers from echoing."""

    def __init__(self, db_module):
        self.db = db_module

    async def apply_document(self, target_org_id: int, target_client_id: int, doc: dict, provenance: dict) -> None:
        await self.db.sharing_apply_document(target_org_id, target_client_id, doc, provenance)

    async def delete_document(self, target_org_id: int, shared_doc_id_: str) -> None:
        await self.db.sharing_delete_document(target_org_id, shared_doc_id_)

    async def apply_profile(self, target_org_id: int, target_client_id: int, patch: dict, provenance: dict) -> None:
        await self.db.sharing_apply_profile(target_org_id, target_client_id, patch, provenance)


_transport: Optional[Transport] = None


def get_transport():
    global _transport
    if _transport is None:
        import context
        _transport = LocalTransport(context.db_module)
    return _transport


# ---------------------------------------------------------------------------
# Outbox worker
# ---------------------------------------------------------------------------

async def process_outbox(limit: int = 200) -> dict:
    """Drain pending share_outbox rows. Returns counters. Safe to call often."""
    import context
    db = context.db_module
    if not context.DB_AVAILABLE or db is None:
        return {"skipped": "db unavailable"}
    transport = get_transport()
    rows = await db.sharing_pending_outbox(limit)
    stats = {"processed": 0, "applied": 0, "errors": 0}
    for row in rows:
        try:
            n = await _process_row(db, transport, row)
            await db.sharing_mark_outbox(row["id"], error=None)
            stats["processed"] += 1
            stats["applied"] += n
        except Exception as exc:  # keep draining; the row is retried later
            logger.warning("sharing outbox #%s failed: %s", row["id"], exc)
            await db.sharing_mark_outbox(row["id"], error=str(exc)[:500])
            stats["errors"] += 1
    return stats


async def _process_row(db, transport: Transport, row: dict) -> int:
    group = await db.sharing_get_group(row["shared_client_id"])
    if not group or group["status"] != "active":
        return 0
    scope = normalize_scope(group.get("scope"))
    members = [m for m in await db.sharing_list_members(group["id"]) if m["left_at"] is None]
    origin = row["origin_org_id"]
    targets = [m for m in members if m["org_id"] != origin]
    if not targets:
        return 0
    origin_member = next((m for m in members if m["org_id"] == origin), None)
    origin_org = await db.get_org(origin) if hasattr(db, "get_org") else None
    prov_base = {
        "shared_client_key": str(group["key"]),
        "origin_org_id": origin,
        "origin_org_name": (origin_org or {}).get("name"),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    kind = row["kind"]
    applied = 0
    if kind == "document":
        doc = await db.sharing_get_document(row["ref_id"])
        if not doc or doc.get("source") == "shared" or not is_shareable_doc(doc["type"], doc["source"], scope):
            return 0
        prov = {**prov_base, "origin_doc_id": doc["doc_id"], "origin_document_id": doc["id"],
                "origin_type": doc["type"]}
        doc_out = dict(doc)
        doc_out["shared_doc_id"] = shared_doc_id(prov["shared_client_key"], origin, doc["doc_id"])
        for m in targets:
            await transport.apply_document(m["org_id"], m["client_id"], doc_out, prov)
            applied += 1
    elif kind == "document_delete":
        origin_doc_id = (row.get("payload") or {}).get("doc_id")
        if not origin_doc_id:
            return 0
        sid = shared_doc_id(str(group["key"]), origin, origin_doc_id)
        for m in targets:
            await transport.delete_document(m["org_id"], sid)
            applied += 1
    elif kind == "profile":
        if not origin_member:
            return 0
        client = await db.get_client_by_id(origin, origin_member["client_id"])
        if not client:
            return 0
        meta = client.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        patch = profile_patch(meta, scope)
        if not patch:
            return 0
        prov = {**prov_base, "keys": sorted(patch)}
        for m in targets:
            await transport.apply_profile(m["org_id"], m["client_id"], patch, prov)
            applied += 1
    elif kind == "full_sync":
        # enqueue every shareable doc + the profile of the origin member (used on join)
        if not origin_member:
            return 0
        n = await db.sharing_enqueue_client(group["id"], origin, origin_member["client_id"], scope["doc_types"])
        applied += n
    return applied


# ---------------------------------------------------------------------------
# Monitoring coordination
# ---------------------------------------------------------------------------

async def monitor_org_for_client(org_id: int, client_id: int) -> Optional[int]:
    """None when the client is not shared; otherwise the org that runs monitoring."""
    import context
    db = context.db_module
    if not context.DB_AVAILABLE or db is None:
        return None
    g = await db.sharing_group_for_client(client_id)
    if not g or g["status"] != "active":
        return None
    return g.get("monitor_org_id")


async def should_monitor(org_id: int, client_id: int) -> bool:
    """Heartbeat/monitor gate: True for unshared clients and for the monitor org."""
    m = await monitor_org_for_client(org_id, client_id)
    return m is None or m == org_id
