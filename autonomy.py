"""
autonomy.py — the one decision layer behind every autonomous agent action.

Design (thesis DP2: autonomy only together with trust & oversight):

  Levels (per org, orgs.settings.autonomy_level):
    0  off      — today's behaviour, byte-for-byte. Nothing here runs.
    1  observe  — decide + log every decision, never act.
    2  act      — may autonomously trigger research / osint / match runs.
    3  outreach — additionally may DRAFT outreach (never send; sending is
                  always human-approved — Phase 3).

  Every decision goes through decide() → Decision and is written to
  agent_runs (agent_type='autonomy_review', trigger_type='autonomous') —
  including skips, so the audit trail shows what the agent chose NOT to do.
  Actions the agent takes carry trigger_type='autonomous' and link back to
  the decision run id, so any autonomous document is traceable to a logged
  reason with evidence.

  Budgets/caps (orgs.settings): max_autonomous_runs_per_day, per-client
  cooldown_hours (clients.metadata.last_autonomous_run_at), kill_switch.

  Graceful degradation: LLM failure or missing config ⇒ decide() returns the
  deterministic fallback the caller passes in, so level ≥ 1 can never be
  worse than level 0.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import context
import llm

logger = logging.getLogger("buzzowl.autonomy")

LEVEL_OFF, LEVEL_OBSERVE, LEVEL_ACT, LEVEL_OUTREACH = 0, 1, 2, 3
LEVEL_NAMES = {0: "off", 1: "observe", 2: "act", 3: "outreach"}

DEFAULT_SETTINGS = {
    "autonomy_level": 0,
    "max_autonomous_runs_per_day": 20,
    "cooldown_hours": 24,
    "kill_switch": False,
}

TRIGGER = "autonomous"          # agent_runs.trigger_type for everything here
REVIEW_TYPE = "autonomy_review"  # agent_runs.agent_type for decision records

# Actions decide() may return. "skip" is the neutral one; the caller decides
# what each of the others means for its seam.
ALLOWED_ACTIONS = ("skip", "research", "osint", "match", "draft_outreach", "flag")


@dataclass
class DecisionContext:
    """What the agent looks at. Keep it small — this goes into a prompt."""
    seam: str                       # "heartbeat" | "monitor" | "selection" | "nba"
    client_name: str
    signals: list[str] = field(default_factory=list)      # what changed / is new
    facts: dict = field(default_factory=dict)             # last_research_at, is_focus, open_draft, …
    allowed_actions: tuple = ("skip", "research")
    fallback_action: str = "skip"   # deterministic answer when the LLM is unavailable


@dataclass
class Decision:
    action: str
    reason: str
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    source: str = "llm"             # "llm" | "fallback" | "budget" | "cooldown" | "kill_switch" | "level"
    review_run_id: Optional[int] = None

    @property
    def acts(self) -> bool:
        return self.action != "skip"


@dataclass
class BudgetStatus:
    ok: bool
    reason: str = ""
    used_today: int = 0
    max_per_day: int = 0
    cooldown_until: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def settings(org_id: int) -> dict:
    """Effective autonomy settings for an org (defaults merged under stored)."""
    stored: dict = {}
    if context.DB_AVAILABLE and context.db_module is not None:
        try:
            stored = await context.db_module.get_org_settings(org_id)
        except Exception as exc:          # DB hiccup ⇒ safest = level 0
            logger.warning("autonomy: could not read org settings (%s) — treating as off", exc)
            return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in (stored or {}).items() if k in DEFAULT_SETTINGS})
    try:
        merged["autonomy_level"] = max(0, min(3, int(merged["autonomy_level"])))
    except (TypeError, ValueError):
        merged["autonomy_level"] = 0
    return merged


async def level(org_id: int) -> int:
    s = await settings(org_id)
    if s.get("kill_switch"):
        return LEVEL_OFF
    return int(s.get("autonomy_level", 0))


async def may_act(org_id: int, action: str = "research") -> bool:
    """True when the org's level permits taking `action` autonomously."""
    lv = await level(org_id)
    if action == "draft_outreach":
        return lv >= LEVEL_OUTREACH
    return lv >= LEVEL_ACT


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

async def check_budget(org_id: int, client: Optional[dict] = None) -> BudgetStatus:
    """Daily cap + per-client cooldown. No new table: counts agent_runs and reads
    clients.metadata.last_autonomous_run_at."""
    s = await settings(org_id)
    if s.get("kill_switch"):
        return BudgetStatus(ok=False, reason="kill switch is on")
    max_per_day = int(s.get("max_autonomous_runs_per_day", 20))
    used = 0
    if context.DB_AVAILABLE and context.db_module is not None:
        try:
            used = await context.db_module.count_autonomous_runs_today(org_id)
        except Exception as exc:
            logger.warning("autonomy: budget count failed (%s) — assuming exhausted", exc)
            return BudgetStatus(ok=False, reason="budget unknown (db error)")
    if used >= max_per_day:
        return BudgetStatus(ok=False, reason=f"daily cap reached ({used}/{max_per_day})",
                            used_today=used, max_per_day=max_per_day)
    if client:
        last = (client.get("metadata") or {}).get("last_autonomous_run_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                until = last_dt + timedelta(hours=float(s.get("cooldown_hours", 24)))
                if until > datetime.now(timezone.utc):
                    return BudgetStatus(ok=False, reason=f"client cooldown until {until.isoformat()}",
                                        used_today=used, max_per_day=max_per_day,
                                        cooldown_until=until)
            except ValueError:
                pass
    return BudgetStatus(ok=True, used_today=used, max_per_day=max_per_day)


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------

_PROMPT = """You are the triage brain of a sales-research assistant. Decide whether the
assistant should ACT on a client right now or SKIP. Be conservative: act only
when the new information is material and the last comparable action is not
recent. Never invent facts.

Seam: {seam}
Client: {client}
New signals / changes:
{signals}
Known facts:
{facts}

Allowed actions: {allowed}
Reply with ONLY a JSON object:
{{"action": "<one of the allowed actions>", "reason": "<one sentence>", "confidence": <0.0-1.0>, "evidence": ["<signal or fact you relied on>", ...]}}"""


def _parse_decision(text: str, allowed: tuple, fallback: str) -> Optional[Decision]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    action = str(data.get("action", "")).strip().lower()
    if action not in allowed:
        return None
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    ev = data.get("evidence") or []
    if not isinstance(ev, list):
        ev = [str(ev)]
    return Decision(action=action, reason=str(data.get("reason", "")).strip()[:500],
                    confidence=max(0.0, min(1.0, conf)), evidence=[str(e)[:200] for e in ev][:8])


async def decide(org_id: int, ctx: DecisionContext, *, record: bool = True) -> Decision:
    """The one gate. Applies level → budget → LLM triage (with deterministic
    fallback), records the decision, and returns it. Callers ACT only when
    decision.acts is True AND the level permits it — level 1 always returns a
    non-acting decision (source='level') after logging what it would have done."""
    lv = await level(org_id)
    if lv == LEVEL_OFF:
        # Level 0 must be byte-for-byte legacy: no LLM call, no record.
        return Decision(action=ctx.fallback_action, reason="autonomy off", source="level")

    allowed = tuple(a for a in ctx.allowed_actions if a in ALLOWED_ACTIONS) or ("skip",)

    # LLM triage (small, strict-JSON) with deterministic fallback
    decision: Optional[Decision] = None
    prompt = _PROMPT.format(
        seam=ctx.seam, client=ctx.client_name,
        signals="\n".join(f"- {s}" for s in ctx.signals) or "- (none)",
        facts="\n".join(f"- {k}: {v}" for k, v in ctx.facts.items()) or "- (none)",
        allowed=", ".join(allowed),
    )
    try:
        text = await llm.acomplete(prompt, role="triage", max_tokens=300, timeout=60)
        decision = _parse_decision(text, allowed, ctx.fallback_action)
    except Exception as exc:
        logger.warning("autonomy triage LLM failed for %r: %s — using fallback", ctx.client_name, exc)
    if decision is None:
        decision = Decision(action=ctx.fallback_action if ctx.fallback_action in allowed else "skip",
                            reason="deterministic fallback (triage unavailable)", source="fallback")

    # Budget gate applies only to acting decisions
    if decision.acts:
        budget = await check_budget(org_id, ctx.facts.get("_client"))
        if not budget.ok:
            decision = Decision(action="skip", reason=f"would {decision.action}: {decision.reason} — "
                                f"but {budget.reason}", confidence=decision.confidence,
                                evidence=decision.evidence, source="budget")

    # Level 1: log what would happen, never act
    if decision.acts and lv == LEVEL_OBSERVE:
        decision = Decision(action="skip", reason=f"observe mode — would {decision.action}: {decision.reason}",
                            confidence=decision.confidence, evidence=decision.evidence, source="level")
    # Level 2 may not draft outreach
    if decision.action == "draft_outreach" and lv < LEVEL_OUTREACH:
        decision = Decision(action="skip", reason=f"level {lv} may not draft outreach: {decision.reason}",
                            confidence=decision.confidence, evidence=decision.evidence, source="level")

    if record:
        decision.review_run_id = await record_decision(org_id, ctx, decision)
    return decision


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

async def record_decision(org_id: int, ctx: DecisionContext, decision: Decision) -> Optional[int]:
    """Write the decision as an agent_runs row (also for skips). Returns run id."""
    if not (context.DB_AVAILABLE and context.db_module is not None):
        return None
    facts = {k: v for k, v in ctx.facts.items() if not k.startswith("_")}
    output = {
        "seam": ctx.seam, "client": ctx.client_name,
        "action": decision.action, "reason": decision.reason,
        "confidence": decision.confidence, "evidence": decision.evidence,
        "source": decision.source, "signals": ctx.signals[:10], "facts": facts,
        "acted": decision.acts,
    }
    try:
        run_id = await context.db_module.create_agent_run(
            org_id=org_id, agent_type=REVIEW_TYPE,
            task=f"[{ctx.seam}] {ctx.client_name}: {decision.action} — {decision.reason[:120]}",
            trigger_type=TRIGGER,
        )
        await context.db_module.update_agent_run(run_id, "done", output=output)
        return run_id
    except Exception as exc:
        logger.warning("autonomy: could not record decision: %s", exc)
        return None


async def mark_client_acted(org_id: int, client_name: str) -> None:
    """Stamp clients.metadata.last_autonomous_run_at (drives the cooldown)."""
    if not (context.DB_AVAILABLE and context.db_module is not None):
        return
    try:
        await context.db_module.update_client_metadata(
            org_id, client_name,
            {"last_autonomous_run_at": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        logger.debug("autonomy: cooldown stamp failed for %r: %s", client_name, exc)


def describe(s: dict) -> str:
    lv = int(s.get("autonomy_level", 0))
    return (f"level {lv} ({LEVEL_NAMES.get(lv, '?')}), "
            f"cap {s.get('max_autonomous_runs_per_day')}/day, "
            f"cooldown {s.get('cooldown_hours')}h"
            + (", KILL SWITCH ON" if s.get("kill_switch") else ""))
