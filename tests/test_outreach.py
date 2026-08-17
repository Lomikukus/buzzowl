"""State-machine tests for outreach.py — the human-in-the-loop guarantees."""

import pytest

import outreach as o


def _meta(state=o.DRAFT):
    m = o.new_draft_metadata(client_name="Acme", to_email="a@acme.test", subject="Hi")
    m["state"] = state
    return m


def test_new_draft_shape():
    m = o.new_draft_metadata(client_name="Acme", to_email="a@acme.test", subject="Hi",
                             sender_user_id=7, source="agent", agent_run_id=99)
    assert m["state"] == o.DRAFT and m["outreach_status"] == "generated"
    assert m["agent_run_id"] == 99 and m["history"][0]["actor"] == o.AGENT


def test_happy_path_human_then_worker_then_imap():
    m = _meta()
    m = o.transition(m, o.PENDING, actor=o.HUMAN, actor_id=1)
    m = o.transition(m, o.APPROVED, actor=o.HUMAN, actor_id=2)
    assert m["approved_by"] == 2 and m["approved_at"]
    m = o.transition(m, o.QUEUED, actor=o.WORKER)
    m = o.transition(m, o.SENT, actor=o.WORKER, extra={"message_id": "<x@y>"})
    assert m["sent_at"] and m["message_id"] == "<x@y>"
    assert m["outreach_status"] == "sent"            # legacy mirror
    m = o.transition(m, o.REPLIED, actor=o.IMAP)
    assert m["outreach_status"] == "replied" and m["replied_at"]
    assert [h["to"] for h in m["history"]] == [o.DRAFT, o.PENDING, o.APPROVED, o.QUEUED, o.SENT, o.REPLIED]


@pytest.mark.parametrize("state,target,actor", [
    (o.DRAFT, o.PENDING, o.AGENT),        # agent may not submit for approval
    (o.PENDING, o.APPROVED, o.AGENT),     # agent may not approve
    (o.PENDING, o.APPROVED, o.WORKER),    # worker may not approve
    (o.APPROVED, o.SENT, o.HUMAN),        # humans don't send directly — worker does
    (o.APPROVED, o.SENT, o.WORKER),       # must go through queued
    (o.DRAFT, o.SENT, o.WORKER),          # no shortcut from draft
    (o.SENT, o.REPLIED, o.WORKER),        # worker cannot fake a reply
    (o.SENT, o.REPLIED, o.AGENT),
    (o.REPLIED, o.SENT, o.HUMAN),         # terminal
    (o.PENDING, o.QUEUED, o.WORKER),      # worker only picks up approved
])
def test_forbidden_transitions(state, target, actor):
    with pytest.raises(o.TransitionError):
        o.transition(_meta(state), target, actor=actor)


def test_reject_and_reopen():
    m = o.transition(_meta(o.PENDING), o.REJECTED, actor=o.HUMAN, actor_id=1, note="tone")
    assert m["history"][-1]["note"] == "tone"
    m = o.transition(m, o.DRAFT, actor=o.HUMAN)
    assert m["state"] == o.DRAFT


def test_cancel_from_any_pre_sent_state_only():
    for s in o.PRE_SENT:
        assert o.can(s, o.CANCELLED, o.HUMAN)
    assert not o.can(o.SENT, o.CANCELLED, o.HUMAN)


def test_send_failure_returns_to_approved():
    m = o.transition(_meta(o.QUEUED), o.APPROVED, actor=o.WORKER, note="smtp 451",
                     extra={"last_error": "smtp 451"})
    assert m["state"] == o.APPROVED and m["last_error"] == "smtp 451"


def test_unknown_state_rejected():
    with pytest.raises(o.TransitionError):
        o.transition(_meta(), "yeeted", actor=o.HUMAN)


def test_message_id_shape():
    mid = o.new_message_id("mail.example.com")
    assert mid.startswith("<") and mid.endswith("@mail.example.com>")
    assert o.new_message_id("").endswith("@buzzowl.local>")


def test_allowed_targets_by_actor():
    assert set(o.allowed_targets(o.PENDING, o.HUMAN)) == {o.APPROVED, o.REJECTED, o.DRAFT, o.CANCELLED}
    assert o.allowed_targets(o.PENDING, o.WORKER) == []
    assert set(o.allowed_targets(o.APPROVED, o.WORKER)) == {o.QUEUED}


def test_legacy_status_mapping():
    assert o.legacy_status(o.DRAFT) == "generated"
    assert o.legacy_status(o.FOLLOWUP_DUE) == "sent"
    assert o.legacy_status(o.BOUNCED) == "sent"
    assert o.legacy_status(o.REPLIED) == "replied"
