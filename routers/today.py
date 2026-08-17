"""
routers/today.py — Next-best-action queue ("who to contact today and why").

Deterministic rule-based scoring over fresh signals, source-monitor flags,
open outreach, and staleness. One batched LLM call writes the reason prose
on top of the scored facts; template reasons are the fallback. The daily
snapshot is stored as a type=nba_queue documents row (no new tables) and
served to the home-page panel and the /today page.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request

import context
from context import DB_AVAILABLE, db_module, cache_get, cache_set
from routers.auth import current_user, _limit

logger = logging.getLogger("wk.today")

router = APIRouter()

_DEFAULT_WEIGHTS = {
    "signal_relevance": 3,
    "signal_type_bonus": {"pain_point": 2, "opportunity": 2, "risk": 1, "news": 0},
    "news_pending": 12,
    "outreach_draft": 10,
    "outreach_followup": 14,
    "recent_outreach": -6,
    "recent_contact": -6,      # rep logged a real contact recently → de-prioritise
    "contact_followup": 12,    # logged contact aged past follow-up window, no reply
    "stale_per_two_weeks": 1,
    "stale_cap": 4,
    "focus_multiplier": 1.5,
    "task_due_overdue": 14,
    "task_due_today": 10,
    "task_due_soon": 6,
}

# A generated draft older than this is treated as abandoned — no "send it" nudge.
_DRAFT_STALE_DAYS = 21

# Win-propensity weights — "best chance to win", NOT "who's neglected". Rewards
# buying intent + engagement + fit; deliberately ignores staleness.
_OPPORTUNITY_WEIGHTS = {
    "buying_signal": 6,          # per pain_point/opportunity signal
    "signal_relevance": 2,       # × relevance for those buying signals
    "buying_cap": 24,            # cap so one noisy client can't run away
    "engagement_reply": 12,      # a contact actually replied → warm lead
    "engagement_followup": 4,    # a logged contact flagged for follow-up
    "news_pending": 5,           # monitored sources just changed
    "product_fit": 5,            # a product match_report exists
    "draft_ready": 4,            # outreach already drafted (momentum)
}


def _require_db() -> None:
    if not DB_AVAILABLE:
        raise HTTPException(status_code=503, detail="DB unavailable")


def _parse_dt(value) -> Optional[datetime]:
    """Parse a timestamp that may be a datetime, ISO string, or bare date.

    clients.last_activity is a TEXT column — always parse in Python.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _last_status_ts(mail: dict, status: str) -> Optional[datetime]:
    """Newest history timestamp for a legacy status. Accepts both history shapes:
    legacy mail_template {"status","ts"} and Phase-3 outreach {"to","ts"} (state
    'sent' ⇔ legacy 'sent'; 'replied' ⇔ 'replied')."""
    for entry in reversed(mail.get("history") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == status or entry.get("to") == status:
            return _parse_dt(entry.get("ts"))
    return None


def _safe_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _owns(client: dict, uid: int) -> bool:
    """True if `uid` is the client's primary owner (created_by) or a co-owner
    (metadata.owner_ids). Mirrors the `isMine` rule used on the clients page."""
    if client.get("created_by") == uid:
        return True
    meta = client.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    owners = meta.get("owner_ids") or []
    try:
        return uid in [int(x) for x in owners]
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Scoring (pure — no I/O)
# ---------------------------------------------------------------------------

def compute_scores(
    clients: list[dict],
    signals_by_client: dict[str, list[dict]],
    outreach_by_client: dict[str, list[dict]],
    tasks_by_client: Optional[dict[str, list[dict]]] = None,
    contacts_log_by_client: Optional[dict[str, list[dict]]] = None,
    now: Optional[datetime] = None,
    weights: Optional[dict] = None,
    followup_days: int = 5,
    queue_size: int = 10,
) -> list[dict]:
    """Rank clients by contact priority. Deterministic; all links come from data."""
    now = now or datetime.now(timezone.utc)
    w = dict(_DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    type_bonus = w.get("signal_type_bonus") or {}
    tasks_by_client = tasks_by_client or {}
    contacts_log_by_client = contacts_log_by_client or {}
    today_date = now.date()

    entries = []
    for client in clients:
        name = client.get("name") or ""
        if not name:
            continue
        meta = client.get("metadata") or {}
        client_url = quote(name, safe="")
        facts: list[dict] = []
        score = 0.0
        has_draft = False
        has_overdue_sent = False
        top_signal_rel = 0

        # Fresh signals — top 3 by relevance
        sigs = sorted(
            signals_by_client.get(name, []),
            key=lambda s: -_safe_int(s.get("relevance"), 3),
        )[:3]
        for s in sigs:
            rel = _safe_int(s.get("relevance"), 3)
            stype = s.get("signal_type") or "news"
            score += rel * w["signal_relevance"] + (type_bonus.get(stype) or 0)
            top_signal_rel = max(top_signal_rel, rel)
            facts.append({
                "type": "signal",
                "signal_type": stype,
                "headline": s.get("title") or "",
                "relevance": rel,
                "doc_ref": s.get("doc_id"),
                "link": f"/news?client={client_url}",
                "source_url": s.get("source_url") or None,
            })

        # Source monitor flagged new info
        if meta.get("news_pending"):
            score += w["news_pending"]
            facts.append({
                "type": "news_pending",
                "headline": "Monitored sources changed",
                "detail": meta.get("news_pending_reason") or [],
                "link": f"/client/{client_url}",
            })

        # Open outreach
        for mail in outreach_by_client.get(name, []):
            status = mail.get("status") or "generated"
            if status == "generated":
                # Only a *fresh* draft is worth a "send it" nudge — old unsent drafts
                # are usually abandoned, so they don't get the boost or the action.
                created = _parse_dt(mail.get("created_at"))
                if created and (now - created).days > _DRAFT_STALE_DAYS:
                    continue
                score += w["outreach_draft"]
                has_draft = True
                facts.append({
                    "type": "outreach_draft",
                    "headline": mail.get("title") or "Drafted mail",
                    "doc_ref": mail.get("id"),
                    "link": f"/client/{client_url}#documents",
                })
            elif status == "sent":
                sent_at = _last_status_ts(mail, "sent") or _parse_dt(mail.get("created_at"))
                days = (now - sent_at).days if sent_at else None
                if days is not None and days >= followup_days:
                    score += w["outreach_followup"]
                    has_overdue_sent = True
                    facts.append({
                        "type": "outreach_followup",
                        "headline": mail.get("title") or "Sent mail",
                        "days_since_sent": days,
                        "doc_ref": mail.get("id"),
                        "link": f"/client/{client_url}#documents",
                    })
                else:
                    score += w["recent_outreach"]

        # Real logged outreach (contact_log) — mirror the draft/mail recency logic
        # for contacts the rep logged manually: stop nagging about a client just
        # emailed, and resurface one that's gone quiet since a contact with no reply.
        last_contact, contact_replied = None, False
        for cl in contacts_log_by_client.get(name, []):
            ts = _parse_dt(cl.get("sent_at"))
            if ts and (last_contact is None or ts > last_contact):
                last_contact, contact_replied = ts, bool(cl.get("replied"))
        if last_contact is not None:
            days_since_contact = (now - last_contact).days
            if days_since_contact < followup_days:
                score += w["recent_contact"]
                facts.append({
                    "type": "recent_contact",
                    "headline": f"You contacted them {days_since_contact}d ago",
                    "link": f"/client/{client_url}",
                })
            elif not contact_replied:
                score += w["contact_followup"]
                has_overdue_sent = True
                facts.append({
                    "type": "contact_followup",
                    "headline": f"No reply since you contacted them {days_since_contact}d ago",
                    "days_since_sent": days_since_contact,
                    "link": f"/client/{client_url}",
                })

        # Staleness
        last = _parse_dt(client.get("last_activity"))
        days_stale = (now - last).days if last else 60
        stale_pts = min((days_stale // 14) * w["stale_per_two_weeks"], w["stale_cap"])
        if stale_pts > 0:
            score += stale_pts
            facts.append({
                "type": "stale",
                "headline": f"No activity for {days_stale} days",
                "link": f"/client/{client_url}",
            })

        # Due / overdue to-dos for this client (rep-created or follow-ups)
        has_task_due = False
        for t in tasks_by_client.get(name, []):
            due = t.get("due_date")
            if not due:
                continue
            days_until = (due - today_date).days
            if days_until < 0:
                pts, when = w["task_due_overdue"], "overdue"
            elif days_until == 0:
                pts, when = w["task_due_today"], "today"
            elif days_until <= 3:
                pts, when = w["task_due_soon"], f"in {days_until}d"
            else:
                continue
            score += pts
            has_task_due = True
            facts.append({
                "type": "task_due",
                "headline": t.get("title") or "Task",
                "due_date": due.isoformat() if hasattr(due, "isoformat") else str(due),
                "when": when,
                "task_id": t.get("id"),
                "link": f"/client/{client_url}",
            })

        is_focus = bool(meta.get("is_focus"))
        if is_focus:
            score *= w["focus_multiplier"]

        if score <= 0:
            continue

        if has_draft:
            action = "send_draft"
        elif has_overdue_sent:
            action = "follow_up"
        elif has_task_due:
            action = "task"
        elif meta.get("news_pending"):
            action = "research"
        elif top_signal_rel >= 4:
            action = "mail"
        else:
            action = "research"
        action_link = _action_link(action, name)

        entries.append({
            "client": name,
            "client_id": client.get("id"),
            "score": round(score, 1),
            "is_focus": is_focus,
            "suggested_action": action,
            "action_link": action_link,
            "facts": facts,
        })

    # Focus clients are a hard top tier — a very high-scoring non-focus client
    # must never outrank a focus client (the 1.5x multiplier alone can't guarantee this).
    entries.sort(key=lambda e: (0 if e["is_focus"] else 1, -e["score"], e["client"]))
    entries = entries[:queue_size]
    for rank, e in enumerate(entries, start=1):
        e["rank"] = rank
    return entries


# ---------------------------------------------------------------------------
# LLM reasons (one batched call) + template fallback
# ---------------------------------------------------------------------------

NBA_ACTIONS = ("send_draft", "follow_up", "task", "mail", "research")


def _action_link(action: str, client_name: str) -> str:
    client_url = quote(client_name, safe="")
    return {
        "send_draft": f"/client/{client_url}#documents",
        "follow_up": f"/client/{client_url}#documents",
        "task": f"/client/{client_url}",
        "mail": f"/match?client={client_url}",
        "research": f"/client/{client_url}",
    }.get(action, f"/client/{client_url}")


def _allowed_nba_actions(entry: dict) -> list[str]:
    """Actions the LLM may choose for an entry — bounded by the facts, so it can
    never pick something the deterministic chain considers impossible (no draft
    ⇒ no send_draft, no unanswered mail ⇒ no follow_up, no due task ⇒ no task)."""
    types = {f["type"] for f in entry["facts"]}
    allowed = ["research"]
    if "outreach_draft" in types:
        allowed.append("send_draft")
    if "outreach_followup" in types:
        allowed.append("follow_up")
    if "task_due" in types:
        allowed.append("task")
    if "signal" in types:
        allowed.append("mail")
    return allowed


def _build_reason_prompt(entries: list[dict], choose_action: bool = False) -> str:
    """Autonomy seam (Phase 2): with choose_action=True the LLM sees the fact
    bundle BEFORE the action is fixed and picks the action + reason together
    (bounded by _allowed_nba_actions); otherwise it only writes the reason for
    the deterministically chosen action (legacy, level 0)."""
    items = []
    for e in entries:
        facts = []
        for f in e["facts"]:
            fc = {"type": f["type"], "headline": f.get("headline") or ""}
            for k in ("signal_type", "relevance", "days_since_sent", "detail", "when"):
                if f.get(k) not in (None, [], ""):
                    fc[k] = f[k]
            facts.append(fc)
        item = {"client": e["client"], "facts": facts}
        if choose_action:
            item["allowed_actions"] = _allowed_nba_actions(e)
            item["default_action"] = e["suggested_action"]
        else:
            item["suggested_action"] = e["suggested_action"]
        items.append(item)
    if choose_action:
        return (
            "You are the triage brain of a sales rep's daily queue. For each client in "
            "the JSON below, choose the single best next action from that client's "
            "allowed_actions and write 1-2 sentences explaining why today is a good day.\n"
            "Rules:\n"
            "- Use ONLY the facts provided. Never invent events, names, dates, or numbers.\n"
            "- Choose ONLY from allowed_actions; default_action is the rule-based choice — "
            "override it only when the facts clearly favour another allowed action.\n"
            "- Reference the most important fact explicitly.\n"
            "- Write in the same language as the fact headlines.\n"
            '- Return ONLY a JSON array: [{"client": "<name>", "action": "<allowed action>", '
            '"reason": "<text>"}] - no markdown fences, no other text.\n\n'
            "CLIENTS:\n" + json.dumps(items, ensure_ascii=False)
        )
    return (
        'You write short "why contact them today" notes for a sales rep\'s daily queue.\n'
        "For each client in the JSON below, write 1-2 sentences explaining why today is "
        "a good day to contact them.\n"
        "Rules:\n"
        "- Use ONLY the facts provided. Never invent events, names, dates, or numbers.\n"
        "- Reference the most important fact explicitly (signal headline, waiting draft, unanswered mail).\n"
        "- Write in the same language as the fact headlines.\n"
        '- Return ONLY a JSON array: [{"client": "<name>", "reason": "<text>"}] '
        "- no markdown fences, no other text.\n\n"
        "CLIENTS:\n" + json.dumps(items, ensure_ascii=False)
    )


def _parse_reason_json(text: str) -> dict[str, str]:
    """Map of lowercased client name → reason. Raises on unparseable output."""
    return {k: v["reason"] for k, v in _parse_reason_action_json(text).items()}


def _parse_reason_action_json(text: str) -> dict[str, dict]:
    """Map of lowercased client name → {"reason", "action"|None}. Raises on
    unparseable output. Action is optional (legacy reason-only replies)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    arr = json.loads(t)
    out: dict[str, dict] = {}
    for item in arr:
        if isinstance(item, dict) and item.get("client") and item.get("reason"):
            action = str(item.get("action") or "").strip().lower() or None
            out[str(item["client"]).strip().lower()] = {
                "reason": str(item["reason"]).strip(), "action": action}
    return out


def _template_reason(entry: dict) -> str:
    by_type: dict[str, dict] = {}
    for f in entry["facts"]:
        by_type.setdefault(f["type"], f)
    parts = []
    if "task_due" in by_type:
        f = by_type["task_due"]
        parts.append(f'Task "{f["headline"]}" is due {f.get("when", "soon")}.')
    if "outreach_draft" in by_type:
        parts.append(f'Drafted mail "{by_type["outreach_draft"]["headline"]}" is ready to send.')
    if "outreach_followup" in by_type:
        f = by_type["outreach_followup"]
        parts.append(f"Mail sent {f.get('days_since_sent', '?')} days ago with no reply — follow up.")
    if "signal" in by_type and len(parts) < 2:
        f = by_type["signal"]
        parts.append(f"Fresh {f.get('signal_type', 'news')} signal ({f.get('relevance', '?')}/5): {f['headline']}")
    if "news_pending" in by_type and len(parts) < 2:
        detail = ", ".join(by_type["news_pending"].get("detail") or []) or "monitored pages"
        parts.append(f"Monitored sources changed ({detail}) — research the update.")
    if "stale" in by_type and not parts:
        parts.append(f"{by_type['stale']['headline']} — time to check in.")
    return " ".join(parts[:2]) or "Worth a look today."


# ---------------------------------------------------------------------------
# Data gathering (3 batched queries) + snapshot
# ---------------------------------------------------------------------------

async def _gather_inputs(
    org_id: int, owner_id: Optional[int] = None
) -> tuple[list[dict], dict, dict, dict, dict]:
    clients = await db_module.list_clients(org_id)
    # Per-rep queue: a seller's Today/home queue is about THEIR clients only.
    if owner_id is not None:
        clients = [c for c in clients if _owns(c, owner_id)]
    signal_days = _safe_int(context.config.get("nba_signal_days"), 7)
    async with db_module._pool.acquire() as conn:
        # Resolve a signal's client from metadata.subject OR, when that's blank
        # (most client-specific signals store the company only in the title), from
        # the document_links table. Without this the queue is blind to ~95% of
        # client buying signals.
        sig_rows = await conn.fetch(
            """SELECT d.doc_id, d.title,
                      COALESCE(NULLIF(d.metadata->>'subject', ''), c.name) AS client,
                      d.metadata->>'signal_type' AS signal_type,
                      d.metadata->>'relevance_score' AS relevance,
                      d.metadata->>'scope' AS scope,
                      d.metadata->>'source_url' AS source_url, d.created_at
               FROM documents d
               LEFT JOIN document_links dl ON dl.document_id = d.id AND dl.entity_type = 'client'
               LEFT JOIN clients c ON c.id = dl.entity_id AND c.org_id = d.org_id
               WHERE d.org_id = $1 AND d.type = 'signal'
                 AND d.created_at >= NOW() - ($2::int * interval '1 day')""",
            org_id, signal_days,
        )
        # Legacy mail_template notes (metadata.subject = client) + Phase-3
        # outreach items (type='outreach', metadata.client). Both mirror the
        # legacy 4-value outreach_status so the scoring below stays unchanged.
        # For outreach items only pending/approved (i.e. actionable, not yet
        # sent) count as a "draft"; sent/followup_due count as "sent".
        mail_rows = await conn.fetch(
            """SELECT id, title, metadata->>'subject' AS client,
                      COALESCE(metadata->>'outreach_status', 'generated') AS status,
                      metadata->'outreach_history' AS history, created_at
               FROM documents
               WHERE org_id = $1 AND metadata->>'brief_type' = 'mail_template'
                 AND COALESCE(metadata->>'outreach_status', 'generated') IN ('generated', 'sent')
             UNION ALL
             SELECT id, title, metadata->>'client' AS client,
                    COALESCE(metadata->>'outreach_status', 'generated') AS status,
                    metadata->'history' AS history, created_at
               FROM documents
               WHERE org_id = $1 AND type = 'outreach'
                 AND metadata->>'state' IN ('draft', 'pending_approval', 'approved', 'sent', 'followup_due')""",
            org_id,
        )
    signals_by_client: dict[str, list[dict]] = defaultdict(list)
    _seen_sig: set = set()  # a signal linked to N clients yields N rows — dedupe per (signal, client)
    for r in sig_rows:
        if not r["client"]:
            continue
        key = (r["doc_id"], r["client"])
        if key in _seen_sig:
            continue
        _seen_sig.add(key)
        signals_by_client[r["client"]].append(dict(r))
    outreach_by_client: dict[str, list[dict]] = defaultdict(list)
    for r in mail_rows:
        if r["client"]:
            outreach_by_client[r["client"]].append(dict(r))
    # Open to-dos → keyed by client_name (rep-scoped for a per-rep queue)
    tasks_by_client: dict[str, list[dict]] = defaultdict(list)
    task_rows = await db_module.list_tasks(org_id, user_id=owner_id, include_done=False)
    for t in task_rows:
        if t.get("client_name"):
            tasks_by_client[t["client_name"]].append(t)
    # Real logged outreach — so scoring knows what the rep actually did.
    contacts_log_by_client: dict[str, list[dict]] = defaultdict(list)
    try:
        for c in await db_module.list_contact_log(org_id, user_id=owner_id, limit=500):
            if c.get("client_name"):
                contacts_log_by_client[c["client_name"]].append(c)
    except Exception:
        pass
    return (clients, dict(signals_by_client), dict(outreach_by_client),
            dict(tasks_by_client), dict(contacts_log_by_client))


async def compute_nba_queue(
    org_id: int, use_llm: bool = True, owner_id: Optional[int] = None
) -> dict:
    """Gather → score → write reasons → persist snapshot. Returns the snapshot.

    When `owner_id` is given, the queue is scoped to that rep's own clients and
    persisted as a per-rep snapshot (so every seller's Today/home shows their
    own book, not the whole org's)."""
    (clients, signals_by_client, outreach_by_client,
     tasks_by_client, contacts_log_by_client) = await _gather_inputs(org_id, owner_id)
    cfg = context.config or {}
    entries = compute_scores(
        clients, signals_by_client, outreach_by_client,
        tasks_by_client=tasks_by_client,
        contacts_log_by_client=contacts_log_by_client,
        now=datetime.now(timezone.utc),
        weights=cfg.get("nba_weights"),
        followup_days=_safe_int(cfg.get("nba_followup_days"), 5),
        queue_size=_safe_int(cfg.get("nba_queue_size"), 10),
    )

    # Autonomy seam (Phase 2): at level >= 1 the LLM chooses the action together
    # with the reason (fact bundle before the precedence chain); level 0 keeps
    # the legacy reason-only call. Same single batched call either way.
    import autonomy
    auto_level = await autonomy.level(org_id) if entries else 0
    choose_action = auto_level >= autonomy.LEVEL_OBSERVE

    llm_used = False
    picks: dict[str, dict] = {}
    if use_llm and entries:
        try:
            from routers.knowledge import _call_brain_sync
            prompt = _build_reason_prompt(entries, choose_action=choose_action)
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, lambda: _call_brain_sync(prompt))
            picks = _parse_reason_action_json(text)
            llm_used = bool(picks)
        except Exception as exc:
            logger.warning("NBA reason LLM failed — using template reasons: %s", exc)
    actions_changed = 0
    for e in entries:
        pick = picks.get(e["client"].strip().lower()) or {}
        if pick.get("reason"):
            e["reason"] = pick["reason"]
            e["reason_source"] = "llm"
        else:
            e["reason"] = _template_reason(e)
            e["reason_source"] = "template"
        if choose_action:
            chosen = pick.get("action")
            e["rule_action"] = e["suggested_action"]
            if chosen and chosen in _allowed_nba_actions(e) and chosen != e["suggested_action"]:
                e["suggested_action"] = chosen
                e["action_link"] = _action_link(chosen, e["client"])
                e["action_source"] = "llm"
                actions_changed += 1
            else:
                e["action_source"] = "rule"

    snapshot = {
        "queue": entries,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "llm_used": llm_used,
        "clients_considered": len(clients),
        "owner_id": owner_id,
        "autonomy_level": auto_level,
        "actions_changed_by_agent": actions_changed,
    }
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        scope = str(owner_id) if owner_id is not None else "org"
        lines = [
            f"{e['rank']}. {e['client']} ({e['suggested_action']}, score {e['score']}) — {e['reason']}"
            for e in entries
        ]
        await db_module.index_document(
            org_id=org_id,
            doc_id=f"nba-queue-{scope}-{today_str}",
            doc_type="nba_queue",
            title=f"Next-best-action queue {today_str}",
            content="\n".join(lines) or "No actionable clients today.",
            metadata=snapshot,
            embedding=[],
            source="agent",
        )
    except Exception as exc:
        logger.warning("NBA snapshot write failed (non-fatal): %s", exc)
    return snapshot


async def _load_latest_snapshot(
    org_id: int, owner_id: Optional[int] = None
) -> Optional[dict]:
    if not getattr(db_module, "_pool", None):
        return None
    async with db_module._pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT metadata FROM documents
               WHERE org_id = $1 AND type = 'nba_queue'
                 AND (metadata->>'owner_id')::int IS NOT DISTINCT FROM $2::int
               ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 1""",
            org_id, owner_id,
        )
    return (row["metadata"] or None) if row else None


# ---------------------------------------------------------------------------
# Opportunities to act on — research nudges + product-led reach-outs
# ---------------------------------------------------------------------------

# Words too generic to signal product relevance — ignored during overlap scoring
# so a coincidental "the"/"data" match never beats a real topical hit.
_PRODUCT_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "with", "to", "of", "in", "on", "at",
    "by", "is", "are", "be", "new", "our", "your", "their", "its", "as", "from",
    "ibm", "solution", "solutions", "platform", "product", "products", "service",
    "services", "software", "tool", "tools", "suite", "system", "systems",
})


def _tokenize(text: str) -> set:
    """Lowercased alphanumeric word tokens (len > 2), minus generic stopwords."""
    import re
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) > 2 and t not in _PRODUCT_STOPWORDS}


def _pick_signal_product(sig_title: str, products: list[dict]) -> Optional[dict]:
    """Choose the FOCUS product most relevant to the triggering signal.

    Deterministic keyword overlap between the signal title and each product's
    name + category + description/key-features (pre-tokenised into `_tokens`).
    Highest overlap wins; ties break on the match-report fit score, then on the
    product's original best-fit order. Falls back to the top-fit product (the
    list is passed best-fit-first) when nothing meaningfully overlaps."""
    if not products:
        return None
    sig_tokens = _tokenize(sig_title)
    best = None  # ((overlap, fit_score, -index), product)
    for idx, p in enumerate(products):
        overlap = len(sig_tokens & p.get("_tokens", set())) if sig_tokens else 0
        key = (overlap, _safe_int(p.get("score"), 0), -idx)
        if best is None or key > best[0]:
            best = (key, p)
    # No meaningful topical overlap → fall back to the overall top-fit product.
    if not sig_tokens or best[0][0] == 0:
        return products[0]
    return best[1]


async def _focus_product_matches(org_id: int) -> dict:
    """{client_name: [ {"product", "score", "category", "_tokens"}, ... ]} — the
    FULL list of matched FOCUS products per client, parsed from its latest match
    report and enriched with each product's category/description/key-features so
    the caller can pick the one most relevant to a given signal. Ordered best-fit
    first. Only focus products count, so WatsonX (un-focused) is never suggested."""
    try:
        focus_rows = await db_module.list_products(org_id, focus_only=True)
    except Exception:
        focus_rows = []
    if not focus_rows:
        return {}
    # product name -> {category, _tokens} enrichment from the products table
    focus_info: dict = {}
    for p in focus_rows:
        pname = (p.get("name") or "").strip()
        if not pname:
            continue
        feats = p.get("key_features") or []
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except Exception:
                feats = []
        feat_text = " ".join(str(f) for f in feats) if isinstance(feats, list) else str(feats)
        blob = " ".join([
            pname, p.get("category") or "", p.get("description") or "",
            p.get("target_customer") or "", feat_text,
        ])
        focus_info[pname] = {
            "category": p.get("category") or "",
            "_tokens": _tokenize(blob),
        }
    from routers.match import _FIT_HEADING  # lazy: avoid an import cycle
    out: dict = {}
    try:
        reports = await db_module.get_match_reports(org_id)  # newest-first
    except Exception:
        return {}
    for r in reports:
        cname = r.get("client_name")
        if not cname or cname in out:  # first row per client == latest report
            continue
        matched: dict = {}  # pname -> best fit score across headings in this report
        for _fit, score, product in _FIT_HEADING.findall(r.get("content") or ""):
            pname = product.strip()
            if pname in focus_info:
                sc = _safe_int(score, 0)
                if pname not in matched or sc > matched[pname]:
                    matched[pname] = sc
        if matched:
            items = [
                {"product": pn, "score": sc, **focus_info[pn]}
                for pn, sc in matched.items()
            ]
            items.sort(key=lambda p: (-p["score"], p["product"]))
            out[cname] = items
    return out


async def compute_overlooked(
    org_id: int, owner_id: Optional[int] = None,
    overlooked_days: Optional[int] = None, limit: int = 10,
) -> list[dict]:
    """Fresh, unaddressed client opportunities worth acting on. Rather than nagging
    about stale unsent drafts, this nudges the rep to *research* a new opportunity,
    or — when the client is already matched to a focus product — to *reach out about
    that product*. A client-specific (non-market) opportunity/pain-point signal that
    hasn't been acted on for `overlooked_days` is the trigger."""
    if overlooked_days is None:
        overlooked_days = _safe_int(context.config.get("overlooked_days"), 5)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=overlooked_days)
    (clients, signals_by_client, outreach_by_client,
     _tasks, contacts_log_by_client) = await _gather_inputs(org_id, owner_id)
    focus_match = await _focus_product_matches(org_id)

    # NBA-suggestion clicks per client (a weak "the rep engaged with this" signal)
    clicks_by_client: dict[str, list] = defaultdict(list)
    try:
        async with db_module._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT prompt AS client, created_at FROM prompt_log
                   WHERE org_id = $1 AND surface = 'nba_click'
                     AND ($2::bigint IS NULL OR user_id = $2)""",
                org_id, owner_id,
            )
        for r in rows:
            if r["client"]:
                clicks_by_client[r["client"]].append(r["created_at"])
    except Exception:
        pass

    out: list[dict] = []
    for client in clients:
        name = client.get("name") or ""
        if not name:
            continue
        client_url = quote(name, safe="")

        # Newest action of any kind (contact logged, NBA click, or sent mail)
        action_ts = [_parse_dt(c.get("sent_at")) for c in contacts_log_by_client.get(name, [])]
        action_ts += [_parse_dt(ts) for ts in clicks_by_client.get(name, [])]
        for mail in outreach_by_client.get(name, []):
            if mail.get("status") == "sent":
                action_ts.append(_last_status_ts(mail, "sent") or _parse_dt(mail.get("created_at")))
        action_ts = [t for t in action_ts if t]
        last_action = max(action_ts) if action_ts else None

        # Trigger: the oldest client-specific, non-market opportunity/pain-point
        # signal (relevance >= 4) that's aged past the cutoff. NOTE: stale unsent
        # drafts are intentionally NOT a trigger — they're old and not to be sent.
        trigger = None  # (ts, signal_title)
        for s in signals_by_client.get(name, []):
            st = s.get("signal_type") or ""
            title = (s.get("title") or "").strip()
            is_market = (s.get("scope") or "") == "market" or title[:7].lower() == "market:"
            if st in ("opportunity", "pain_point") and not is_market and _safe_int(s.get("relevance"), 3) >= 4:
                ts = _parse_dt(s.get("created_at"))
                if ts and ts < cutoff and (trigger is None or ts < trigger[0]):
                    trigger = (ts, title)
        if not trigger:
            continue
        opp_ts, sig_title = trigger
        # Only surface it if nothing was done since the opportunity appeared.
        if last_action is not None and last_action >= opp_ts:
            continue

        headline = sig_title[:80]
        # Pick the focus product most relevant to THIS signal (not the overall
        # top-fit), so a litigation signal isn't paired with an AI-coding product.
        picked = _pick_signal_product(sig_title, focus_match.get(name) or [])
        if picked:
            action = "reach_out"
            product = picked["product"]
            why = f"Reach out about {product} — {headline}"
        else:
            action = "research"
            product = None
            why = f"New opportunity — research {name} first: {headline}"
        out.append({
            "client": name,
            "client_id": client.get("id"),
            "overlooked_action": action,
            "product": product,
            "why": why,
            "age_days": (now - opp_ts).days,
            "link": f"/client/{client_url}",
        })

    out.sort(key=lambda e: -e["age_days"])
    return out[:limit]


# ---------------------------------------------------------------------------
# Win-propensity ranking — "best chance to win" across the whole book
# ---------------------------------------------------------------------------

async def compute_opportunity_scores(
    org_id: int, owner_id: Optional[int] = None, limit: int = 50,
) -> list[dict]:
    """Rank clients by chance-to-win: buying-intent signals + engagement + fit +
    fresh triggers. Unlike the NBA queue this ignores staleness (neglect ≠ chance)
    and applies no focus hard-tier — it's a pure opportunity leaderboard."""
    (clients, signals_by_client, outreach_by_client,
     _tasks, contacts_log_by_client) = await _gather_inputs(org_id, owner_id)
    w = _OPPORTUNITY_WEIGHTS

    # Which clients have a product match_report (linked via document_links).
    fit_ids: set = set()
    # Client-specific buying signals, joined via document_links. Many signals store
    # the client only in the title (metadata.subject is blank), so keying on subject
    # misses them — the link table is the reliable path. Market/industry news is
    # excluded so this reflects real per-account intent.
    buying_by_cid: dict = defaultdict(list)
    signal_days = _safe_int(context.config.get("nba_signal_days"), 7)
    try:
        async with db_module._pool.acquire() as conn:
            fit_rows = await conn.fetch(
                """SELECT DISTINCT dl.entity_id AS cid
                   FROM document_links dl JOIN documents d ON d.id = dl.document_id
                   WHERE d.org_id = $1 AND d.type = 'match_report' AND dl.entity_type = 'client'""",
                org_id,
            )
            fit_ids = {r["cid"] for r in fit_rows}
            buy_rows = await conn.fetch(
                """SELECT dl.entity_id AS cid, d.title,
                          COALESCE(d.metadata->>'relevance_score', '3') AS relevance,
                          d.metadata->>'signal_type' AS signal_type
                   FROM documents d JOIN document_links dl
                     ON dl.document_id = d.id AND dl.entity_type = 'client'
                   WHERE d.org_id = $1 AND d.type = 'signal'
                     AND d.metadata->>'signal_type' IN ('pain_point', 'opportunity')
                     AND d.title NOT ILIKE 'Market:%'
                     AND COALESCE(d.metadata->>'scope', '') <> 'market'
                     AND d.created_at >= NOW() - ($2::int * interval '1 day')""",
                org_id, signal_days,
            )
        for r in buy_rows:
            buying_by_cid[r["cid"]].append(dict(r))
    except Exception:
        pass

    entries: list[dict] = []
    for client in clients:
        name = client.get("name") or ""
        if not name:
            continue
        meta = client.get("metadata") or {}
        client_url = quote(name, safe="")
        score = 0.0
        reasons: list[str] = []

        buying = 0.0
        for s in buying_by_cid.get(client.get("id"), []):
            st = s.get("signal_type") or "opportunity"
            rel = _safe_int(s.get("relevance"), 3)
            buying += w["buying_signal"] + rel * w["signal_relevance"]
            reasons.append(f"{st.replace('_', ' ')}: {(s.get('title') or '').strip()[:70]}")
        if buying:
            score += min(buying, w["buying_cap"])

        logs = contacts_log_by_client.get(name, [])
        if any(c.get("replied") for c in logs):
            score += w["engagement_reply"]
            reasons.append("Replied to your outreach")
        if any(c.get("follow_up") for c in logs):
            score += w["engagement_followup"]

        if meta.get("news_pending"):
            score += w["news_pending"]
            reasons.append("Monitored sources changed")
        if client.get("id") in fit_ids:
            score += w["product_fit"]
            reasons.append("Product match on file")
        if any(m.get("status") == "generated" for m in outreach_by_client.get(name, [])):
            score += w["draft_ready"]
            reasons.append("Outreach already drafted")

        if score <= 0:
            continue
        entries.append({
            "client": name,
            "client_id": client.get("id"),
            "score": round(score, 1),
            "is_focus": bool(meta.get("is_focus")),
            "reasons": reasons[:3],
            "link": f"/client/{client_url}",
        })

    entries.sort(key=lambda e: (-e["score"], e["client"]))
    for rank, e in enumerate(entries, start=1):
        e["rank"] = rank
    return entries[:limit]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/next-actions")
async def get_next_actions(limit: int = 0, user: dict = Depends(current_user)):
    if not DB_AVAILABLE:
        return {"computed_at": None, "llm_used": False, "recomputed": False, "queue": []}
    org_id = user["org_id"]
    owner_id = user["id"]
    snapshot = await _load_latest_snapshot(org_id, owner_id)
    recomputed = False
    stale = True
    if snapshot and snapshot.get("computed_at"):
        ts = _parse_dt(snapshot["computed_at"])
        ttl_h = _safe_int(context.config.get("nba_snapshot_ttl_hours"), 24)
        stale = not ts or (datetime.now(timezone.utc) - ts).total_seconds() > ttl_h * 3600
    if not snapshot or stale:
        snapshot = await compute_nba_queue(org_id, owner_id=owner_id)
        recomputed = True
    queue = snapshot.get("queue") or []
    if limit > 0:
        queue = queue[:limit]
    return {
        "computed_at": snapshot.get("computed_at"),
        "llm_used": snapshot.get("llm_used", False),
        "recomputed": recomputed,
        "queue": queue,
    }


@router.get("/api/overlooked")
async def get_overlooked(limit: int = 10, user: dict = Depends(current_user)):
    """Opportunities the rep had but never acted on (aged, unaddressed)."""
    if not DB_AVAILABLE:
        return {"items": []}
    items = await compute_overlooked(user["org_id"], owner_id=user["id"], limit=max(1, min(limit, 25)))
    return {"items": items}


@router.get("/api/ranking")
async def get_ranking(scope: str = "mine", limit: int = 50, user: dict = Depends(current_user)):
    """Best-chance-to-win client leaderboard. scope: all | mine | focus.

    Cached for 5 min per (org, scope, owner, limit): the score is driven by signals
    and outreach that only change on the heartbeat cadence, and the computation is
    heavy enough (per-client) to briefly block the single-worker event loop — so we
    compute it at most once per window and serve the cached result (also keeps the
    News page, which reads this to order companies, from re-triggering it each load).
    """
    if not DB_AVAILABLE:
        return {"items": [], "scope": scope}
    limit = max(1, min(limit, 200))
    owner = None if scope == "all" else user["id"]
    cache_key = ("ranking", user["org_id"], scope, owner, limit)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    items = await compute_opportunity_scores(user["org_id"], owner_id=owner, limit=limit)
    if scope == "focus":
        items = [e for e in items if e.get("is_focus")]
        for rank, e in enumerate(items, start=1):
            e["rank"] = rank
    result = {"items": items, "scope": scope}
    cache_set(cache_key, result, ttl=300)
    return result


@router.post("/api/next-actions/refresh")
@_limit("3/minute")
async def refresh_next_actions(request: Request, user: dict = Depends(current_user)):
    _require_db()
    snapshot = await compute_nba_queue(user["org_id"], owner_id=user["id"])
    return {
        "computed_at": snapshot.get("computed_at"),
        "llm_used": snapshot.get("llm_used", False),
        "recomputed": True,
        "queue": snapshot.get("queue") or [],
    }


@router.post("/api/next-actions/click")
async def log_next_action_click(body: dict, user: dict = Depends(current_user)):
    """Thesis signal: were the queue's suggestions followed?"""
    client = (body.get("client") or "").strip()
    if not client:
        raise HTTPException(status_code=400, detail="client is required")
    db_module.log_prompt(user["org_id"], user["id"], "nba_click", client, {
        "action": (body.get("action") or "")[:40],
        "rank": body.get("rank"),
        "page": (body.get("page") or "")[:20],
    })
    return {"ok": True}
