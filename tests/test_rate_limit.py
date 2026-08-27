"""
tests/test_rate_limit.py — the app-wide default rate limit.

context.py gives the limiter a generous default (RATE_LIMIT_DEFAULT) so the
250+ endpoints without an explicit @_limit decorator are not wide open. Two
things have to hold, neither of them obvious from reading the wiring:

  - it fires for router-mounted endpoints. FastAPI >= 0.141 includes routers
    lazily, so middleware that walks app.routes sees 27 routes instead of 294
    and silently limits nothing — hence context.iter_effective_routes.
  - the agent-service callbacks and /api/health stay exempt, or one busy
    research run rate-limits the system against itself.

The limiter is disabled under pytest (context._rate_limits_enabled) so the rest
of the suite can hammer endpoints from the same TestClient "IP" without
flaking; the tests below swap in their own tiny limiter and restore it after.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

import context


# A limit small enough to trip in a handful of requests.
TINY_LIMIT = "3/minute"
TINY_N = 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """Module-scoped TestClient with DB forced available (no auth overrides —
    these tests only need routes to exist, not to succeed)."""
    with (
        patch("server.get_live_model", return_value=MagicMock()),
        patch("server.db_module.init_db", new_callable=AsyncMock),
        patch("server.db_module.close_db", new_callable=AsyncMock),
        patch("server.DB_AVAILABLE", True),
    ):
        from server import app
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


@pytest.fixture()
def tiny_limiter(app_client, monkeypatch):
    """Swap app.state.limiter for an enabled one with a 3/minute default.

    Each test gets a fresh Limiter, so the in-memory counters start at zero.
    The real singleton (disabled under pytest) is put back afterwards.
    """
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    # Internal endpoints must answer 401, not run — keep the dev backdoor off.
    monkeypatch.delenv("ALLOW_INSECURE_INTERNAL", raising=False)

    app = app_client.app
    original = app.state.limiter
    app.state.limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[TINY_LIMIT],
        key_style="endpoint",
        enabled=True,
    )
    context.configure_rate_limits(app, app.state.limiter)
    yield app.state.limiter
    app.state.limiter = original


# ---------------------------------------------------------------------------
# The default limit fires
# ---------------------------------------------------------------------------

class TestDefaultLimit:
    def test_undecorated_router_endpoint_is_limited(self, app_client, tiny_limiter):
        """GET /api/users carries no @_limit of its own — the middleware has to
        apply the default. It answers 401 without a token and never reaches the
        DB, so the only interesting status is the 429."""
        codes = [app_client.get("/api/users").status_code for _ in range(TINY_N + 2)]

        assert codes[:TINY_N] == [401] * TINY_N, codes
        assert codes[TINY_N] == 429, codes
        assert codes[-1] == 429, codes

    def test_limit_is_per_endpoint(self, app_client, tiny_limiter):
        """Exhausting one endpoint must not throttle an unrelated one."""
        for _ in range(TINY_N + 1):
            app_client.get("/api/users")
        assert app_client.get("/api/users").status_code == 429
        assert app_client.get("/api/config/models").status_code == 200

    def test_default_is_generous(self):
        """A regression guard on the shipped value: the SPA polls a few
        endpoints every 3s, so anything below ~60/minute would bite real use."""
        amount, _, per = context.RATE_LIMIT_DEFAULT.partition("/")
        assert per == "minute", context.RATE_LIMIT_DEFAULT
        assert int(amount) >= 60, context.RATE_LIMIT_DEFAULT


# ---------------------------------------------------------------------------
# Exemptions — machine-to-machine paths must never be throttled
# ---------------------------------------------------------------------------

class TestExemptions:
    def test_internal_agent_callbacks_are_exempt(self, app_client, tiny_limiter):
        """Pi's callbacks arrive in bursts from one container IP. Well past the
        limit, /api/internal/* still answers 401 (bad token) rather than 429."""
        codes = [
            app_client.get("/api/internal/system-status?org_id=1").status_code
            for _ in range(TINY_N * 4)
        ]

        assert 429 not in codes, codes
        assert set(codes) == {401}, codes

    def test_exempt_registry_covers_every_machine_path(self, app_client, tiny_limiter):
        """The paths that must never be throttled, resolved to the endpoint
        names slowapi actually matches on."""
        exempt = tiny_limiter._exempt_routes

        assert "server.health_check" in exempt                      # uptime monitors
        assert "routers.agents.agent_service_callback" in exempt    # Pi run completion
        assert "routers.agents.internal_trigger_run" in exempt      # Pi child run
        assert {n for n in exempt if n.startswith("routers.internal.")}

    def test_websockets_and_static_are_untouched(self, app_client, tiny_limiter):
        """slowapi only sees http scopes, and a StaticFiles Mount has no
        .endpoint — so neither can end up in the exempt list or be limited."""
        exempt = tiny_limiter._exempt_routes
        assert not any("websocket_endpoint" in n for n in exempt)
        for _ in range(TINY_N + 2):
            assert app_client.get("/static/index.html").status_code == 200


# ---------------------------------------------------------------------------
# Existing per-endpoint limits survive
# ---------------------------------------------------------------------------

class TestExplicitLimits:
    @pytest.mark.parametrize("endpoint", [
        "routers.auth.register",
        "routers.auth.login",
        "routers.auth.external_login",
        "routers.auth.accept_invite",
        "routers.chat.chat_endpoint",
        "routers.today.refresh_next_actions",
        "routers.agents.enqueue_research",
        "routers.agents.trigger_research_no_auth",
    ])
    def test_stricter_limits_are_still_registered(self, endpoint):
        """The default must not have displaced any hand-picked limit; slowapi
        skips defaults for routes it finds in _route_limits."""
        assert endpoint in context.limiter._route_limits


class TestOperatorApi:
    """X-Operator-Key is one shared secret guarding tenant create/delete and SSO
    login tokens, so the operator API gets a tighter backstop than the default."""

    @pytest.mark.parametrize("endpoint", [
        "routers.operator.list_orgs",
        "routers.operator.create_org",
        "routers.operator.get_org",
        "routers.operator.set_plan",
        "routers.operator.suspend",
        "routers.operator.resume",
        "routers.operator.login_token",
        "routers.operator.usage",
        "routers.operator.delete_org",
    ])
    def test_every_operator_endpoint_is_limited(self, endpoint):
        limits = context.limiter._route_limits[endpoint]
        assert limits, endpoint
        assert all(lim.limit.amount <= 30 for lim in limits), endpoint


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class TestWiring:
    def test_route_table_is_flattened(self, app_client):
        """The bug this guards: FastAPI's lazy include_router hides child routes
        behind an opaque _IncludedRouter, and a limiter that cannot see a route
        treats it as exempt."""
        routes = context.iter_effective_routes(app_client.app)
        paths = {getattr(r, "path", "") for r in routes}

        assert len(routes) >= len(app_client.app.routes)
        assert {"/api/users", "/api/operator/orgs", "/api/internal/clients"} <= paths

    def test_limiter_off_under_pytest_unless_asked(self, monkeypatch):
        """Suite stability: process-global counters + one TestClient IP would
        make any test that calls an endpoint repeatedly flake."""
        monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
        assert context._rate_limits_enabled() is False

        monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
        assert context._rate_limits_enabled() is True

        monkeypatch.setenv("RATE_LIMIT_ENABLED", "off")
        assert context._rate_limits_enabled() is False
