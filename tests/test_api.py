"""
tests/test_api.py — FastAPI endpoint tests.

Uses starlette TestClient with:
  - Startup model loading mocked (no Whisper models loaded)
  - DB pool mocked (no PostgreSQL connection needed)
  - current_user dependency overridden for authenticated routes

Covers: auth endpoints, agent API, knowledge API basics, search.
"""

import pytest
from datetime import datetime, timezone, timedelta
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

FAKE_ORG = {"id": 1, "name": "North", "slug": "north"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _startup_patches():
    """Return a context manager that mocks the expensive startup calls."""
    return patch.multiple(
        "server",
        get_live_model=MagicMock(return_value=MagicMock()),
        DB_AVAILABLE=True,
    )


@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with:
    - Whisper model loading mocked
    - DB forced available
    - current_user overridden to return FAKE_USER
    """
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
    """Per-test client with NO dependency overrides — tests real auth guard.

    Saves and restores app.dependency_overrides so the module-scoped
    app_client fixture is not affected.
    """
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
# Auth — register
# ---------------------------------------------------------------------------

FAKE_REG_KEY = {"id": 1, "key": "testkey123", "label": "test", "used_at": None, "expires_at": None}


class TestAuthRegister:
    def test_register_creates_org_and_user_returns_token(self, app_client):
        fake_user_row = {**FAKE_USER, "password_hash": "hashed"}
        with (
            patch("routers.auth.db_module.get_registration_key", new_callable=AsyncMock, return_value=FAKE_REG_KEY),
            patch("routers.auth.db_module.get_org_by_slug", new_callable=AsyncMock, return_value=None),
            patch("routers.auth.db_module.create_org", new_callable=AsyncMock, return_value=FAKE_ORG),
            patch("routers.auth.db_module.create_user", new_callable=AsyncMock, return_value=fake_user_row),
            patch("routers.auth.db_module.create_session_token", new_callable=AsyncMock),
            patch("routers.auth.db_module.seed_default_heartbeats", new_callable=AsyncMock),
            patch("routers.auth.db_module.consume_registration_key", new_callable=AsyncMock, return_value=True),
        ):
            resp = app_client.post("/api/auth/register", json={
                "org_name": "North",
                "org_slug": "north",
                "username": "konrad",
                "password": "secret",
                "registration_key": "testkey123",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["user"]["username"] == "konrad"
        assert data["org"]["slug"] == "north"

    def test_register_409_if_slug_taken(self, app_client):
        with (
            patch("routers.auth.db_module.get_registration_key", new_callable=AsyncMock, return_value=FAKE_REG_KEY),
            patch("routers.auth.db_module.get_org_by_slug", new_callable=AsyncMock, return_value=FAKE_ORG),
        ):
            resp = app_client.post("/api/auth/register", json={
                "org_name": "North", "org_slug": "north",
                "username": "bob", "password": "secret",
                "registration_key": "testkey123",
            })
        assert resp.status_code == 409

    def test_register_400_if_fields_missing(self, app_client):
        resp = app_client.post("/api/auth/register", json={"org_name": "North"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth — login / logout / me
# ---------------------------------------------------------------------------

class TestAuthLogin:
    def test_login_returns_token(self, app_client):
        fake_user_row = {**FAKE_USER, "password_hash": "$2b$12$abc"}
        with (
            patch("routers.auth.db_module.get_org_by_slug", new_callable=AsyncMock, return_value=FAKE_ORG),
            patch("routers.auth.db_module.get_user_by_username", new_callable=AsyncMock, return_value=fake_user_row),
            patch("routers.auth.pwd_context.verify", return_value=True),
            patch("routers.auth.db_module.create_session_token", new_callable=AsyncMock),
        ):
            resp = app_client.post("/api/auth/login", json={
                "org_slug": "north", "username": "konrad", "password": "secret",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["user"]["username"] == "konrad"

    def test_login_401_wrong_password(self, app_client):
        fake_user_row = {**FAKE_USER, "password_hash": "$2b$12$abc"}
        with (
            patch("routers.auth.db_module.get_org_by_slug", new_callable=AsyncMock, return_value=FAKE_ORG),
            patch("routers.auth.db_module.get_user_by_username", new_callable=AsyncMock, return_value=fake_user_row),
            patch("routers.auth.pwd_context.verify", return_value=False),
        ):
            resp = app_client.post("/api/auth/login", json={
                "org_slug": "north", "username": "konrad", "password": "wrong",
            })
        assert resp.status_code == 401

    def test_login_401_unknown_org(self, app_client):
        with patch("routers.auth.db_module.get_org_by_slug", new_callable=AsyncMock, return_value=None):
            resp = app_client.post("/api/auth/login", json={
                "org_slug": "ghost", "username": "x", "password": "y",
            })
        assert resp.status_code == 401

    def test_me_returns_current_user(self, app_client):
        resp = app_client.get("/api/auth/me", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["username"] == "konrad"
        assert data["org"]["slug"] == "north"

    def test_logout_ok(self, app_client):
        with patch("server.db_module.delete_session_token", new_callable=AsyncMock):
            resp = app_client.post("/api/auth/logout", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Auth guard (uses unauthed_client — no override)
# ---------------------------------------------------------------------------

class TestAuthGuard:
    def test_no_token_returns_401(self, unauthed_client):
        resp = unauthed_client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_bad_token_returns_401(self, unauthed_client):
        resp = unauthed_client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401

    def test_missing_bearer_prefix_returns_401(self, unauthed_client):
        resp = unauthed_client.get("/api/auth/me", headers={"Authorization": "garbage"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Agent API
# ---------------------------------------------------------------------------

class TestAgentRun:
    def test_trigger_run_returns_run_id(self, app_client):
        with (
            patch("server.db_module.create_agent_run", new_callable=AsyncMock, return_value=42),
            patch("server.asyncio.create_task"),
        ):
            resp = app_client.post(
                "/api/agents/run",
                json={"agent_type": "research", "task": "Research Horizon Logistik"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == 42
        assert data["status"] == "pending"

    def test_trigger_run_400_missing_task(self, app_client):
        resp = app_client.post(
            "/api/agents/run",
            json={"agent_type": "research"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_get_task_returns_run(self, app_client):
        fake_run = {
            "id": 42, "org_id": 1, "agent_type": "research",
            "status": "done", "task": "Research Horizon",
            "tool_calls": [], "output": {"text": "Summary here", "iterations": 3},
            "error": None, "trigger_type": "manual", "triggered_by": 1,
            "created_at": datetime.now(timezone.utc),
            "completed_at": datetime.now(timezone.utc),
        }
        with patch("server.db_module.get_agent_run", new_callable=AsyncMock, return_value=fake_run):
            resp = app_client.get("/api/agents/tasks/42", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"
        assert resp.json()["output"]["text"] == "Summary here"

    def test_get_task_404_not_found(self, app_client):
        with patch("server.db_module.get_agent_run", new_callable=AsyncMock, return_value=None):
            resp = app_client.get("/api/agents/tasks/999", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 404

    def test_list_runs_returns_list(self, app_client):
        fake_runs = [
            {"id": 1, "agent_type": "research", "status": "done",
             "task": "Research ACME", "trigger_type": "manual",
             "triggered_by": 1, "created_at": datetime.now(timezone.utc),
             "completed_at": None, "error": None},
        ]
        with patch("server.db_module.list_agent_runs", new_callable=AsyncMock, return_value=fake_runs):
            resp = app_client.get("/api/agents/runs", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["runs"][0]["agent_type"] == "research"


# ---------------------------------------------------------------------------
# Knowledge API — Documents
# ---------------------------------------------------------------------------

class TestDocumentsAPI:
    def test_create_document(self, app_client):
        with (
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=5),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None),
        ):
            resp = app_client.post(
                "/api/documents",
                json={"doc_id": "doc-abc", "type": "note", "title": "Test Note", "content": "Hello"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["id"] == 5

    def test_create_document_400_missing_fields(self, app_client):
        resp = app_client.post(
            "/api/documents",
            json={"type": "note", "content": "No title or doc_id"},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_get_document(self, app_client):
        fake_doc = {
            "id": 5, "doc_id": "doc-abc", "type": "note", "title": "Test Note",
            "content": "Hello", "source": "human",
            "created_at": datetime.now(timezone.utc),
        }
        with patch("server.db_module.get_document", new_callable=AsyncMock, return_value=fake_doc):
            resp = app_client.get("/api/documents/doc-abc", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test Note"

    def test_get_document_404(self, app_client):
        with patch("server.db_module.get_document", new_callable=AsyncMock, return_value=None):
            resp = app_client.get("/api/documents/ghost", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Knowledge API — Clients
# ---------------------------------------------------------------------------

class TestClientsAPI:
    def test_create_client(self, app_client):
        with (
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.upsert_client", new_callable=AsyncMock, return_value=10),
        ):
            resp = app_client.post(
                "/api/clients",
                json={"name": "ACME GmbH", "metadata": {"industry": "SaaS"}},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["id"] == 10

    def test_create_client_400_missing_name(self, app_client):
        resp = app_client.post(
            "/api/clients",
            json={"metadata": {}},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_get_client(self, app_client):
        fake_client = {
            "id": 10, "name": "ACME GmbH", "metadata": {"industry": "SaaS"},
            "session_count": 2, "last_activity": "2026-04-20",
        }
        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=fake_client),
            patch("server.db_module.list_documents", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.get("/api/clients/ACME GmbH", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "ACME GmbH"
        assert data["documents"] == []

    def test_get_client_404(self, app_client):
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None):
            resp = app_client.get("/api/clients/Ghost Corp", headers={"Authorization": "Bearer fake"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Search (no auth required)
# ---------------------------------------------------------------------------

class TestSearchAPI:
    def test_search_returns_results(self, app_client):
        fake_results = [
            {"type": "client", "id": "10", "display_title": "ACME GmbH",
             "snippet": "", "subtype": "client", "metadata": {},
             "vec_score": 0.9, "fts_score": 0.8, "trgm_score": 0.7, "combined_score": 0.85},
        ]
        with (
            patch("server.db_module.get_first_org", new_callable=AsyncMock, return_value=FAKE_ORG),
            patch("server.db_module.hybrid_search", new_callable=AsyncMock, return_value=fake_results),
        ):
            resp = app_client.get("/api/search?q=ACME")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["display_title"] == "ACME GmbH"
        assert data["query"] == "ACME"

    def test_search_empty_query_returns_empty(self, app_client):
        resp = app_client.get("/api/search?q=")
        assert resp.status_code == 200
        assert resp.json()["results"] == []
