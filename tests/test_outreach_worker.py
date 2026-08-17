"""Send-worker e2e against a real local SMTP server (aiosmtpd) + guardrails.

The DB is a fake module that implements exactly the helpers the worker uses,
so the state machine, guardrails, identity, headers and contact_log write are
exercised end-to-end without Postgres.
"""

import asyncio
import email
from datetime import datetime, timedelta, timezone

import pytest
from aiosmtpd.controller import Controller

import autonomy
import context
import mailer
import outreach as o
from routers import outreach as r


class _Handler:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(email.message_from_bytes(envelope.content))
        return "250 OK"


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def smtp():
    h = _Handler()
    c = Controller(h, hostname="127.0.0.1", port=_free_port())
    c.start()
    yield c, h
    c.stop()


class FakeDB:
    def __init__(self, settings):
        self.settings = settings
        self.docs: dict[int, dict] = {}
        self.contact_log = []
        self._next = 1
        self.sent_history = {}      # to_email → last sent_at

    async def get_org_settings(self, org_id):
        return dict(self.settings)

    def add(self, org_id, meta, content="Hello <b>there</b>", title="Outreach"):
        i = self._next; self._next += 1
        self.docs[i] = {"id": i, "org_id": org_id, "doc_id": f"outreach-{i}", "title": title,
                        "content": content, "metadata": meta, "type": "outreach", "created_by": 1}
        return i

    async def claim_next_approved_outreach(self, org_id=None, exclude_org_ids=None):
        for d in sorted(self.docs.values(), key=lambda x: x["id"]):
            if d["org_id"] in (exclude_org_ids or set()):
                continue
            if d["metadata"].get("state") == o.APPROVED and (org_id is None or d["org_id"] == org_id):
                d["metadata"] = o.transition(d["metadata"], o.QUEUED, actor=o.WORKER)
                return dict(d)
        return None

    async def update_document_metadata(self, org_id, int_id, metadata, content=None, title=None):
        self.docs[int_id]["metadata"] = metadata
        return True

    async def count_outreach_sent_today(self, org_id):
        return sum(1 for d in self.docs.values() if d["metadata"].get("state") in (o.SENT, o.REPLIED))

    async def last_outreach_sent_to(self, org_id, to_email):
        return self.sent_history.get(to_email.lower())

    async def get_user_identity(self, org_id, user_id):
        return {"display_name": "Erika Rep", "reply_to": "erika@example.test",
                "signature": "Best,\nErika", "email": "erika@example.test"}

    async def log_contact(self, org_id, user_id, client_name, **kw):
        self.contact_log.append({"client": client_name, **kw})
        return len(self.contact_log)


@pytest.fixture()
def env(monkeypatch, smtp):
    controller, handler = smtp
    cfg = {"smtp_host": "127.0.0.1", "smtp_port": controller.port,
           "smtp_from": "outreach@example.test", "smtp_from_name": "Example Sales"}
    monkeypatch.setattr(context, "config", cfg)
    monkeypatch.setattr(mailer, "config", cfg)
    db = FakeDB({"outreach_enabled": True, "outreach_quiet_hours": [0, 0],
                 "outreach_max_per_day": 10, "outreach_contact_floor_days": 7})
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(context, "db_module", db)
    monkeypatch.setattr(r, "DB_AVAILABLE", True)
    monkeypatch.setattr(r, "db_module", db)
    return db, handler


def _approved(sender=7, to="buyer@client.test", client="Acme"):
    m = o.new_draft_metadata(client_name=client, to_email=to, to_contact="Bea Buyer",
                             subject="Quick idea for Acme", sender_user_id=sender)
    m = o.transition(m, o.PENDING, actor=o.HUMAN, actor_id=sender)
    return o.transition(m, o.APPROVED, actor=o.HUMAN, actor_id=1)


# ---------------------------------------------------------------------------

async def test_worker_sends_with_identity_and_logs_contact(env):
    db, handler = env
    oid = db.add(1, _approved())
    res = await r.send_one()
    assert res["sent"] is True and res["id"] == oid
    # state machine
    meta = db.docs[oid]["metadata"]
    assert meta["state"] == o.SENT and meta["message_id"].startswith("<") and meta["sent_at"]
    assert meta["outreach_status"] == "sent"
    # what actually left over SMTP
    assert len(handler.messages) == 1
    msg = handler.messages[0]
    assert msg["From"] == "Erika Rep via Example Sales <outreach@example.test>"
    assert msg["Reply-To"] == "erika@example.test"
    assert msg["Message-ID"] == meta["message_id"]
    assert msg["To"] == "buyer@client.test" and msg["Subject"] == "Quick idea for Acme"
    html = [p for p in msg.walk() if p.get_content_type() == "text/html"][0].get_payload(decode=True).decode()
    assert "Erika" in html          # signature appended
    # contact_log written
    assert db.contact_log[0]["client"] == "Acme" and db.contact_log[0]["contact_email"] == "buyer@client.test"


async def test_worker_noop_when_nothing_approved(env):
    assert await r.send_one() is None


async def test_guardrail_disabled_holds_and_records_reason(env):
    db, handler = env
    db.settings["outreach_enabled"] = False
    oid = db.add(1, _approved())
    res = await r.send_one()
    assert res["sent"] is False and "disabled" in res["reason"]
    assert db.docs[oid]["metadata"]["state"] == o.APPROVED       # back for retry, not lost
    assert "disabled" in db.docs[oid]["metadata"]["last_error"]
    assert handler.messages == []


async def test_guardrail_kill_switch(env):
    db, handler = env
    db.settings["outreach_kill_switch"] = True
    db.add(1, _approved())
    res = await r.send_one()
    assert res["sent"] is False and "kill switch" in res["reason"] and handler.messages == []


async def test_guardrail_daily_cap(env):
    db, handler = env
    db.settings["outreach_max_per_day"] = 1
    db.add(1, o.transition(_approved(), o.QUEUED, actor=o.WORKER) | {"state": o.SENT})  # one already sent today
    db.add(1, _approved(to="second@client.test"))
    res = await r.send_one()
    assert res["sent"] is False and "daily send cap" in res["reason"]


async def test_guardrail_quiet_hours(env):
    db, handler = env
    h = datetime.now(timezone.utc).hour
    db.settings["outreach_quiet_hours"] = [h, (h + 1) % 24]      # now is inside
    db.add(1, _approved())
    res = await r.send_one()
    assert res["sent"] is False and "quiet hours" in res["reason"]


async def test_guardrail_contact_floor(env):
    db, handler = env
    db.sent_history["buyer@client.test"] = datetime.now(timezone.utc) - timedelta(days=2)
    db.add(1, _approved())
    res = await r.send_one()
    assert res["sent"] is False and "contact floor" in res["reason"]
    assert handler.messages == []


async def test_guardrail_no_recipient(env):
    db, _ = env
    db.add(1, _approved(to=""))
    res = await r.send_one()
    assert res["sent"] is False and "no recipient" in res["reason"]


async def test_smtp_failure_returns_to_approved(env, monkeypatch):
    db, _ = env
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: (False, "451 try later"))
    oid = db.add(1, _approved())
    res = await r.send_one()
    assert res["sent"] is False and "smtp: 451" in res["reason"]
    assert db.docs[oid]["metadata"]["state"] == o.APPROVED


async def test_worker_tick_stops_after_refusal(env):
    db, handler = env
    db.settings["outreach_enabled"] = False
    db.add(1, _approved()); db.add(1, _approved(to="b@x.test"))
    results = await r.worker_tick()
    assert len(results) == 1            # org-wide refusal → don't hammer the rest of THAT org
    assert results[0]["org_id"] == 1 and results[0]["sent"] is False


async def test_guardrail_status_endpoint_shape(env):
    db, _ = env
    st = await r._guardrails(1)
    assert st["ok"] is True and st["enabled"] and st["smtp_configured"]
    assert set(("max_per_day", "sent_today", "quiet_hours", "contact_floor_days")) <= set(st)
