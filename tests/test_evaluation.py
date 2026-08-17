"""
tests/test_evaluation.py — thesis evaluation infrastructure (Session 88).

Covers: research-session timer endpoints, match report feedback,
prompt logging hook + admin endpoints, and the new auth guards.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

FAKE_USER = {
    "id": 1, "org_id": 1, "username": "konrad", "display_name": "Konrad",
    "email": "k@test.com", "role": "admin", "org_name": "North", "org_slug": "north",
}
FAKE_MEMBER = {**FAKE_USER, "id": 2, "username": "tester", "role": "member"}

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


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

        async def _fake_user():
            return FAKE_USER

        app.dependency_overrides[current_user] = _fake_user
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
        app.dependency_overrides.pop(current_user, None)


@pytest.fixture()
def member_client():
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        from routers.auth import current_user

        async def _fake_member():
            return FAKE_MEMBER

        saved = dict(app.dependency_overrides)
        app.dependency_overrides[current_user] = _fake_member
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                yield client
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)


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
# Manual research timer
# ---------------------------------------------------------------------------

class TestResearchSessions:
    def test_start_returns_id(self, app_client):
        with patch("server.db_module.start_research_session", new_callable=AsyncMock,
                   return_value={"id": 7, "started_at": NOW}):
            r = app_client.post("/api/eval/research-sessions/start", json={"client_name": "ACME"})
        assert r.status_code == 200
        assert r.json()["id"] == 7

    def test_start_requires_client_name(self, app_client):
        r = app_client.post("/api/eval/research-sessions/start", json={})
        assert r.status_code == 400

    def test_stop_returns_duration(self, app_client):
        with patch("server.db_module.stop_research_session", new_callable=AsyncMock,
                   return_value={"id": 7, "client_name": "ACME", "started_at": NOW,
                                 "ended_at": NOW, "duration_secs": 540}) as stop:
            r = app_client.post("/api/eval/research-sessions/7/stop",
                                json={"sources_checked": 4, "notes": "LinkedIn + website"})
        assert r.status_code == 200
        assert r.json()["duration_secs"] == 540
        assert stop.await_args.kwargs["sources_checked"] == 4

    def test_stop_unknown_session_404(self, app_client):
        with patch("server.db_module.stop_research_session", new_callable=AsyncMock, return_value=None):
            r = app_client.post("/api/eval/research-sessions/999/stop", json={})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Match report feedback
# ---------------------------------------------------------------------------

def _mock_pool():
    conn = MagicMock()
    conn.execute = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


class TestMatchFeedback:
    REPORT = {"id": 55, "type": "match_report", "metadata": {}}

    def test_vote_recorded(self, app_client):
        pool, conn = _mock_pool()
        with (patch("server.db_module.get_document_by_int_id", new_callable=AsyncMock, return_value=dict(self.REPORT)),
              patch("server.db_module._pool", pool)):
            r = app_client.post("/api/match/reports/55/feedback",
                                json={"section": "✓ Strong Fit [8/10]: Product X", "vote": "up"})
        assert r.status_code == 200
        saved_meta = conn.execute.await_args.args[3]
        assert saved_meta["match_feedback"]["✓ Strong Fit [8/10]: Product X"]["vote"] == "up"

    def test_traceability_appended(self, app_client):
        pool, conn = _mock_pool()
        with (patch("server.db_module.get_document_by_int_id", new_callable=AsyncMock, return_value=dict(self.REPORT)),
              patch("server.db_module._pool", pool)):
            r = app_client.post("/api/match/reports/55/feedback", json={"traceability": 4})
        assert r.status_code == 200
        saved_meta = conn.execute.await_args.args[3]
        assert saved_meta["traceability"][0]["rating"] == 4

    def test_invalid_vote_400(self, app_client):
        with patch("server.db_module.get_document_by_int_id", new_callable=AsyncMock, return_value=dict(self.REPORT)):
            r = app_client.post("/api/match/reports/55/feedback", json={"section": "x", "vote": "maybe"})
        assert r.status_code == 400

    def test_non_match_report_404(self, app_client):
        with patch("server.db_module.get_document_by_int_id", new_callable=AsyncMock,
                   return_value={"id": 55, "type": "note", "metadata": {}}):
            r = app_client.post("/api/match/reports/55/feedback", json={"traceability": 3})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Prompt logging
# ---------------------------------------------------------------------------

class TestPromptLogging:
    def test_chat_logs_prompt(self, app_client):
        with (patch("server.db_module.log_prompt") as log,
              patch("routers.chat._start_pi_chat_async", new_callable=AsyncMock, return_value="abc")):
            r = app_client.post("/api/chat", json={"message": "What about Bosch?",
                                                   "backend": "pi", "stream": True})
        assert r.status_code == 200
        assert log.call_args.args[2] == "chat"
        assert log.call_args.args[3] == "What about Bosch?"

    def test_log_prompt_without_pool_is_noop(self):
        import db
        with patch.object(db, "_pool", None):
            assert db.log_prompt(1, 1, "chat", "hello") is None

    def test_prompts_endpoint_admin_only(self, member_client):
        r = member_client.get("/api/eval/prompts")
        assert r.status_code == 403

    def test_prompts_endpoint_returns_rows(self, app_client):
        with patch("server.db_module.list_prompts", new_callable=AsyncMock,
                   return_value=[{"id": 1, "surface": "chat", "prompt": "hi",
                                  "context": {}, "created_at": NOW, "user_name": "Konrad"}]):
            r = app_client.get("/api/eval/prompts")
        assert r.status_code == 200
        assert r.json()["prompts"][0]["surface"] == "chat"

    def test_export_admin_only(self, member_client):
        r = member_client.get("/api/eval/export")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Hardening guards
# ---------------------------------------------------------------------------

class TestAuthGuards:
    @pytest.mark.parametrize("path", ["/api/clients", "/api/people", "/api/search?q=test", "/api/sessions"])
    def test_reads_require_auth(self, unauthed_client, path):
        assert unauthed_client.get(path).status_code == 401

    def test_settings_write_requires_admin(self, member_client):
        r = member_client.post("/api/settings", json={"hf_token": "x"})
        assert r.status_code == 403

    def test_research_trigger_requires_token(self, unauthed_client):
        r = unauthed_client.post("/api/research/trigger", json={"subject": "ACME"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Page-view beacon (Session 89)
# ---------------------------------------------------------------------------

class TestPageview:
    def test_pageview_logged(self, app_client):
        with patch("server.db_module.log_prompt") as log:
            r = app_client.post("/api/eval/pageview", json={"path": "/today"})
        assert r.status_code == 200
        assert log.call_args.args[2] == "page_view"
        assert log.call_args.args[3] == "/today"

    def test_pageview_rejects_bad_path(self, app_client):
        r = app_client.post("/api/eval/pageview", json={"path": "https://evil.example"})
        assert r.status_code == 400

    def test_pageview_requires_auth(self, unauthed_client):
        r = unauthed_client.post("/api/eval/pageview", json={"path": "/today"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Gap-fill prompt logging (Session 89) — the 5 previously unlogged AI surfaces.
# Downstream of each endpoint is made to raise so only the logging (which
# happens before the heavy work) is exercised — no LLM/network calls.
# ---------------------------------------------------------------------------

@pytest.fixture()
def lenient_client(app_client):
    """Same app + auth override, but server exceptions become 500 responses."""
    from server import app
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


CLIENT_ROW = {"id": 1, "name": "ACME", "metadata": {}}
PRODUCT_ROW = {"id": 3, "name": "WidgetPro"}


class TestGapFillLogging:
    def test_brief_logs(self, lenient_client):
        with (patch("server.db_module.get_client", new_callable=AsyncMock, return_value=CLIENT_ROW),
              patch("server.db_module.log_prompt") as log,
              patch("routers.knowledge._build_brief_context", new_callable=AsyncMock,
                    side_effect=RuntimeError("stop"))):
            lenient_client.post("/api/clients/ACME/brief")
        assert log.call_args.args[2] == "brief"
        assert log.call_args.args[4]["client"] == "ACME"

    def test_meeting_prep_logs(self, lenient_client):
        with (patch("server.db_module.get_client", new_callable=AsyncMock, return_value=CLIENT_ROW),
              patch("server.db_module.log_prompt") as log,
              patch("routers.knowledge._build_meeting_prep_context", new_callable=AsyncMock,
                    side_effect=RuntimeError("stop"))):
            lenient_client.post("/api/clients/ACME/meeting-prep")
        assert log.call_args.args[2] == "mp_brief"

    def test_presentation_prompt_logs(self, lenient_client):
        with (patch("server.db_module.get_client", new_callable=AsyncMock, return_value=CLIENT_ROW),
              patch("server.db_module.get_product", new_callable=AsyncMock, return_value=PRODUCT_ROW),
              patch("server.db_module.log_prompt") as log,
              patch("routers.knowledge._build_presentation_prompt_context", new_callable=AsyncMock,
                    side_effect=RuntimeError("stop"))):
            lenient_client.post("/api/clients/ACME/presentation-prompt", json={"product_id": 3})
        assert log.call_args.args[2] == "presentation"
        assert log.call_args.args[4]["product_id"] == 3

    def test_mail_template_logs(self, lenient_client):
        with (patch("server.db_module.get_client", new_callable=AsyncMock, return_value=CLIENT_ROW),
              patch("server.db_module.log_prompt") as log,
              patch("routers.knowledge._build_brief_context", new_callable=AsyncMock,
                    side_effect=RuntimeError("stop"))):
            lenient_client.post("/api/clients/ACME/mail-template",
                                json={"template_type": "follow_up",
                                      "custom_instructions": "keep it short"})
        assert log.call_args.args[2] == "mail"
        assert log.call_args.args[3] == "follow_up: ACME"
        assert log.call_args.args[4]["custom_instructions"] == "keep it short"

    def test_product_chat_logs(self, lenient_client):
        with (patch("server.db_module.get_product", new_callable=AsyncMock, return_value=PRODUCT_ROW),
              patch("server.db_module.log_prompt") as log,
              patch("routers.products._get_or_create_product_session", new_callable=AsyncMock,
                    side_effect=RuntimeError("stop"))):
            lenient_client.post("/api/products/3/chat", json={"message": "What does it cost?"})
        assert log.call_args.args[2] == "product_chat"
        assert log.call_args.args[3] == "What does it cost?"
        assert log.call_args.args[4]["product_name"] == "WidgetPro"


# ---------------------------------------------------------------------------
# Outreach outcome tracking
# ---------------------------------------------------------------------------

class TestOutreachTracking:
    MAIL_DOC = {"id": 80, "type": "note", "metadata": {"brief_type": "mail_template", "subject": "ACME"}}

    def test_status_recorded_with_history(self, app_client):
        pool, conn = _mock_pool()
        with (patch("server.db_module.get_document_by_int_id", new_callable=AsyncMock,
                    return_value={**self.MAIL_DOC, "metadata": dict(self.MAIL_DOC["metadata"])}),
              patch("server.db_module._pool", pool)):
            r = app_client.post("/api/outreach/80/status", json={"status": "replied"})
        assert r.status_code == 200
        saved_meta = conn.execute.await_args.args[3]
        assert saved_meta["outreach_status"] == "replied"
        assert saved_meta["outreach_history"][0]["status"] == "replied"

    def test_invalid_status_400(self, app_client):
        r = app_client.post("/api/outreach/80/status", json={"status": "ghosted"})
        assert r.status_code == 400

    def test_non_mail_doc_404(self, app_client):
        with patch("server.db_module.get_document_by_int_id", new_callable=AsyncMock,
                   return_value={"id": 80, "type": "note", "metadata": {}}):
            r = app_client.post("/api/outreach/80/status", json={"status": "sent"})
        assert r.status_code == 404

    def test_summary_counts(self, app_client):
        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=[
            [{"status": "generated", "n": 5}, {"status": "sent", "n": 3}],
            [],
        ])
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock(); pool.acquire = MagicMock(return_value=ctx)
        with patch("server.db_module._pool", pool):
            r = app_client.get("/api/outreach/summary")
        assert r.status_code == 200
        assert r.json()["counts"] == {"generated": 5, "sent": 3}
