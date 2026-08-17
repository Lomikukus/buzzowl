"""
tests/test_knowledge_api.py — Tests for routers/knowledge.py

Covers extended KB management endpoints not covered by test_api.py:
  - Contacts CRUD
  - Client extended endpoints: docs, findings, delete
  - Mail template generation
  - Meeting prep generation and retrieval
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

FAKE_CLIENT = {
    "id": 10,
    "name": "ACME GmbH",
    "metadata": {"industry": "SaaS", "deal_stage": "discovery"},
    "session_count": 2,
    "last_activity": "2026-04-20",
}

FAKE_CONTACT = {
    "id": 5,
    "org_id": 1,
    "name": "Anna Müller",
    "metadata": {"role": "CEO", "email": "anna@acme.de"},
    "client_id": 10,
    "last_activity": None,
    "session_count": 0,
}

FAKE_DOCS = [
    {
        "id": 1,
        "doc_id": "meeting-001",
        "type": "meeting",
        "title": "Kick-off call",
        "content": "Discussed requirements.",
        "source": "human",
        "created_at": "2026-04-10T09:00:00",
    },
]

FAKE_FINDINGS = [
    {
        "id": 20,
        "doc_id": "finding-001",
        "title": "ACME expanding to DACH market",
        "content": "ACME announced expansion.",
        "metadata": {"relevance_score": 4, "source_url": "https://news.example.com/acme"},
        "source": "agent",
        "created_at": "2026-04-15T10:00:00",
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
# TestContactsAPI
# ---------------------------------------------------------------------------

class TestContactsAPI:
    def test_create_contact(self, app_client):
        """POST /api/contacts creates a contact and returns ok + id."""
        with (
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.upsert_contact", new_callable=AsyncMock, return_value=5),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None),
        ):
            resp = app_client.post(
                "/api/contacts",
                json={"name": "Anna Müller", "metadata": {"role": "CEO"}},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] == 5

    def test_create_contact_with_client_link(self, app_client):
        """POST /api/contacts with a client name resolves the client_id."""
        with (
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.upsert_contact", new_callable=AsyncMock, return_value=6),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
        ):
            resp = app_client.post(
                "/api/contacts",
                json={"name": "Hans Schmidt", "client": "ACME GmbH", "metadata": {"role": "CTO"}},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_create_contact_400_missing_name(self, app_client):
        """POST /api/contacts without name returns 400."""
        resp = app_client.post(
            "/api/contacts",
            json={"metadata": {"role": "CEO"}},
            headers={"Authorization": "Bearer fake"},
        )
        assert resp.status_code == 400

    def test_get_contact(self, app_client):
        """GET /api/contacts/{name} returns the contact with its name."""
        with (
            patch("server.db_module.get_contact", new_callable=AsyncMock, return_value=FAKE_CONTACT),
            patch("server.db_module.list_documents", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.get_client_by_id", new_callable=AsyncMock, return_value=FAKE_CLIENT),
        ):
            resp = app_client.get(
                "/api/contacts/Anna Müller",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Anna Müller"

    def test_get_contact_404(self, app_client):
        """GET /api/contacts/{name} returns 404 when contact not found."""
        with patch("server.db_module.get_contact", new_callable=AsyncMock, return_value=None):
            resp = app_client.get(
                "/api/contacts/Ghost Person",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_list_contacts(self, app_client):
        """GET /api/people returns all contacts."""
        fake_contacts = [FAKE_CONTACT, {**FAKE_CONTACT, "id": 6, "name": "Hans Schmidt"}]
        with (
            patch("server.db_module.get_first_org", new_callable=AsyncMock, return_value={"id": 1}),
            patch("server.db_module.list_contacts", new_callable=AsyncMock, return_value=fake_contacts),
        ):
            resp = app_client.get("/api/people")

        assert resp.status_code == 200
        data = resp.json()
        assert "contacts" in data
        assert len(data["contacts"]) == 2


# ---------------------------------------------------------------------------
# TestClientExtendedAPI
# ---------------------------------------------------------------------------

class TestClientExtendedAPI:
    def test_client_docs(self, app_client):
        """GET /api/clients/{name}/docs returns documents linked to the client."""
        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module.list_documents", new_callable=AsyncMock, return_value=FAKE_DOCS),
        ):
            resp = app_client.get(
                "/api/clients/ACME GmbH/docs",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data
        assert len(data["documents"]) == 1
        assert data["documents"][0]["title"] == "Kick-off call"

    def test_client_docs_404_unknown_client(self, app_client):
        """GET /api/clients/{name}/docs returns 404 for an unknown client."""
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None):
            resp = app_client.get(
                "/api/clients/Ghost Corp/docs",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_client_findings(self, app_client):
        """GET /api/clients/{name}/findings returns findings for the client."""
        # findings endpoint uses db_module._pool directly — mock the pool
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {
                "id": 20, "doc_id": "finding-001", "title": "ACME expansion",
                "content": "ACME expanding.", "metadata": {"relevance_score": 4},
                "source": "agent", "agent_run_id": 1, "created_at": "2026-04-15T10:00:00",
            }
        ])
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_pool)
        mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", mock_pool),
        ):
            resp = app_client.get(
                "/api/clients/ACME GmbH/findings",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "findings" in data
        assert data["client"] == "ACME GmbH"

    def test_client_findings_404_unknown_client(self, app_client):
        """GET /api/clients/{name}/findings returns 404 for an unknown client."""
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None):
            resp = app_client.get(
                "/api/clients/Ghost Corp/findings",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_delete_client(self, app_client):
        """DELETE /api/clients?name=ACME GmbH deletes the client and returns ok."""
        with patch("server.db_module.delete_client", new_callable=AsyncMock, return_value=True):
            resp = app_client.delete(
                "/api/clients?name=ACME GmbH",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["deleted"] == "ACME GmbH"

    def test_delete_client_404_unknown(self, app_client):
        """DELETE /api/clients?name=Ghost Corp returns 404 when not found."""
        with patch("server.db_module.delete_client", new_callable=AsyncMock, return_value=False):
            resp = app_client.delete(
                "/api/clients?name=Ghost Corp",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestMailTemplate
# ---------------------------------------------------------------------------

class TestMailTemplate:
    def _mock_pool(self):
        """Build a mock asyncpg pool that returns no rows for context queries."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_pool)
        mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.__aexit__ = AsyncMock(return_value=False)
        return mock_pool

    def test_mail_template_generate(self, app_client):
        """POST /api/clients/{name}/mail-template generates and returns an email."""
        mock_pool = self._mock_pool()
        email_text = "Dear Anna,\n\nI noticed ACME is expanding...\n[Your name]\n[Your title]\n[Company]"

        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", mock_pool),
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=99),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("server.db_module.get_client_findings", new_callable=AsyncMock, return_value=FAKE_FINDINGS),
            patch("server.db_module.list_signals", new_callable=AsyncMock, return_value=[]),
            patch("routers.knowledge._call_brain_sync", return_value=email_text),
        ):
            resp = app_client.post(
                "/api/clients/ACME GmbH/mail-template",
                json={"template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "email" in data
        assert data["template_type"] == "follow_up"
        assert data["client_name"] == "ACME GmbH"

    def test_mail_template_with_sources_separator(self, app_client):
        """POST /api/clients/{name}/mail-template splits email body and sources_reasoning."""
        mock_pool = self._mock_pool()
        generated = (
            "Dear Anna,\n\nFollowing up...\n[Your name]\n[Your title]\n[Company]\n"
            "---SOURCES---\n- [finding] \"ACME expansion\": used to personalise the opening."
        )

        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", mock_pool),
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=100),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("server.db_module.get_client_findings", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.list_signals", new_callable=AsyncMock, return_value=[]),
            patch("routers.knowledge._call_brain_sync", return_value=generated),
        ):
            resp = app_client.post(
                "/api/clients/ACME GmbH/mail-template",
                json={"template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "---SOURCES---" not in data["email"]
        assert data["sources_reasoning"] is not None

    def test_mail_template_400_invalid_type(self, app_client):
        """POST /api/clients/{name}/mail-template with invalid template_type returns 400."""
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT):
            resp = app_client.post(
                "/api/clients/ACME GmbH/mail-template",
                json={"template_type": "invalid"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 400

    def test_mail_template_404_unknown_client(self, app_client):
        """POST /api/clients/{name}/mail-template with unknown client returns 404."""
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None):
            resp = app_client.post(
                "/api/clients/Ghost Corp/mail-template",
                json={"template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_mail_template_valid_types(self, app_client):
        """All four valid template_types are accepted."""
        mock_pool = self._mock_pool()
        valid_types = ["event_invitation", "follow_up", "introduction", "check_in"]
        email_text = "Dear Contact,\n\nMessage body.\n[Your name]\n[Your title]\n[Company]"

        for t in valid_types:
            with (
                patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
                patch("server.db_module._pool", mock_pool),
                patch("server.db_module.get_embedding", return_value=[0.1] * 768),
                patch("server.db_module.index_document", new_callable=AsyncMock, return_value=1),
                patch("server.db_module.link_document", new_callable=AsyncMock),
                patch("server.db_module.get_client_findings", new_callable=AsyncMock, return_value=[]),
                patch("server.db_module.list_signals", new_callable=AsyncMock, return_value=[]),
                patch("routers.knowledge._call_brain_sync", return_value=email_text),
            ):
                resp = app_client.post(
                    "/api/clients/ACME GmbH/mail-template",
                    json={"template_type": t},
                    headers={"Authorization": "Bearer fake"},
                )
            assert resp.status_code == 200, f"template_type={t!r} unexpectedly returned {resp.status_code}"


# ---------------------------------------------------------------------------
# TestMeetingPrep
# ---------------------------------------------------------------------------

class TestMeetingPrep:
    def _mock_pool_with_row(self, row=None):
        """Return a mock pool whose fetchrow returns the given row (or None)."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=row)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_pool)
        mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.__aexit__ = AsyncMock(return_value=False)
        return mock_pool

    def test_meeting_prep_generate(self, app_client):
        """POST /api/clients/{name}/meeting-prep generates and returns a brief."""
        brief_content = "## Context\nACME is expanding...\n\n## Last Discussed\n- Price proposal."
        mock_pool = self._mock_pool_with_row(None)

        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", mock_pool),
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=55),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("server.db_module.get_client_findings", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.list_signals", new_callable=AsyncMock, return_value=[]),
            patch("routers.knowledge._call_brain_sync", return_value=brief_content),
        ):
            resp = app_client.post(
                "/api/clients/ACME GmbH/meeting-prep",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "brief" in data
        assert data["brief"] == brief_content
        assert data["doc_id"] == 55

    def test_meeting_prep_generate_404_unknown_client(self, app_client):
        """POST /api/clients/{name}/meeting-prep returns 404 for unknown client."""
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None):
            resp = app_client.post(
                "/api/clients/Ghost Corp/meeting-prep",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404

    def test_meeting_prep_get_existing(self, app_client):
        """GET /api/clients/{name}/meeting-prep returns the latest saved brief."""
        fake_row = MagicMock()
        fake_row.__getitem__ = lambda self, key: {
            "id": 55,
            "content": "## Context\nACME brief content.",
            "created_at": "2026-05-01T09:00:00",
        }[key]
        mock_pool = self._mock_pool_with_row(fake_row)

        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", mock_pool),
        ):
            resp = app_client.get(
                "/api/clients/ACME GmbH/meeting-prep",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "brief" in data

    def test_meeting_prep_get_none(self, app_client):
        """GET /api/clients/{name}/meeting-prep returns brief=None when none exists yet."""
        mock_pool = self._mock_pool_with_row(None)

        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", mock_pool),
        ):
            resp = app_client.get(
                "/api/clients/ACME GmbH/meeting-prep",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["brief"] is None
        assert data["generated_at"] is None

    def test_meeting_prep_get_404_unknown_client(self, app_client):
        """GET /api/clients/{name}/meeting-prep returns 404 for unknown client."""
        with patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None):
            resp = app_client.get(
                "/api/clients/Ghost Corp/meeting-prep",
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 404
