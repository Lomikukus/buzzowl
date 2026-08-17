"""
tests/test_products.py — Tests for routers/products.py.

Covers seller-company endpoints, product CRUD, per-product chat,
bulk-mail generation, and multi-product mail.
"""

import pytest
from datetime import datetime, timezone
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

FAKE_COMPANY = {
    "id": 10,
    "org_id": 1,
    "name": "NorthStar AG",
    "website_url": "https://northstar.ag",
    "industry": "B2B SaaS",
    "research_status": "products_found",
    "metadata": {},
    "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
}

FAKE_PRODUCT = {
    "id": 1,
    "org_id": 1,
    "seller_company_id": 10,
    "name": "NorthStar CRM",
    "category": "CRM",
    "description": "A B2B CRM platform",
    "key_features": ["pipelines", "reporting"],
    "pricing_info": "€500/month",
    "target_customer": "SMEs",
    "is_focus": False,
    "priority": 0,
    "is_favorite": False,
    "is_shared": False,
    "status": "active",
    "metadata": {},
    "website_url": None,
    "source_doc_id": None,
}


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
        from server import app  # noqa: F811
        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            app.dependency_overrides.update(saved)


# ---------------------------------------------------------------------------
# TestSellerCompany
# ---------------------------------------------------------------------------

class TestSellerCompany:
    def test_get_seller_company_none(self, app_client):
        """GET /api/seller/company when no company is set up returns 200 with null company."""
        with (
            patch("server.db_module.get_seller_company", new_callable=AsyncMock, return_value=None),
        ):
            resp = app_client.get("/api/seller/company", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["company"] is None
        assert data["products"] == []

    def test_get_seller_company_exists(self, app_client):
        """GET /api/seller/company returns company dict and product list when set up."""
        products = [FAKE_PRODUCT]
        with (
            patch("server.db_module.get_seller_company", new_callable=AsyncMock, return_value=FAKE_COMPANY),
            patch("server.db_module.list_products", new_callable=AsyncMock, return_value=products),
        ):
            resp = app_client.get("/api/seller/company", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["company"]["name"] == "NorthStar AG"
        assert data["company"]["research_status"] == "products_found"
        assert len(data["products"]) == 1
        assert data["products"][0]["name"] == "NorthStar CRM"

    def test_create_seller_company(self, app_client):
        """POST /api/seller/company creates company and fires Hermes research run."""
        with (
            patch("server.db_module.upsert_seller_company", new_callable=AsyncMock, return_value=10),
            patch("server.db_module.update_seller_company_status", new_callable=AsyncMock),
            patch("server.db_module.create_agent_run", new_callable=AsyncMock, return_value=99),
            patch("server.db_module.update_agent_run", new_callable=AsyncMock),
            patch(
                "routers.products._fire_agent_service",
                new_callable=AsyncMock,
                return_value=("http://localhost:8002", 42),
            ),
            patch("routers.products._watch_agent_service_run", new_callable=AsyncMock),
            patch("asyncio.create_task"),
        ):
            resp = app_client.post(
                "/api/seller/company",
                json={"name": "NorthStar AG", "description": "B2B SaaS"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["research_status"] == "researching"
        assert data["company_id"] == 10


# ---------------------------------------------------------------------------
# TestProductsCRUD
# ---------------------------------------------------------------------------

class TestProductsCRUD:
    def test_list_products(self, app_client):
        """GET /api/products returns all products for the org."""
        products = [
            {**FAKE_PRODUCT, "id": i, "name": f"Product {i}"}
            for i in range(1, 4)
        ]
        with patch("server.db_module.list_products", new_callable=AsyncMock, return_value=products):
            resp = app_client.get("/api/products", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["products"]) == 3

    def test_create_product(self, app_client):
        """POST /api/products creates a product and returns the new id."""
        with (
            patch("server.db_module.get_seller_company", new_callable=AsyncMock, return_value=FAKE_COMPANY),
            patch("server.db_module.create_product", new_callable=AsyncMock, return_value=42),
        ):
            resp = app_client.post(
                "/api/products",
                json={
                    "name": "NorthStar CRM",
                    "category": "CRM",
                    "description": "A B2B CRM platform",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] == 42

    def test_get_product(self, app_client):
        """GET /api/products/1 returns product fields when found."""
        with patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT):
            resp = app_client.get("/api/products/1", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["product"]["name"] == "NorthStar CRM"
        assert data["product"]["category"] == "CRM"

    def test_get_product_404(self, app_client):
        """GET /api/products/999 returns 404 when product does not exist."""
        with patch("server.db_module.get_product", new_callable=AsyncMock, return_value=None):
            resp = app_client.get("/api/products/999", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 404

    def test_patch_product(self, app_client):
        """PATCH /api/products/1 updates allowed fields and returns updated product."""
        updated = {**FAKE_PRODUCT, "description": "Updated description"}
        with patch("server.db_module.update_product", new_callable=AsyncMock, return_value=updated):
            resp = app_client.patch(
                "/api/products/1",
                json={"description": "Updated description"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["product"]["description"] == "Updated description"

    def test_delete_product(self, app_client):
        """DELETE /api/products/1 removes the product and returns ok."""
        with patch("server.db_module.delete_product", new_callable=AsyncMock, return_value=True):
            resp = app_client.delete("/api/products/1", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_products_unauthenticated(self, unauthed_client):
        """GET /api/products without auth header returns 401."""
        resp = unauthed_client.get("/api/products")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestProductChat
# ---------------------------------------------------------------------------

class TestProductChat:
    def test_product_chat_get_session_creates_if_missing(self, app_client):
        """GET /api/products/1/chat creates a session when none exists and returns session_id."""
        product_no_session = {**FAKE_PRODUCT, "metadata": {}}
        fake_session = {"id": 77, "messages": []}

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=product_no_session),
            patch("server.db_module.create_chat_session", new_callable=AsyncMock, return_value={"id": 77}),
            patch("server.db_module.update_product", new_callable=AsyncMock),
            patch("server.db_module.get_chat_session", new_callable=AsyncMock, return_value=fake_session),
        ):
            resp = app_client.get("/api/products/1/chat", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == 77
        assert data["messages"] == []

    def test_product_chat_get_session_reuses_existing(self, app_client):
        """GET /api/products/1/chat returns existing session_id from product metadata."""
        product_with_session = {**FAKE_PRODUCT, "metadata": {"chat_session_id": 55}}
        existing_messages = [{"role": "user", "content": "Hello"}, {"role": "ai", "content": "Hi there"}]
        fake_session = {"id": 55, "messages": existing_messages}

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=product_with_session),
            patch("server.db_module.get_chat_session", new_callable=AsyncMock, return_value=fake_session),
        ):
            resp = app_client.get("/api/products/1/chat", headers={"Authorization": "Bearer fake"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == 55
        assert len(data["messages"]) == 2

    def test_product_chat_send(self, app_client):
        """POST /api/products/1/chat sends a message and returns an answer."""
        product_with_session = {**FAKE_PRODUCT, "metadata": {"chat_session_id": 55}}
        fake_session = {"id": 55, "messages": []}
        fake_ai_answer = "NorthStar CRM is priced at €500/month."

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=product_with_session),
            patch("server.db_module.get_chat_session", new_callable=AsyncMock, return_value=fake_session),
            patch("server.db_module.append_chat_turn", new_callable=AsyncMock),
            patch("routers.products.llm.complete", return_value=fake_ai_answer),
        ):
            resp = app_client.post(
                "/api/products/1/chat",
                json={"message": "What is the pricing?"},
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert data["answer"] == fake_ai_answer
        assert data["session_id"] == 55


# ---------------------------------------------------------------------------
# TestBulkMail
# ---------------------------------------------------------------------------

class TestBulkMail:
    def test_bulk_mail_single_client(self, app_client):
        """POST /api/products/1/bulk-mail generates email for a single client."""
        fake_client = {"id": 5, "name": "ACME GmbH", "org_id": 1}
        fake_email = "Dear ACME GmbH, we'd like to introduce NorthStar CRM..."

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=fake_client),
            patch(
                "routers.products._build_presentation_prompt_context",
                new_callable=AsyncMock,
                return_value="[context]",
            ),
            patch(
                "routers.products._call_brain_sync",
                return_value=fake_email,
            ),
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=101),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch(
                "routers.products._get_source_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={
                    "client_names": ["ACME GmbH"],
                    "template_type": "follow_up",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_count"] == 1
        assert data["error_count"] == 0
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert result["client_name"] == "ACME GmbH"
        assert result["email"] == fake_email
        assert result["error"] is None

    def test_bulk_mail_client_not_found(self, app_client):
        """POST /api/products/1/bulk-mail records error when client does not exist."""
        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=None),
        ):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={
                    "client_names": ["Unknown Corp"],
                    "template_type": "introduction",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["error_count"] == 1
        assert data["results"][0]["error"] == "client not found"

    def test_bulk_mail_invalid_template_type(self, app_client):
        """POST /api/products/1/bulk-mail with unknown template_type returns 400."""
        with patch("server.db_module.get_product", new_callable=AsyncMock, return_value=FAKE_PRODUCT):
            resp = app_client.post(
                "/api/products/1/bulk-mail",
                json={
                    "client_names": ["ACME GmbH"],
                    "template_type": "nonexistent_type",
                },
                headers={"Authorization": "Bearer fake"},
            )

        assert resp.status_code == 400

    def test_multi_product_mail(self, app_client):
        """POST /api/products/multi-mail generates one email per client for multiple products."""
        product2 = {**FAKE_PRODUCT, "id": 2, "name": "NorthStar Analytics"}
        fake_client = {"id": 5, "name": "ACME GmbH", "org_id": 1}
        fake_email = "Dear ACME GmbH, check out our product suite..."

        with (
            patch("server.db_module.get_product", new_callable=AsyncMock, side_effect=[FAKE_PRODUCT, product2]),
            patch("server.db_module.get_client", new_callable=AsyncMock, return_value=fake_client),
            patch(
                "routers.products._build_brief_context",
                new_callable=AsyncMock,
                return_value="[context]",
            ),
            patch("routers.products._call_brain_sync", return_value=fake_email),
            patch("server.db_module.get_embedding", return_value=[0.1] * 768),
            patch("server.db_module.index_document", new_callable=AsyncMock, return_value=102),
            patch("server.db_module.link_document", new_callable=AsyncMock),
            patch(
                "routers.products._get_source_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
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
        data = resp.json()
        assert data["generated_count"] == 1
        assert data["error_count"] == 0
        result = data["results"][0]
        assert result["client_name"] == "ACME GmbH"
        assert result["email"] == fake_email
        assert result["error"] is None
