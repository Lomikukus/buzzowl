"""
tests/test_agents_api.py — Agent API and routing unit tests.

Note on patch targets:
  - routers/agents.py uses `from context import DB_AVAILABLE` (bound name), so DB_AVAILABLE
    checks inside that module must be patched at "routers.agents.DB_AVAILABLE".
  - routers/agents.py uses `from context import config` — patch "routers.agents.config"
    to control routing behaviour without touching the real config file.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# TestServiceRouting — Phase 29: all types route to Pi
# ---------------------------------------------------------------------------

class TestServiceRouting:
    """Unit tests for _get_service_url() routing decisions.

    After Phase 29 (Hermes retirement), _get_service_url() is a one-liner
    that always returns the Pi URL regardless of agent_type.
    """

    def _url(self, agent_type: str, pi_url="http://pi:8001"):
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": pi_url,
            }.get(key, default)
            return _get_service_url(agent_type)

    def test_research_routes_to_pi(self):
        assert self._url("research") == "http://pi:8001"

    def test_osint_routes_to_pi(self):
        assert self._url("osint") == "http://pi:8001"

    def test_enrichment_routes_to_pi(self):
        assert self._url("enrichment") == "http://pi:8001"

    def test_contact_extraction_routes_to_pi(self):
        assert self._url("contact_extraction") == "http://pi:8001"

    def test_monitor_routes_to_pi(self):
        assert self._url("monitor") == "http://pi:8001"

    def test_product_research_routes_to_pi(self):
        assert self._url("product_research") == "http://pi:8001"

    def test_pain_point_research_routes_to_pi(self):
        assert self._url("pain_point_research") == "http://pi:8001"

    def test_match_monitor_routes_to_pi(self):
        assert self._url("match_monitor") == "http://pi:8001"

    def test_product_deep_research_routes_to_pi(self):
        assert self._url("product_deep_research") == "http://pi:8001"

    def test_match_synthesis_routes_to_pi(self):
        assert self._url("match_synthesis") == "http://pi:8001"

    def test_unknown_type_routes_to_pi(self):
        assert self._url("some_future_type") == "http://pi:8001"

    def test_pi_backend_routes_all_to_pi(self):
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url_pi": "http://pi:8001",
            }.get(key, default)
            assert _get_service_url("research") == "http://pi:8001"
            assert _get_service_url("monitor") == "http://pi:8001"
            assert _get_service_url("pain_point_research") == "http://pi:8001"
            assert _get_service_url("match_monitor") == "http://pi:8001"

    def test_fallback_to_agent_service_url_when_pi_url_missing(self):
        """Falls back to generic agent_service_url if agent_service_url_pi is not set."""
        from routers.agents import _get_service_url
        with patch("routers.agents.config") as mock_cfg:
            mock_cfg.get = lambda key, default=None: {
                "agent_service_url": "http://fallback:8001",
            }.get(key, default)
            assert _get_service_url("research") == "http://fallback:8001"


# ---------------------------------------------------------------------------
# TestMatchProductCatalog — client-product MATCH must only feed FOCUS products
# ---------------------------------------------------------------------------

class TestMatchProductCatalog:
    """The client-product match synthesis must only match/score FOCUS products.

    Non-focus products (e.g. WatsonX) are deliberately excluded, with a safe
    fallback to all products when the org has zero focus products.
    """

    # --- _fetch_match_products: focus-only with fallback -------------------

    @pytest.mark.asyncio
    async def test_fetch_uses_focus_only_when_focus_products_exist(self):
        """When focus products exist, only they are returned — the all-products
        fallback branch is never taken, so non-focus products never leak in."""
        from routers import agents as agents_mod

        focus = [{"name": "NorthStar CRM"}, {"name": "Insight Analytics"}]

        async def _fake_list_products(org_id, focus_only=False, shared_only=False):
            if focus_only:
                return list(focus)
            # All products would additionally include WatsonX — must NOT be reached.
            return list(focus) + [{"name": "IBM watsonx"}]

        with patch.object(agents_mod.db_module, "list_products",
                          new=AsyncMock(side_effect=_fake_list_products)):
            products = await agents_mod._fetch_match_products(org_id=1)

        names = [p["name"] for p in products]
        assert names == ["NorthStar CRM", "Insight Analytics"]
        assert not any("watsonx" in n.lower() for n in names)

    @pytest.mark.asyncio
    async def test_fetch_falls_back_to_all_when_no_focus_products(self):
        """Fresh org with zero focus products falls back to the full catalog so
        the match isn't silently broken."""
        from routers import agents as agents_mod

        all_products = [{"name": "Alpha"}, {"name": "Beta"}]

        async def _fake_list_products(org_id, focus_only=False, shared_only=False):
            if focus_only:
                return []
            return list(all_products)

        with patch.object(agents_mod.db_module, "list_products",
                          new=AsyncMock(side_effect=_fake_list_products)):
            products = await agents_mod._fetch_match_products(org_id=1)

        assert [p["name"] for p in products] == ["Alpha", "Beta"]

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_when_org_has_no_products(self):
        from routers import agents as agents_mod
        with patch.object(agents_mod.db_module, "list_products",
                          new=AsyncMock(return_value=[])):
            products = await agents_mod._fetch_match_products(org_id=1)
        assert products == []

    # --- _format_match_product_catalog: formatting ------------------------

    def test_format_lists_each_product_as_a_bullet(self):
        from routers.agents import _format_match_product_catalog
        out = _format_match_product_catalog([
            {"name": "NorthStar CRM", "category": "sales",
             "description": "Pipeline management.", "key_features": ["a", "b"],
             "target_customer": "SMB"},
            {"name": "Insight Analytics"},
        ])
        assert "- **NorthStar CRM** (sales): Pipeline management." in out
        assert "Features: a, b" in out
        assert "Target: SMB" in out
        # Missing fields degrade gracefully.
        assert "- **Insight Analytics** (general): (no description)" in out

    def test_format_returns_placeholder_when_no_products(self):
        from routers.agents import _format_match_product_catalog, _NO_PRODUCTS_PLACEHOLDER
        assert _format_match_product_catalog([]) == _NO_PRODUCTS_PLACEHOLDER

    def test_format_excludes_watsonx_when_given_focus_only_list(self):
        """End-to-end shape: a focus-only list (WatsonX already filtered out by
        _fetch_match_products) produces a catalog string with no WatsonX."""
        from routers.agents import _format_match_product_catalog
        focus_products = [
            {"name": "NorthStar CRM"},
            {"name": "Insight Analytics"},
        ]
        out = _format_match_product_catalog(focus_products)
        assert "watsonx" not in out.lower()
        assert "NorthStar CRM" in out and "Insight Analytics" in out
