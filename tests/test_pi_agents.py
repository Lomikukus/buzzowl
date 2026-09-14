"""
tests/test_pi_agents.py — Pi agent service integration tests.

Covers:
- All agent types route to Pi (Phase 29: Hermes retired)
- POST /api/agents/run creation
- GET /api/agents/tasks/{run_id} polling
- POST /api/agents/system/run (monitor trigger)
- POST /api/agents/callback chain reactions
- _ascii_name() helper
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared test data
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

FAKE_AGENT_RUN = {
    "id": 99,
    "org_id": 1,
    "agent_type": "research",
    "status": "done",
    "task": "Research: Bosch",
    "trigger_type": "manual",
    "tool_calls": [],
    "output": {"service_run_id": "42", "service_url": "http://pi:8001"},
    "error": None,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _dev_backdoor(monkeypatch):
    """Callback auth is fail-closed since P1b: empty token ⇒ 401. Opt into the
    explicit dev backdoor (only effective with an empty token) so the chain
    tests keep exercising callback logic."""
    monkeypatch.setenv("ALLOW_INSECURE_INTERNAL", "1")


@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with DB forced available and user stubbed."""
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


def _mock_pool(fetchrow_return=None, fetch_return=None):
    """Return a mock asyncpg pool that handles acquire() context manager."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    mock_conn.fetch = AsyncMock(return_value=fetch_return or [])
    mock_pool = MagicMock()
    mock_pool.__bool__ = lambda self: True
    mock_pool.acquire = MagicMock(return_value=mock_pool)
    mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


# ---------------------------------------------------------------------------
# TestAllTypesRouteToPi — Phase 29 comprehensive check
# ---------------------------------------------------------------------------

class TestAllTypesRouteToPi:
    """After Phase 29, _get_service_url() returns Pi URL for every agent type."""

    ALL_TYPES = [
        "research", "osint", "enrichment", "contact_extraction",
        "monitor", "product_research", "product_deep_research",
        "pain_point_research", "match_monitor", "match_synthesis",
        "org", "quality_digest", "orchestrate", "research_prep",
        "contact_enrich", "people_search", "chat", "some_future_type",
    ]

    @pytest.mark.parametrize("agent_type", ALL_TYPES)
    def test_all_types_route_to_pi(self, agent_type):
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": "http://pi:8001",
            }.get(key, default)
            assert _get_service_url(agent_type) == "http://pi:8001"


# ---------------------------------------------------------------------------
# TestAgentRunCreation — POST /api/agents/run
# ---------------------------------------------------------------------------

class TestAgentRunCreation:
    def test_create_run_returns_run_id(self, app_client):
        """POST /api/agents/run with valid body returns run_id."""
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module.create_agent_run", new_callable=AsyncMock, return_value=77),
            patch("routers.agents._run_agent_background", new_callable=AsyncMock),
        ):
            resp = app_client.post(
                "/api/agents/run",
                json={"agent_type": "research", "task": "Research Bosch AG", "client_name": "Bosch"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == 77
        assert data["status"] == "pending"

    def test_create_run_missing_task_returns_400(self, app_client):
        """POST /api/agents/run without task returns 400."""
        with patch("routers.agents.DB_AVAILABLE", True):
            resp = app_client.post(
                "/api/agents/run",
                json={"agent_type": "research"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 400

    def test_create_run_db_unavailable_returns_503(self, app_client):
        """POST /api/agents/run when DB is down returns 503."""
        with patch("routers.agents.DB_AVAILABLE", False):
            resp = app_client.post(
                "/api/agents/run",
                json={"agent_type": "research", "task": "Research Bosch"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# TestAgentRunPolling — GET /api/agents/tasks/{run_id}
# ---------------------------------------------------------------------------

class TestAgentRunPolling:
    def test_get_known_run(self, app_client):
        """GET /api/agents/tasks/99 returns full run record."""
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module.get_agent_run", new_callable=AsyncMock, return_value=FAKE_AGENT_RUN),
        ):
            resp = app_client.get(
                "/api/agents/tasks/99",
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 99
        assert data["agent_type"] == "research"
        assert data["status"] == "done"

    def test_get_unknown_run_returns_404(self, app_client):
        """GET /api/agents/tasks/999 returns 404 when not found."""
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module.get_agent_run", new_callable=AsyncMock, return_value=None),
        ):
            resp = app_client.get(
                "/api/agents/tasks/999",
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestSystemRun — POST /api/agents/system/run
# ---------------------------------------------------------------------------

class TestSystemRun:
    def test_system_run_fires_monitor_to_pi(self, app_client):
        """POST /api/agents/system/run triggers a monitor run on Pi."""
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module.create_agent_run", new_callable=AsyncMock, return_value=50),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("routers.agents._fire_agent_service", new_callable=AsyncMock,
                  return_value=("http://pi:8001", 99)) as mock_fire,
            patch("routers.agents._watch_agent_service_run", new_callable=AsyncMock),
        ):
            resp = app_client.post(
                "/api/agents/system/run",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == 50
        assert data["status"] == "started"

        call_kwargs = mock_fire.call_args
        assert call_kwargs.kwargs.get("agent_type") == "monitor" or call_kwargs.args[5] == "monitor" or "monitor" in str(call_kwargs)


# ---------------------------------------------------------------------------
# TestCallbackChain — POST /api/agents/callback
# ---------------------------------------------------------------------------

class TestCallbackChain:
    """Tests for the callback chain logic that drives cascaded agent runs."""

    def test_monitor_with_stale_clients_fires_research(self, app_client):
        """Monitor completion → research fired for each stale client."""
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", None),  # no pool — org_id from body
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("server.db_module.create_agent_run", new_callable=AsyncMock, return_value=55),
            patch("routers.agents._watch_agent_service_run", new_callable=AsyncMock),
            patch("routers.agents._fire_agent_service", new_callable=AsyncMock,
                  return_value=("http://pi:8001", 200)) as mock_fire,
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {
                "agent_service_token": "",
                "agent_service_brain": "openrouter",
                "agent_service_model": "deepseek/deepseek-v4-flash",
            }.get(key, default)

            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "42",
                    "status": "done",
                    "agent_type": "monitor",
                    "subject": "org",
                    "org_id": 1,
                    "stale_clients": ["Bosch AG", "ACME GmbH"],
                },
            )

        assert resp.status_code == 200
        # _fire_agent_service should be called once per stale client
        assert mock_fire.call_count == 2
        call_subjects = [c.args[0] for c in mock_fire.call_args_list]
        assert "Bosch AG" in call_subjects
        assert "ACME GmbH" in call_subjects
        # All calls should use agent_type="research"
        for call in mock_fire.call_args_list:
            assert call.kwargs.get("agent_type") == "research"

    def test_monitor_with_empty_stale_clients_no_research_fired(self, app_client):
        """Monitor completion with empty stale_clients fires no research."""
        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", None),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("routers.agents._fire_agent_service", new_callable=AsyncMock,
                  return_value=("http://pi:8001", 200)) as mock_fire,
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {
                "agent_service_token": "",
                "agent_service_brain": "openrouter",
            }.get(key, default)

            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "43",
                    "status": "done",
                    "agent_type": "monitor",
                    "subject": "org",
                    "org_id": 1,
                    "stale_clients": [],
                },
            )

        assert resp.status_code == 200
        # _fire_agent_service should not be called for research
        research_calls = [c for c in mock_fire.call_args_list if c.kwargs.get("agent_type") == "research"]
        assert len(research_calls) == 0

    def test_callback_missing_run_id_returns_400(self, app_client):
        """POST /api/agents/callback without run_id returns 400."""
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)
            resp = app_client.post(
                "/api/agents/callback",
                json={"status": "done", "agent_type": "research"},
            )
        assert resp.status_code == 400

    def test_pain_point_research_completion_fires_match_synthesis(self, app_client):
        """pain_point_research completion schedules _handle_pain_point_callback."""
        db_row = {"id": 42, "org_id": 1}
        pool = _mock_pool(fetchrow_return=db_row)

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", pool),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("server.db_module.get_agent_run", new_callable=AsyncMock,
                  return_value={"id": 42, "agent_type": "pain_point_research",
                                "output": {"_pi_brain": "openrouter", "_pi_model": "deepseek/deepseek-v4-pro"}}),
            patch("routers.agents._handle_pain_point_callback",
                  new_callable=AsyncMock) as mock_cb,
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {
                "agent_service_token": "",
                "match_brain": "openrouter",
                "match_model": "deepseek/deepseek-v4-pro",
            }.get(key, default)

            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "42",
                    "status": "done",
                    "agent_type": "pain_point_research",
                    "subject": "Bosch AG",
                    "org_id": 1,
                    "output": {"findings_saved": 5},
                },
            )

        assert resp.status_code == 200
        # _handle_pain_point_callback should have been scheduled (via create_task)
        # Since TestClient runs the event loop through response, the task completes
        assert mock_cb.called

    def test_research_completion_fires_brief_then_match(self, app_client):
        """research completion schedules _brief_then_match."""
        db_row = {"id": 44, "org_id": 1}
        pool = _mock_pool(fetchrow_return=db_row)

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", pool),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("server.db_module.get_agent_run", new_callable=AsyncMock,
                  return_value={"id": 44, "agent_type": "research", "output": {}}),
            patch("routers.agents._brief_then_match", new_callable=AsyncMock) as mock_btm,
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)

            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "44",
                    "status": "done",
                    "agent_type": "research",
                    "subject": "Bosch AG",
                    "org_id": 1,
                    "output": {},
                },
            )

        assert resp.status_code == 200
        assert mock_btm.called
        call_args = mock_btm.call_args
        assert call_args.args[1] == "Bosch AG" or "Bosch AG" in str(call_args)

    def test_research_completion_no_products_no_pain_point(self, app_client):
        """research completion does not fire pain_point_research if org has no products."""
        db_row = {"id": 45, "org_id": 1}
        pool = _mock_pool(fetchrow_return=db_row)

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", pool),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("server.db_module.get_agent_run", new_callable=AsyncMock,
                  return_value={"id": 45, "agent_type": "research", "output": {}}),
            patch("routers.agents._fire_agent_service", new_callable=AsyncMock,
                  return_value=("http://pi:8001", 300)) as mock_fire,
            patch("routers.agents._brief_then_match", new_callable=AsyncMock),
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)

            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "45",
                    "status": "done",
                    "agent_type": "research",
                    "subject": "Bosch AG",
                    "org_id": 1,
                    "output": {},
                },
            )

        assert resp.status_code == 200
        pain_calls = [c for c in mock_fire.call_args_list
                      if c.kwargs.get("agent_type") == "pain_point_research"]
        assert len(pain_calls) == 0

    def test_callback_persists_full_tool_calls_before_scheduling_reflection(self, app_client):
        """WP4 review nit: Pi's callback body never carries tool_calls
        (runner.ts) — whatever's already stored is at best a PARTIAL list
        from the watcher's last 5s "running" poll. _persist_tool_calls (which
        fetches the FULL list from agent-pi) must be awaited before
        playbook.reflect_on_run is scheduled, even when the callback body
        already carries a (partial) tool_calls list — the old code skipped
        the fetch in exactly that case, so reflection classified the
        truncated log and stamped `reflected` before the watcher's later,
        complete write ever landed."""
        db_row = {"id": 46, "org_id": 1}
        pool = _mock_pool(fetchrow_return=db_row)
        order = []

        with (
            patch("routers.agents.DB_AVAILABLE", True),
            patch("server.db_module._pool", pool),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch("routers.agents._persist_tool_calls", new_callable=AsyncMock,
                  side_effect=lambda *a, **k: order.append("persist")) as mock_persist,
            patch("routers.agents.playbook.reflect_on_run", new_callable=AsyncMock,
                  side_effect=lambda *a, **k: order.append("reflect")) as mock_reflect,
            patch("routers.agents.config") as mock_cfg,
        ):
            mock_cfg.get = lambda key, default=None: {"agent_service_token": ""}.get(key, default)

            resp = app_client.post(
                "/api/agents/callback",
                json={
                    "run_id": "46",
                    "status": "done",
                    "agent_type": "research",
                    "subject": "Bosch AG",
                    "org_id": 1,
                    # a PARTIAL tool_calls list already on the body — the old
                    # code's `if not (body.get("tool_calls") or ...)` guard
                    # skipped _persist_tool_calls whenever this was truthy.
                    "tool_calls": [{"tool": "web_search", "args": {}, "result": "...", "ts": "t0"}],
                    "output": {},
                },
            )

        assert resp.status_code == 200
        mock_persist.assert_awaited_once()
        mock_reflect.assert_called_once()
        assert mock_reflect.call_args.kwargs.get("subject") == "Bosch AG"
        assert order == ["persist", "reflect"]


# ---------------------------------------------------------------------------
# D15 — _handle_match_synthesis_callback's type='research' backstop
# ---------------------------------------------------------------------------

def _mock_pool_sequenced(fetchrow_side_effect):
    """Like _mock_pool but fetchrow returns a DIFFERENT value on each
    successive call (a list of return values, one per call) — needed here
    since the callback makes up to three sequential fetchrow lookups."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.__bool__ = lambda self: True
    mock_pool.acquire = MagicMock(return_value=mock_pool)
    mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.__aexit__ = AsyncMock(return_value=False)
    return mock_pool, mock_conn


class TestMatchSynthesisTypeCoercionBackstop:
    """D15 — a match_synthesis run occasionally writes its final report as
    type='research' (the generic final-report type every other agent uses)
    instead of 'match_report', despite the prompt and the tools.ts-side
    coercion. Measured regression: Festo's match_synthesis run wrote doc id
    473 as type='research', so match_status=done pointed at a document no
    type='match_report' consumer could ever find."""

    @pytest.mark.asyncio
    async def test_research_doc_promoted_to_match_report_when_no_match_report_exists(self):
        from routers.agents import _handle_match_synthesis_callback

        # Sequence: (1) match_report by agent_run_id -> None, (2) match_report
        # by content ILIKE -> None, (3) research doc by agent_run_id -> found.
        pool, conn = _mock_pool_sequenced([None, None, {"id": 473}])

        with patch("server.db_module._pool", pool), \
             patch("notifications.notify_org", new_callable=AsyncMock, create=True):
            await _handle_match_synthesis_callback(1, 220, "Festo")

        update_calls = [c for c in conn.execute.await_args_list
                        if "UPDATE documents" in c.args[0]]
        assert len(update_calls) == 1
        assert "type='match_report'" in update_calls[0].args[0]
        assert update_calls[0].args[1] == 473

    @pytest.mark.asyncio
    async def test_no_coercion_when_a_match_report_doc_already_exists(self):
        from routers.agents import _handle_match_synthesis_callback

        pool, conn = _mock_pool_sequenced([{"id": 349}])  # found on the first lookup

        with patch("server.db_module._pool", pool), \
             patch("notifications.notify_org", new_callable=AsyncMock, create=True):
            await _handle_match_synthesis_callback(1, 220, "Festo")

        update_calls = [c for c in conn.execute.await_args_list
                        if "UPDATE documents" in c.args[0]]
        assert update_calls == []

    @pytest.mark.asyncio
    async def test_no_coercion_when_neither_match_report_nor_research_doc_exists(self):
        from routers.agents import _handle_match_synthesis_callback

        pool, conn = _mock_pool_sequenced([None, None, None])

        with patch("server.db_module._pool", pool), \
             patch("notifications.notify_org", new_callable=AsyncMock, create=True):
            await _handle_match_synthesis_callback(1, 220, "Festo")

        update_calls = [c for c in conn.execute.await_args_list
                        if "UPDATE documents" in c.args[0]]
        assert update_calls == []


# ---------------------------------------------------------------------------
# TestAsciiNameConversion
# ---------------------------------------------------------------------------

class TestAsciiNameConversion:
    """Tests for _ascii_name() — normalises German umlauts for English-language searches."""

    def _convert(self, name: str) -> str:
        from routers.agents import _ascii_name
        return _ascii_name(name)

    def test_umlaut_ue(self):
        assert self._convert("Isabellenhütte Heusler GmbH") == "Isabellenhuette Heusler GmbH"

    def test_umlaut_oe(self):
        assert self._convert("Böhler-Uddeholm") == "Boehler-Uddeholm"

    def test_umlaut_ae(self):
        assert self._convert("Bärenfänger AG") == "Baerenfaenger AG"

    def test_eszett(self):
        assert self._convert("Straße") == "Strasse"

    def test_ascii_unchanged(self):
        assert self._convert("ASCII Corp GmbH") == "ASCII Corp GmbH"

    def test_uppercase_umlaut(self):
        assert self._convert("Österreich AG") == "Oesterreich AG"

    def test_mixed_string(self):
        result = self._convert("Müller & Söhne GmbH — Straße 42")
        assert "ü" not in result
        assert "ö" not in result
        assert "ß" not in result
        assert "Mueller" in result
        assert "Soehne" in result
        assert "Strasse" in result
