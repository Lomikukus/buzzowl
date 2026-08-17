"""
tests/test_chat.py — Tests for routers/chat.py

Covers:
  - POST /api/chat  (basic response, client scope, unauthenticated)
  - Session CRUD   (create, list, load, rename, delete, 404)
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

FAKE_SESSION = {
    "id": 1,
    "org_id": 1,
    "user_id": 1,
    "title": "ACME prep",
    "client_name": None,
    "messages": [],
    "created_at": "2026-05-01T10:00:00",
    "updated_at": "2026-05-01T10:00:00",
}

FAKE_DOCS = [
    {
        "id": "doc-1",
        "display_title": "ACME GmbH Profile",
        "snippet": "ACME is a leading SaaS company.",
        "result_type": "document",
        "subtype": "research",
        "metadata": {"source_url": "https://example.com/acme"},
    },
    {
        "id": "doc-2",
        "display_title": "Bosch Annual Report",
        "snippet": "Bosch reported strong growth.",
        "result_type": "document",
        "subtype": "finding",
        "metadata": {},
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with Whisper/DB mocked and current_user overridden."""
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
    """Per-test client with no dependency overrides — real auth guard active."""
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
        patch("server.db_module.get_user_by_token", new_callable=AsyncMock, return_value=None),
    ):
        from server import app  # noqa: F401 (already imported above via module cache)
        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            app.dependency_overrides.update(saved)


# ---------------------------------------------------------------------------
# TestChatEndpoint
# ---------------------------------------------------------------------------

class TestChatEndpoint:
    def test_chat_basic_response(self, app_client):
        """POST /api/chat returns answer and sources keys."""
        with (
            patch("server.db_module.list_clients", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.list_contacts", new_callable=AsyncMock, return_value=[]),
            # Tool loop calls llm.chat (via llm.achat) — patch it to return a direct answer
            patch(
                "routers.chat.llm.chat",
                return_value={"content": "You have 2 clients.", "tool_calls": []},
            ),
        ):
            resp = app_client.post(
                "/api/chat",
                json={"message": "what clients do we have?"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "sources" in data

    def test_chat_with_client_scope(self, app_client):
        """POST /api/chat with client_name scope works end-to-end."""
        fake_client = {
            "id": 10,
            "name": "Bosch",
            "metadata": {"industry": "Manufacturing"},
            "session_count": 3,
            "last_activity": "2026-04-01",
            "documents": [],
        }
        with (
            patch("server.db_module.list_clients", new_callable=AsyncMock, return_value=[fake_client]),
            patch("server.db_module.list_contacts", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=fake_client),
            patch(
                "routers.chat.llm.chat",
                return_value={"content": "Bosch is in Manufacturing.", "tool_calls": []},
            ),
        ):
            resp = app_client.post(
                "/api/chat",
                json={"message": "tell me about Bosch", "client_name": "Bosch"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "sources" in data

    def test_chat_unauthenticated(self, unauthed_client):
        """POST /api/chat without a valid token returns 401."""
        resp = unauthed_client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer invalid"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestChatSessions
# ---------------------------------------------------------------------------

class TestChatSessions:
    def test_session_create(self, app_client):
        """POST /api/chat/sessions creates a session and returns it."""
        with patch(
            "server.db_module.create_chat_session",
            new_callable=AsyncMock,
            return_value=FAKE_SESSION,
        ):
            resp = app_client.post(
                "/api/chat/sessions",
                json={"title": "ACME prep"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["title"] == "ACME prep"

    def test_session_list(self, app_client):
        """GET /api/chat/sessions returns a list of sessions."""
        fake_sessions = [
            {**FAKE_SESSION, "id": 1, "title": "Session One"},
            {**FAKE_SESSION, "id": 2, "title": "Session Two"},
        ]
        with patch(
            "server.db_module.list_chat_sessions",
            new_callable=AsyncMock,
            return_value=fake_sessions,
        ):
            resp = app_client.get(
                "/api/chat/sessions",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert len(data["sessions"]) == 2

    def test_session_load(self, app_client):
        """GET /api/chat/sessions/1 returns a session with messages."""
        session_with_messages = {
            **FAKE_SESSION,
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "ai", "content": "Hi there"},
            ],
        }
        with patch(
            "server.db_module.get_chat_session",
            new_callable=AsyncMock,
            return_value=session_with_messages,
        ):
            resp = app_client.get(
                "/api/chat/sessions/1",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert len(data["messages"]) == 2

    def test_session_rename(self, app_client):
        """PATCH /api/chat/sessions/1 renames a session and returns ok."""
        with patch(
            "server.db_module.update_chat_session_title",
            new_callable=AsyncMock,
        ):
            resp = app_client.patch(
                "/api/chat/sessions/1",
                json={"title": "New title"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_session_rename_400_empty_title(self, app_client):
        """PATCH /api/chat/sessions/1 with an empty title returns 400."""
        resp = app_client.patch(
            "/api/chat/sessions/1",
            json={"title": ""},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_session_delete(self, app_client):
        """DELETE /api/chat/sessions/1 deletes a session and returns ok."""
        with patch(
            "server.db_module.delete_chat_session",
            new_callable=AsyncMock,
        ):
            resp = app_client.delete(
                "/api/chat/sessions/1",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_session_404(self, app_client):
        """GET /api/chat/sessions/999 returns 404 when session not found."""
        with patch(
            "server.db_module.get_chat_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = app_client.get(
                "/api/chat/sessions/999",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_session_create_503_db_unavailable(self, app_client):
        """POST /api/chat/sessions returns 503 when DB is unavailable."""
        with patch("routers.chat.DB_AVAILABLE", False):
            resp = app_client.post(
                "/api/chat/sessions",
                json={"title": "Test"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# TestChatActionTools — cloud-chat dispatch of the action tools
# (find_people, create_task) via routers.chat._run_tool
# ---------------------------------------------------------------------------


class TestChatActionTools:
    """Exercise the in-process tool dispatcher for the two action tools."""

    async def test_find_people_dispatch_calls_start_people_search(self):
        """find_people → routers.agents._start_people_search(org, client, roles, user)."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch(
                "routers.agents._start_people_search",
                new_callable=AsyncMock,
                return_value={"run_id": 77, "status": "running"},
            ) as mock_ps,
        ):
            result, sources = await _run_tool(
                "find_people",
                {"client_name": "Bosch", "target_roles": "CISO, IT-Architekt"},
                org_id=1,
                user_id=5,
            )

        mock_ps.assert_awaited_once()
        # scoped to caller's org + user, roles forwarded
        assert mock_ps.await_args.args[0] == 1
        assert mock_ps.await_args.args[1] == "Bosch"
        assert mock_ps.await_args.kwargs["target_roles"] == "CISO, IT-Architekt"
        assert mock_ps.await_args.kwargs["user_id"] == 5
        assert "run #77" in result
        assert sources == []

    async def test_find_people_requires_client_name(self):
        """find_people with no client_name returns an error and never dispatches."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch("routers.agents._start_people_search", new_callable=AsyncMock) as mock_ps,
        ):
            result, _ = await _run_tool("find_people", {"client_name": "  "}, org_id=1, user_id=5)

        mock_ps.assert_not_awaited()
        assert "No client name" in result

    async def test_create_task_dispatch_calls_db_create_task(self):
        """create_task → db.create_task(org, user, title, client_name, notes, due_date)."""
        import datetime as _dt
        from routers.chat import _run_tool

        created_row = {
            "id": 3, "org_id": 1, "user_id": 5, "title": "Call about renewal",
            "client_name": "Bosch", "due_date": _dt.date(2026, 8, 1),
        }
        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch(
                "routers.chat.db_module.create_task",
                new_callable=AsyncMock,
                return_value=created_row,
            ) as mock_ct,
        ):
            result, sources = await _run_tool(
                "create_task",
                {
                    "title": "Call about renewal",
                    "client_name": "Bosch",
                    "due_date": "2026-08-01",
                    "notes": "renewal in Q3",
                },
                org_id=1,
                user_id=5,
            )

        mock_ct.assert_awaited_once()
        # positional: org_id, user_id, title
        assert mock_ct.await_args.args[0] == 1
        assert mock_ct.await_args.args[1] == 5
        assert mock_ct.await_args.args[2] == "Call about renewal"
        assert mock_ct.await_args.kwargs["client_name"] == "Bosch"
        assert mock_ct.await_args.kwargs["notes"] == "renewal in Q3"
        assert mock_ct.await_args.kwargs["due_date"] == _dt.date(2026, 8, 1)
        assert "Call about renewal" in result
        assert sources == []

    async def test_create_task_requires_title(self):
        """create_task with an empty title returns an error and never writes."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch("routers.chat.db_module.create_task", new_callable=AsyncMock) as mock_ct,
        ):
            result, _ = await _run_tool("create_task", {"title": "   "}, org_id=1, user_id=5)

        mock_ct.assert_not_awaited()
        assert "No task title" in result

    async def test_create_task_bad_due_date_is_ignored(self):
        """An unparseable due_date is dropped (passed as None), task still created."""
        from routers.chat import _run_tool

        with (
            patch("routers.chat.DB_AVAILABLE", True),
            patch(
                "routers.chat.db_module.create_task",
                new_callable=AsyncMock,
                return_value={"id": 9, "title": "Follow up", "client_name": None},
            ) as mock_ct,
        ):
            await _run_tool(
                "create_task",
                {"title": "Follow up", "due_date": "not-a-date"},
                org_id=1,
                user_id=5,
            )

        assert mock_ct.await_args.kwargs["due_date"] is None
