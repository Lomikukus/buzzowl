"""
tests/test_mail_generation.py — Mass email generation failure mode tests.

Covers the failure modes that caused "no mails generated" in production:
- LLM throws (OpenRouter auth failure / network error)
- LLM returns empty string (blank textarea scenario)
- Sources separator parsing (---SOURCES--- split)
- Multiple clients with mixed success/failure
- Event invitation fields in prompt
- Product block assembly in multi-mail
- Edge cases: empty lists, product not found

All three mail endpoints are tested:
  POST /api/products/{id}/bulk-mail     (routers/products.py)
  POST /api/products/multi-mail         (routers/products.py)
  POST /api/clients/{name}/mail-template (routers/knowledge.py)
"""

import requests
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
    "id": 5,
    "name": "ACME GmbH",
    "org_id": 1,
    "metadata": {"industry": "Manufacturing"},
}

FAKE_CLIENT_2 = {
    "id": 6,
    "name": "Bosch AG",
    "org_id": 1,
    "metadata": {"industry": "Engineering"},
}

FAKE_PRODUCT = {
    "id": 1,
    "org_id": 1,
    "seller_company_id": 10,
    "name": "NorthStar CRM",
    "category": "CRM",
    "description": "A B2B CRM platform for sales teams",
    "key_features": ["pipelines", "reporting"],
    "pricing_info": "€500/month",
    "target_customer": "SMEs",
    "is_focus": False,
    "metadata": {},
    "website_url": "https://northstar.ag",
}

FAKE_PRODUCT_2 = {
    **FAKE_PRODUCT,
    "id": 2,
    "name": "NorthStar Analytics",
    "description": "Business intelligence for sales",
    "website_url": None,
}

GOOD_EMAIL = "Dear ACME GmbH,\n\nFollowing up on our last discussion..."


# ---------------------------------------------------------------------------
# Fixture
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


def _mock_pool_empty():
    """Mock pool returning no rows — for context queries."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_pool)
    mock_pool.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


# ---------------------------------------------------------------------------
# TestBulkMailFailureModes — POST /api/products/{id}/bulk-mail
# ---------------------------------------------------------------------------

class TestBulkMailFailureModes:

    def test_openrouter_throws_returns_error_in_result(self, app_client):
        """When _call_brain_sync raises (e.g. OpenRouter 401), error is captured per client."""
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_presentation_prompt_context",
                  new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync",
                  side_effect=requests.HTTPError("401 Client Error: Unauthorized")),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={"client_names": ["ACME GmbH"], "template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_count"] == 0
        assert data["error_count"] == 1
        result = data["results"][0]
        assert result["email"] is None
        assert result["error"] is not None
        assert "401" in result["error"] or "Unauthorized" in result["error"]

    def test_empty_llm_response_generates_but_email_is_blank(self, app_client):
        """Empty string from LLM still counts as generated — user sees blank textarea."""
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_presentation_prompt_context",
                  new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", return_value=""),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=101),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={"client_names": ["ACME GmbH"], "template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_count"] == 1
        assert data["error_count"] == 0
        assert data["results"][0]["email"] == ""

    def test_sources_separator_parsed_in_bulk_mail(self, app_client):
        """---SOURCES--- splits email body from reasoning in bulk-mail."""
        generated = f"{GOOD_EMAIL}\n---SOURCES---\n- Used ACME expansion news to personalise."
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_presentation_prompt_context",
                  new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", return_value=generated),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=102),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={"client_names": ["ACME GmbH"], "template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert "---SOURCES---" not in result["email"]
        assert result["email"] == GOOD_EMAIL
        assert result["sources_reasoning"] is not None
        assert "ACME expansion" in result["sources_reasoning"]

    def test_multiple_clients_mixed_results(self, app_client):
        """2 clients succeed, 1 not found — generated_count=2, error_count=1."""
        client_side_effects = [FAKE_CLIENT, None, FAKE_CLIENT_2]

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock,
                  side_effect=client_side_effects),
            patch("routers.products._build_presentation_prompt_context",
                  new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", return_value=GOOD_EMAIL),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock,
                  side_effect=[103, 104]),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={
                    "client_names": ["ACME GmbH", "Ghost Corp", "Bosch AG"],
                    "template_type": "introduction",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_count"] == 2
        assert data["error_count"] == 1
        errors = [r for r in data["results"] if r["error"]]
        assert len(errors) == 1
        assert errors[0]["client_name"] == "Ghost Corp"
        assert errors[0]["error"] == "client not found"

    def test_event_invitation_fields_in_prompt(self, app_client):
        """Event invitation details are included in the LLM prompt."""
        captured_prompts = []

        def _capture_prompt(prompt, **kw):
            captured_prompts.append(prompt)
            return GOOD_EMAIL

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_presentation_prompt_context",
                  new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", side_effect=_capture_prompt),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=105),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={
                    "client_names": ["ACME GmbH"],
                    "template_type": "event_invitation",
                    "event_name": "TechConf 2026",
                    "event_date": "2026-09-15",
                    "event_link": "https://techconf.example.com",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert captured_prompts, "No prompt was generated"
        prompt = captured_prompts[0]
        assert "TechConf 2026" in prompt
        assert "2026-09-15" in prompt
        assert "techconf.example.com" in prompt

    def test_empty_client_names_returns_400(self, app_client):
        """POST with empty client_names list returns 400."""
        with patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={"client_names": [], "template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 400

    def test_product_not_found_returns_404(self, app_client):
        """POST with non-existent product returns 404."""
        with patch("server.db_module.get_product", new_callable=AsyncMock, return_value=None):
            resp = app_client.post(
                "/api/products/999/bulk-mail",
                json={"client_names": ["ACME GmbH"], "template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestMultiMailFailureModes — POST /api/products/multi-mail
# ---------------------------------------------------------------------------

class TestMultiMailFailureModes:

    def test_openrouter_throws_returns_error_in_result(self, app_client):
        """When _call_brain_sync raises, error is captured per client."""
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_brief_context", new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync",
                  side_effect=requests.HTTPError("401 Client Error: Unauthorized")),
        ):
            resp = app_client.post(
                "/api/products/multi-mail",
                json={
                    "product_ids": [1],
                    "client_names": ["ACME GmbH"],
                    "template_type": "introduction",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_count"] == 0
        assert data["error_count"] == 1
        result = data["results"][0]
        assert result["email"] is None
        assert result["error"] is not None

    def test_empty_llm_response_email_is_blank(self, app_client):
        """Empty LLM response → email field is empty string."""
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_brief_context", new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", return_value=""),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=200),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/multi-mail",
                json={
                    "product_ids": [1],
                    "client_names": ["ACME GmbH"],
                    "template_type": "introduction",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_count"] == 1
        assert data["results"][0]["email"] == ""

    def test_sources_separator_in_multi_mail(self, app_client):
        """---SOURCES--- is split correctly in multi-mail."""
        generated = f"{GOOD_EMAIL}\n---SOURCES---\nReasoning about why this mail was written."
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_brief_context", new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", return_value=generated),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=201),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/multi-mail",
                json={
                    "product_ids": [1],
                    "client_names": ["ACME GmbH"],
                    "template_type": "follow_up",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert "---SOURCES---" not in result["email"]
        assert result["email"] == GOOD_EMAIL
        assert result["sources_reasoning"] == "Reasoning about why this mail was written."

    def test_product_block_contains_all_selected_products(self, app_client):
        """Multi-mail prompt includes all selected products."""
        captured = []

        def _capture(prompt, **kw):
            captured.append(prompt)
            return GOOD_EMAIL

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock,
                  side_effect=[FAKE_PRODUCT, FAKE_PRODUCT_2]),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("routers.products._build_brief_context", new_callable=AsyncMock, return_value="[context]"),
            patch("routers.products._call_brain_sync", side_effect=_capture),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=202),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("routers.products._get_source_refs", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/products/multi-mail",
                json={
                    "product_ids": [1, 2],
                    "client_names": ["ACME GmbH"],
                    "template_type": "introduction",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        assert captured, "No prompt was generated"
        prompt = captured[0]
        assert "NorthStar CRM" in prompt
        assert "NorthStar Analytics" in prompt
        assert "PRODUCTS TO PROMOTE" in prompt

    def test_no_valid_products_returns_404(self, app_client):
        """POST with all invalid product IDs returns 404."""
        with patch("server.db_module.get_product", new_callable=AsyncMock, return_value=None):
            resp = app_client.post(
                "/api/products/multi-mail",
                json={
                    "product_ids": [999],
                    "client_names": ["ACME GmbH"],
                    "template_type": "introduction",
                },
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestMailTemplateEndpoint — POST /api/clients/{name}/mail-template
# ---------------------------------------------------------------------------

class TestMailTemplateEndpoint:

    def test_openrouter_throws_returns_5xx(self, app_client):
        """When _call_brain_sync raises, the single-client endpoint returns a 5xx error."""
        pool = _mock_pool_empty()
        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", pool),
            patch("routers.knowledge._call_brain_sync",
                  side_effect=requests.HTTPError("401 Client Error: Unauthorized")),
        ):
            resp = app_client.post(
                "/api/clients/ACME GmbH/mail-template",
                json={"template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code >= 500

    def test_empty_llm_response_email_field_is_empty(self, app_client):
        """Empty string from LLM → email field is empty string in response."""
        pool = _mock_pool_empty()
        with (
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=FAKE_CLIENT),
            patch("server.db_module._pool", pool),
            patch("routers.knowledge._call_brain_sync", return_value=""),
            patch("server.db_module.get_embedding", return_value=[0.0] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=300),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch("server.db_module.get_client_findings", new_callable=AsyncMock, return_value=[]),
            patch("server.db_module.list_signals", new_callable=AsyncMock, return_value=[]),
        ):
            resp = app_client.post(
                "/api/clients/ACME GmbH/mail-template",
                json={"template_type": "follow_up"},
                headers={"Authorization": "Bearer fake"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == ""
        assert data["client_name"] == "ACME GmbH"
        assert data["template_type"] == "follow_up"


# ---------------------------------------------------------------------------
# Brain dispatch — retry/backoff itself now lives in llm.py (tests/test_llm.py);
# here we only verify the mail path routes through it with the research role.
# ---------------------------------------------------------------------------

class TestBrainDispatch:
    def test_call_brain_sync_uses_llm_research_role(self):
        from routers import knowledge
        with patch("routers.knowledge.llm.complete", return_value="hello") as complete:
            out = knowledge._call_brain_sync("prompt")
        assert out == "hello"
        complete.assert_called_once_with("prompt", role="research", timeout=180, org_id=None)

    def test_call_brain_sync_propagates_llm_error(self):
        import llm as llm_module
        from routers import knowledge
        with patch("routers.knowledge.llm.complete",
                   side_effect=llm_module.LLMError("failed after 4 attempts")):
            with pytest.raises(RuntimeError):
                knowledge._call_brain_sync("prompt")

    def test_mail_prompt_repeats_instructions_after_context(self):
        # The custom instructions must appear AFTER the CLIENT DATA block so a long
        # context can't bury them (the "client #2 ignored my instructions" bug).
        from routers.knowledge import _MAIL_TEMPLATE_PROMPT
        rendered = _MAIL_TEMPLATE_PROMPT.format(
            type_label="follow-up", client_name="ACME", event_block="",
            product_block="", instructions_block="ADDITIONAL INSTRUCTIONS: be brief\n",
            mode_block="", context="(client data here)",
        )
        ctx_idx = rendered.index("(client data here)")
        # the instruction text appears both before and after the context
        assert rendered.rfind("be brief") > ctx_idx
