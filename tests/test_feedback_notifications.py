"""
tests/test_feedback_notifications.py — Tests for feedback, notifications, and heartbeat endpoints.

Covers:
  - POST /api/feedback         (routers/feedback.py)
  - GET  /api/notifications/status
  - POST /api/notifications/test
  - GET  /api/heartbeats
  - POST /api/heartbeats
  - PATCH /api/heartbeats/{id}
  - DELETE /api/heartbeats/{id}
  - POST /api/heartbeats/{id}/run
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

FAKE_USER = {
    "id": 1, "org_id": 1, "username": "konrad", "display_name": "Konrad",
    "email": "k@test.com", "role": "admin", "org_name": "North", "org_slug": "north",
}

AUTH = {"Authorization": "Bearer fake"}

FAKE_HEARTBEAT = {
    "id": 1,
    "org_id": 1,
    "agent_type": "research",
    "cron_expr": "0 8 * * 1",
    "task": "Weekly research sweep",
    "enabled": True,
    "last_run_at": None,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user
        async def _fake_user(): return FAKE_USER
        app.dependency_overrides[current_user] = _fake_user
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
        from routers.auth import current_user as _cu
        app.dependency_overrides.pop(_cu, None)


@pytest.fixture()
def unauthed_client():
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
# TestFeedback
# ---------------------------------------------------------------------------

class TestFeedback:
    def test_submit_feedback_authenticated(self, app_client):
        """POST /api/feedback with auth header stores to DB and notifies."""
        with (
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=1),
            patch("server.db_module._pool") as mock_pool,
            patch("notifications.notify") as mock_notify,
        ):
            # Simulate a valid user session in the feedback pool lookup
            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(return_value={
                "id": 1, "org_id": 1, "username": "konrad"
            })
            mock_pool.acquire = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            ))
            resp = app_client.post(
                "/api/feedback",
                json={"subject": "Bug report", "message": "Something broke"},
                headers=AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_submit_feedback_unauthenticated(self, app_client):
        """POST /api/feedback without auth header still returns 200 (open endpoint)."""
        with (
            patch("server.db_module._pool", None),
            patch("notifications.notify"),
        ):
            resp = app_client.post(
                "/api/feedback",
                json={"subject": "Anonymous tip", "message": "This feature rocks"},
                # No Authorization header
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_submit_feedback_missing_fields_returns_400(self, app_client):
        """POST /api/feedback with empty body returns 400."""
        resp = app_client.post("/api/feedback", json={})
        assert resp.status_code == 400

    def test_submit_feedback_missing_subject_returns_400(self, app_client):
        """POST /api/feedback with only message (no subject) returns 400."""
        resp = app_client.post(
            "/api/feedback",
            json={"message": "No subject here"},
        )
        assert resp.status_code == 400

    def test_submit_feedback_missing_message_returns_400(self, app_client):
        """POST /api/feedback with only subject (no message) returns 400."""
        resp = app_client.post(
            "/api/feedback",
            json={"subject": "Has subject only"},
        )
        assert resp.status_code == 400

    def test_submit_feedback_db_failure_still_returns_ok(self, app_client):
        """POST /api/feedback DB write failure is non-fatal; notify is still called."""
        with (
            patch("server.db_module._pool") as mock_pool,
            patch("notifications.notify") as mock_notify,
        ):
            # Pool lookup succeeds but index_document will raise
            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(return_value={
                "id": 1, "org_id": 1, "username": "konrad"
            })
            mock_pool.acquire = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            ))
            with patch("server.db_module.index_document",
                       new_callable=AsyncMock, side_effect=Exception("DB write failed")):
                resp = app_client.post(
                    "/api/feedback",
                    json={"subject": "Test", "message": "Still works?"},
                    headers=AUTH,
                )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# TestNotifications
# ---------------------------------------------------------------------------

class TestNotifications:
    def test_notifications_status_configured(self, app_client):
        """GET /api/notifications/status returns configured=True when Telegram is set up."""
        with patch("notifications._configured", return_value=True):
            resp = app_client.get("/api/notifications/status", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "configured" in data
        assert data["configured"] is True
        assert data["channel"] == "telegram"

    def test_notifications_status_not_configured(self, app_client):
        """GET /api/notifications/status returns configured=False when Telegram is not set up."""
        with patch("notifications._configured", return_value=False):
            resp = app_client.get("/api/notifications/status", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["channel"] is None

    def test_notifications_status_requires_auth(self, unauthed_client):
        """GET /api/notifications/status requires authentication."""
        resp = unauthed_client.get("/api/notifications/status", headers=AUTH)
        assert resp.status_code == 401

    def test_notifications_test_send_ok(self, app_client):
        """POST /api/notifications/test sends to the CURRENT USER's linked chat (per-user
        routing, Phase 6b) — never to an org-wide list."""
        from unittest.mock import AsyncMock
        with (
            patch("notifications.notify_user", new=AsyncMock(return_value=True)) as mock_user,
            patch("notifications._configured", return_value=True),
        ):
            resp = app_client.post("/api/notifications/test", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["configured"] is True
        mock_user.assert_awaited_once()
        args = mock_user.await_args.args
        assert args[0] == 1                      # the caller's user id (fixture user)
        assert "Konrad" in args[1]               # message mentions the display name

    def test_notifications_test_send_not_configured(self, app_client):
        """POST /api/notifications/test reflects Telegram not configured in response."""
        with (
            patch("notifications.notify", return_value=False),
            patch("notifications._configured", return_value=False),
        ):
            resp = app_client.post("/api/notifications/test", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["configured"] is False

    def test_notifications_test_requires_auth(self, unauthed_client):
        """POST /api/notifications/test requires authentication."""
        resp = unauthed_client.post("/api/notifications/test", headers=AUTH)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestHeartbeats
# ---------------------------------------------------------------------------

class TestHeartbeats:
    def test_list_heartbeats(self, app_client):
        """GET /api/heartbeats returns list of heartbeat jobs."""
        fake_hbs = [
            {**FAKE_HEARTBEAT, "id": 1, "agent_type": "research"},
            {**FAKE_HEARTBEAT, "id": 2, "agent_type": "enrichment"},
            {**FAKE_HEARTBEAT, "id": 3, "agent_type": "weekly_digest"},
        ]
        with (
            patch("server.db_module.list_all_heartbeats", new_callable=AsyncMock, return_value=fake_hbs),
            patch("context._scheduler", None),
        ):
            resp = app_client.get("/api/heartbeats", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "heartbeats" in data
        assert len(data["heartbeats"]) == 3
        # Verify the name lookup is applied (e.g. "research" → "Weekday Research")
        names = {hb["agent_type"]: hb["name"] for hb in data["heartbeats"]}
        assert names["research"] == "Weekday Research"
        assert names["enrichment"] == "Daily Enrichment"

    def test_list_heartbeats_db_unavailable(self, app_client):
        """GET /api/heartbeats returns empty list when DB is unavailable."""
        with patch("routers.pipeline.DB_AVAILABLE", False):
            resp = app_client.get("/api/heartbeats", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["heartbeats"] == []

    def test_list_heartbeats_requires_auth(self, unauthed_client):
        """GET /api/heartbeats requires authentication."""
        resp = unauthed_client.get("/api/heartbeats", headers=AUTH)
        assert resp.status_code == 401

    def test_create_heartbeat(self, app_client):
        """POST /api/heartbeats creates a new job and registers with scheduler."""
        fake_hb = {
            "id": 5, "org_id": 1,
            "agent_type": "research",
            "cron_expr": "0 8 * * 1",
            "task": "Weekly research sweep",
            "enabled": True,
            "last_run_at": None,
        }
        with (
            patch("server.db_module.create_heartbeat", new_callable=AsyncMock, return_value=fake_hb),
            patch("context._scheduler", None),  # No scheduler — just verify DB call
            patch("context.SCHEDULER_AVAILABLE", False),
        ):
            resp = app_client.post(
                "/api/heartbeats",
                json={
                    "agent_type": "research",
                    "cron_expr": "0 8 * * 1",
                    "task": "Weekly research sweep",
                    "enabled": True,
                },
                headers=AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["heartbeat"]["agent_type"] == "research"
        assert data["heartbeat"]["cron_expr"] == "0 8 * * 1"

    def test_create_heartbeat_400_missing_fields(self, app_client):
        """POST /api/heartbeats returns 400 when required fields are missing."""
        resp = app_client.post(
            "/api/heartbeats",
            json={"agent_type": "research"},  # missing cron_expr and task
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_create_heartbeat_400_invalid_cron(self, app_client):
        """POST /api/heartbeats returns 400 for a cron expression with wrong number of parts."""
        resp = app_client.post(
            "/api/heartbeats",
            json={
                "agent_type": "research",
                "cron_expr": "0 8 *",  # only 3 parts — invalid
                "task": "Research sweep",
            },
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_create_heartbeat_db_unavailable_returns_503(self, app_client):
        """POST /api/heartbeats returns 503 when DB is unavailable.

        Patching routers.pipeline.DB_AVAILABLE because pipeline.py uses
        `from context import DB_AVAILABLE` (bound name, not live lookup).
        """
        with patch("routers.pipeline.DB_AVAILABLE", False):
            resp = app_client.post(
                "/api/heartbeats",
                json={"agent_type": "research", "cron_expr": "0 8 * * 1", "task": "Sweep"},
                headers=AUTH,
            )
        assert resp.status_code == 503

    def test_patch_heartbeat(self, app_client):
        """PATCH /api/heartbeats/1 updates an existing heartbeat job."""
        updated_hb = {**FAKE_HEARTBEAT, "id": 1, "enabled": False}
        with (
            patch("server.db_module.get_heartbeat", new_callable=AsyncMock, return_value=FAKE_HEARTBEAT),
            patch("server.db_module.update_heartbeat", new_callable=AsyncMock, return_value=updated_hb),
            patch("context._scheduler", None),
            patch("context.SCHEDULER_AVAILABLE", False),
        ):
            resp = app_client.patch(
                "/api/heartbeats/1",
                json={"enabled": False},
                headers=AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["heartbeat"]["enabled"] is False

    def test_patch_heartbeat_404_not_found(self, app_client):
        """PATCH /api/heartbeats/99 returns 404 when heartbeat does not exist."""
        with patch("server.db_module.get_heartbeat", new_callable=AsyncMock, return_value=None):
            resp = app_client.patch(
                "/api/heartbeats/99",
                json={"enabled": False},
                headers=AUTH,
            )
        assert resp.status_code == 404

    def test_patch_heartbeat_requires_auth(self, unauthed_client):
        """PATCH /api/heartbeats/1 requires authentication."""
        resp = unauthed_client.patch(
            "/api/heartbeats/1",
            json={"enabled": False},
            headers=AUTH,
        )
        assert resp.status_code == 401

    def test_delete_heartbeat(self, app_client):
        """DELETE /api/heartbeats/1 removes the job and returns ok."""
        with (
            patch("server.db_module.delete_heartbeat", new_callable=AsyncMock, return_value=True),
            patch("context._scheduler", None),
            patch("context.SCHEDULER_AVAILABLE", False),
        ):
            resp = app_client.delete("/api/heartbeats/1", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_heartbeat_404_not_found(self, app_client):
        """DELETE /api/heartbeats/99 returns 404 when heartbeat does not exist."""
        with patch("server.db_module.delete_heartbeat", new_callable=AsyncMock, return_value=False):
            resp = app_client.delete("/api/heartbeats/99", headers=AUTH)
        assert resp.status_code == 404

    def test_delete_heartbeat_db_unavailable_returns_503(self, app_client):
        """DELETE /api/heartbeats/1 returns 503 when DB is unavailable."""
        with patch("routers.pipeline.DB_AVAILABLE", False):
            resp = app_client.delete("/api/heartbeats/1", headers=AUTH)
        assert resp.status_code == 503

    def test_delete_heartbeat_requires_auth(self, unauthed_client):
        """DELETE /api/heartbeats/1 requires authentication."""
        resp = unauthed_client.delete("/api/heartbeats/1", headers=AUTH)
        assert resp.status_code == 401

    def test_heartbeat_run_now(self, app_client):
        """POST /api/heartbeats/1/run triggers immediate one-shot execution."""
        with (
            patch("server.db_module.get_heartbeat", new_callable=AsyncMock, return_value=FAKE_HEARTBEAT),
            patch("routers.pipeline.asyncio.create_task"),
        ):
            resp = app_client.post("/api/heartbeats/1/run", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_heartbeat_run_now_404_not_found(self, app_client):
        """POST /api/heartbeats/99/run returns 404 when heartbeat does not exist."""
        with patch("server.db_module.get_heartbeat", new_callable=AsyncMock, return_value=None):
            resp = app_client.post("/api/heartbeats/99/run", headers=AUTH)
        assert resp.status_code == 404

    def test_heartbeat_run_now_db_unavailable_returns_503(self, app_client):
        """POST /api/heartbeats/1/run returns 503 when DB is unavailable."""
        with patch("routers.pipeline.DB_AVAILABLE", False):
            resp = app_client.post("/api/heartbeats/1/run", headers=AUTH)
        assert resp.status_code == 503

    def test_heartbeat_run_now_requires_auth(self, unauthed_client):
        """POST /api/heartbeats/1/run requires authentication."""
        resp = unauthed_client.post("/api/heartbeats/1/run", headers=AUTH)
        assert resp.status_code == 401
