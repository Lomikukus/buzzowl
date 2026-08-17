"""mailer.build_message header contract (no network) + send_email degradation."""

import pytest

import context
import mailer


@pytest.fixture()
def smtp_cfg(monkeypatch):
    monkeypatch.setattr(context, "config", {
        "smtp_host": "smtp.example.test", "smtp_port": 587,
        "smtp_from": "outreach@example.test", "smtp_from_name": "Example Sales",
    })
    monkeypatch.setattr(mailer, "config", context.config)


def test_build_message_headers_and_parts(smtp_cfg):
    msg = mailer.build_message(
        "buyer@client.test", "Hello", "<p>Hi <b>there</b></p><p>Bye</p>",
        from_name="Erika Rep", reply_to="erika@example.test",
        message_id="<abc@example.test>", in_reply_to="<prev@client.test>",
        references=["<root@client.test>"],
    )
    assert msg["From"] == "Erika Rep via Example Sales <outreach@example.test>"
    assert msg["Reply-To"] == "erika@example.test"
    assert msg["Message-ID"] == "<abc@example.test>"
    assert msg["In-Reply-To"] == "<prev@client.test>"
    assert msg["References"] == "<root@client.test> <prev@client.test>"
    assert msg["Date"]
    parts = msg.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert parts[0].get_payload(decode=True).decode() == "Hi there\nBye"


def test_build_message_without_identity_uses_org_name(smtp_cfg):
    msg = mailer.build_message("x@y.test", "S", "<p>b</p>")
    assert msg["From"] == "Example Sales <outreach@example.test>"
    assert msg["Reply-To"] is None and msg["Message-ID"] is None


def test_sender_domain(smtp_cfg):
    assert mailer.sender_domain() == "example.test"


def test_send_email_unconfigured_degrades(monkeypatch):
    monkeypatch.setattr(context, "config", {})
    monkeypatch.setattr(mailer, "config", context.config)
    ok, msg = mailer.send_email("x@y.test", "S", "<p>b</p>")
    assert ok is False and "not configured" in msg


def test_send_email_no_recipient(smtp_cfg):
    ok, msg = mailer.send_email("", "S", "<p>b</p>")
    assert ok is False and "recipient" in msg


def test_html_to_text():
    assert mailer.html_to_text("<div>a<br>b</div><p>c</p>") == "a\nb\nc"
