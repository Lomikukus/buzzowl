"""
tests/test_health.py — /api/health embeddings-probe caching.

/api/health is public and polled by uptime monitors, and the embeddings check
is a real (paid) call to the configured provider. These tests pin the caching
contract: at most one live embed call per TTL window, failures cached too, and
concurrent pollers never stampede the provider.
"""

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def cold_probe_cache():
    """Every test starts (and leaves) the probe cache cold."""
    def _reset():
        server._embed_probe_cache["ok"] = False
        server._embed_probe_cache["at"] = None

    _reset()
    yield
    _reset()


class _CountingEmbed:
    """Stand-in for db.embed_text that counts real calls."""

    def __init__(self, result=(0.1, 0.2), exc: Exception | None = None):
        self.calls = 0
        self._result = result
        self._exc = exc

    async def __call__(self, text: str, model=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return list(self._result)


@contextlib.contextmanager
def _health_env(embed_text):
    """Mock everything the handler touches except the embeddings probe.

    config is emptied so `agent_service_url_pi` is missing and no HTTP call to
    the agent service is attempted.
    """
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)
    with (
        patch("server.DB_AVAILABLE", True),
        patch("server.config", {}),
        patch("server.db_module._pool", pool),
        patch("server.db_module.embed_text", embed_text),
    ):
        yield


def _call_health():
    return asyncio.run(server.health_check())


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestEmbedProbeCache:
    def test_two_immediate_calls_make_one_embed_call(self):
        embed = _CountingEmbed()
        with _health_env(embed):
            first = _call_health()
            second = _call_health()

        assert embed.calls == 1
        assert first["checks"]["embeddings"] is True
        assert second["checks"]["embeddings"] is True

    def test_expired_ttl_triggers_a_second_embed_call(self):
        embed = _CountingEmbed()
        with _health_env(embed):
            _call_health()
            assert embed.calls == 1
            # Age the cached probe past the TTL.
            server._embed_probe_cache["at"] = time.monotonic() - (server._EMBED_PROBE_TTL_S + 1)
            _call_health()

        assert embed.calls == 2

    def test_failures_are_cached_too(self):
        """A provider that is already down must not be hammered."""
        embed = _CountingEmbed(exc=RuntimeError("provider down"))
        with _health_env(embed):
            first = _call_health()
            second = _call_health()

        assert embed.calls == 1
        assert first["checks"]["embeddings"] is False
        assert second["checks"]["embeddings"] is False

    def test_empty_embedding_counts_as_failure(self):
        embed = _CountingEmbed(result=())
        with _health_env(embed):
            result = _call_health()

        assert embed.calls == 1
        assert result["checks"]["embeddings"] is False

    def test_concurrent_requests_do_not_stampede(self):
        embed = _CountingEmbed()

        async def _hammer():
            return await asyncio.gather(*(server.health_check() for _ in range(5)))

        with _health_env(embed):
            results = asyncio.run(_hammer())

        assert embed.calls == 1
        assert all(r["checks"]["embeddings"] is True for r in results)


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

class TestHealthPayload:
    def test_existing_fields_unchanged(self):
        embed = _CountingEmbed()
        with _health_env(embed):
            result = _call_health()

        assert result["status"] == "healthy"
        checks = result["checks"]
        assert checks["db"] is True
        assert checks["pi"] is False
        assert checks["embeddings"] is True
        assert isinstance(checks["embed_stats"], dict)

    def test_probe_age_reported(self):
        embed = _CountingEmbed()
        with _health_env(embed):
            first = _call_health()
            server._embed_probe_cache["at"] = time.monotonic() - 12.0
            second = _call_health()

        assert first["checks"]["embed_checked_ago_s"] == 0.0
        assert second["checks"]["embed_checked_ago_s"] == pytest.approx(12.0, abs=1.0)

    def test_db_ping_stays_live_on_every_request(self):
        """Only the paid embeddings probe is cached — the DB ping is not."""
        embed = _CountingEmbed()
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=1)
        with (
            patch("server.DB_AVAILABLE", True),
            patch("server.config", {}),
            patch("server.db_module._pool", pool),
            patch("server.db_module.embed_text", embed),
        ):
            _call_health()
            _call_health()

        assert pool.fetchval.await_count == 2
        assert embed.calls == 1

    def test_no_db_layer_reports_degraded_without_probing(self):
        embed = _CountingEmbed()
        with (
            patch("server.DB_AVAILABLE", False),
            patch("server.config", {}),
            patch("server.db_module.embed_text", embed),
        ):
            result = _call_health()

        assert embed.calls == 0
        assert result["status"] == "degraded"
        assert result["checks"]["embeddings"] is False
        assert result["checks"]["embed_checked_ago_s"] is None
