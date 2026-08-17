"""federation.py — pure rules + inbox application against a fake DB (Phase 5b)."""

import asyncio
from types import SimpleNamespace

import pytest

import context
import federation as fed
import sharing


def test_fingerprint_groups_of_four():
    assert fed.fingerprint("abcdefghijkl") == "abcd efgh ijkl"
    assert fed.fingerprint("abcdefghij==") == "abcd efgh ij"
    assert fed.fingerprint(None) == ""


def test_kinds_partition():
    assert "hello" in fed.OPEN_KINDS and "hello" not in fed.DATA_KINDS
    assert {"document", "document_delete", "profile", "share_invite", "share_accept"} <= fed.DATA_KINDS


class _FakeDB:
    """Just enough of db.py for apply_inbox_event."""

    def __init__(self):
        self.partners = {1: {"id": 1, "org_id": 5, "partner_mxid": "@bot:x", "partner_name": "Partner Co", "status": "active"}}
        self.groups = {"k1": {"id": 10, "key": "k1", "status": "active", "scope": {}, "my_client_id": 77,
                              "monitor_partner_id": None, "monitor_org_id": 5}}
        self.calls = []

    async def fed_get_partner(self, pid): return self.partners.get(pid)
    async def fed_update_partner(self, pid, **f): self.partners[pid].update(f); self.calls.append(("partner", f))
    async def sharing_group_by_key(self, org_id, key): return self.groups.get(key)
    async def sharing_create_remote_invite(self, org_id, pid, **kw): self.calls.append(("invite", kw)); return {"id": 1}
    async def sharing_add_remote_member(self, gid, pid, role="member"): self.calls.append(("add_remote", gid, pid))
    async def sharing_remove_remote_member(self, gid, pid): self.calls.append(("remove_remote", gid, pid))
    async def sharing_respond_invite(self, iid, status): self.calls.append(("respond", iid, status))
    async def sharing_enqueue_client(self, gid, org, cid, types): self.calls.append(("full_sync", gid, org, cid)); return 3
    async def sharing_set_monitor_remote(self, gid, pid, org): self.calls.append(("monitor", gid, pid, org))
    async def sharing_apply_document(self, org, cid, doc, prov): self.calls.append(("doc", org, cid, doc["shared_doc_id"], doc["type"], prov["remote"]))
    async def sharing_delete_document(self, org, sid): self.calls.append(("del", org, sid))
    async def sharing_apply_profile(self, org, cid, patch, prov): self.calls.append(("profile", org, cid, patch))
    async def embed_text(self, t): return None


@pytest.fixture
def fake(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(context, "db_module", db)
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    return db


def _row(kind, payload):
    return {"org_id": 5, "partner_id": 1, "kind": kind, "payload": payload, "event_id": "$e1"}


def test_apply_hello_sets_name(fake):
    asyncio.run(fed.apply_inbox_event(None, _row("hello", {"org_name": "  Partner GmbH "})))
    assert fake.partners[1]["partner_name"] == "Partner GmbH"


def test_apply_document_uses_partner_namespace_and_scope(fake):
    doc = {"doc_id": "d1", "type": "research", "title": "T", "content": "C", "metadata": {"owner_ids": [1], "x": 1}}
    asyncio.run(fed.apply_inbox_event(None, _row("document", {"group_key": "k1", "doc": doc})))
    call = next(c for c in fake.calls if c[0] == "doc")
    assert call[1] == 5 and call[2] == 77 and call[3] == "shared:k1:p1:d1" and call[4] == "research" and call[5] is True
    # a type outside the scope is ignored, not applied
    fake.calls.clear()
    asyncio.run(fed.apply_inbox_event(None, _row("document", {"group_key": "k1", "doc": {**doc, "type": "meeting"}})))
    assert not [c for c in fake.calls if c[0] == "doc"]


def test_apply_delete_profile_monitor(fake):
    asyncio.run(fed.apply_inbox_event(None, _row("document_delete", {"group_key": "k1", "doc_id": "d1"})))
    assert ("del", 5, "shared:k1:p1:d1") in fake.calls
    asyncio.run(fed.apply_inbox_event(None, _row("profile", {"group_key": "k1", "patch": {"industry": "X", "notes": "SECRET"}})))
    prof = next(c for c in fake.calls if c[0] == "profile")
    assert prof[3] == {"industry": "X"}                     # private key filtered by PROFILE_KEYS
    asyncio.run(fed.apply_inbox_event(None, _row("monitor", {"group_key": "k1", "monitor": "me"})))
    assert ("monitor", 10, 1, None) in fake.calls
    asyncio.run(fed.apply_inbox_event(None, _row("monitor", {"group_key": "k1", "monitor": "you"})))
    assert ("monitor", 10, None, 5) in fake.calls


def test_apply_share_accept_adds_remote_member_and_full_syncs(fake):
    asyncio.run(fed.apply_inbox_event(None, _row("share_accept", {"group_key": "k1", "invite_id": 42})))
    assert ("add_remote", 10, 1) in fake.calls and ("respond", 42, "accepted") in fake.calls
    assert ("full_sync", 10, 5, 77) in fake.calls


def test_apply_unknown_group_raises(fake):
    with pytest.raises(RuntimeError):
        asyncio.run(fed.apply_inbox_event(None, _row("document", {"group_key": "nope", "doc": {"doc_id": "d", "type": "research"}})))


def test_doc_payload_strips_owner_ids():
    p = sharing._doc_payload({"doc_id": "d", "type": "research", "title": "t", "content": "c",
                              "metadata": {"owner_ids": [1], "shared_from": {}, "keep": 1}, "created_at": None})
    assert "owner_ids" not in p["metadata"] and "shared_from" not in p["metadata"] and p["metadata"]["keep"] == 1
