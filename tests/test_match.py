"""
tests/test_match.py — Tests for routers/match.py.

Covers match run triggering, status polling, report listing/retrieval,
and authentication guards.

Note on db_module._pool: the run_match and reset_match endpoints acquire a
raw asyncpg connection via db_module._pool.acquire().  We mock that pool
with a context-manager shim so those code paths work without a real DB.
"""

import pytest
from datetime import datetime, timezone
from contextlib import asynccontextmanager
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

FAKE_CLIENT_ROW = {"id": 5, "name": "ACME GmbH"}

FAKE_REPORT = {
    "id": 201,
    "client_name": "ACME GmbH",
    "content": "## NorthStar CRM ✓ Strong Fit [8/10]\nExcellent pipeline management.",
    "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    "agent_run_id": 99,
}


# ---------------------------------------------------------------------------
# Pool mock helpers
# ---------------------------------------------------------------------------

def _make_pool_mock(fetchrow_return=None, execute_return="UPDATE 1"):
    """Return a MagicMock shaped like asyncpg Pool with acquire() as async ctx-manager."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value=execute_return)

    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool, conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with DB forced available and user stubbed."""
    with (
        patch("server.get_live_model", return_value=MagicMock()),
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


@pytest.fixture()
def unauthed_client():
    """Per-test client with no dependency overrides to test auth guards."""
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
        patch("server.db_module.get_user_by_token", new_callable=AsyncMock, return_value=None),
    ):
        from server import app
        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            app.dependency_overrides.update(saved)


# ---------------------------------------------------------------------------
# TestMatchAPI
# ---------------------------------------------------------------------------

class TestMatchAPI:
    def test_match_run_triggers_job(self, app_client):
        """POST /api/match/run fires Hermes research and returns run_id."""
        pool_mock, conn_mock = _make_pool_mock(fetchrow_return=FAKE_CLIENT_ROW)

        with (
            patch("server.db_module._pool", pool_mock),
            patch("routers.match.db_module._pool", pool_mock),
            patch("server.db_module.create_agent_run", new_callable=AsyncMock, return_value=42),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch(
                "routers.match._fire_pain_point_research",
                new_callable=AsyncMock,
                return_value={"run_id": 42, "svc_run_id": 7},
            ),
        ):
            resp = app_client.post(
                "/api/match/run",
                json={"client_name": "ACME GmbH"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == 42
        assert data["status"] == "researching"
        assert data["client_name"] == "ACME GmbH"

    def test_match_run_client_not_found(self, app_client):
        """POST /api/match/run with unknown client returns 404."""
        pool_mock, _ = _make_pool_mock(fetchrow_return=None)

        with patch("routers.match.db_module._pool", pool_mock):
            resp = app_client.post(
                "/api/match/run",
                json={"client_name": "Ghost Corp"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_match_status_pending(self, app_client):
        """GET /api/match/status/{client} when run is in progress returns researching status."""
        status_row = {
            "match_status": "researching",
            "match_updated_at": None,
            "match_run_id": "42",
        }
        with patch(
            "server.db_module.get_client_match_status",
            new_callable=AsyncMock,
            return_value=status_row,
        ):
            resp = app_client.get(
                "/api/match/status/ACME GmbH",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "researching"
        assert data["client_name"] == "ACME GmbH"

    def test_match_status_none(self, app_client):
        """GET /api/match/status/{client} when no run exists returns status 'none'."""
        with patch(
            "server.db_module.get_client_match_status",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = app_client.get(
                "/api/match/status/ACME GmbH",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "none"

    def test_match_status_done_with_report(self, app_client):
        """GET /api/match/status/{client} when done returns report_doc_id."""
        status_row = {
            "match_status": "done",
            "match_updated_at": None,
            "match_run_id": "42",
        }
        with (
            patch(
                "server.db_module.get_client_match_status",
                new_callable=AsyncMock,
                return_value=status_row,
            ),
            patch(
                "server.db_module.get_match_reports",
                new_callable=AsyncMock,
                return_value=[FAKE_REPORT],
            ),
        ):
            resp = app_client.get(
                "/api/match/status/ACME GmbH",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "done"
        assert data["report_doc_id"] == 201

    def test_match_reports_list(self, app_client):
        """GET /api/match/reports returns all match report documents for the org."""
        with patch(
            "server.db_module.get_match_reports",
            new_callable=AsyncMock,
            return_value=[FAKE_REPORT],
        ):
            resp = app_client.get(
                "/api/match/reports",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["reports"]) == 1
        assert data["reports"][0]["id"] == 201
        assert data["reports"][0]["client_name"] == "ACME GmbH"
        assert "created_at" in data["reports"][0]

    def test_match_reports_list_filtered_by_client(self, app_client):
        """GET /api/match/reports?client_name=X passes the filter to db_module."""
        with patch(
            "server.db_module.get_match_reports",
            new_callable=AsyncMock,
            return_value=[FAKE_REPORT],
        ) as mock_get:
            resp = app_client.get(
                "/api/match/reports?client_name=ACME GmbH",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        mock_get.assert_called_once_with(1, client_name="ACME GmbH")

    def test_match_report_by_client(self, app_client):
        """GET /api/match/reports/{client} returns the latest report content."""
        with patch(
            "server.db_module.get_match_reports",
            new_callable=AsyncMock,
            return_value=[FAKE_REPORT],
        ):
            resp = app_client.get(
                "/api/match/reports/ACME GmbH",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["client_name"] == "ACME GmbH"
        assert "content" in data
        assert "NorthStar CRM" in data["content"]
        assert data["doc_id"] == 201

    def test_match_report_by_client_404(self, app_client):
        """GET /api/match/reports/{client} returns 404 when no report exists."""
        with patch(
            "server.db_module.get_match_reports",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = app_client.get(
                "/api/match/reports/Unknown Corp",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_match_unauthenticated(self, unauthed_client):
        """POST /api/match/run without auth header returns 401."""
        resp = unauthed_client.post(
            "/api/match/run",
            json={"client_name": "ACME GmbH"},
        )
        assert resp.status_code == 401
