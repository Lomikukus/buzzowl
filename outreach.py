"""
outreach.py — the supervised-outreach state machine (Phase 3).

An outreach item is a `documents` row with type='outreach'; its lifecycle is
`metadata.state`. The machine is the ONE place that decides which transition
is legal, who may perform it, and what it records — routers, the send worker,
the IMAP poller and the Pi tool all call transition() and never write `state`
directly.

    draft ──▶ pending_approval ──▶ approved ──▶ queued ──▶ sent ──▶ replied
      │              │                 │           │          └───▶ bounced
      │              └──▶ rejected     │           │          └───▶ followup_due
      └────────────────── cancelled ◀──┴───────────┘
                          (from any pre-sent state; rejected/cancelled → draft)

Hard rules (thesis DP2 — humans keep control):
  * Only a human user may move draft → pending_approval → approved (approve is
    admin/owner). The agent (Pi draft_outreach tool) can create drafts only.
  * Only the send worker moves approved → queued → sent (actor='worker').
  * Only ingestion (IMAP) moves sent → replied | bounced (actor='imap'); a human
    may set replied manually.
  * Every transition appends to metadata.history {from, to, actor, ts, note}.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

DOC_TYPE = "outreach"

DRAFT, PENDING, APPROVED, QUEUED, SENT = "draft", "pending_approval", "approved", "queued", "sent"
REPLIED, BOUNCED, FOLLOWUP_DUE, REJECTED, CANCELLED = "replied", "bounced", "followup_due", "rejected", "cancelled"

STATES = (DRAFT, PENDING, APPROVED, QUEUED, SENT, REPLIED, BOUNCED, FOLLOWUP_DUE, REJECTED, CANCELLED)
PRE_SENT = (DRAFT, PENDING, APPROVED, QUEUED)
TERMINAL = (REPLIED, BOUNCED, CANCELLED)

# Actor kinds
HUMAN, WORKER, IMAP, AGENT = "human", "worker", "imap", "agent"

# (from, to) → allowed actor kinds
_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    (DRAFT, PENDING):        (HUMAN,),
    (PENDING, APPROVED):     (HUMAN,),
    (PENDING, REJECTED):     (HUMAN,),
    (PENDING, DRAFT):        (HUMAN,),          # "send back for edits"
    (REJECTED, DRAFT):       (HUMAN,),
    (CANCELLED, DRAFT):      (HUMAN,),
    (APPROVED, QUEUED):      (WORKER,),
    (APPROVED, PENDING):     (HUMAN,),          # un-approve before the worker picks it up
    (QUEUED, SENT):          (WORKER,),
    (QUEUED, APPROVED):      (WORKER,),         # send failed → back for retry
    (SENT, REPLIED):         (IMAP, HUMAN),
    (SENT, BOUNCED):         (IMAP, WORKER),
    (SENT, FOLLOWUP_DUE):    (WORKER, HUMAN),
    (FOLLOWUP_DUE, REPLIED): (IMAP, HUMAN),
    (FOLLOWUP_DUE, SENT):    (HUMAN,),          # follow-up handled elsewhere
    (DRAFT, CANCELLED):      (HUMAN,),
    (PENDING, CANCELLED):    (HUMAN,),
    (APPROVED, CANCELLED):   (HUMAN,),
    (QUEUED, CANCELLED):     (HUMAN,),
}


class TransitionError(ValueError):
    pass


def allowed_targets(state: str, actor: str = HUMAN) -> list[str]:
    return [to for (frm, to), actors in _TRANSITIONS.items() if frm == state and actor in actors]


def can(state: str, target: str, actor: str) -> bool:
    return actor in _TRANSITIONS.get((state, target), ())


def transition(meta: dict, target: str, *, actor: str, actor_id: Optional[int] = None,
               note: str = "", extra: Optional[dict] = None) -> dict:
    """Return a NEW metadata dict with the transition applied (caller persists).
    Raises TransitionError when the move is not legal for this actor."""
    state = meta.get("state") or DRAFT
    if target not in STATES:
        raise TransitionError(f"unknown state {target!r}")
    if not can(state, target, actor):
        raise TransitionError(f"{actor} may not move {state} → {target}")
    new = dict(meta)
    new["state"] = target
    ts = datetime.now(timezone.utc).isoformat()
    hist = list(new.get("history") or [])
    hist.append({"from": state, "to": target, "actor": actor,
                 "actor_id": actor_id, "ts": ts, "note": note[:300]})
    new["history"] = hist[-50:]
    if target == APPROVED:
        new["approved_by"] = actor_id
        new["approved_at"] = ts
    if target == SENT:
        new["sent_at"] = ts
    if target == REPLIED:
        new["replied_at"] = ts
    if target == BOUNCED:
        new["bounced_at"] = ts
    if extra:
        new.update(extra)
    # legacy mirror so old readers (NBA weights, evaluation metrics) keep working
    new["outreach_status"] = legacy_status(target)
    return new


def legacy_status(state: str) -> str:
    """Map machine states onto the legacy 4-value outreach_status enum."""
    if state in (SENT, FOLLOWUP_DUE, BOUNCED):
        return "sent"
    if state == REPLIED:
        return "replied"
    return "generated"


def new_message_id(domain: str) -> str:
    """RFC 5322 Message-ID: <uuid@domain>."""
    dom = re.sub(r"[^A-Za-z0-9.-]", "", domain or "") or "buzzowl.local"
    return f"<{uuid.uuid4().hex}@{dom}>"


def new_draft_metadata(*, client_name: str, to_email: str = "", to_contact: str = "",
                       subject: str = "", sender_user_id: Optional[int] = None,
                       source: str = "human", agent_run_id: Optional[int] = None,
                       purpose: str = "", extra: Optional[dict] = None) -> dict:
    """Metadata for a freshly created outreach draft."""
    meta = {
        "state": DRAFT,
        "outreach_status": "generated",
        "client": client_name,
        "to_email": to_email,
        "to_contact": to_contact,
        "subject": subject,
        "sender_user_id": sender_user_id,
        "purpose": purpose,
        "source": source,               # human | agent
        "history": [{"from": None, "to": DRAFT, "actor": AGENT if source == "agent" else HUMAN,
                     "actor_id": sender_user_id, "ts": datetime.now(timezone.utc).isoformat(),
                     "note": "created"}],
    }
    if agent_run_id is not None:
        meta["agent_run_id"] = agent_run_id
    if extra:
        meta.update(extra)
    return meta


def summarize(meta: dict) -> dict:
    """Compact view for lists/queues."""
    return {
        "state": meta.get("state") or DRAFT,
        "client": meta.get("client"),
        "to_email": meta.get("to_email"),
        "to_contact": meta.get("to_contact"),
        "subject": meta.get("subject"),
        "sender_user_id": meta.get("sender_user_id"),
        "source": meta.get("source"),
        "approved_at": meta.get("approved_at"),
        "sent_at": meta.get("sent_at"),
        "replied_at": meta.get("replied_at"),
        "message_id": meta.get("message_id"),
        "last_error": meta.get("last_error"),
    }
