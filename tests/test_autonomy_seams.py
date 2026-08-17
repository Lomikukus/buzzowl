"""Seam tests: autonomy wired into the heartbeat research branch and the source
monitor. Level 0 must be legacy; level 2 must skip a fresh client (logged) and
act on a stale one with trigger_type='autonomous' + the orchestrate agent.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import autonomy
import context
from routers import pipeline

NOW = datetime.now(timezone.utc)


def _client(cid, name, is_focus=False, **meta):
    m = {"is_focus": is_focus}
    m.update(meta)
    return {"id": cid, "name": name, "metadata": m, "last_activity": None}


class _DB:
    def __init__(self, clients, settings):
        self.clients = clients
        self.settings = settings
        self.runs = []
        self.updates = []
        self.meta_patches = []

    async def list_clients(self, org_id):
        return self.clients

    async def get_client_last_doc_dates(self, org_id):
        return {}

    async def get_org_settings(self, org_id):
        return dict(self.settings)

    async def count_autonomous_runs_today(self, org_id):
        return 0

    async def create_agent_run(self, org_id, agent_type, task, trigger_type="manual", **kw):
        self.runs.append({"agent_type": agent_type, "trigger_type": trigger_type, "task": task})
        return len(self.runs)

    async def update_agent_run(self, run_id, status, output=None, error=None):
        self.updates.append((run_id, status, output, error))

    async def update_client_metadata(self, org_id, name, patch):
        self.meta_patches.append((name, patch))


def _wire(monkeypatch, db, config_extra=None):
    cfg = {"agent_service_backend": "pi", "heartbeat_stale_days": 14,
           "heartbeat_max_nonfocus_per_run": 3, "news_change_detection": False}
    cfg.update(config_extra or {})
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(context, "db_module", db)
    monkeypatch.setattr(context, "config", cfg)
    monkeypatch.setattr(pipeline, "db_module", db)
    monkeypatch.setattr(pipeline, "DB_AVAILABLE", True)
    fire = AsyncMock(return_value=("http://pi", "svc-1"))
    watch = AsyncMock()
    monkeypatch.setattr("routers.agents._fire_agent_service", fire)
    monkeypatch.setattr("routers.agents._watch_agent_service_run", watch)
    return fire


# ---------------------------------------------------------------------------
# Heartbeat research branch
# ---------------------------------------------------------------------------

async def test_heartbeat_level0_is_legacy(monkeypatch):
    db = _DB([_client(1, "Fresh Corp", True), _client(2, "Stale GmbH", True)], {"autonomy_level": 0})
    fire = _wire(monkeypatch, db)
    triage = AsyncMock()
    monkeypatch.setattr(autonomy.llm, "acomplete", triage)
    await pipeline._run_heartbeat_job(1, 1, "research", "refresh")
    triage.assert_not_awaited()
    child_types = [(r["agent_type"], r["trigger_type"]) for r in db.runs[1:]]
    assert child_types == [("research_prep", "heartbeat"), ("research_prep", "heartbeat")]
    assert fire.await_count == 2


async def test_heartbeat_level2_skips_fresh_acts_on_stale(monkeypatch):
    db = _DB([_client(1, "Fresh Corp", True), _client(2, "Stale GmbH", True)], {"autonomy_level": 2})
    fire = _wire(monkeypatch, db)

    async def triage(prompt, **kw):
        if "Fresh Corp" in prompt:
            return '{"action":"skip","reason":"researched 2 days ago","confidence":0.8}'
        return '{"action":"research","reason":"90 days stale + new signal","confidence":0.9,"evidence":["stale"]}'
    monkeypatch.setattr(autonomy.llm, "acomplete", AsyncMock(side_effect=triage))

    await pipeline._run_heartbeat_job(1, 1, "research", "refresh")

    # exactly one Pi run fired, for the stale client, as orchestrate + autonomous
    assert fire.await_count == 1
    assert fire.await_args.args[0] == "Stale GmbH"
    assert fire.await_args.kwargs["agent_type"] == "orchestrate"
    acted = [r for r in db.runs if r["agent_type"] == "orchestrate"]
    assert len(acted) == 1 and acted[0]["trigger_type"] == "autonomous"
    # both decisions recorded (skip + act)
    reviews = [r for r in db.runs if r["agent_type"] == autonomy.REVIEW_TYPE]
    assert len(reviews) == 2 and all(r["trigger_type"] == "autonomous" for r in reviews)
    # cooldown stamped only for the acted client
    assert [n for n, _ in db.meta_patches] == ["Stale GmbH"]
    # parent run output carries the audit summary
    parent = [u for u in db.updates if u[0] == 1 and u[1] == "done"][0][2]
    assert parent["autonomy_level"] == 2
    assert [s["name"] for s in parent["skipped_by_agent"]] == ["Fresh Corp"]


async def test_heartbeat_level1_logs_and_runs_legacy(monkeypatch):
    db = _DB([_client(1, "Any Corp", True)], {"autonomy_level": 1})
    fire = _wire(monkeypatch, db)
    monkeypatch.setattr(autonomy.llm, "acomplete",
                        AsyncMock(return_value='{"action":"skip","reason":"nothing new"}'))
    await pipeline._run_heartbeat_job(1, 1, "research", "refresh")
    # decision logged, but legacy research_prep still fires (observe mode)
    assert any(r["agent_type"] == autonomy.REVIEW_TYPE for r in db.runs)
    assert fire.await_count == 1
    assert fire.await_args.kwargs["agent_type"] == "research_prep"


async def test_heartbeat_level2_budget_exhausted_halts_actions(monkeypatch):
    db = _DB([_client(1, "A", True), _client(2, "B", True)],
             {"autonomy_level": 2, "max_autonomous_runs_per_day": 0})
    fire = _wire(monkeypatch, db)
    monkeypatch.setattr(autonomy.llm, "acomplete",
                        AsyncMock(return_value='{"action":"research","reason":"go"}'))
    await pipeline._run_heartbeat_job(1, 1, "research", "refresh")
    assert fire.await_count == 0
    reviews = [r for r in db.runs if r["agent_type"] == autonomy.REVIEW_TYPE]
    assert len(reviews) == 2                     # decisions still logged
    parent = [u for u in db.updates if u[0] == 1 and u[1] == "done"][0][2]
    assert len(parent["skipped_by_agent"]) == 2
    assert all("daily cap" in s["reason"] for s in parent["skipped_by_agent"])


# ---------------------------------------------------------------------------
# Source monitor decision point
# ---------------------------------------------------------------------------

async def _monitor_with_change(monkeypatch, db, client):
    monkeypatch.setattr(pipeline, "_discovery_marker_stale", lambda m: False)
    monkeypatch.setattr(pipeline, "_client_news_changed", AsyncMock(return_value=True))
    monkeypatch.setattr(pipeline, "_maybe_escalate_match", AsyncMock(return_value=False))
    fired = AsyncMock(return_value=42)
    monkeypatch.setattr(pipeline, "_fire_news_research", fired)
    summary = await pipeline._monitor_client(1, client)
    return summary, fired


async def test_monitor_level0_nonfocus_only_flags(monkeypatch):
    db = _DB([], {"autonomy_level": 0})
    _wire(monkeypatch, db)
    client = _client(1, "NonFocus AG", False, news_fp="abc")
    summary, fired = await _monitor_with_change(monkeypatch, db, client)
    fired.assert_not_awaited()
    assert summary["flagged"] is True and "decision" not in summary


async def test_monitor_level2_nonfocus_can_be_researched(monkeypatch):
    db = _DB([], {"autonomy_level": 2})
    _wire(monkeypatch, db)
    monkeypatch.setattr(autonomy.llm, "acomplete",
                        AsyncMock(return_value='{"action":"research","reason":"CEO change","confidence":0.9}'))
    client = _client(1, "NonFocus AG", False, news_fp="abc")
    summary, fired = await _monitor_with_change(monkeypatch, db, client)
    fired.assert_awaited_once()
    assert fired.await_args.kwargs["autonomous"] is True
    assert summary["researched"] is True and summary["flagged"] is False
    assert summary["decision"]["action"] == "research"


async def test_monitor_level2_focus_can_be_skipped(monkeypatch):
    db = _DB([], {"autonomy_level": 2})
    _wire(monkeypatch, db)
    monkeypatch.setattr(autonomy.llm, "acomplete",
                        AsyncMock(return_value='{"action":"skip","reason":"cosmetic page change"}'))
    client = _client(1, "Focus AG", True, news_fp="abc")
    summary, fired = await _monitor_with_change(monkeypatch, db, client)
    fired.assert_not_awaited()
    assert summary["flagged"] is True            # falls back to the badge


async def test_monitor_level1_logs_keeps_legacy(monkeypatch):
    db = _DB([], {"autonomy_level": 1})
    _wire(monkeypatch, db)
    monkeypatch.setattr(autonomy.llm, "acomplete",
                        AsyncMock(return_value='{"action":"research","reason":"go"}'))
    client = _client(1, "NonFocus AG", False, news_fp="abc")
    summary, fired = await _monitor_with_change(monkeypatch, db, client)
    fired.assert_not_awaited()                   # non-focus → legacy = flag only
    assert summary["flagged"] is True
    assert "would research" in summary["decision"]["reason"]


# ---------------------------------------------------------------------------
# NBA action choice (today.py)
# ---------------------------------------------------------------------------

from routers import today as _today


def _entry(client, action, facts):
    return {"client": client, "suggested_action": action, "facts": facts,
            "action_link": _today._action_link(action, client), "score": 10, "rank": 1,
            "is_focus": False}


def test_nba_allowed_actions_bounded_by_facts():
    e = _entry("A", "research", [{"type": "signal", "relevance": 4}])
    assert set(_today._allowed_nba_actions(e)) == {"research", "mail"}
    e2 = _entry("B", "send_draft", [{"type": "outreach_draft"}, {"type": "task_due"}])
    assert set(_today._allowed_nba_actions(e2)) == {"research", "send_draft", "task"}


def test_nba_prompt_level0_reason_only_unchanged():
    e = _entry("A", "research", [{"type": "signal", "headline": "h", "relevance": 4}])
    p = _today._build_reason_prompt([e])
    assert "allowed_actions" not in p and '"suggested_action": "research"' in p


def test_nba_prompt_choose_action_bounds():
    e = _entry("A", "research", [{"type": "signal", "headline": "h", "relevance": 4}])
    p = _today._build_reason_prompt([e], choose_action=True)
    assert '"allowed_actions": ["research", "mail"]' in p and '"default_action": "research"' in p


def test_nba_parse_reason_action_backcompat():
    legacy = _today._parse_reason_action_json('[{"client":"A","reason":"r"}]')
    assert legacy["a"] == {"reason": "r", "action": None}
    new = _today._parse_reason_action_json('[{"client":"A","reason":"r","action":"MAIL"}]')
    assert new["a"]["action"] == "mail"


async def test_nba_level2_llm_can_override_within_bounds(monkeypatch):
    """With a draft present the LLM cannot pick 'research' away from a rule that
    says send_draft... unless research is allowed — it always is; but it can NEVER
    pick send_draft where no draft exists."""
    entries = [
        _entry("HasDraft", "send_draft", [{"type": "outreach_draft", "headline": "d"},
                                          {"type": "signal", "headline": "s", "relevance": 5}]),
        _entry("NoDraft", "research", [{"type": "signal", "headline": "s2", "relevance": 4}]),
    ]
    monkeypatch.setattr(_today, "compute_scores", lambda *a, **k: entries)
    monkeypatch.setattr(_today, "_gather_inputs", AsyncMock(return_value=([{}], {}, {}, {}, {})))
    monkeypatch.setattr(_today.context, "config", {})
    db = _DB([], {"autonomy_level": 2})
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(context, "db_module", db)
    idx = AsyncMock()
    monkeypatch.setattr(_today, "db_module", MagicMock(index_document=idx))
    reply = ('[{"client":"HasDraft","action":"mail","reason":"hot signal, mail first"},'
             ' {"client":"NoDraft","action":"send_draft","reason":"send it"}]')
    monkeypatch.setattr("routers.knowledge._call_brain_sync", lambda p: reply)

    snap = await _today.compute_nba_queue(1)
    q = {e["client"]: e for e in snap["queue"]}
    # allowed override: HasDraft has a signal ⇒ mail is allowed
    assert q["HasDraft"]["suggested_action"] == "mail" and q["HasDraft"]["action_source"] == "llm"
    assert q["HasDraft"]["rule_action"] == "send_draft"
    assert q["HasDraft"]["action_link"].startswith("/match?client=")
    # illegal override: NoDraft has no draft ⇒ send_draft rejected, rule kept
    assert q["NoDraft"]["suggested_action"] == "research" and q["NoDraft"]["action_source"] == "rule"
    assert snap["autonomy_level"] == 2 and snap["actions_changed_by_agent"] == 1


async def test_nba_level0_never_changes_action(monkeypatch):
    entries = [_entry("A", "research", [{"type": "signal", "headline": "s", "relevance": 4}])]
    monkeypatch.setattr(_today, "compute_scores", lambda *a, **k: entries)
    monkeypatch.setattr(_today, "_gather_inputs", AsyncMock(return_value=([{}], {}, {}, {}, {})))
    monkeypatch.setattr(_today.context, "config", {})
    db = _DB([], {"autonomy_level": 0})
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(context, "db_module", db)
    monkeypatch.setattr(_today, "db_module", MagicMock(index_document=AsyncMock()))
    captured = {}
    def brain(p):
        captured["prompt"] = p
        return '[{"client":"A","action":"mail","reason":"r"}]'
    monkeypatch.setattr("routers.knowledge._call_brain_sync", brain)
    snap = await _today.compute_nba_queue(1)
    assert "allowed_actions" not in captured["prompt"]           # legacy prompt
    assert snap["queue"][0]["suggested_action"] == "research"     # action untouched
    assert "action_source" not in snap["queue"][0]


# ---------------------------------------------------------------------------
# Selection triage (heartbeat candidate pre-filter)
# ---------------------------------------------------------------------------

async def test_selection_triage_level0_untouched(monkeypatch):
    clients = [_client(i, f"C{i}", True) for i in range(8)]
    db = _DB(clients, {"autonomy_level": 0})
    _wire(monkeypatch, db)
    triage = AsyncMock()
    monkeypatch.setattr(pipeline.llm, "acomplete", triage)
    selected, summary = await pipeline._select_heartbeat_clients(1, "research")
    triage.assert_not_awaited()
    assert len(selected) == 8 and "triage" not in summary


async def test_selection_triage_level2_filters_and_orders(monkeypatch):
    clients = [_client(i, f"C{i}", True) for i in range(8)]
    db = _DB(clients, {"autonomy_level": 2})
    _wire(monkeypatch, db)
    monkeypatch.setattr(pipeline.llm, "acomplete",
                        AsyncMock(return_value='["C5", "C2", "Unknown Corp"]'))
    selected, summary = await pipeline._select_heartbeat_clients(1, "research")
    assert [c["name"] for c in selected] == ["C5", "C2"]
    assert summary["triage"]["applied"] and summary["triage"]["before"] == 8


async def test_selection_triage_llm_failure_keeps_deterministic(monkeypatch):
    clients = [_client(i, f"C{i}", True) for i in range(8)]
    db = _DB(clients, {"autonomy_level": 2})
    _wire(monkeypatch, db)
    monkeypatch.setattr(pipeline.llm, "acomplete", AsyncMock(side_effect=RuntimeError("down")))
    selected, summary = await pipeline._select_heartbeat_clients(1, "research")
    assert len(selected) == 8 and "triage" not in summary       # exact legacy selection


async def test_selection_triage_small_list_skips_llm(monkeypatch):
    clients = [_client(i, f"C{i}", True) for i in range(3)]
    db = _DB(clients, {"autonomy_level": 2})
    _wire(monkeypatch, db)
    triage = AsyncMock()
    monkeypatch.setattr(pipeline.llm, "acomplete", triage)
    selected, _ = await pipeline._select_heartbeat_clients(1, "research")
    triage.assert_not_awaited()
    assert len(selected) == 3
