"""IMAP ingestion — seeded raw messages drive sent→replied and DSN→bounced.
process_message() is pure (no IMAP); the DB is a fake with our issued ids."""

from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

import context
import imap_sync
import outreach as o

OUR_MID = "<abc123@example.test>"
OTHER_MID = "<zzz@somewhere-else.test>"


class FakeDB:
    def __init__(self):
        m = o.new_draft_metadata(client_name="Acme", to_email="buyer@client.test", subject="Hi",
                                 sender_user_id=7)
        m = o.transition(m, o.PENDING, actor=o.HUMAN, actor_id=7)
        m = o.transition(m, o.APPROVED, actor=o.HUMAN, actor_id=1)
        m = o.transition(m, o.QUEUED, actor=o.WORKER)
        m = o.transition(m, o.SENT, actor=o.WORKER, extra={"message_id": OUR_MID})
        self.docs = {42: {"id": 42, "org_id": 1, "doc_id": "outreach-42", "title": "t", "metadata": m}}
        self.updates = []

    async def find_outreach_by_message_id(self, mid):
        for d in self.docs.values():
            if d["metadata"].get("message_id") == mid:
                return dict(d)
        return None

    async def update_document_metadata(self, org_id, int_id, meta, content=None, title=None):
        self.docs[int_id]["metadata"] = meta
        self.updates.append(int_id)
        return True


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(context, "config", {"smtp_from": "outreach@example.test"})
    db = FakeDB()
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(context, "db_module", db)
    return db


def _reply(in_reply_to=OUR_MID, references=None, subject="Re: Hi"):
    m = EmailMessage()
    m["From"] = "buyer@client.test"
    m["To"] = "erika@example.test"
    m["Subject"] = subject
    m["Message-ID"] = "<reply1@client.test>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content("Thanks, let's talk next week.")
    return m.as_bytes()


def _dsn(original_mid=OUR_MID):
    """A realistic multipart/report DSN as raw RFC822 (Postfix-style)."""
    return f"""From: MAILER-DAEMON@mx.client.test
To: outreach@example.test
Subject: Undelivered Mail Returned to Sender
Message-ID: <dsn1@mx.client.test>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="BND"

--BND
Content-Type: text/plain; charset=utf-8

The address buyer@client.test does not exist.

--BND
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.client.test

Final-Recipient: rfc822; buyer@client.test
Action: failed
Status: 5.1.1

--BND
Content-Type: text/rfc822-headers

Message-ID: {original_mid}
Subject: Hi
To: buyer@client.test

--BND--
""".encode()


# ---------------------------------------------------------------------------

async def test_reply_marks_replied(env):
    r = await imap_sync.process_message(_reply())
    assert r["kind"] == "reply" and r["message_ids"] == [OUR_MID]
    assert r["changes"] == [{"id": 42, "from_state": o.SENT, "to_state": o.REPLIED, "message_id": OUR_MID}]
    meta = env.docs[42]["metadata"]
    assert meta["state"] == o.REPLIED and meta["outreach_status"] == "replied"
    assert meta["history"][-1]["actor"] == o.IMAP


async def test_reply_via_references_only(env):
    raw = _reply(in_reply_to=None, references=f"<root@x.test> {OUR_MID}")
    r = await imap_sync.process_message(raw)
    assert r["kind"] == "reply" and env.docs[42]["metadata"]["state"] == o.REPLIED


async def test_foreign_thread_ignored(env):
    r = await imap_sync.process_message(_reply(in_reply_to=OTHER_MID))
    assert r["kind"] == "ignore" and env.updates == []


async def test_reply_is_idempotent(env):
    await imap_sync.process_message(_reply())
    r2 = await imap_sync.process_message(_reply())
    assert r2["changes"] == [] and env.updates == [42]


async def test_dsn_marks_bounced(env):
    r = await imap_sync.process_message(_dsn())
    assert r["kind"] == "bounce" and OUR_MID in r["message_ids"]
    assert env.docs[42]["metadata"]["state"] == o.BOUNCED
    assert env.docs[42]["metadata"]["outreach_status"] == "sent"      # legacy mirror unchanged


async def test_dsn_for_unknown_message_ignored(env):
    r = await imap_sync.process_message(_dsn(original_mid=OTHER_MID))
    assert r["kind"] == "ignore"


async def test_bounce_after_reply_not_applied(env):
    await imap_sync.process_message(_reply())
    r = await imap_sync.process_message(_dsn())
    assert r["changes"] == [] and env.docs[42]["metadata"]["state"] == o.REPLIED


async def test_poll_once_noop_when_unconfigured(env):
    assert (await imap_sync.poll_once())["skipped"]


def test_configured_flag(monkeypatch):
    monkeypatch.setattr(context, "config", {})
    assert imap_sync.configured() is False
    monkeypatch.setattr(context, "config", {"imap_host": "h", "imap_user": "u"})
    assert imap_sync.configured() is True
