"""Tests for autonomy.py — the decision layer behind autonomous actions.

DB is a fake module (settings, budget counter, recorded runs); the triage LLM
is patched at llm.acomplete so no network is involved.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import autonomy
import context
import llm


class FakeDB:
    def __init__(self, settings=None, used_today=0):
        self._settings = settings or {}
        self._used = used_today
        self.runs: list[dict] = []
        self.updates: list[tuple] = []
        self.client_patches: list[tuple] = []

    async def get_org_settings(self, org_id):
        return dict(self._settings)

    async def count_autonomous_runs_today(self, org_id):
        return self._used

    async def create_agent_run(self, org_id, agent_type, task, trigger_type="manual", **kw):
        self.runs.append({"org_id": org_id, "agent_type": agent_type, "task": task,
                          "trigger_type": trigger_type})
        return len(self.runs)

    async def update_agent_run(self, run_id, status, output=None, error=None):
        self.updates.append((run_id, status, output))

    async def update_client_metadata(self, org_id, name, patch):
        self.client_patches.append((org_id, name, patch))


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(context, "DB_AVAILABLE", True)
    monkeypatch.setattr(context, "db_module", db)
    return db


def _ctx(**kw):
    base = dict(seam="heartbeat", client_name="Acme Corp",
                signals=["news: new CFO appointed"], facts={"last_research_days_ago": 30},
                allowed_actions=("skip", "research"), fallback_action="research")
    base.update(kw)
    return autonomy.DecisionContext(**base)


def _llm_says(monkeypatch, payload: str):
    mock = AsyncMock(return_value=payload)
    monkeypatch.setattr(llm, "acomplete", mock)
    return mock


# ---------------------------------------------------------------------------
# Level 0 — must be byte-for-byte legacy: no LLM call, no record
# ---------------------------------------------------------------------------

async def test_level0_no_llm_no_record(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 0}
    m = _llm_says(monkeypatch, '{"action":"research"}')
    d = await autonomy.decide(1, _ctx())
    assert d.source == "level" and d.action == "research"      # fallback passthrough
    m.assert_not_awaited()
    assert fake_db.runs == []


async def test_missing_settings_is_level0(fake_db, monkeypatch):
    m = _llm_says(monkeypatch, '{"action":"research"}')
    d = await autonomy.decide(1, _ctx())
    assert d.source == "level"
    m.assert_not_awaited()


# ---------------------------------------------------------------------------
# Level 1 — decides + records, never acts
# ---------------------------------------------------------------------------

async def test_level1_logs_but_never_acts(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 1}
    _llm_says(monkeypatch, '{"action":"research","reason":"CFO change is material","confidence":0.8,"evidence":["news"]}')
    d = await autonomy.decide(1, _ctx())
    assert d.acts is False
    assert d.source == "level"
    assert "would research" in d.reason
    assert d.evidence == ["news"]
    assert len(fake_db.runs) == 1
    assert fake_db.runs[0]["agent_type"] == autonomy.REVIEW_TYPE
    assert fake_db.runs[0]["trigger_type"] == autonomy.TRIGGER
    assert fake_db.updates[0][2]["acted"] is False


# ---------------------------------------------------------------------------
# Level 2 — acts within budget
# ---------------------------------------------------------------------------

async def test_level2_acts_and_records(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2}
    _llm_says(monkeypatch, '{"action":"research","reason":"material","confidence":0.9,"evidence":["news"]}')
    d = await autonomy.decide(1, _ctx())
    assert d.acts and d.action == "research" and d.source == "llm"
    assert d.review_run_id == 1
    assert fake_db.updates[0][2]["acted"] is True


async def test_level2_llm_skip_respected(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2}
    _llm_says(monkeypatch, '{"action":"skip","reason":"researched yesterday","confidence":0.7}')
    d = await autonomy.decide(1, _ctx())
    assert d.acts is False and d.source == "llm"
    assert len(fake_db.runs) == 1        # skips are recorded too


async def test_budget_exhausted_blocks_action_but_records(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2, "max_autonomous_runs_per_day": 5}
    fake_db._used = 5
    _llm_says(monkeypatch, '{"action":"research","reason":"material"}')
    d = await autonomy.decide(1, _ctx())
    assert d.acts is False and d.source == "budget"
    assert "daily cap" in d.reason
    assert len(fake_db.runs) == 1


async def test_client_cooldown_blocks(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2, "cooldown_hours": 24}
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    client = {"name": "Acme Corp", "metadata": {"last_autonomous_run_at": recent}}
    _llm_says(monkeypatch, '{"action":"research","reason":"material"}')
    d = await autonomy.decide(1, _ctx(facts={"_client": client}))
    assert d.acts is False and d.source == "budget" and "cooldown" in d.reason


async def test_kill_switch_forces_level0(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 3, "kill_switch": True}
    m = _llm_says(monkeypatch, '{"action":"research"}')
    d = await autonomy.decide(1, _ctx())
    assert d.source == "level"
    m.assert_not_awaited()
    assert await autonomy.level(1) == 0


# ---------------------------------------------------------------------------
# Fallback + guardrails
# ---------------------------------------------------------------------------

async def test_llm_failure_uses_deterministic_fallback(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2}
    monkeypatch.setattr(llm, "acomplete", AsyncMock(side_effect=llm.LLMError("boom")))
    d = await autonomy.decide(1, _ctx(fallback_action="skip"))
    assert d.action == "skip" and d.source == "fallback"
    d2 = await autonomy.decide(1, _ctx(fallback_action="research"))
    assert d2.action == "research" and d2.source == "fallback"   # legacy behaviour preserved


async def test_llm_garbage_uses_fallback(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2}
    _llm_says(monkeypatch, "Sure! I think you should research. {not json")
    d = await autonomy.decide(1, _ctx(fallback_action="skip"))
    assert d.source == "fallback"


async def test_disallowed_action_rejected(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2}
    _llm_says(monkeypatch, '{"action":"delete_everything","reason":"lol"}')
    d = await autonomy.decide(1, _ctx(fallback_action="skip"))
    assert d.action == "skip" and d.source == "fallback"


async def test_level2_cannot_draft_outreach(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 2}
    _llm_says(monkeypatch, '{"action":"draft_outreach","reason":"hot lead"}')
    d = await autonomy.decide(1, _ctx(allowed_actions=("skip", "draft_outreach")))
    assert d.acts is False and d.source == "level" and "may not draft" in d.reason


async def test_level3_may_draft_outreach(fake_db, monkeypatch):
    fake_db._settings = {"autonomy_level": 3}
    _llm_says(monkeypatch, '{"action":"draft_outreach","reason":"hot lead"}')
    d = await autonomy.decide(1, _ctx(allowed_actions=("skip", "draft_outreach")))
    assert d.acts and d.action == "draft_outreach"


async def test_mark_client_acted_stamps_cooldown(fake_db):
    await autonomy.mark_client_acted(1, "Acme Corp")
    assert fake_db.client_patches[0][1] == "Acme Corp"
    assert "last_autonomous_run_at" in fake_db.client_patches[0][2]


async def test_settings_clamped_and_defaulted(fake_db):
    fake_db._settings = {"autonomy_level": 9, "unknown_key": 1}
    s = await autonomy.settings(1)
    assert s["autonomy_level"] == 3
    assert "unknown_key" not in s
    assert s["cooldown_hours"] == 24
