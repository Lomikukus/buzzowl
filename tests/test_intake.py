"""
tests/test_intake.py — WP5 intake orchestrator (intake.py).

Covers:
- Pure logic: summary()/is_active(), _missing_parts()
- part_done() against a fake db: foreign run_id, idempotency, the four-way
  concurrent race into _finish (CAS keeps _auto_generate_brief to one call),
  the deadline → partial → one-time-refresh lifecycle, brief failure/attempt
  bookkeeping
- sweep()'s lost-callback rescue
- The /api/agents/callback and _watch_agent_service_run hooks (mirrors
  tests/test_pi_agents.py)
- The API surface: create_client / internal_create_client start intake,
  GET /api/clients/{name}/intake
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

import intake

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FAKE_USER = {
    "id": 1,
    "org_id": 1,
    "username": "konrad",
    "display_name": "Konrad",
    "email": "k@test.com",
    "role": "admin",
    "org_name": "North",
    "org_slug": "north",
}


def _mock_pool(fetchrow_return=None, fetch_return=None):
    """Mock asyncpg pool handling `acquire()` as an async context manager
    (mirrors tests/test_pi_agents.py::_mock_pool)."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    mock_conn.fetch = AsyncMock(return_value=fetch_return or [])
    mock_pool = MagicMock()
    mock_pool.__bool__ = lambda self: True
    mock_pool.acquire = MagicMock(return_value=mock_pool)
    mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


def _fake_db(**overrides):
    """A MagicMock standing in for db.py, with every method intake.py calls
    wired to an AsyncMock. Tests override individual methods as needed."""
    db = MagicMock()
    db._pool = None  # keeps _has_match_report() a plain "no report" without a pool
    db.get_client = AsyncMock(return_value=None)
    db.set_client_intake_path = AsyncMock(return_value=None)
    db.cas_client_intake_brief = AsyncMock(return_value=None)
    db.update_client_metadata = AsyncMock(return_value=None)
    db.create_agent_run = AsyncMock(return_value=1)
    db.update_agent_run = AsyncMock(return_value=None)
    db.get_agent_run = AsyncMock(return_value=None)
    db.list_clients_with_open_intake = AsyncMock(return_value=[])
    for key, value in overrides.items():
        setattr(db, key, value)
    return db


def _parts(**statuses) -> dict:
    """Build a full parts dict; unspecified parts default to queued."""
    out = {}
    for i, p in enumerate(intake.PARTS):
        st = statuses.get(p, "queued")
        out[p] = {**intake._empty_part(), "run_id": i + 1, "status": st}
    return out


def _intake_state(parts=None, brief=None, **overrides) -> dict:
    state = {
        "version": 1, "trigger": "create", "started_at": intake._iso(intake._now()),
        "deadline_at": None, "attempt": 1,
        "parts": parts if parts is not None else _parts(),
        "brief": brief if brief is not None else intake._empty_brief(),
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

class TestPureLogic:
    def test_summary_percent_and_active(self):
        # 2 terminal parts (done) out of (4 parts + the brief stage) = 40%,
        # not 50% — percent counts terminal parts (done or failed) plus the
        # brief as one more stage (see summary()'s docstring).
        parts = _parts(osint="done", research="done", jobs="running", news="queued")
        meta = {"intake": _intake_state(parts=parts)}
        data = intake.summary(meta)
        assert data["percent"] == 40
        assert data["active"] is True
        assert set(data["parts"].keys()) == set(intake.PARTS)

    def test_summary_percent_counts_failed_parts_and_brief_stage(self):
        # All four parts terminal (one failed) + a written brief = 100%, not
        # 75% — a failed part isn't "still in progress", and the brief itself
        # is a stage too.
        parts = _parts(osint="done", research="done", jobs="failed", news="done")
        meta = {"intake": _intake_state(parts=parts, brief={**intake._empty_brief(), "status": "written"})}
        assert intake.summary(meta)["percent"] == 100

    def test_summary_inactive_once_brief_terminal(self):
        for terminal in ("written", "refreshed", "failed"):
            meta = {"intake": _intake_state(brief={**intake._empty_brief(), "status": terminal})}
            assert intake.summary(meta)["active"] is False

    def test_is_active_false_without_intake(self):
        assert intake.is_active({}) is False
        assert intake.is_active(None) is False

    def test_missing_lists_failed_and_pending(self):
        parts = {
            "osint": {"status": "done"},
            "research": {"status": "failed", "error": "timeout"},
            "jobs": {"status": "queued"},
            "news": {"status": "running"},
        }
        missing = intake._missing_parts(parts)
        assert "research (failed: timeout)" in missing
        assert "jobs" in missing
        assert "news" in missing
        assert "osint" not in missing


# ---------------------------------------------------------------------------
# part_done() / _maybe_finish() / _finish() against a fake db
# ---------------------------------------------------------------------------

class TestPartDone:
    async def test_ignores_foreign_run_id(self):
        parts = _parts()  # osint run_id=1
        meta = {"intake": _intake_state(parts=parts)}
        db = _fake_db(get_client=AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta}))
        with patch("intake.db_module", db):
            await intake.part_done(1, "Bosch AG", "osint", "done", run_id=999)
        db.set_client_intake_path.assert_not_called()

    async def test_idempotent_once_terminal(self):
        parts = _parts(osint="done")
        meta = {"intake": _intake_state(parts=parts)}
        db = _fake_db(get_client=AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta}))
        with patch("intake.db_module", db):
            await intake.part_done(1, "Bosch AG", "osint", "done", run_id=1)
        db.set_client_intake_path.assert_not_called()

    async def test_four_concurrent_calls_finish_exactly_once(self):
        """All four parts finishing near-simultaneously must not double-write
        the brief — db.cas_client_intake_brief is the single-flight gate."""
        meta_before = {"intake": _intake_state(parts=_parts())}
        parts_all_done = {
            p: {**st, "status": "done", "done_at": intake._iso(intake._now())}
            for p, st in _parts().items()
        }
        meta_all_done = {"intake": _intake_state(parts=parts_all_done)}
        writing_meta = {"intake": {**meta_all_done["intake"],
                                    "brief": {**meta_all_done["intake"]["brief"], "status": "writing"}}}

        # A plain AsyncMock(side_effect=[...]) never actually suspends, so the
        # four part_done() coroutines would run to completion one after another
        # instead of really interleaving — which would let this test pass even
        # if the CAS call weren't doing any real exclusion. Force a genuine
        # await point before each answer so all four tasks reach the CAS call
        # before any of them gets its result back.
        cas_results = iter([writing_meta, None, None, None])

        async def fake_cas(*_args, **_kwargs):
            await asyncio.sleep(0)
            return next(cas_results)

        db = _fake_db(
            get_client=AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta_before}),
            set_client_intake_path=AsyncMock(return_value=meta_all_done),
            cas_client_intake_brief=AsyncMock(side_effect=fake_cas),
        )
        mock_brief = AsyncMock(return_value=True)
        mock_match = AsyncMock()

        with (
            patch("intake.db_module", db),
            patch("routers.knowledge._auto_generate_brief", mock_brief),
            patch("routers.agents._maybe_trigger_pain_point_research", mock_match),
        ):
            await asyncio.gather(*[
                intake.part_done(1, "Bosch AG", p, "done", run_id=i + 1)
                for i, p in enumerate(intake.PARTS)
            ])

        mock_brief.assert_awaited_once()
        assert db.cas_client_intake_brief.call_count == 4

    async def test_deadline_then_exactly_one_refresh(self):
        t0 = intake._now() - timedelta(minutes=40)
        deadline = t0 + timedelta(minutes=25)
        parts_at_deadline = _parts(osint="done", research="done", jobs="done", news="running")
        for p in ("osint", "research", "jobs"):
            parts_at_deadline[p]["done_at"] = intake._iso(t0 + timedelta(minutes=5))
        state = _intake_state(parts=parts_at_deadline, deadline_at=intake._iso(deadline), started_at=intake._iso(t0))
        meta = {"intake": state}

        written_at = intake._iso(deadline)
        partial_brief = {"status": "partial", "written_at": written_at, "missing": ["news"],
                          "refreshed_at": None, "error": None}
        meta_after_partial = {"intake": {**state, "brief": partial_brief}}
        writing_meta_1 = {"intake": {**state, "brief": {**partial_brief, "status": "writing"}}}

        db = _fake_db(
            get_client=AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta}),
            set_client_intake_path=AsyncMock(return_value=meta_after_partial),
            cas_client_intake_brief=AsyncMock(return_value=writing_meta_1),
        )
        mock_brief = AsyncMock(return_value=True)
        mock_match = AsyncMock()

        with (
            patch("intake.db_module", db),
            patch("routers.knowledge._auto_generate_brief", mock_brief),
            patch("routers.agents._maybe_trigger_pain_point_research", mock_match),
        ):
            # sweep()-style: the deadline has passed, nothing terminal to rescue.
            await intake._maybe_finish(1, "Bosch AG", meta)

        assert mock_brief.await_count == 1
        assert mock_brief.call_args.kwargs.get("partial_missing") == ["news"]

        # Phase 2: news finishes AFTER the partial brief was written.
        news_done_at = intake._iso(datetime.fromisoformat(written_at) + timedelta(minutes=2))
        parts_after_news = {**parts_at_deadline, "news": {**parts_at_deadline["news"], "status": "done",
                                                           "done_at": news_done_at}}
        # Before the write: news is still 'running' (that's what makes this a
        # genuinely new completion) but the brief is already 'partial' from phase 1.
        meta_before_phase2 = {"intake": {**state, "parts": parts_at_deadline, "brief": partial_brief}}
        meta_partial_news_done = {"intake": {**state, "parts": parts_after_news, "brief": partial_brief}}
        refreshed_brief = {"status": "refreshed", "written_at": intake._iso(intake._now()), "missing": [],
                            "refreshed_at": intake._iso(intake._now()), "error": None}
        writing_meta_2 = {"intake": {**state, "parts": parts_after_news, "brief": {**partial_brief, "status": "writing"}}}

        db.get_client = AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta_before_phase2})
        db.set_client_intake_path = AsyncMock(return_value=meta_partial_news_done)
        db.cas_client_intake_brief = AsyncMock(return_value=writing_meta_2)

        with (
            patch("intake.db_module", db),
            patch("routers.knowledge._auto_generate_brief", mock_brief),
            patch("routers.agents._maybe_trigger_pain_point_research", mock_match),
            patch("intake._finish", new_callable=AsyncMock, wraps=intake._finish) as mock_finish,
        ):
            await intake.part_done(1, "Bosch AG", "news", "done", run_id=4)

        assert mock_brief.await_count == 2  # exactly one refresh
        # The refresh path must be the one that actually reaches _finish, and
        # must be flagged as a refresh (not a fresh 'partial'/'written' finish).
        mock_finish.assert_awaited_once()
        assert mock_finish.call_args.kwargs.get("refresh") is True

        # Phase 3: the brief is now terminal ("refreshed") — further events are no-ops.
        meta_refreshed = {"intake": {**state, "parts": parts_after_news, "brief": refreshed_brief}}
        db.get_client = AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta_refreshed})
        with patch("intake.db_module", db):
            await intake.part_done(1, "Bosch AG", "news", "done", run_id=4)
        assert mock_brief.await_count == 2  # unchanged — is_active() is now False

    async def test_partial_refresh_guard_skips_second_refresh(self):
        """A second late part finishing after refreshed_at is already set must
        not trigger another refresh — the one-time-refresh guard in
        _maybe_finish's 'partial' branch must return before ever reaching
        _finish again, independent of the is_active() gate covered above."""
        parts = _parts(osint="done", research="done", jobs="done", news="done")
        already_refreshed = {
            "status": "partial",
            "written_at": intake._iso(intake._now() - timedelta(minutes=5)),
            "missing": ["news"],
            "refreshed_at": intake._iso(intake._now() - timedelta(minutes=1)),
            "error": None,
        }
        meta = {"intake": _intake_state(parts=parts, brief=already_refreshed)}
        db = _fake_db(get_client=AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta}))

        with (
            patch("intake.db_module", db),
            patch("intake._finish", new_callable=AsyncMock) as mock_finish,
        ):
            await intake._maybe_finish(1, "Bosch AG", meta)

        mock_finish.assert_not_called()

    async def test_finish_failure_resets_status_and_increments_attempt(self):
        state = _intake_state(parts=_parts(osint="done", research="done", jobs="done", news="done"))
        writing_meta = {"intake": {**state, "brief": {**state["brief"], "status": "writing"}}}
        db = _fake_db(
            cas_client_intake_brief=AsyncMock(return_value=writing_meta),
            set_client_intake_path=AsyncMock(return_value=None),
        )
        with (
            patch("intake.db_module", db),
            patch("routers.knowledge._auto_generate_brief", AsyncMock(return_value=False)),
        ):
            await intake._finish(1, "Bosch AG", missing=[], refresh=False)

        calls = db.set_client_intake_path.call_args_list
        brief_call = next(c for c in calls if c.args[2] == ["intake", "brief"])
        attempt_call = next(c for c in calls if c.args[2] == ["intake", "attempt"])
        assert brief_call.args[3]["status"] == "waiting"
        assert attempt_call.args[3] == 2

    async def test_sweep_reconciles_lost_callback(self):
        parts = _parts(osint="running", research="queued", jobs="done", news="done")
        meta = {"intake": _intake_state(parts=parts, deadline_at=intake._iso(intake._now() + timedelta(minutes=10)))}
        row = {"id": 1, "org_id": 1, "name": "Bosch AG", "metadata": meta}

        db = _fake_db(
            list_clients_with_open_intake=AsyncMock(return_value=[row]),
            get_agent_run=AsyncMock(return_value={"id": 1, "status": "done", "error": None}),
            get_client=AsyncMock(return_value={"id": 1, "org_id": 1, "metadata": meta}),
            set_client_intake_path=AsyncMock(return_value=meta),
        )
        with patch("intake.db_module", db):
            await intake.sweep()

        # The rescue loop found osint (running) and research (queued) both
        # backed by an agent_runs row that already finished, and reconciled
        # them via part_done() → set_client_intake_path() — check the parts
        # actually landed as 'done', not just that some write happened.
        calls = db.set_client_intake_path.call_args_list
        osint_calls = [c for c in calls if c.args[2] == ["intake", "parts", "osint"]]
        research_calls = [c for c in calls if c.args[2] == ["intake", "parts", "research"]]
        assert osint_calls and osint_calls[0].args[3]["status"] == "done"
        assert research_calls and research_calls[0].args[3]["status"] == "done"

    async def test_all_parts_terminal_with_a_failure_finishes_written_and_inactive(self):
        """BLOCKER 1 regression: every part terminal but one FAILED must finish
        as 'written' (with the failure recorded in missing), not 'partial' —
        'partial' is not a terminal brief state, so a partial-with-nothing-
        left-to-wait-for brief would keep the intake active forever (endless
        sweeps, dead OSINT/research→refresh routing, endless client polling)."""
        parts = _parts(osint="done", research="done", jobs="failed", news="done")
        parts["jobs"]["error"] = "boom"
        state = _intake_state(parts=parts)
        meta = {"intake": state}
        writing_meta = {"intake": {**state, "brief": {**state["brief"], "status": "writing"}}}

        db = _fake_db(
            cas_client_intake_brief=AsyncMock(return_value=writing_meta),
            set_client_intake_path=AsyncMock(return_value=None),
        )
        mock_brief = AsyncMock(return_value=True)
        mock_match = AsyncMock()

        with (
            patch("intake.db_module", db),
            patch("routers.knowledge._auto_generate_brief", mock_brief),
            patch("routers.agents._maybe_trigger_pain_point_research", mock_match),
        ):
            await intake._maybe_finish(1, "Bosch AG", meta)

        brief_call = next(c for c in db.set_client_intake_path.call_args_list if c.args[2] == ["intake", "brief"])
        assert brief_call.args[3]["status"] == "written"
        assert "jobs (failed: boom)" in brief_call.args[3]["missing"]

        written_meta = {"intake": {**state, "parts": parts, "brief": brief_call.args[3]}}
        assert intake.is_active(written_meta) is False


# ---------------------------------------------------------------------------
# Callback / watcher hooks (mirrors tests/test_pi_agents.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _dev_backdoor(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_INTERNAL", "1")


@pytest.fixture(scope="module")
def app_client():
    with (
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user

        async def _fake_user():
            return FAKE_USER

        app.dependency_overrides[current_user] = _fake_user
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
        app.dependency_overrides.pop(current_user, None)


class TestCallbackAndWatcher:
    def test_failed_research_callback_reports_part_failed(self, app_client):
        db_row = {"id": 50, "org_id": 1}
        pool = _mock_pool(fetchrow_return=db_row)
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", pool),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("server.db_module.get_agent_run", new_callable=AsyncMock,
                  return_value={"id": 50, "agent_type": "research", "output": {}}),
            patch("routers.agents._brief_then_match", new_callable=AsyncMock) as mock_btm,
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)
            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "50", "status": "failed", "agent_type": "research",
                    "subject": "Bosch AG", "org_id": 1, "error": "mid-run 403",
                },
            )
        assert resp.status_code == 200
        assert mock_btm.called
        call = mock_btm.call_args
        assert call.args[1] == "Bosch AG"
        assert call.kwargs.get("part") == "research"
        assert call.kwargs.get("status") == "failed"

    async def test_watcher_terminal_calls_intake_part_done(self):
        from routers import agents as agents_mod

        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"status": "done", "tool_calls": [], "output": {}}
        fake_resp.raise_for_status = MagicMock()

        fake_intake = MagicMock()
        fake_intake.part_done = AsyncMock()
        run_info = {"id": 1, "org_id": 7, "agent_type": "research"}

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("routers.agents.db_module.get_agent_run", new_callable=AsyncMock,
                  side_effect=[None, run_info]),
            patch("routers.agents.db_module.update_agent_run", new_callable=AsyncMock),
            patch("routers.agents.config") as mock_cfg,
            patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=fake_resp),
            patch("routers.agents.asyncio.sleep", new_callable=AsyncMock),
            patch.dict("sys.modules", {"intake": fake_intake}),
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)
            await agents_mod._watch_agent_service_run(1, "http://svc", 100, subject="Bosch AG")

        assert fake_intake.part_done.called
        call = fake_intake.part_done.call_args
        assert call.args == (7, "Bosch AG", "research", "done")
        assert call.kwargs.get("run_id") == 1

    async def test_watcher_queued_poll_does_not_start_deadline_clock(self):
        """BLOCKER 2 regression: agent-pi reports 'queued' while a run waits
        for a FIFO slot (runner.ts:21, index.ts:75) — that must NOT start the
        intake deadline clock. Only a 'running' poll may call
        intake.note_run_started()."""
        from routers import agents as agents_mod

        queued_resp = MagicMock(status_code=200)
        queued_resp.json.return_value = {"status": "queued", "tool_calls": []}
        queued_resp.raise_for_status = MagicMock()
        done_resp = MagicMock(status_code=200)
        done_resp.json.return_value = {"status": "done", "tool_calls": [], "output": {}}
        done_resp.raise_for_status = MagicMock()

        fake_intake = MagicMock()
        fake_intake.note_run_started = AsyncMock()
        fake_intake.part_done = AsyncMock()
        run_info = {"id": 1, "org_id": 7, "agent_type": "research"}

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("routers.agents.db_module.get_agent_run", new_callable=AsyncMock,
                  side_effect=[None, None, run_info]),
            patch("routers.agents.db_module.update_agent_run", new_callable=AsyncMock),
            patch("routers.agents.config") as mock_cfg,
            patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=[queued_resp, done_resp]),
            patch("routers.agents.asyncio.sleep", new_callable=AsyncMock),
            patch.dict("sys.modules", {"intake": fake_intake}),
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)
            await agents_mod._watch_agent_service_run(1, "http://svc", 100, subject="Bosch AG")

        fake_intake.note_run_started.assert_not_called()

    async def test_watcher_running_poll_starts_deadline_clock(self):
        """Counterpart to the queued regression above: a 'running' poll must
        still call intake.note_run_started() — only 'queued' is excluded."""
        from routers import agents as agents_mod

        running_resp = MagicMock(status_code=200)
        running_resp.json.return_value = {"status": "running", "tool_calls": []}
        running_resp.raise_for_status = MagicMock()
        done_resp = MagicMock(status_code=200)
        done_resp.json.return_value = {"status": "done", "tool_calls": [], "output": {}}
        done_resp.raise_for_status = MagicMock()

        fake_intake = MagicMock()
        fake_intake.note_run_started = AsyncMock()
        fake_intake.part_done = AsyncMock()
        run_info_for_start = {"id": 1, "org_id": 7, "agent_type": "research"}
        run_info_for_finish = {"id": 1, "org_id": 7, "agent_type": "research"}

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("routers.agents.db_module.get_agent_run", new_callable=AsyncMock,
                  side_effect=[None, run_info_for_start, None, run_info_for_finish]),
            patch("routers.agents.db_module.update_agent_run", new_callable=AsyncMock),
            patch("routers.agents.config") as mock_cfg,
            patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=[running_resp, done_resp]),
            patch("routers.agents.asyncio.sleep", new_callable=AsyncMock),
            patch.dict("sys.modules", {"intake": fake_intake}),
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)
            await agents_mod._watch_agent_service_run(1, "http://svc", 100, subject="Bosch AG")

        fake_intake.note_run_started.assert_called_once_with(7, "Bosch AG", 1)


# ---------------------------------------------------------------------------
# _brief_then_match: active-intake dispatch vs. legacy immediate path
# ---------------------------------------------------------------------------

class TestBriefThenMatch:
    async def test_active_intake_dispatches_to_part_done_and_returns(self):
        """When the client has an active intake collection point (WP5),
        _brief_then_match must hand the event to intake.part_done() and
        return immediately — never generate a brief itself (that's the
        collection point's job, once all four parts are in)."""
        from routers import agents as agents_mod

        client = {"id": 1, "org_id": 1, "name": "Bosch AG", "metadata": {"intake": {"trigger": "create"}}}
        with (
            patch("routers.agents.db_module.get_client", new_callable=AsyncMock, return_value=client),
            patch("intake.is_active", return_value=True),
            patch("intake.part_done", new_callable=AsyncMock) as mock_part_done,
            patch("routers.knowledge._auto_generate_brief", new_callable=AsyncMock) as mock_brief,
        ):
            await agents_mod._brief_then_match(1, "Bosch AG", part="research", status="done", run_id=42)

        mock_part_done.assert_awaited_once_with(1, "Bosch AG", "research", "done", run_id=42)
        mock_brief.assert_not_called()

    async def test_no_active_intake_falls_back_to_legacy_brief_then_match(self):
        """Without an active intake (never started, or already finished),
        _brief_then_match must run its legacy immediate path: brief →
        (jobs scan if needed) → pain-point match trigger. This is what keeps
        the source-monitor's OSINT-after-news-change → brief-refresh flow
        working for clients that don't have an open intake."""
        from routers import agents as agents_mod

        client = {"id": 1, "org_id": 1, "name": "Bosch AG", "metadata": {}}
        with (
            patch("routers.agents.db_module.get_client", new_callable=AsyncMock, return_value=client),
            patch("intake.is_active", return_value=False),
            patch("intake.part_done", new_callable=AsyncMock) as mock_part_done,
            patch("routers.knowledge._auto_generate_brief", new_callable=AsyncMock) as mock_brief,
            patch("routers.agents._needs_jobs_scan", new_callable=AsyncMock, return_value=True),
            patch("routers.pipeline._scan_client_jobs", new_callable=AsyncMock) as mock_jobs_scan,
            patch("routers.agents._maybe_trigger_pain_point_research", new_callable=AsyncMock) as mock_match,
        ):
            await agents_mod._brief_then_match(1, "Bosch AG", part="research", status="done", run_id=42)

        mock_part_done.assert_not_called()
        mock_brief.assert_awaited_once_with(1, "Bosch AG")
        mock_jobs_scan.assert_awaited_once()
        mock_match.assert_awaited_once_with(1, "Bosch AG")


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

class TestIntakeAPI:
    def test_create_client_starts_intake(self, app_client):
        with (
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.upsert_client", new_callable=AsyncMock, return_value=10),
            patch("routers.knowledge.intake.start", new_callable=AsyncMock) as mock_start,
        ):
            resp = app_client.post(
                "/api/clients",
                json={"name": "Bosch AG", "metadata": {"industry": "Manufacturing"}},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        assert mock_start.called
        assert mock_start.call_args.kwargs.get("trigger") == "create"

    @pytest.mark.xfail(
        reason="Pre-existing bug on main (routers/internal.py:68): cache_clear(org_id) runs "
               "before org_id is assigned, so this handler always 500s today. WP0 fixes the "
               "ordering on its own branch; WP5's ownership explicitly excludes those lines "
               "('leave those lines alone'). Once WP0 merges this test should pass unmodified.",
        strict=False,
    )
    def test_internal_create_client_starts_intake_and_discovers_sources(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch("routers.internal.db_module.embed_text", new_callable=AsyncMock, return_value=[0.1] * 768),
            patch("routers.internal.db_module.upsert_client", new_callable=AsyncMock, return_value=20),
            patch("intake.start", new_callable=AsyncMock) as mock_start,
            patch("routers.knowledge._discover_sources_for_new_client", new_callable=AsyncMock) as mock_discover,
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)
            resp = app_client.post("/api/internal/clients", json={"org_id": 1, "name": "Bosch AG"})
        assert resp.status_code == 200
        assert mock_start.called
        assert mock_start.call_args.kwargs.get("trigger") == "internal"
        assert mock_discover.called

    def test_get_client_intake_returns_summary(self, app_client):
        parts = {p: {**intake._empty_part(), "status": "done"} for p in intake.PARTS}
        meta = {"intake": _intake_state(parts=parts, brief={**intake._empty_brief(), "status": "written"})}
        fake_client = {"id": 10, "name": "Bosch AG", "metadata": meta}
        pool = _mock_pool(fetchrow_return=None)
        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=fake_client),
            patch("server.db_module._pool", pool),
        ):
            resp = app_client.get("/api/clients/Bosch%20AG/intake", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["percent"] == 100
        assert data["brief"]["status"] == "written"
        assert "brief_generated_at" in data
