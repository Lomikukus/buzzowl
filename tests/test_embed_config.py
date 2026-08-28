"""
tests/test_embed_config.py — embedding-config correctness (no DB required).

Guards the three holes that let bring-your-own-embedding-provider setups
silently produce mixed vector spaces:

1. mcp_server must pass the FULL embed config (backend/url/key/model/dim) to
   db.init_db — otherwise it defaults to the "ollama" backend and writes
   vectors in a different embedding space than the main server.
2. mcp_server must honour EMBED_* env overrides the same way the main server
   does (context.py).
3. db.warn_on_embed_dim_mismatch must warn loudly on a stored-vs-configured
   dimension mismatch, and stay silent + non-fatal in every other case.

…and the log-noise contract around a missing/rejected key, which is what a
first-run user actually sees:

4. NOT CONFIGURED (remote backend, no key) — no HTTP call at all, exactly one
   INFO, embedding comes back empty.
5. CONFIGURED BUT FAILING — full warning once, then suppressed for the window;
   again after it expires, and immediately on a different error.
6. LOCAL backend with no key — normal, so the call must still be attempted.

All tests run without postgres: init_db is monkeypatched and the dim probe is
exercised against a fake pool/connection.
"""

import importlib
import inspect
import logging

import pytest
import requests

import db
import mcp_server


# ---------------------------------------------------------------------------
# Fakes for the dim-mismatch probe
# ---------------------------------------------------------------------------

class FakeConn:
    """Connection whose fetchval returns a canned value or raises."""

    def __init__(self, value=None, exc: Exception = None):
        self.value = value
        self.exc = exc
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if self.exc is not None:
            raise self.exc
        return self.value


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# 1. mcp_server → init_db plumbing
# ---------------------------------------------------------------------------

def test_init_db_signature_accepts_embed_kwargs():
    """Signature drift in db.init_db would break the mcp_server call."""
    params = inspect.signature(db.init_db).parameters
    for name in ("embed_model", "embed_dim", "embed_backend", "embed_url", "embed_api_key"):
        assert name in params, f"db.init_db lost parameter {name!r}"


async def test_mcp_server_lifespan_passes_full_embed_config(monkeypatch):
    """The MCP server must hand init_db the same backend/url/key as the main
    server — model+dim alone silently falls back to the ollama backend."""
    calls = {}

    async def fake_init_db(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs

    async def fake_close_db():
        calls["closed"] = True

    monkeypatch.setattr(db, "init_db", fake_init_db)
    monkeypatch.setattr(db, "close_db", fake_close_db)

    async with mcp_server.lifespan(None):
        pass

    assert calls["args"] == (
        mcp_server._DB_URL,
        mcp_server._EMBED_MODEL,
        mcp_server._EMBED_DIM,
    )
    assert calls["kwargs"] == {
        "embed_backend": mcp_server._EMBED_BACKEND,
        "embed_url": mcp_server._EMBED_URL,
        "embed_api_key": mcp_server._EMBED_API_KEY,
    }
    assert calls.get("closed") is True


def test_mcp_server_honours_embed_env_overrides(monkeypatch):
    """EMBED_* env vars must win over config.yaml, mirroring context.py."""
    monkeypatch.setenv("EMBED_BACKEND", "openai")
    monkeypatch.setenv("EMBED_URL", "https://example.test/api")
    monkeypatch.setenv("EMBED_API_KEY", "test-key-123")
    monkeypatch.setenv("EMBED_MODEL", "test/embed-model")
    monkeypatch.setenv("EMBED_DIM", "512")
    try:
        importlib.reload(mcp_server)
        assert mcp_server._EMBED_BACKEND == "openai"
        assert mcp_server._EMBED_URL == "https://example.test/api"
        assert mcp_server._EMBED_API_KEY == "test-key-123"
        assert mcp_server._EMBED_MODEL == "test/embed-model"
        assert mcp_server._EMBED_DIM == 512
    finally:
        monkeypatch.undo()
        importlib.reload(mcp_server)


def test_mcp_server_falls_back_to_config_yaml():
    """Without env overrides the module reads config.yaml directly."""
    # After the reload in the previous test the module reflects the real
    # config.yaml (plus whatever EMBED_* the outer environment carries) —
    # assert the values match the same precedence rule used by context.py.
    import os

    import yaml

    with open(mcp_server._cfg_path) as f:
        cfg = yaml.safe_load(f)
    expected_backend = os.environ.get("EMBED_BACKEND") or cfg.get("embed_backend", "")
    expected_dim = int(os.environ.get("EMBED_DIM") or cfg.get("embed_dim", 768))
    assert mcp_server._EMBED_BACKEND == expected_backend
    assert mcp_server._EMBED_DIM == expected_dim


# ---------------------------------------------------------------------------
# 2. warn_on_embed_dim_mismatch
# ---------------------------------------------------------------------------

async def test_dim_mismatch_warns_loudly(monkeypatch, caplog):
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(value=1536)))
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0]
    assert "1536" in msg and "768" in msg, "warning must name both dimensions"
    assert "scripts/backfill_embeddings.py --all" in msg, "warning must point at the backfill script"


async def test_dim_match_stays_silent(monkeypatch, caplog):
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(value=768)))
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_empty_table_stays_silent(monkeypatch, caplog):
    """No embedded documents yet — nothing to compare, no noise."""
    monkeypatch.setattr(db, "_pool", FakePool(FakeConn(value=None)))
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_probe_failure_is_non_fatal(monkeypatch, caplog):
    """A broken query (e.g. missing vector_dims, no documents table) must
    never raise — graceful degradation is a repo rule."""
    monkeypatch.setattr(
        db, "_pool", FakePool(FakeConn(exc=RuntimeError("function vector_dims does not exist")))
    )
    monkeypatch.setattr(db, "_embed_dim", 768)
    with caplog.at_level(logging.WARNING, logger="whisper.db"):
        await db.warn_on_embed_dim_mismatch()  # must not raise
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_no_pool_returns_silently(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    await db.warn_on_embed_dim_mismatch()  # must not raise


# ---------------------------------------------------------------------------
# 3. Log noise: not-configured vs failing vs working
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status: int = 200, payload: dict = None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Client Error for url: /v1/embeddings")
            err.response = self
            raise err

    def json(self):
        return self._payload


def _openai_ok(vec):
    return _FakeResponse(200, {"data": [{"embedding": vec}]})


def _ollama_ok(vec):
    return _FakeResponse(200, {"embedding": vec})


@pytest.fixture()
def embed_env(monkeypatch):
    """Isolate every module-level bit of embed config + log-noise state.

    monkeypatch restores the originals, so tests never leak a suppression
    window or a 'already logged' flag into each other.
    """
    for var in ("EMBED_BACKEND", "EMBED_URL", "EMBED_API_KEY", "OLLAMA_URL",
                "OPENROUTER_API_KEY", "OPENROUTE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(db, "_embed_backend", "openai")
    monkeypatch.setattr(db, "_embed_url", "https://openrouter.ai/api")
    monkeypatch.setattr(db, "_embed_api_key", "")
    monkeypatch.setattr(db, "_embed_model", "test-embed")
    monkeypatch.setattr(db, "_embed_dim", 4)
    monkeypatch.setattr(db, "_embed_unconfigured_logged", False)
    monkeypatch.setattr(db, "_embed_warn_state", {"sig": None, "at": 0.0})
    monkeypatch.setattr(
        db, "embed_stats", {"ok": 0, "fail": 0, "skipped": 0, "last_error": None}
    )
    return monkeypatch


@pytest.fixture()
def no_http(monkeypatch):
    """Records outbound embedding requests instead of making them.

    Returns the (expected-empty) list of attempted URLs. Raising here would be
    swallowed by get_embedding's own `except Exception`, so the check has to be
    an assertion on this list.
    """
    attempted = []

    def _record(url, **kwargs):
        attempted.append(url)
        raise AssertionError("no network in tests")

    monkeypatch.setattr(db.requests, "post", _record)
    return attempted


def _records(caplog, level):
    return [r for r in caplog.records if r.levelno == level and r.name == "whisper.db"]


class TestNotConfigured:
    """No API key for a remote backend — degraded, not broken."""

    def test_no_http_call_and_empty_result(self, embed_env, no_http, caplog):
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            assert db.get_embedding("hello") == []
        assert no_http == [], "the provider must not be contacted without a key"

    def test_logs_exactly_one_info_however_often_it_is_called(self, embed_env, no_http, caplog):
        """The health probe re-embeds every 60s forever — it must stay quiet."""
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            for _ in range(5):
                db.get_embedding("health check")

        assert no_http == []
        infos = _records(caplog, logging.INFO)
        assert len(infos) == 1, f"expected one INFO, got {[r.message for r in infos]}"
        assert not _records(caplog, logging.WARNING), "a missing key must never warn"

    def test_info_says_what_still_works_and_what_to_set(self, embed_env, no_http, caplog):
        with caplog.at_level(logging.INFO, logger="whisper.db"):
            db.get_embedding("hello")

        msg = _records(caplog, logging.INFO)[0].getMessage()
        assert "not configured" in msg
        assert "full-text search still works" in msg
        assert "OPENROUTER_API_KEY" in msg, "must name the key for THIS endpoint"

    def test_generic_provider_points_at_embed_api_key(self, embed_env, no_http, caplog):
        embed_env.setattr(db, "_embed_url", "https://api.openai.com")
        with caplog.at_level(logging.INFO, logger="whisper.db"):
            db.get_embedding("hello")

        assert "EMBED_API_KEY" in _records(caplog, logging.INFO)[0].getMessage()

    def test_whitespace_key_counts_as_missing(self, embed_env, no_http):
        embed_env.setattr(db, "_embed_api_key", "   ")
        assert db.embeddings_configured() is False
        assert db.get_embedding("hello") == []

    def test_stats_distinguish_skipped_from_failed(self, embed_env, no_http):
        db.get_embedding("hello")
        assert db.embed_stats["skipped"] == 1
        assert db.embed_stats["fail"] == 0, "a call never made is not a provider failure"
        assert "not configured" in db.embed_stats["last_error"]

    async def test_init_db_announces_it_once_at_startup(self, embed_env, no_http, caplog):
        """The message belongs in the boot output, not in the first write."""
        async def _no_db(*args, **kwargs):
            raise RuntimeError("no postgres in unit tests")

        embed_env.setattr(db.asyncpg, "connect", _no_db)
        embed_env.setattr(db.asyncpg, "create_pool", _no_db)
        embed_env.setattr(db, "_pool", None)

        with caplog.at_level(logging.INFO, logger="whisper.db"):
            await db.init_db(
                "postgresql://whisper:whisper@localhost:5432/whisper",
                "test-embed", 4,
                embed_backend="openai",
                embed_url="https://openrouter.ai/api",
                embed_api_key="",
            )
            db.get_embedding("first write")  # must not repeat the message

        assert no_http == []
        infos = [r for r in _records(caplog, logging.INFO) if "not configured" in r.getMessage()]
        assert len(infos) == 1


class TestConfiguredButFailing:
    """A key IS set and the provider rejects it — a real fault, worth a warning,
    but not once a minute forever."""

    def _fail_with(self, embed_env, status):
        embed_env.setattr(db.requests, "post", lambda *a, **k: _FakeResponse(status))

    def test_first_failure_warns_in_full(self, embed_env, caplog):
        embed_env.setattr(db, "_embed_api_key", "sk-broken")
        self._fail_with(embed_env, 401)
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            assert db.get_embedding("hello") == []

        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "Embedding failed" in msg and "401" in msg
        assert "openrouter.ai" in msg, "the endpoint must stay in the message"

    def test_repeats_are_suppressed_within_the_window(self, embed_env, caplog):
        embed_env.setattr(db, "_embed_api_key", "sk-broken")
        self._fail_with(embed_env, 401)
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            for _ in range(6):
                db.get_embedding("health check")

        assert len(_records(caplog, logging.WARNING)) == 1
        assert len(_records(caplog, logging.DEBUG)) == 5, "suppressed repeats still go to DEBUG"
        assert db.embed_stats["fail"] == 6, "stats count every attempt regardless of logging"

    def test_warns_again_once_the_window_expires(self, embed_env, caplog):
        embed_env.setattr(db, "_embed_api_key", "sk-broken")
        self._fail_with(embed_env, 401)
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            db.get_embedding("hello")
            db._embed_warn_state["at"] -= db._EMBED_WARN_INTERVAL_S + 1
            db.get_embedding("hello")

        assert len(_records(caplog, logging.WARNING)) == 2

    def test_a_different_status_warns_immediately(self, embed_env, caplog):
        embed_env.setattr(db, "_embed_api_key", "sk-broken")
        self._fail_with(embed_env, 401)
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            db.get_embedding("hello")
            self._fail_with(embed_env, 403)
            db.get_embedding("hello")

        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 2, "a changed error signature must not be suppressed"
        assert "403" in warnings[1].getMessage()

    def test_a_different_exception_type_warns_immediately(self, embed_env, caplog):
        embed_env.setattr(db, "_embed_api_key", "sk-broken")
        self._fail_with(embed_env, 401)

        def _connection_error(*a, **k):
            raise requests.ConnectionError("connection refused")

        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            db.get_embedding("hello")
            embed_env.setattr(db.requests, "post", _connection_error)
            db.get_embedding("hello")

        assert len(_records(caplog, logging.WARNING)) == 2


class TestLocalBackendNeedsNoKey:
    """Key absence is normal for a local server — the call MUST be attempted."""

    @pytest.mark.parametrize("url", [
        "http://localhost:1234",
        "http://127.0.0.1:1234",
        "http://host.docker.internal:11434",
        "http://lmstudio:1234",          # docker-compose service name
        "http://192.168.1.50:8000",      # LAN box
    ])
    def test_openai_compatible_local_server_is_called(self, embed_env, url, caplog):
        calls = []

        def _post(u, **kwargs):
            calls.append(u)
            return _openai_ok([1.0, 0.0, 0.0, 0.0])

        embed_env.setattr(db, "_embed_url", url)
        embed_env.setattr(db.requests, "post", _post)
        with caplog.at_level(logging.DEBUG, logger="whisper.db"):
            result = db.get_embedding("hello")

        assert db.embeddings_configured() is True
        assert calls == [f"{url}/v1/embeddings"]
        assert result == [1.0, 0.0, 0.0, 0.0]
        assert db.embed_stats["ok"] == 1 and db.embed_stats["skipped"] == 0
        assert not _records(caplog, logging.INFO), "a local server is not 'not configured'"

    def test_ollama_backend_never_needs_a_key(self, embed_env):
        calls = []

        def _post(u, **kwargs):
            calls.append(u)
            return _ollama_ok([1.0, 0.0, 0.0, 0.0])

        embed_env.setattr(db, "_embed_backend", "ollama")
        embed_env.setattr(db, "_embed_url", "http://localhost:11434")
        embed_env.setattr(db.requests, "post", _post)

        assert db.embeddings_configured() is True
        assert db.get_embedding("hello") == [1.0, 0.0, 0.0, 0.0]
        assert calls == ["http://localhost:11434/api/embeddings"]

    def test_working_remote_backend_is_unchanged(self, embed_env):
        """Case (c): a configured, working provider logs nothing new."""
        embed_env.setattr(db, "_embed_api_key", "sk-good")
        embed_env.setattr(
            db.requests, "post", lambda *a, **k: _openai_ok([1.0, 0.0, 0.0, 0.0])
        )
        assert db.get_embedding("hello") == [1.0, 0.0, 0.0, 0.0]
        assert db.embed_stats == {"ok": 1, "fail": 0, "skipped": 0, "last_error": None}


class TestLocalUrlDetection:
    @pytest.mark.parametrize("url,expected", [
        ("http://localhost:11434", True),
        ("http://LocalHost:11434", True),
        ("http://127.0.0.1:1234", True),
        ("http://[::1]:1234", True),
        ("http://host.docker.internal:11434", True),
        ("http://ollama:11434", True),
        ("http://10.0.0.7:8000", True),
        ("http://172.17.0.2:8000", True),
        ("https://openrouter.ai/api", False),
        ("https://api.openai.com", False),
        ("https://api.jina.ai", False),
    ])
    def test_classification(self, url, expected):
        assert db._is_local_embed_url(url) is expected
