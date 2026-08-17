"""
tests/test_internal.py — token-authed internal endpoints for the Pi agent service.

Covers the two chat-action endpoints Pi calls back into:
  - POST /api/internal/find-people  → routers.agents._start_people_search
  - POST /api/internal/tasks         → db.create_task

Auth: Bearer {agent_service_token}. When the token is empty (dev), auth is skipped.
"""

import datetime as _dt

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with DB forced available (no user auth — token-guarded)."""
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client


@pytest.fixture(autouse=True)
def _dev_backdoor(monkeypatch):
    """Internal auth is fail-closed since P1b: empty token ⇒ 401. These tests
    exercise endpoint logic, not auth — opt into the explicit dev backdoor
    (only effective when the token is empty, so the wrong-token 401 tests
    below still bite)."""
    monkeypatch.setenv("ALLOW_INSECURE_INTERNAL", "1")


def _cfg(token=""):
    """Return a fake config.get that only surfaces the agent_service_token."""
    return lambda key, default=None: {"agent_service_token": token}.get(key, default)


# ---------------------------------------------------------------------------
# POST /api/internal/find-people
# ---------------------------------------------------------------------------

class TestInternalFindPeople:
    def test_find_people_starts_search(self, app_client):
        """Valid body → _start_people_search called with org/client/roles, returns run_id."""
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch(
                "routers.agents._start_people_search",
                new_callable=AsyncMock,
                return_value={"run_id": 101, "status": "running"},
            ) as mock_ps,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post(
                "/api/internal/find-people",
                json={"org_id": 1, "client_name": "Bosch", "target_roles": "CISO, DevOps", "user_id": 5},
            )

        assert resp.status_code == 200
        assert resp.json()["run_id"] == 101
        mock_ps.assert_awaited_once()
        assert mock_ps.await_args.args[0] == 1
        assert mock_ps.await_args.args[1] == "Bosch"
        assert mock_ps.await_args.kwargs["target_roles"] == "CISO, DevOps"
        assert mock_ps.await_args.kwargs["user_id"] == 5
        assert mock_ps.await_args.kwargs["trigger_type"] == "chat"

    def test_find_people_missing_client_name_400(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post("/api/internal/find-people", json={"org_id": 1, "client_name": "  "})
        assert resp.status_code == 400

    def test_find_people_missing_org_id_400(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post("/api/internal/find-people", json={"client_name": "Bosch"})
        assert resp.status_code == 400

    def test_find_people_bad_token_401(self, app_client):
        """When a token is configured, a wrong bearer is rejected before any work."""
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch("routers.agents._start_people_search", new_callable=AsyncMock) as mock_ps,
        ):
            mock_cfg.get = _cfg(token="secret")
            resp = app_client.post(
                "/api/internal/find-people",
                json={"org_id": 1, "client_name": "Bosch"},
                headers={"Authorization": "Bearer wrong"},
            )
        assert resp.status_code == 401
        mock_ps.assert_not_awaited()

    def test_find_people_good_token_ok(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch(
                "routers.agents._start_people_search",
                new_callable=AsyncMock,
                return_value={"run_id": 7, "status": "running"},
            ),
        ):
            mock_cfg.get = _cfg(token="secret")
            resp = app_client.post(
                "/api/internal/find-people",
                json={"org_id": 1, "client_name": "Bosch"},
                headers={"Authorization": "Bearer secret"},
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/internal/tasks
# ---------------------------------------------------------------------------

class TestInternalCreateTask:
    def test_create_task_ok(self, app_client):
        """Valid body → db.create_task called with scoping + parsed due_date."""
        created = {
            "id": 12, "org_id": 1, "user_id": 5, "title": "Call renewal",
            "client_name": "Bosch", "due_date": _dt.date(2026, 8, 1),
        }
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch(
                "routers.internal.db_module.create_task",
                new_callable=AsyncMock,
                return_value=created,
            ) as mock_ct,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post(
                "/api/internal/tasks",
                json={
                    "org_id": 1, "user_id": 5, "title": "Call renewal",
                    "client_name": "Bosch", "due_date": "2026-08-01", "notes": "Q3",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["id"] == 12
        mock_ct.assert_awaited_once()
        assert mock_ct.await_args.args[0] == 1        # org_id
        assert mock_ct.await_args.args[1] == 5        # user_id
        assert mock_ct.await_args.args[2] == "Call renewal"
        assert mock_ct.await_args.kwargs["client_name"] == "Bosch"
        assert mock_ct.await_args.kwargs["notes"] == "Q3"
        assert mock_ct.await_args.kwargs["due_date"] == _dt.date(2026, 8, 1)
        assert mock_ct.await_args.kwargs["source"] == "chat"

    def test_create_task_missing_title_400(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post("/api/internal/tasks", json={"org_id": 1, "title": "  "})
        assert resp.status_code == 400

    def test_create_task_missing_org_id_400(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post("/api/internal/tasks", json={"title": "Follow up"})
        assert resp.status_code == 400

    def test_create_task_bad_due_date_becomes_none(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch(
                "routers.internal.db_module.create_task",
                new_callable=AsyncMock,
                return_value={"id": 1, "title": "Follow up", "client_name": None},
            ) as mock_ct,
        ):
            mock_cfg.get = _cfg(token="")
            resp = app_client.post(
                "/api/internal/tasks",
                json={"org_id": 1, "title": "Follow up", "due_date": "nope"},
            )
        assert resp.status_code == 200
        assert mock_ct.await_args.kwargs["due_date"] is None

    def test_create_task_bad_token_401(self, app_client):
        with (
            patch("routers.internal.DB_AVAILABLE", True),
            patch("routers.internal.config") as mock_cfg,
            patch("routers.internal.db_module.create_task", new_callable=AsyncMock) as mock_ct,
        ):
            mock_cfg.get = _cfg(token="secret")
            resp = app_client.post(
                "/api/internal/tasks",
                json={"org_id": 1, "title": "Follow up"},
                headers={"Authorization": "Bearer wrong"},
            )
        assert resp.status_code == 401
        mock_ct.assert_not_awaited()
